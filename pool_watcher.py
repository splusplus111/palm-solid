import asyncio
import json
import time
import re
import websockets
import aiohttp

from config import SOLANA_WS_URL, WATCH_PROGRAM_IDS, FORCE_TOKEN_MINT, LOG_LEVEL, PUMPFUN_PROGRAM_ID
from constants import SOL_MINT, USDC_MINT

# --- Only react to brand-new Pump.fun deploy/initialize logs ---
def _pumpfun_creation_only(program_id: str | None, logs: list[str]) -> bool:
    """
    Return True only for Pump.fun 'create/deploy/initialize' events.

    Note: some RPCs omit programId in logsNotification when using {"mentions":[...]}.
    If program_id is missing, allow markers-only *iff* your WATCH_PROGRAM_IDS is exactly [PUMPFUN_PROGRAM_ID].
    """
    blob = " ".join(logs)
    markers = ("Initialize", "initialize", "Create", "create", "Deploy", "deploy", "bonding", "Bonding", "DB")

    if program_id:
        return (program_id == PUMPFUN_PROGRAM_ID) and any(m in blob for m in markers)

    # program_id missing → safe fallback only if you're subscribed to Pump.fun alone
    try:
        if WATCH_PROGRAM_IDS and len(WATCH_PROGRAM_IDS) == 1 and WATCH_PROGRAM_IDS[0] == PUMPFUN_PROGRAM_ID:
            return any(m in blob for m in markers)
    except Exception:
        pass
    return False


# --- Fallback: extract mint from a transaction (when logs don’t print it) ---
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # SPL Token 2022
}

async def _extract_mint_from_tx(sig: str) -> str | None:
    """
    Try to recover the mint pubkey from the transaction's message contents.
    Fast path only: we DO NOT validate via getAccountInfo here to avoid extra HTTP.
    """
    rpc_http = SOLANA_WS_URL.replace("wss", "https", 1)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.0)) as s:
            async with s.post(rpc_http, json=payload) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception:
        return None

    tx = (data.get("result") or {})
    msg = (tx.get("transaction") or {}).get("message") or {}

    # account keys (legacy or v0)
    keys = []
    if isinstance(msg.get("accountKeys"), list):
        keys = msg["accountKeys"]
    else:
        static_keys = msg.get("staticAccountKeys") or []
        la = msg.get("loadedAddresses") or {}
        keys = static_keys + (la.get("writable") or []) + (la.get("readonly") or [])

    # instructions (legacy only exposed here)
    ixs = msg.get("instructions") or []

    # 1) look for Token Program instruction: first account is mint on initializeMint
    for ix in ixs:
        prog_idx = ix.get("programIdIndex")
        if isinstance(prog_idx, int) and 0 <= prog_idx < len(keys):
            if keys[prog_idx] in TOKEN_PROGRAMS:
                accs = ix.get("accounts") or []
                if accs:
                    mint_idx = accs[0]
                    if isinstance(mint_idx, int) and 0 <= mint_idx < len(keys):
                        return keys[mint_idx]

    # 2) fallback: just return the first non-program key as candidate (no HTTP validation here)
    for cand in keys:
        if isinstance(cand, str) and cand not in TOKEN_PROGRAMS:
            return cand
    return None


# === Lightweight logging ======================================================
try:
    _CFG_LOG_LEVEL = str(LOG_LEVEL).upper()
except Exception:
    _CFG_LOG_LEVEL = "INFO"

_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2}
_CURRENT_LEVEL = _LEVELS.get(_CFG_LOG_LEVEL, 1)

def _log(level: str, msg: str):
    if _LEVELS.get(level, 1) >= _CURRENT_LEVEL:
        print(msg)

def log_debug(msg: str): _log("DEBUG", msg)
def log_info(msg: str):  _log("INFO", msg)
def log_warn(msg: str):  _log("WARN", msg)

def _short_error(e: Exception) -> str:
    msg = str(e) or repr(e)
    if "custom program error" in msg:
        return "custom program error: " + msg.split("custom program error")[-1].strip()
    if "Transaction simulation failed" in msg:
        return "Transaction simulation failed"
    return msg.splitlines()[0]
# ============================================================================


# === Regex & skip list ===
ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

SKIP_LIST = {
    SOL_MINT,
    USDC_MINT,
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "SysvarRent111111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}


async def _subscribe_program(ws, program_id: str, sub_id: int) -> None:
    req = {
        "jsonrpc": "2.0",
        "id": sub_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": "processed"},
        ],
    }
    await ws.send(json.dumps(req))
    ack_raw = await ws.recv()
    try:
        ack = json.loads(ack_raw)
        log_info(f"✅ subscribed → program={program_id} sub_id={sub_id} ack={ack.get('result')}")
    except Exception:
        log_info(f"✅ subscribed → program={program_id} sub_id={sub_id} (ack parse failed)")


