import asyncio
import os
from dotenv import load_dotenv
from src.wallet import Wallet
from src.jupiter import Jupiter

load_dotenv()   # <-- this makes sure .env is read

async def main():
    wallet = Wallet(os.getenv("WALLET_PRIVATE_KEY_JSON"))
    jupiter = Jupiter(os.getenv("SOLANA_RPC_URL"))

    print("🔍 Starting roundtrip test: SOL → USDC → SOL")

    # Step 1: swap SOL → USDC
    sig1 = await jupiter.swap(wallet, "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 5)
    print(f"✅ SOL → USDC tx: {sig1}")

    # Step 2: swap USDC → SOL
    sig2 = await jupiter.swap(wallet, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "So11111111111111111111111111111111111111112", 5)
    print(f"✅ USDC → SOL tx: {sig2}")

if __name__ == "__main__":
    asyncio.run(main())
