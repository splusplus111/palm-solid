import os, aiohttp, asyncio, random, time
from typing import Any, Dict, Optional
from config import SLIPPAGE_BPS, JUP_MAX_RPS, JUP_MAX_BURST, JUP_MAX_RETRIES, JUP_BACKOFF_BASE_MS

JUP_BASE = "https://quote-api.jup.ag/v6"

# -------- process-wide token bucket (quotes + swaps) --------
class _TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = max(0.1, float(rate_per_sec))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, amount: float = 1.0):
        amount = max(0.1, float(amount))
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
            # sleep to allow refill
            await asyncio.sleep(max(0.01, amount / self.rate))

# one global limiter shared by all Jupiter instances
_JUP_LIMITER = _TokenBucket(JUP_MAX_RPS, JUP_MAX_BURST)

# -------- Jupiter client with retry/backoff --------
class Jupiter:
    def __init__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        )

    async def close(self):
        if not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def _request(self, method: str, url: str, *, json_body: Optional[dict] = None, params: Optional[dict] = None) -> Dict[str, Any]:
        # throttle globally
        await _JUP_LIMITER.take(1.0)

        attempt = 0
        while True:
            attempt += 1
            try:
                async with self.session.request(method, url, json=json_body, params=params) as r:
                    text = await r.text()
                    if r.status == 429:
                        # respect Retry-After if present; otherwise exponential backoff w/ jitter
                        ra = r.headers.get("Retry-After")
                        if ra:
                            try:
                                delay = float(ra)
                            except:
                                delay = 1.0
                        else:
                            delay = (JUP_BACKOFF_BASE_MS / 1000.0) * (2 ** (attempt - 1))
                            delay += random.uniform(0, 0.2)
                        if attempt <= JUP_MAX_RETRIES:
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(f"jupiter 429 after retries: {text[:200]}")

                    if 500 <= r.status < 600:
                        delay = (JUP_BACKOFF_BASE_MS / 1000.0) * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                        if attempt <= JUP_MAX_RETRIES:
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(f"jupiter {r.status} after retries: {text[:200]}")

                    if r.status != 200:
                        raise RuntimeError(f"jupiter http {r.status}: {text[:200]}")

                    try:
                        return await r.json()
                    except Exception:
                        raise RuntimeError(f"bad json: {text[:200]}")
            except aiohttp.ClientError as e:
                # network hiccup → backoff + retry
                delay = (JUP_BACKOFF_BASE_MS / 1000.0) * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                if attempt <= JUP_MAX_RETRIES:
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"jupiter client error: {e!r}")

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_in_lamports: int,
        *,
        slippage_bps: Optional[int] = None,
    ) -> Dict[str, Any]:
        eff_slip = SLIPPAGE_BPS if slippage_bps is None else int(slippage_bps)
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_in_lamports),
            "slippageBps": str(eff_slip),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
            "restrictIntermediateTokens": "true",
            "swapMode": "ExactIn",
        }
        url = f"{JUP_BASE}/quote"
        return await self._request("GET", url, params=params)

    async def swap_tx(
        self,
        quote_json: Dict[str, Any],
        user_pubkey: str,
        tip_lamports: int,
        *,
        slippage_bps: Optional[int] = None,
    ) -> str:
        eff_slip = SLIPPAGE_BPS if slippage_bps is None else int(slippage_bps)
        use_jito = str(os.getenv("JUP_USE_JITO", "true")).lower() == "true"

        url = f"{JUP_BASE}/swap"
        payload = {
            "userPublicKey": user_pubkey,
            "quoteResponse": quote_json,
            "dynamicSlippage": {"maxBps": eff_slip},
            "asLegacyTransaction": False,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": False,
            "useTokenLedger": False,
            "prioritizationFeeLamports": int(tip_lamports) if tip_lamports else 0,
            "useJito": use_jito,
            "useAtaProgramId": True,
        }
        data = await self._request("POST", url, json_body=payload)
        tx_b64 = data.get("swapTransaction")
        if not tx_b64:
            raise RuntimeError("no swapTransaction returned")
        return tx_b64
