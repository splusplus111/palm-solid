from __future__ import annotations
import asyncio, time
from time import perf_counter as now
import os
import contextlib
from typing import Dict, Any
import aiohttp
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

from wallet import Wallet
from jupiter import Jupiter
from price import get_sol_usd_price
from constants import SOL_MINT, LAMPORTS_PER_SOL
from ata_janitor import note_activity  # ← added
from config import (
    BUY_USD,
    ENTRY_MAX_AGE_SECONDS,
    EXIT_AFTER_SECONDS,
    SELL_RETRY_MAX_TRIES,
    MAX_BUYS_PER_SEC,
    PRIORITY_FEE_USD,
    PRIORITY_FEE_LAMPORTS,
    MIN_LIQUIDITY_USD,
)
from analytics import analytics
from rugpull_detector import check_rug_pull

# --- Per-side slippage with safe fallback ------------------------------------
# Prefer explicit per-side values from config.py; if missing, fall back to env
# or the global SLIPPAGE_BPS so the bot still runs.
from config import SLIPPAGE_BPS  # global default as fallback
try:
    from config import SLIPPAGE_BPS_BUY as _SBUY, SLIPPAGE_BPS_SELL as _SSELL
    SLIPPAGE_BPS_BUY, SLIPPAGE_BPS_SELL = _SBUY, _SSELL
except Exception:
    SLIPPAGE_BPS_BUY  = int(os.getenv("SLIPPAGE_BPS_BUY",  SLIPPAGE_BPS))
    SLIPPAGE_BPS_SELL = int(os.getenv("SLIPPAGE_BPS_SELL", SLIPPAGE_BPS))
# ----------------------------------------------------------------------------- 

# --- dynamic sell percent & retry schedule ---
try:
    from config import SELL_PERCENT as _SELL_FRACTION  # normalized 0..1 from config.py
except Exception:
    _SELL_FRACTION = None
    
def _resolve_sell_fraction() -> float:
    raw = os.getenv("SELL_PERCENT", "").strip()
    if raw:
        try:
            v = float(raw)
            return v if v <= 1.0 else v / 100.0  # accept 0.98 or 98
        except Exception:
            pass
    if _SELL_FRACTION is not None:
        return float(_SELL_FRACTION)
    return 0.995  # safe default

SELL_FRACTION = _resolve_sell_fraction()

def _parse_retry_schedule():
    # Try config first, then env 'SELL_RETRY_SCHEDULE' as comma-separated seconds; else default bounded backoff.
    try:
        from config import SELL_RETRY_SCHEDULE as _SRS  # optional
        if isinstance(_SRS, (list, tuple)) and _SRS:
            return [float(x) for x in _SRS]
        if isinstance(_SRS, str) and _SRS.strip():
            return [float(x.strip()) for x in _SRS.split(",") if x.strip()]
    except Exception:
        pass
    s = os.getenv("SELL_RETRY_SCHEDULE", "")
    if s.strip():
        try:
            return [float(x.strip()) for x in s.split(",") if x.strip()]
        except Exception:
            pass
    return [0.6, 1.3, 2.1, 3.0, 4.0]

SELL_RETRY_SCHEDULE = _parse_retry_schedule()

# SELL-only timing/fee overrides (optional; driven by .env)
SELL_PREQUOTE_ADVANCE_SECONDS = float(os.getenv("SELL_PREQUOTE_ADVANCE_SECONDS", "0.15"))
SELL_PRIORITY_FEE_LAMPORTS_OVERRIDE = int(os.getenv("SELL_PRIORITY_FEE_LAMPORTS", "0"))

def _sell_retry_delay(tries: int) -> float:
    if tries <= 0:
        return SELL_RETRY_SCHEDULE[0]
    idx = min(tries - 1, len(SELL_RETRY_SCHEDULE) - 1)
    return SELL_RETRY_SCHEDULE[idx]

