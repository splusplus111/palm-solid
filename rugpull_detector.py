import aiohttp
import os

async def check_rug_pull(mint: str) -> bool:
    """
    Basic rug-pull detection:
    - Check if mint authority is set to None (renounced)
    - Check for recent liquidity removal events (placeholder)
    Returns True if suspicious, False otherwise.
    """
    rpc_url = os.getenv("SOLANA_RPC_URL")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(rpc_url, json=payload) as r:
            data = await r.json()
    info = (((data.get("result") or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info", {})
    mint_authority = info.get("mintAuthority")
    if mint_authority is None:
        return True  # suspicious: mint authority renounced
    # TODO: Add liquidity removal checks
    return False