async def raydium_new_pool_stream(out_q: asyncio.Queue):
    # Optional smoke test
    if FORCE_TOKEN_MINT:
        log_info(f"⚡ Forced token injection: {FORCE_TOKEN_MINT}")
        await out_q.put({"sig": "manual", "mint": FORCE_TOKEN_MINT, "t": time.time(), "slot": 0})

    if not WATCH_PROGRAM_IDS:
        log_warn("⚠️ No program IDs configured; nothing to watch.")
        return

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            log_debug("⏳ watcher heartbeat — connected & listening...")

    # simple duplicate suppression
    seen: set[str] = set()

    async def _tx_fallback_task(sig: str, slot: int | None):
        """Run tx fallback off the hot path; enqueue if we discover a candidate mint."""
        try:
            mint = await _extract_mint_from_tx(sig)
            if not mint:
                return
            if mint in SKIP_LIST or mint in seen:
                return
            seen.add(mint)
            await out_q.put({"sig": sig, "mint": mint, "t": float(time.time()), "slot": int(slot or 0)})
            log_info(f"🟢 queued via tx fallback: {mint} | slot={slot}")
        except Exception as e:
            log_debug(f"tx fallback error ({sig[:8]}…): {_short_error(e)}")

    while True:
        try:
            async with websockets.connect(
                SOLANA_WS_URL,
                max_size=10_000_000,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                log_info(f"🧩 opening WS to {SOLANA_WS_URL} and subscribing to {len(WATCH_PROGRAM_IDS)} program(s)...")
                for i, pid in enumerate(WATCH_PROGRAM_IDS, start=1):
                    await _subscribe_program(ws, pid, i)
                log_info("🟢 all subscriptions sent — waiting for logs...")

                hb = asyncio.create_task(heartbeat())

                while True:
                    raw = await ws.recv()
                    t0 = time.perf_counter()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("method") != "logsNotification":
                        continue

                    params = msg.get("params") or {}
                    result = params.get("result") or {}
                    value = result.get("value") or {}
                    context = result.get("context") or {}

                    sig: str = value.get("signature") or ""
                    logs: list[str] = value.get("logs") or []
                    slot = context.get("slot")
                    program_id: str | None = value.get("programId") or value.get("program") or None

                    if not sig or not logs:
                        continue

                    # 💡 Pump.fun deploy-only gate (prevents reacting to old Raydium swaps)
                    if not _pumpfun_creation_only(program_id, logs):
                        continue

                    log_debug(f"🔔 ws msg: slot={slot} sig={str(sig)[:8]}… logs={len(logs)}")

                    # --- CANDIDATE EXTRACTION (ZERO-HTTP PATH) ---
                    candidates: list[str] = []
                    for line in logs:
                        candidates.extend(ADDRESS_RE.findall(line))

                    # basic sanity for base58-ish len; keep cheap
                    candidates = [c for c in candidates if 32 <= len(c) <= 44]

                    # 🚀 ENQUEUE FIRST, VALIDATE LATER (no HTTP before queue)
                    enqueued_any = False
                    enqueued_count = 0
                    for cand in candidates:
                        if cand in SKIP_LIST or cand in seen:
                            continue
                        seen.add(cand)
                        await out_q.put({
                            "sig": str(sig),
                            "mint": cand,
                            "t": float(time.time()),
                            "slot": int(slot or 0),
                        })
                        enqueued_any = True
                        enqueued_count += 1
                        if enqueued_count == 1:
                            dt_ms = (time.perf_counter() - t0) * 1000.0
                            log_info(f"🟢 queued mint fast: {cand} | slot={slot} | ws→enqueue {dt_ms:.1f} ms")
                        else:
                            log_debug(f"🟢 queued extra candidate: {cand} | slot={slot}")

                    # If logs didn’t surface a mint, try the tx fallback *off* the hot path
                    if not enqueued_any:
                        log_debug(f"🧪 scheduling tx fallback for sig {str(sig)[:8]}…")
                        asyncio.create_task(_tx_fallback_task(str(sig), slot))

        except Exception as e:
            log_warn(f"⚠️ WebSocket error, reconnecting in 0.5s… ({_short_error(e)})")
            await asyncio.sleep(0.5)
            continue
