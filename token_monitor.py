import asyncio
import websockets
import json
from config import SOLANA_WS_URL, PUMPFUN_PROGRAM_ID
from sniper import instant_buy

async def monitor_pumpfun_tokens():
    async with websockets.connect(SOLANA_WS_URL) as ws:
        # Subscribe to logs for Pump.fun program
        sub_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_PROGRAM_ID]},
                {"commitment": "finalized"}
            ]
        }
        await ws.send(json.dumps(sub_msg))
        print("Subscribed to Pump.fun logs...")
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            # Filter for token creation events
            if "result" in data and "value" in data["result"]:
                logs = data["result"]["value"].get("logs", [])
                if any(m in " ".join(logs) for m in ["Initialize", "Create", "Deploy"]):
                    print("New token detected!", logs)
                    # Call instant buy logic (implement in sniper.py)
                    await instant_buy(data)

if __name__ == "__main__":
    asyncio.run(monitor_pumpfun_tokens())
