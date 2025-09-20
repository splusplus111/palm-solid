# main.py
import asyncio
from pool_watcher import raydium_new_pool_stream
from sniper import coordinator  # classic mode coordinator
from ata_janitor import janitor_loop
from wallet import Wallet
from config import (
    WATCH_PROGRAM_IDS, SOLANA_RPC_URL, SOLANA_WS_URL,
    BUY_USD, ENTRY_MAX_AGE_SECONDS, EXIT_AFTER_SECONDS,
    SELL_RETRY_INTERVAL_SECS, SELL_RETRY_MAX_TRIES, MAX_BUYS_PER_SEC,
    SLIPPAGE_BPS, MIN_LIQUIDITY_USD, SELL_PERCENT, LOG_LEVEL,
    STAIRS_MODE, STAIRS_MAX_CONCURRENT
)

# Try to import the S1 runner and print any traceback if it fails
try:
    from s1_stairs import run_stairs_for_mint  # S1 stairs entrypoint
except Exception as e:
    import traceback
    print("❌ Import error loading s1_stairs.run_stairs_for_mint:", e)
    traceback.print_exc()
    run_stairs_for_mint = None


async def _stairs_dispatcher(candidates_q: asyncio.Queue):
    """Consumes new mints and runs at most STAIRS_MAX_CONCURRENT stairs tasks."""
    if run_stairs_for_mint is None:
        raise RuntimeError("STAIRS_MODE is enabled but s1_stairs.run_stairs_for_mint is missing.")

    sem = asyncio.Semaphore(max(1, int(STAIRS_MAX_CONCURRENT)))
    active = set()  # track currently-traded mints to avoid duplicates

    async def _runner(mint: str):
        try:
            await run_stairs_for_mint(mint)
        finally:
            active.discard(mint)
            sem.release()

    while True:
        cand = await candidates_q.get()
        mint = cand.get("mint") if isinstance(cand, dict) else cand
        if not mint or mint in active:
            continue
        await sem.acquire()
        active.add(mint)
        asyncio.create_task(_runner(mint))


async def main():
    candidates_q = asyncio.Queue()

    print("=== Membot Startup ===")
    print(f"MODE: {'STAIRS (S1)' if STAIRS_MODE else 'CLASSIC'}")
    print(f"RPC URL: {SOLANA_RPC_URL}")
    print(f"WS URL:  {SOLANA_WS_URL}")
    print("Watching program IDs:")
    for pid in WATCH_PROGRAM_IDS:
        print(f"  - {pid}")

    print("\n# === Trading Parameters ===")
    print(f"BUY_USD={BUY_USD}")
    print(f"ENTRY_MAX_AGE_SECONDS={ENTRY_MAX_AGE_SECONDS}")
    print(f"EXIT_AFTER_SECONDS={EXIT_AFTER_SECONDS}")
    print(f"SELL_PERCENT={SELL_PERCENT}")
    print(f"SELL_RETRY_INTERVAL_SECS={SELL_RETRY_INTERVAL_SECS}")
    print(f"SELL_RETRY_MAX_TRIES={SELL_RETRY_MAX_TRIES}")
    print(f"MAX_BUYS_PER_SEC={MAX_BUYS_PER_SEC}")

    print("\n# === Routing / Slippage ===")
    print(f"SLIPPAGE_BPS={SLIPPAGE_BPS}")

    print("\n# === Heuristics / Filters ===")
    print(f"MIN_LIQUIDITY_USD={MIN_LIQUIDITY_USD}")

    print("\n# === Logging ===")
    print(f"LOG_LEVEL={LOG_LEVEL}")
    if STAIRS_MODE:
        print(f"\n# === Stairs Concurrency ===\nSTAIRS_MAX_CONCURRENT={STAIRS_MAX_CONCURRENT}")
    print("=====================================\n")

    # If S1 is enabled but import failed, stop here with a readable message.
    if STAIRS_MODE and run_stairs_for_mint is None:
        print("⚠️ STAIRS_MODE is true but s1_stairs failed to import (see traceback above). Fix that first.")
        return

    janitor_wallet = Wallet()
    tasks = [
        raydium_new_pool_stream(candidates_q),
        janitor_loop(janitor_wallet),
    ]
    if STAIRS_MODE:
        tasks.append(_stairs_dispatcher(candidates_q))
    else:
        tasks.append(coordinator(candidates_q))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("❌ Stopped by user.")
