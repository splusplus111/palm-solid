# s1_stairs.py (drop-in)
import asyncio, time, logging
from typing import Optional
import time as _time

# ------------ config imports (safe defaults for optional knobs) ------------
from config import (
    ENTRY_CLIP_USD, SCALP_HOLD_SEC, SCALP_REENTER_UNTIL_LOSS, SCALP_COOLDOWN_SEC,
    BLACKLIST_COOLDOWN_SEC, MAX_BUYS_PER_SEC, MOON_BAG_ENABLED, SELL_LADDER,
    SLIPPAGE_BPS_BUY, SLIPPAGE_BPS_SELL,
    MCAP_TP_LEVELS, MCAP_TP_FRACTIONS, MCAP_SELL_ALL_LEVEL,
    MCAP_ARM_STOP_AFTER, MCAP_STOP_LOSS, INSTANT_DROP_STOP_PCT,
    TOKEN_TOTAL_SUPPLY, TOKEN_DECIMALS, MCAP_CHECK_INTERVAL_MS,
    SOL_MINT,
    REENTER_NEEDS_NEXT_POP, REENTER_POP_TIMEOUT_MS,
    # dynamic ladder
    DYNAMIC_BAG_ENABLED, DYNAMIC_BAG_START_USD, DYNAMIC_BAG_STEP_USD,
    DYNAMIC_BAG_SELL_FRAC, DYNAMIC_BAG_MAX_USD,
    DYNAMIC_BAG_IDLE_TIMEOUT_SEC, DYNAMIC_BAG_MAX_DURATION_SEC,
    LOG_LEVEL,
    # used by _get_sol_usd_cached()
    SOL_PRICE_TTL_SEC,
)

# These are OPTIONAL. If your config doesn’t define them, we default them here.
try:
    from config import (
        MCAP_JUMP_MODE_ENABLED,      # bool: enable “jump-from→to” entry mode
        MCAP_JUMP_FROM_USD,          # float: e.g. 15000
        MCAP_JUMP_TO_USD,            # float: e.g. 60000
        MCAP_JUMP_HOLD_SEC,          # float: e.g. 60.0
    )
except Exception:
    MCAP_JUMP_MODE_ENABLED = False
    MCAP_JUMP_FROM_USD = 15000.0
    MCAP_JUMP_TO_USD = 60000.0
    MCAP_JUMP_HOLD_SEC = 60.0

from wallet import Wallet
from jupiter import Jupiter
from price import get_sol_usd_price
from spike_detector import monitor_spikes_by_mint, wait_for_next_pop_or_bucket
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

LAMPORTS_PER_SOL = 1_000_000_000

# ------------ logging helpers ------------
_level = getattr(logging, str(LOG_LEVEL).upper(), logging.INFO)
logging.basicConfig(level=_level, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("s1")
def info(msg: str): _log.info(msg)
def warn(msg: str): _log.warning(msg)

# ------------ small token bucket ------------
class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float = 1.0):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = burst
        self.updated = _time.monotonic()
    def take(self, amount=1.0):
        now = _time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

# ------------ on-chain helpers ------------
async def _get_spl_balance_lamports(client: AsyncClient, owner: str, mint: str) -> int:
    try:
        resp = await client.get_token_accounts_by_owner(Pubkey.from_string(owner), mint=Pubkey.from_string(mint))
        accs = getattr(resp, "value", []) or []
        tot = 0
        for a in accs:
            ui = a.account.data.parsed["info"]["tokenAmount"]
            tot += int(ui["amount"])
        return tot
    except Exception:
        return 0

_SOL_PRICE_CACHE = {"t": 0.0, "v": 0.0}
async def _get_sol_usd_cached():
    now = _time.monotonic()
    if now - _SOL_PRICE_CACHE["t"] < float(SOL_PRICE_TTL_SEC):
        return max(0.01, _SOL_PRICE_CACHE["v"])
    v = max(0.01, await get_sol_usd_price())
    _SOL_PRICE_CACHE.update(t=now, v=v)
    return v

