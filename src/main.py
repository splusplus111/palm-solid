import asyncio, time
from .pool_watcher import raydium_new_pool_stream
from .sniper import coordinator

async def main():
    q = asyncio.Queue()
    asyncio.create_task(raydium_new_pool_stream(q))
    await coordinator(q)

if __name__ == "__main__":
    asyncio.run(main())
