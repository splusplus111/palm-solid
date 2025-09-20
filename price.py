import aiohttp

LAMPORTS_PER_SOL = 1_000_000_000
USDC_DECIMALS = 6

async def get_sol_usd_price() -> float:
    # Ask for a quote: 1 SOL → USDC
    url = (
        "https://quote-api.jup.ag/v6/quote"
        "?inputMint=So11111111111111111111111111111111111111112"
        "&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        f"&amount={LAMPORTS_PER_SOL}"   # 1 SOL
        "&slippageBps=50"
    )

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
        async with s.get(url) as r:
            if r.status != 200:
                return 150.0  # fallback
            j = await r.json()

            # v6 gives outAmount (how many USDC you get for input amount)
            out_amount = int(j["outAmount"]) / (10 ** USDC_DECIMALS)
            return out_amount  # price of 1 SOL in USDC

# Debug: run directly
if __name__ == "__main__":
    import asyncio
    async def main():
        price = await get_sol_usd_price()
        print(f"1 SOL = {price:.2f} USDC")
    asyncio.run(main())
