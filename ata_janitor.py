# ata_janitor.py — background task to reclaim rent from empty ATAs (safe, rate-limited)
import asyncio
import time
from typing import Dict, Deque, Set, Tuple
from collections import deque
import aiohttp

from constants import LAMPORTS_PER_SOL
from wallet import Wallet
from config import (
    CLOSE_ATA_ENABLED,
    CLOSE_ATA_COOLDOWN_SECS,
    CLOSE_ATA_INTERVAL_SECS,
    CLOSE_ATA_MAX_PER_MIN,
    CLOSE_ATA_TIP_LAMPORTS,      # currently unused (close uses base fee); kept for future
    CLOSE_ATA_MIN_SOL_RESERVE,
    CLOSE_ATA_IDLE_WINDOW_SECS,
    CLOSE_ATA_EXCLUDE_MINTS,     # list[str]
)

_NOW = time.monotonic

# In–process state
_zero_since: Dict[str, float] = {}       # mint -> first time we saw it empty
_recent_closes: Deque[float] = deque()   # timestamps of closes for rate limit
_last_activity_ts: float = 0.0           # updated by note_activity()

def note_activity() -> None:
    """Call this from buy/sell paths to pause janitor briefly during trading."""
    global _last_activity_ts
    _last_activity_ts = _NOW()

async def _list_zero_balance_atas(wallet: Wallet) -> Tuple[Tuple[str, str], ...]:
    """
    Return tuple of (ata_pubkey, mint) for ATAs with balance == 0.
    Uses direct JSON-RPC to ensure 'jsonParsed' response quickly.
    """
    url = wallet.client._provider.endpoint_uri  # type: ignore[attr-defined]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            str(wallet.pubkey),
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ],
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=payload, timeout=10) as r:
                data = await r.json()
    except Exception:
        return tuple()

    out = []
    for item in (data.get("result") or {}).get("value") or []:
        try:
            acc = (item.get("account") or {})
            parsed = ((acc.get("data") or {}).get("parsed") or {})
            info = parsed.get("info") or {}
            amount = (info.get("tokenAmount") or {}).get("amount")
            mint = info.get("mint")
            if amount == "0" and isinstance(mint, str):
                out.append((item.get("pubkey"), mint))
        except Exception:
            continue
    return tuple(out)

async def _rate_limited() -> bool:
    """Keep at most CLOSE_ATA_MAX_PER_MIN closes in the last rolling minute."""
    now = _NOW()
    while _recent_closes and now - _recent_closes[0] > 60.0:
        _recent_closes.popleft()
    return len(_recent_closes) >= CLOSE_ATA_MAX_PER_MIN

async def janitor_loop(wallet: Wallet):
    """
    Background task:
      - waits for idle window since last buy/sell
      - keeps SOL >= CLOSE_ATA_MIN_SOL_RESERVE
      - respects cooldown per mint and a per-minute hard cap
      - closes empty ATAs to reclaim rent
    """
    if not CLOSE_ATA_ENABLED:
        print("🧹 ATA Janitor disabled (CLOSE_ATA_ENABLED=false).")
        return

    print("🧹 ATA Janitor started…")
    exclude: Set[str] = set(CLOSE_ATA_EXCLUDE_MINTS or [])

    try:
        while True:
            # Idle gating
            if CLOSE_ATA_IDLE_WINDOW_SECS > 0 and (_NOW() - _last_activity_ts) < CLOSE_ATA_IDLE_WINDOW_SECS:
                await asyncio.sleep(1.0)
                continue

            # SOL reserve check
            lamports = await wallet.get_lamports()
            if lamports < int(CLOSE_ATA_MIN_SOL_RESERVE * LAMPORTS_PER_SOL):
                # Not enough SOL buffer; skip this cycle
                await asyncio.sleep(CLOSE_ATA_INTERVAL_SECS)
                continue

            # Per-minute cap
            if await _rate_limited():
                await asyncio.sleep(2.0)
                continue

            # Scan empties
            empties = await _list_zero_balance_atas(wallet)
            if not empties:
                await asyncio.sleep(CLOSE_ATA_INTERVAL_SECS)
                continue

            # Iterate empties; enforce per-mint cooldown
            now = _NOW()
            did_close = False
            for _ata, mint in empties:
                if mint in exclude:
                    continue
                first = _zero_since.get(mint)
                if first is None:
                    _zero_since[mint] = now
                    continue
                if now - first < CLOSE_ATA_COOLDOWN_SECS:
                    continue

                try:
                    ok = await wallet.try_close_ata(mint)
                    if ok:
                        _recent_closes.append(now)
                        _zero_since.pop(mint, None)
                        print(f"🧹 closed empty ATA for {mint}")
                        did_close = True
                        # Respect per-minute cap as we go
                        if await _rate_limited():
                            break
                except Exception:
                    # Non-fatal; keep going
                    continue

            # Pace the loop
            await asyncio.sleep(CLOSE_ATA_INTERVAL_SECS if not did_close else 1.0)
    except asyncio.CancelledError:
        print("🧹 ATA Janitor stopping…")
        raise
