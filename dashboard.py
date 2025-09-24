from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from analytics import analytics
import os
import tempfile

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/status")
def status():
    return {"status": "running"}

@app.get("/analytics")
def get_analytics():
    return analytics.summary()

@app.get("/config")
def get_config():
    return {
        "hold_time": float(os.getenv("SCALP_HOLD_SEC", "3.0")),
        "slippage_buy": int(os.getenv("SLIPPAGE_BPS_BUY", "9000")),
        "slippage_sell": int(os.getenv("SLIPPAGE_BPS_SELL", "800")),
        "stop_loss": float(os.getenv("MCAP_STOP_LOSS", "0.05")),
    }

@app.post("/config")
async def update_config(request: Request):
    data = await request.json()
    for k, v in data.items():
        os.environ[k] = str(v)
    return {"updated": data}

@app.post("/config")
async def update_config_form(
    SCALP_HOLD_SEC: str = Form(...),
    SLIPPAGE_BPS_BUY: str = Form(...),
    SLIPPAGE_BPS_SELL: str = Form(...),
    MCAP_STOP_LOSS: str = Form(...)
):
    os.environ["SCALP_HOLD_SEC"] = SCALP_HOLD_SEC
    os.environ["SLIPPAGE_BPS_BUY"] = SLIPPAGE_BPS_BUY
    os.environ["SLIPPAGE_BPS_SELL"] = SLIPPAGE_BPS_SELL
    os.environ["MCAP_STOP_LOSS"] = MCAP_STOP_LOSS
    return {"updated": {
        "SCALP_HOLD_SEC": SCALP_HOLD_SEC,
        "SLIPPAGE_BPS_BUY": SLIPPAGE_BPS_BUY,
        "SLIPPAGE_BPS_SELL": SLIPPAGE_BPS_SELL,
        "MCAP_STOP_LOSS": MCAP_STOP_LOSS
    }}

@app.get("/api/missed")
def api_missed():
    return JSONResponse(content=analytics.get_missed())

@app.get("/api/logs")
def api_logs():
    return JSONResponse(content=analytics.get_logs())

@app.get("/api/metrics")
def api_metrics():
    return JSONResponse(content=analytics.get_metrics())

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    summary = analytics.summary()
    config = {
        "hold_time": float(os.getenv("SCALP_HOLD_SEC", "3.0")),
        "slippage_buy": int(os.getenv("SLIPPAGE_BPS_BUY", "9000")),
        "slippage_sell": int(os.getenv("SLIPPAGE_BPS_SELL", "800")),
        "stop_loss": float(os.getenv("MCAP_STOP_LOSS", "0.05")),
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, "summary": summary, "config": config})

@app.get("/download-log")
def download_log():
    # Write the in-memory logs to a temp file and serve it
    logs = analytics.analytics.get_logs()
    with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as f:
        f.write('\n'.join(logs))
        temp_path = f.name
    return FileResponse(temp_path, filename="bot-log.txt", media_type="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