# === Lightweight logging ======================================================
try:
    from config import LOG_LEVEL as _CFG_LOG_LEVEL
except Exception:
    _CFG_LOG_LEVEL = "INFO"

_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2}
_CURRENT_LEVEL = _LEVELS.get(str(_CFG_LOG_LEVEL).upper(), 1)

def _log(level: str, msg: str):
    if _LEVELS.get(level, 1) >= _CURRENT_LEVEL:
        print(msg)

def log_debug(msg: str): _log("DEBUG", msg)
def log_info(msg: str):  _log("INFO", msg)
def log_warn(msg: str):  _log("WARN", msg)

def _short_error(e: Exception) -> str:
    """Trim Solana/RPC errors so console isn’t flooded."""
    msg = str(e)
    if not msg:
        return repr(e)
    if "custom program error" in msg:
        return "custom program error: " + msg.split("custom program error")[-1].strip()
    if "Transaction simulation failed" in msg:
        return "Transaction simulation failed"
    return msg.splitlines()[0]
# ============================================================================


# --- Token bucket to enforce MAX_BUYS_PER_SEC ---
class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = burst
        self.updated = time.monotonic()

    def take(self, amount=1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


def _priority_fee_lamports(sol_usd: float) -> int:
    if PRIORITY_FEE_LAMPORTS > 0:
        return PRIORITY_FEE_LAMPORTS
    sol = PRIORITY_FEE_USD / max(sol_usd, 0.01)
    return int(sol * LAMPORTS_PER_SOL)


def _looks_liquid_enough(quote: Dict[str, Any]) -> bool:
    pi = quote.get("priceImpactPct")
    if pi is None:
        return True
    try:
        return float(pi) <= 0.95
    except Exception:
        return True


def _estimate_liquidity_usd(quote: Dict[str, Any], trade_usd: float) -> float:
    try:
        pi = float(quote.get("priceImpactPct", 0.0))
        if pi <= 0.0:
            return float("inf")
        return trade_usd / max(pi, 1e-6)
    except Exception:
        return float("inf")


async def _lamports_for_usd(usd: float) -> int:
    sol_price = await get_sol_usd_price()
    lamports = int(max(1, (usd / max(sol_price, 0.01)) * LAMPORTS_PER_SOL))
    return lamports


async def _ensure_ata(wallet: Wallet, mint: str):
    # Check the ATA tied to (owner, mint); create only if missing
    bal = await wallet.get_token_balance(mint)
    if bal is not None:
        return
    try:
        log_debug(f"🛠 ensuring ATA exists for {mint}")
        await wallet.create_associated_token_account(mint)
    except Exception as e:
        log_warn(f"⚠️ ATA ensure failed for {mint}: {_short_error(e)}")


async def snipe_once(wallet: Wallet, jup: Jupiter, mint: str, sell_queue: asyncio.Queue):
    lamports_in = await _lamports_for_usd(BUY_USD)
    sol_price = await get_sol_usd_price()
    tip_lamports = _priority_fee_lamports(sol_price)

    await _ensure_ata(wallet, mint)

    last_err = None
    quote = None
    for attempt in range(1, 4):
        try:
            q = await jup.quote(SOL_MINT, mint, lamports_in, slippage_bps=SLIPPAGE_BPS_BUY)
            if not _looks_liquid_enough(q):
                last_err = RuntimeError("route illiquid / extreme price impact")
                log_debug(f"⚠️ illiquid route attempt {attempt}/3 for {mint}")
                await asyncio.sleep(0.2)
                continue
            est_liq = _estimate_liquidity_usd(q, float(BUY_USD))
            if est_liq < float(MIN_LIQUIDITY_USD):
                last_err = RuntimeError(
                    f"estimated pool depth {est_liq:,.2f} USD < MIN_LIQUIDITY_USD {MIN_LIQUIDITY_USD:,.2f}"
                )
                log_debug(f"⚠️ skip {mint}: est_liquidity≈${est_liq:,.2f} < ${MIN_LIQUIDITY_USD:,.2f}")
                await asyncio.sleep(0.2)
                continue
            quote = q
            break
        except Exception as e:
            last_err = e
            log_warn(f"❌ quote fetch failed attempt {attempt}/3 for {mint}: {_short_error(e)}")
            await asyncio.sleep(0.2)

    if not quote:
        log_warn(f"⛔ no usable quote for {mint} ({_short_error(last_err)})")
        raise last_err or RuntimeError("no quote found")

    log_info(f"🟢 BUY ${BUY_USD} → {mint} (~{lamports_in / LAMPORTS_PER_SOL:.6f} SOL, tip≈{tip_lamports} lamports)")
    tx_b64 = await jup.swap_tx(quote, str(wallet.kp.pubkey()), tip_lamports, slippage_bps=SLIPPAGE_BPS_BUY)
    sig = await wallet.send_serialized_tx(tx_b64)
    log_info(f"  ↳ buy sig: {sig}")
    note_activity()

    # Best-effort confirmation (don’t block too long)
    try:
        await wallet.confirm(sig, commitment="confirmed", timeout_s=2.5)
    except Exception:
        pass

    # Poll for token balance for up to ~2.5s
    balance_seen = False
    t0 = now()
    while now() - t0 < 2.5:
        try:
            bal_lamports = await _get_spl_balance_lamports(wallet.client, str(wallet.kp.pubkey()), mint)
            if bal_lamports > 0:
                balance_seen = True
                break
        except Exception:
            pass
        await asyncio.sleep(0.25)

    # Schedule first SELL (non-blocking)
    settle_buf_cfg = float(os.getenv("SETTLE_BUFFER_SECONDS", "0.5"))
    settle_buf = settle_buf_cfg if not balance_seen else 0.0
    sell_at = now() + EXIT_AFTER_SECONDS + settle_buf
    await sell_queue.put({"mint": mint, "sell_at": sell_at, "tries": 0})
    log_info(f"⏳ scheduled sell for {mint} at +{EXIT_AFTER_SECONDS + settle_buf:.1f}s (balance_ready={balance_seen})")

async def _get_spl_balance_lamports(rpc, owner_pubkey, mint: str) -> int:
    url = rpc._provider.endpoint_uri
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [str(owner_pubkey), {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as r:
                data = await r.json()
    except Exception as e:
        log_warn(f"⚠️ direct RPC call error: {_short_error(e)}")
        return 0

    accounts = (data.get("result") or {}).get("value") or []
    log_debug(f"🔎 found {len(accounts)} token accounts for {mint}")

    total = 0
    for acc in accounts:
        try:
            amount = int(((acc.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("tokenAmount", {}).get("amount", 0))
            total += amount
        except Exception as e:
            log_debug(f"⚠️ parse error for {mint} account {acc.get('pubkey')}: {_short_error(e)}")

    return total


async def seller_loop(wallet: Wallet, jup: Jupiter, sell_queue: asyncio.Queue):
    while True:
        item = await sell_queue.get()
        delay = max(0.0, item["sell_at"] - now())  # monotonic
        if delay:
            log_debug(f"⏳ waiting {delay:.2f}s before selling {item['mint']}")
            await asyncio.sleep(delay)

        mint = item["mint"]
        tries = item["tries"]

        await _ensure_ata(wallet, mint)

        try:
            amount_in = await _get_spl_balance_lamports(wallet.client, str(wallet.kp.pubkey()), mint)
            # compute fraction of balance to sell based on SELL_FRACTION
            _fraction = max(0.0, min(1.0, SELL_FRACTION))
            sell_amount = int(amount_in * _fraction) if _fraction < 0.999999 else int(amount_in)
            if sell_amount <= 0 and amount_in > 0:
                sell_amount = min(1, amount_in)
        except Exception as e:
            log_warn(f"⚠️ error checking balance for {mint}: {_short_error(e)}")
            amount_in = 0

        if amount_in <= 0:
            tries += 1
            if tries < SELL_RETRY_MAX_TRIES:
                delay_s = _sell_retry_delay(tries)
                log_info(f"⚠️ no balance yet for {mint}, retrying {tries}/{SELL_RETRY_MAX_TRIES} in {delay_s:.2f}s")
                await sell_queue.put({
                    "mint": mint,
                    "sell_at": now() + delay_s,   # monotonic + your schedule
                    "tries": tries
                })
                continue
            else:
                log_warn(f"⛔ gave up selling {mint} (no balance after {tries} tries)")
                continue

        # --- SELL (mint -> SOL) ---
        try:
            # SELL-side tip: prefer SELL override, then global lamports, else compute via USD
            if SELL_PRIORITY_FEE_LAMPORTS_OVERRIDE > 0:
                tip_lamports = SELL_PRIORITY_FEE_LAMPORTS_OVERRIDE
            elif PRIORITY_FEE_LAMPORTS > 0:
                tip_lamports = PRIORITY_FEE_LAMPORTS
            else:
                sol_price = await get_sol_usd_price()
                tip_lamports = _priority_fee_lamports(sol_price)

            q = None
            last_err = None
            for attempt in range(1, 4):
                try:
                    q = await jup.quote(mint, SOL_MINT, sell_amount, slippage_bps=SLIPPAGE_BPS_SELL)
                    break
                except Exception as e:
                    last_err = e
                    log_warn(f"❌ sell quote fetch failed attempt {attempt}/3 for {mint}: {_short_error(e)}")
                    await asyncio.sleep(0.5)

            if not q:
                # Non-blocking scheduled retry on missing quote
                tries += 1
                if tries < SELL_RETRY_MAX_TRIES:
                    delay_s = _sell_retry_delay(tries)
                    log_info(f"⛔ no usable sell quote for {mint}; retry {tries}/{SELL_RETRY_MAX_TRIES} in {delay_s:.2f}s "
                            f"({_short_error(last_err)})")
                    await sell_queue.put({"mint": mint, "sell_at": now() + delay_s, "tries": tries})
                    continue
                else:
                    log_warn(f"🚫 giving up selling {mint}: no quote ({_short_error(last_err)})")
                    continue

            # Execute SELL with SELL-side slippage
            tx_b64 = await jup.swap_tx(q, str(wallet.kp.pubkey()), tip_lamports, slippage_bps=SLIPPAGE_BPS_SELL)
            sell_sig = await wallet.send_serialized_tx(tx_b64)
            log_info(f"💸 sell {mint} sig: {sell_sig}")
            analytics.log_trade(mint, "sell", sell_amount, 0)  # TODO: fill with actual price
            note_activity()

        except Exception as e:
            # Non-blocking scheduled retry on swap/tx failure
            tries += 1
            if tries < SELL_RETRY_MAX_TRIES:
                delay_s = _sell_retry_delay(tries)
                log_info(f"⟳ sell retry {tries}/{SELL_RETRY_MAX_TRIES} for {mint} in {delay_s:.2f}s: {_short_error(e)}")
                await sell_queue.put({"mint": mint, "sell_at": now() + delay_s, "tries": tries})
                continue
            else:
                log_warn(f"🚫 giving up selling {mint}: {_short_error(e)}")
                continue

# --- helper: fetch current slot quickly via JSON-RPC --------------------------------
async def _get_current_slot(rpc_url: str) -> int:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5)) as s:
        async with s.post(rpc_url, json=payload) as r:
            j = await r.json()
    return int(j.get("result") or 0)
# ------------------------------------------------------------------------------------

async def _get_true_mint_age_seconds(
    client: AsyncClient,
    mint_str: str,
    threshold_s: float | None = None,
    page_limit: int = 1000,
    max_pages: int = 6,
) -> float | None:
    """
    Return *true* on-chain age (seconds since the earliest known tx touching the Mint account).

    Strategy:
      - Page get_signatures_for_address backwards using `before`.
      - Each page is newest->older; we look at the oldest entry in the page.
      - Prefer `block_time` on the signature; else fall back via get_block_time(slot).
      - If `threshold_s` is provided, early-exit once oldest-so-far already exceeds it.
    Returns None when no timestamp could be determined.
    """
    try:
        pk = Pubkey.from_string(mint_str)
    except Exception:
        return None

    before_sig = None
    earliest_ts = None
    now_ts = time.time()

    for _ in range(max_pages):
        try:
            resp = await client.get_signatures_for_address(pk, limit=page_limit, before=before_sig)
            vals = getattr(resp, "value", None) or []
        except Exception:
            vals = []
        if not vals:
            break

        oldest = vals[-1]
        block_time = getattr(oldest, "block_time", None)
        if block_time is None:
            try:
                slot_val = getattr(oldest, "slot", None)
                if slot_val is not None:
                    bt = await client.get_block_time(slot_val)
                    block_time = getattr(bt, "value", None)
            except Exception:
                block_time = None

        if block_time is not None:
            earliest_ts = block_time if earliest_ts is None else min(earliest_ts, block_time)
            if threshold_s is not None and (now_ts - block_time) > threshold_s:
                return now_ts - block_time

        sig_obj = getattr(oldest, "signature", None)
        before_sig = str(sig_obj) if sig_obj is not None else None
        if before_sig is None:
            break

    if earliest_ts is None:
        return None
    return now_ts - earliest_ts

async def coordinator(candidates_q: asyncio.Queue):
    wallet = Wallet()
    jup = Jupiter()
    sell_q = asyncio.Queue()
    seller_task = asyncio.create_task(seller_loop(wallet, jup, sell_q))

    limiter = TokenBucket(rate_per_sec=MAX_BUYS_PER_SEC,
                         burst=max(1.0, MAX_BUYS_PER_SEC))
    seen: set[str] = set()

    try:
        while True:
            c = await candidates_q.get()
            mint = c["mint"]
            first_seen = float(c["t"])
            age = time.time() - first_seen

            log_info(f"🛠 evaluating {mint} | age={age:.2f}s")

            # Slot-age guard: only snipe within N slots of detection (default 3)
            ENTRY_MAX_AGE_SLOTS = int(os.getenv("ENTRY_MAX_AGE_SLOTS", "3"))
            slot_at_detection = int(c.get("slot") or 0)
            if ENTRY_MAX_AGE_SLOTS > 0 and slot_at_detection:
                try:
                    current_slot = await _get_current_slot(wallet.client._provider.endpoint_uri)
                    slot_age = max(0, current_slot - slot_at_detection)
                    if slot_age > ENTRY_MAX_AGE_SLOTS:
                        log_debug(f"⏩ skipped {mint} (slot age {slot_age} > {ENTRY_MAX_AGE_SLOTS})")
                        continue
                except Exception as e:
                    log_warn(f"⚠️ getSlot failed, skipping slot-age check: {_short_error(e)}")

            # --- True on-chain mint-age gate (min + max; optional) ---
            TRUE_AGE_MIN_SECONDS = float(os.getenv("MINT_AGE_MIN_SECONDS", "0"))
            TRUE_AGE_MAX_SECONDS = float(os.getenv("MINT_AGE_MAX_SECONDS", "0"))
            if TRUE_AGE_MAX_SECONDS > 0 or TRUE_AGE_MIN_SECONDS > 0:
                try:
                    onchain_age = await _get_true_mint_age_seconds(
                        wallet.client,
                        mint,
                        # keep early-exit fast path only when max is set
                        threshold_s=TRUE_AGE_MAX_SECONDS if TRUE_AGE_MAX_SECONDS > 0 else None,
                        page_limit=int(os.getenv("MINT_AGE_PAGE_LIMIT", "1000")),
                        max_pages=int(os.getenv("MINT_AGE_MAX_PAGES", "6")),
                    )
                except Exception as _e:
                    onchain_age = None
                if onchain_age is not None:
                    if TRUE_AGE_MIN_SECONDS > 0 and onchain_age < TRUE_AGE_MIN_SECONDS:
                        log_info(
                            f"⏩ skipped {mint} (true on-chain age {onchain_age:.2f}s < {TRUE_AGE_MIN_SECONDS}s)"
                        )
                        continue
                    if TRUE_AGE_MAX_SECONDS > 0 and onchain_age > TRUE_AGE_MAX_SECONDS:
                        log_info(
                            f"⏩ skipped {mint} (true on-chain age {onchain_age:.2f}s > {TRUE_AGE_MAX_SECONDS}s)"
                        )
                        continue

            if mint in seen:
                log_debug(f"⏩ skipped {mint} (already seen)")
                continue
            if not limiter.take():
                log_debug(f"⏩ skipped {mint} (rate limited)")
                continue

            seen.add(mint)

            async def attempt_buy_until_window(mint_local: str, first_seen_local: float):
                attempt = 0
                while time.time() - first_seen_local <= ENTRY_MAX_AGE_SECONDS:
                    attempt += 1
                    try:
                        log_info(f"⚡ attempting buy (attempt {attempt}) for {mint_local}")
                        await snipe_once(wallet, jup, mint_local, sell_q)
                        return
                    except Exception as e:
                        log_warn(f"❌ buy attempt {attempt} failed for {mint_local}: {_short_error(e)}")
                        await asyncio.sleep(0.2)
                log_warn(f"❌ window expired without buy for {mint_local}")

            asyncio.create_task(attempt_buy_until_window(mint, first_seen))
    except asyncio.CancelledError:
        log_info("🛑 coordinator cancelled, shutting down gracefully...")
        raise
    finally:
        try:
            seller_task.cancel()
            with contextlib.suppress(Exception):
                await seller_task
        except Exception:
            pass
        try:
            await jup.close()
        except Exception:
            pass
        try:
            await wallet.close()   # ← use .close(), not .aclose()
        except Exception:
            pass

# --- Instant buy engine for token monitor integration ---
async def instant_buy(event_data):
    """
    Called by token_monitor when a new Pump.fun token is detected.
    event_data: dict from websocket logs notification
    """
    from wallet import Wallet
    from jupiter import Jupiter
    
    # Extract mint address from event_data (customize as needed)
    logs = event_data.get("result", {}).get("value", {}).get("logs", [])
    mint = None
    for log in logs:
        if "mint" in log:
            # crude extraction, customize for your log format
            parts = log.split()
            for part in parts:
                if len(part) == 44:  # Solana mint length
                    mint = part
                    break
        if mint:
            break
    if not mint:
        print("No mint found in logs, skipping buy.")
        return

    wallet = Wallet()
    jup = Jupiter()
    sell_queue = asyncio.Queue()

    # Configurable hold time, slippage, stop-loss
    hold_time = float(os.getenv("SCALP_HOLD_SEC", "3.0"))
    slippage_buy = int(os.getenv("SLIPPAGE_BPS_BUY", "9000"))
    stop_loss = float(os.getenv("MCAP_STOP_LOSS", "0.05"))

    # Rug-pull detection
    if await check_rug_pull(mint):
        print(f"Rug-pull detected for mint {mint}, skipping buy.")
        return

    await snipe_once(wallet, jup, mint, sell_queue)
    # Log buy trade (amount and price can be customized)
    analytics.log_trade(mint, "buy", 1, 0)  # TODO: fill with actual amount/price
    asyncio.create_task(seller_loop(wallet, jup, sell_queue))
