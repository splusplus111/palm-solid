import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Membot Service", version="1.0.0")


def _bool_env(name: str) -> bool:
    return bool(os.getenv(name))


@app.get("/", response_class=HTMLResponse)
async def root():
    has_wallet = _bool_env("WALLET_PRIVATE_KEY_JSON")
    rpc = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    ws = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com/")
    html = (
        "<!doctype html>\n"
        "<html><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        "<title>Membot Service</title>"
        "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px}"
        ".card{max-width:760px;margin:auto;padding:16px 20px;border:1px solid #e5e7eb;border-radius:12px}"
        ".ok{color:#16a34a}.warn{color:#ef4444} code{background:#f3f4f6;padding:2px 4px;border-radius:6px}</style>"
        "</head><body>"
        "<div class='card'>"
        "<h1>✅ Service running</h1>"
        "<p>This project is a Python async service (no frontend UI). The server exposes simple status endpoints for preview.</p>"
        f"<ul><li>RPC: <code>{rpc}</code></li><li>WS: <code>{ws}</code></li>"
        f"<li>Wallet configured: "
        f"{'<span class=\"ok\">yes</span>' if has_wallet else '<span class=\"warn\">no</span>'}</li></ul>"
        "<p>Endpoints: <code>/health</code>, <code>/config</code>, <code>/docs</code></p>"
        "</div></body></html>"
    )
    return HTMLResponse(content=html)


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.get("/config")
async def config_state():
    # Do not leak secrets; only report presence
    keys = [
        "WALLET_PRIVATE_KEY_JSON",
        "SOLANA_RPC_URL",
        "SOLANA_WS_URL",
        "WATCH_PROGRAM_IDS",
    ]
    out = {k: ("<set>" if (k == "WALLET_PRIVATE_KEY_JSON" and _bool_env(k)) else os.getenv(k, "")) for k in keys}
    if "WALLET_PRIVATE_KEY_JSON" in out and out["WALLET_PRIVATE_KEY_JSON"] == "":
        out["WALLET_PRIVATE_KEY_JSON"] = "<missing>"
    return JSONResponse(out)