async def _priority_tip_lamports():
    sol_usd = await _get_sol_usd_cached()
    return int((0.005 / sol_usd) * LAMPORTS_PER_SOL)  # ≈$0.50; tune if blocks sticky

# ------------ swap wrappers ------------
async def buy_once(wallet: Wallet, jup: Jupiter, mint: str, usd: float) -> Optional[str]:
    sol_usd = max(0.01, await get_sol_usd_price())
    lamports_in = max(1, int((usd / sol_usd) * LAMPORTS_PER_SOL))
    q = await jup.quote(SOL_MINT, mint, lamports_in, slippage_bps=SLIPPAGE_BPS_BUY)
    txb64 = await jup.swap_tx(q, str(wallet.kp.pubkey()), await _priority_tip_lamports(), slippage_bps=SLIPPAGE_BPS_BUY)
    return await wallet.send_serialized_tx(txb64)

async def sell_fraction(wallet: Wallet, jup: Jupiter, mint: str, frac: float) -> Optional[str]:
    owner = str(wallet.kp.pubkey())
    bal = await _get_spl_balance_lamports(wallet.client, owner, mint)
    if bal <= 0:
        return None
    q = await jup.quote(mint, SOL_MINT, max(1, int(bal * max(0.0, min(1.0, frac)))), slippage_bps=SLIPPAGE_BPS_SELL)
    txb64 = await jup.swap_tx(q, owner, await _priority_tip_lamports(), slippage_bps=SLIPPAGE_BPS_SELL)
    return await wallet.send_serialized_tx(txb64)

# ------------ mcap estimation ------------
_PRICE_CACHE = {}  # mint -> {"t": ts, "v": price_usd_per_token}
async def _price_usd_per_token(jup: Jupiter, mint: str) -> float:
    try:
        now = _time.monotonic()
        entry = _PRICE_CACHE.get(mint)
        min_gap = max(0.05, float(MCAP_JUMP_CHECK_MS) / 1000.0)  # fast when jump-scouting
        if entry and (now - entry["t"]) < min_gap:
            return entry["v"]
        amount = 10 ** TOKEN_DECIMALS  # 1 token
        q = await jup.quote(mint, SOL_MINT, amount)
        out_lamports = float(q.get("outAmount", "0"))
        sol_amount = out_lamports / float(LAMPORTS_PER_SOL)
        sol_usd = await _get_sol_usd_cached()
        v = sol_amount * sol_usd
        _PRICE_CACHE[mint] = {"t": now, "v": v}
        return v
    except Exception:
        return 0.0

async def _est_mcap_usd(jup: Jupiter, mint: str) -> float:
    p = await _price_usd_per_token(jup, mint)
    return p * float(TOKEN_TOTAL_SUPPLY)

