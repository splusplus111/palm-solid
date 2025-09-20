# spike_detector.py
import asyncio, json, time
from typing import Optional
import websockets

from config import (
    SOLANA_WS_URL,
    # detector core
    SPIKE_WINDOW_SEC, SPIKE_MIN_USD, SPIKE_REQUIRED, SPIKE_GAP_MIN_MS, SPIKE_GAP_MAX_MS,
    # buckets
    SPIKE_USE_BUCKETS, SPIKE_BUCKET_SECS,
    # reentry gate
    REENTER_NEEDS_NEXT_POP, REENTER_POP_TIMEOUT_MS,
    # optional cumulative
    PUMP_CUM_WINDOW_SEC, PUMP_CUM_MIN_USD,
)

# We treat each log batch that mentions the mint as a "trade event".
# For bucket mode, ≥1 event inside a bucket qualifies that bucket.

# ------------------ low-level WS helpers ------------------

def _ws_subscribe_body(mint: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [mint]},
            {"commitment": "processed"}  # use "processed" for slightly faster, "confirmed" for safer
        ]
    })

def _open_ws():
    """
    Return an async context manager for the websocket connection.
    Usage:  async with _open_ws() as ws: ...
    """
    return websockets.connect(SOLANA_WS_URL, open_timeout=3)

# ------------------ non-bucket “pop” detector ------------------

async def _detect_by_pops(ws, mint: str) -> bool:
    pops = []
    last_t: Optional[float] = None
    start = time.monotonic()
    await ws.send(_ws_subscribe_body(mint))

    while (time.monotonic() - start) <= SPIKE_WINDOW_SEC:
        # keep timeout positive
        remaining = max(0.05, SPIKE_WINDOW_SEC - (time.monotonic() - start))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break

        try:
            j = json.loads(raw)
            logs = j.get("params", {}).get("result", {}).get("value", {}).get("logs", []) or []
            if not logs:
                continue

            now = time.monotonic()
            if last_t is None or (SPIKE_GAP_MIN_MS <= (now - last_t) * 1000 <= SPIKE_GAP_MAX_MS):
                pops.append(now)
                last_t = now
                print(f"🔔 pop {len(pops)}/{SPIKE_REQUIRED} for {mint}")
                if len(pops) >= max(1, int(SPIKE_REQUIRED)):
                    return True
            else:
                # gap too small/large → reset chain
                pops = [now]
                last_t = now
        except Exception as e:
            # swallow parse glitches; continue receiving
            # print("pop detector parse error:", e)
            continue

    return False

# ------------------ bucketed detector (recommended for this team) ------------------

async def _detect_by_buckets(ws, mint: str) -> bool:
    """
    N-second buckets (default 2s). A bucket 'qualifies' if it has >=1 event.
    Trigger when we see SPIKE_REQUIRED qualified buckets within SPIKE_WINDOW_SEC.
    Also supports a light cumulative fallback during the early window.
    """
    bucket_secs = max(1, int(SPIKE_BUCKET_SECS))

    start = time.monotonic()
    bucket_start = start
    bucket_events = 0
    qualified_buckets = 0

    # For the cumulative fallback, we just count events quickly.
    events_in_window = 0

    await ws.send(_ws_subscribe_body(mint))

    while (time.monotonic() - start) <= SPIKE_WINDOW_SEC:
        # roll bucket if needed
        now = time.monotonic()
        if (now - bucket_start) >= bucket_secs:
            # finalize the last bucket
            if bucket_events > 0:
                qualified_buckets += 1
                print(f"🪣 bucket qualified ({qualified_buckets}/{SPIKE_REQUIRED}) for {mint}")
                if qualified_buckets >= max(1, int(SPIKE_REQUIRED)):
                    return True
            # next bucket
            bucket_start = now
            bucket_events = 0

        # receive with a short timeout to keep rolling buckets
        per_iter_timeout = max(0.05, min(0.5, bucket_secs / 4))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=per_iter_timeout)
        except asyncio.TimeoutError:
            continue

        try:
            j = json.loads(raw)
            logs = j.get("params", {}).get("result", {}).get("value", {}).get("logs", []) or []
            if not logs:
                continue

            # treat each batch as one event
            bucket_events += 1
            events_in_window += 1

            # refresh 'now' after await for timing logic below
            now = time.monotonic()

            # CUMULATIVE EARLY TRIGGER (heuristic):
            # If we see several events quickly, treat as continuous blast.
            if PUMP_CUM_WINDOW_SEC > 0 and (now - start) <= PUMP_CUM_WINDOW_SEC:
                if events_in_window >= 3 and PUMP_CUM_MIN_USD > 0:
                    print(f"⚡ cumulative early trigger for {mint} (events={events_in_window})")
                    return True

        except Exception as e:
            # print("bucket detector parse error:", e)
            continue

    # finalize last bucket if loop ends mid-bucket
    if bucket_events > 0:
        qualified_buckets += 1
        if qualified_buckets >= max(1, int(SPIKE_REQUIRED)):
            return True

    return False

# ------------------ public API ------------------

async def monitor_spikes_by_mint(mint: str, jup=None, timeout: Optional[float] = None) -> bool:
    """Return True when a qualifying rush is detected for `mint`."""
    _ = jup  # reserved for future USD-aware logic
    _ = timeout
    try:
        async with _open_ws() as ws:
            if SPIKE_USE_BUCKETS:
                return await _detect_by_buckets(ws, mint)
            else:
                return await _detect_by_pops(ws, mint)
    except Exception as e:
        print("spike monitor error:", e)
        return False

async def wait_for_next_pop_or_bucket(mint: str, jup=None, timeout_ms: int = 6000) -> bool:
    """
    Used for re-entry gating: wait for *one* qualifying pop/bucket, or time out.
    """
    _ = jup
    deadline = time.monotonic() + max(0.1, timeout_ms / 1000.0)
    try:
        async with _open_ws() as ws:
            await ws.send(_ws_subscribe_body(mint))
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    j = json.loads(raw)
                    logs = j.get("params", {}).get("result", {}).get("value", {}).get("logs", []) or []
                    if logs:
                        # one event is enough to say "fresh flow"
                        return True
                except Exception:
                    continue
    except Exception as e:
        print("wait_for_next_pop error:", e)
        return False
    return False