# ------------ milestone scalp (kept as fallback) ------------
async def milestone_scalp_round(wallet: Wallet, jup: Jupiter, mint: str, usd: float) -> int:
    pre_lamports = await wallet.get_lamports()
    bsig = await buy_once(wallet, jup, mint, usd)
    info(f"BUY ${usd:.2f} {mint} | {bsig}")

    levels = list(MCAP_TP_LEVELS)
    fracs = list(MCAP_TP_FRACTIONS)
    armed_stop = False
    last_mcap = None
    t0 = time.monotonic()

    while (time.monotonic() - t0) < SCALP_HOLD_SEC:
        mcap = await _est_mcap_usd(jup, mint)
        if mcap <= 0:
            await asyncio.sleep(MCAP_CHECK_INTERVAL_MS / 1000.0); continue

        if last_mcap is not None:
            drop_pct = 100.0 * (last_mcap - mcap) / max(1e-9, last_mcap)
            if drop_pct >= INSTANT_DROP_STOP_PCT:
                info(f"⚠️ instant drop {drop_pct:.2f}% → EXIT ALL")
                await sell_fraction(wallet, jup, mint, 1.0)
                post = await wallet.get_lamports()
                return post - pre_lamports
        last_mcap = mcap

        if not armed_stop and mcap >= MCAP_ARM_STOP_AFTER:
            armed_stop = True
        if armed_stop and mcap <= MCAP_STOP_LOSS:
            info(f"🛑 mcap stop {mcap:,.0f} ≤ {MCAP_STOP_LOSS:,.0f} → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0)
            post = await wallet.get_lamports()
            return post - pre_lamports

        if levels and mcap >= levels[0]:
            frac = fracs[0] if fracs else 0.0
            info(f"🎯 hit {levels[0]:,.0f} → SELL {frac*100:.0f}%")
            await sell_fraction(wallet, jup, mint, frac)
            levels.pop(0)
            if fracs: fracs.pop(0)

        if mcap >= MCAP_SELL_ALL_LEVEL:
            info(f"🏁 reached {MCAP_SELL_ALL_LEVEL:,.0f} → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0)
            post = await wallet.get_lamports()
            return post - pre_lamports

        await asyncio.sleep(MCAP_CHECK_INTERVAL_MS / 1000.0)

    await sell_fraction(wallet, jup, mint, 1.0)
    post_lamports = await wallet.get_lamports()
    return post_lamports - pre_lamports

# ------------ dynamic moon-bag ladder ------------
async def dynamic_bag_round(wallet: Wallet, jup: Jupiter, mint: str, usd: float) -> int:
    pre_lamports = await wallet.get_lamports()
    bsig = await buy_once(wallet, jup, mint, usd)
    info(f"BUY ${usd:.2f} {mint} | {bsig}")

    armed_stop = False
    last_mcap = None
    next_level = float(DYNAMIC_BAG_START_USD)
    idle_deadline = time.monotonic() + float(DYNAMIC_BAG_IDLE_TIMEOUT_SEC)
    absolute_deadline = time.monotonic() + float(DYNAMIC_BAG_MAX_DURATION_SEC)

    while True:
        if time.monotonic() >= absolute_deadline:
            info("⏱️ max duration reached → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0); break

        mcap = await _est_mcap_usd(jup, mint)
        if mcap <= 0:
            await asyncio.sleep(MCAP_CHECK_INTERVAL_MS / 1000.0); continue

        if last_mcap is not None:
            drop_pct = 100.0 * (last_mcap - mcap) / max(1e-9, last_mcap)
            if drop_pct >= INSTANT_DROP_STOP_PCT:
                info(f"⚠️ instant drop {drop_pct:.2f}% → EXIT ALL")
                await sell_fraction(wallet, jup, mint, 1.0); break
        last_mcap = mcap

        if not armed_stop and mcap >= MCAP_ARM_STOP_AFTER:
            armed_stop = True
        if armed_stop and mcap <= MCAP_STOP_LOSS:
            info(f"🛑 mcap stop {mcap:,.0f} ≤ {MCAP_STOP_LOSS:,.0f} → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0); break

        if mcap >= float(DYNAMIC_BAG_MAX_USD):
            info(f"🏁 reached {DYNAMIC_BAG_MAX_USD:,.0f} → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0); break

        while mcap >= next_level:
            info(f"🌙 dynamic ladder hit {next_level:,.0f} → SELL {DYNAMIC_BAG_SELL_FRAC*100:.0f}% of remaining")
            await sell_fraction(wallet, jup, mint, float(DYNAMIC_BAG_SELL_FRAC))
            next_level += float(DYNAMIC_BAG_STEP_USD)

        if REENTER_NEEDS_NEXT_POP:
            ok_flow = await wait_for_next_pop_or_bucket(mint, jup, int(MCAP_CHECK_INTERVAL_MS))
            if ok_flow:
                idle_deadline = time.monotonic() + float(DYNAMIC_BAG_IDLE_TIMEOUT_SEC)

        if time.monotonic() >= idle_deadline:
            info("⏸️ no fresh flow for dynamic ladder → EXIT ALL")
            await sell_fraction(wallet, jup, mint, 1.0); break

        await asyncio.sleep(MCAP_CHECK_INTERVAL_MS / 1000.0)

    post_lamports = await wallet.get_lamports()
    pnl = post_lamports - pre_lamports
    info(f"round PnL: {pnl} lamports")
    return pnl

# ------------ NEW: MCAP jump trigger ------------
async def wait_for_mcap_jump(jup: Jupiter, mint: str) -> bool:
    """
    Watch early mcaps and trigger as soon as a 'low→high' jump or big delta appears.
    """
    lo = float(MCAP_JUMP_LO_USD)
    hi = float(MCAP_JUMP_HI_USD)
    need_delta = float(MCAP_JUMP_REQUIRE_DELTA)
    check_dt = max(50, int(MCAP_JUMP_CHECK_MS)) / 1000.0
    deadline = time.monotonic() + float(MCAP_JUMP_WINDOW_SEC)

    m0 = 0.0
    first_good = None

    info(f"👀 watching MCAP jump on {mint} for {MCAP_JUMP_WINDOW_SEC}s (lo={lo:,.0f} → hi={hi:,.0f}, delta={need_delta:,.0f})")

    while time.monotonic() < deadline:
        m = await _est_mcap_usd(jup, mint)
        if m <= 0:
            await asyncio.sleep(check_dt); continue

        if first_good is None:
            first_good = m
            m0 = m
            info(f"📈 initial mcap ~ {m0:,.0f}")
        else:
            delta = m - m0
            if (m0 <= lo and m >= hi) or (delta >= need_delta):
                info(f"⚡ MCAP jump detected: {m0:,.0f} → {m:,.0f} (Δ={delta:,.0f})")
                return True

        await asyncio.sleep(check_dt)

    info("⌛ no MCAP jump within window")
    return False

# ------------ main stairs loop ------------
async def run_stairs_for_mint(mint: str):
    wallet = Wallet()
    jup = Jupiter()
    limiter = TokenBucket(MAX_BUYS_PER_SEC, 1.0)
    try:
        ok = False
        if MCAP_JUMP_MODE_ENABLED:
            ok = await wait_for_mcap_jump(jup, mint)
        else:
            ok = await monitor_spikes_by_mint(mint, jup)

        if not ok:
            warn(f"no step pattern for {mint}")
            return

        info(f"🚀 stairs active on {mint}")

        # Moon-bag preferred if enabled
        if DYNAMIC_BAG_ENABLED:
            if not limiter.take():
                await asyncio.sleep(0.2)
            await dynamic_bag_round(wallet, jup, mint, ENTRY_CLIP_USD)
            info(f"🛑 finished stairs for {mint} (dynamic ladder mode)")
            return

        # (fallback) milestone scalp loop
        while True:
            if not limiter.take():
                await asyncio.sleep(0.2); continue

            pnl = await milestone_scalp_round(wallet, jup, mint, ENTRY_CLIP_USD)

            if pnl < 0:
                warn("❌ losing round — stopping & blacklisting")
                await asyncio.sleep(BLACKLIST_COOLDOWN_SEC)
                break

            if REENTER_NEEDS_NEXT_POP:
                ok_next = await wait_for_next_pop_or_bucket(mint, jup, REENTER_POP_TIMEOUT_MS)
                if not ok_next:
                    info("⏸️ no fresh flow for re-entry — stopping mint")
                    break

            if not SCALP_REENTER_UNTIL_LOSS:
                break
            await asyncio.sleep(SCALP_COOLDOWN_SEC)

        info(f"🛑 finished stairs for {mint}")
    finally:
        try: await jup.close()
        except: pass
        try: await wallet.close()
        except: pass
