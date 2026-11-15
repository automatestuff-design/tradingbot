"""FastAPI app: runs the engine in the background and serves the dashboard + JSON API."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .backtest import DATA_DIR, FX_FILE, available_csvs, run_backtest
from .brokers import AlpacaBroker, SimBroker
from .config import Connection, Settings
from .engine import Engine
from .strategies import STRATEGIES
from .telegram_bot import TelegramBot, notify_startup_problem

conn = Connection()
conn.validate()
settings = Settings()


def make_broker():
    if conn.mode == "sim":
        return SimBroker()
    if conn.broker == "alpaca":
        return AlpacaBroker(conn.alpaca_key, conn.alpaca_secret, live=(conn.mode == "live"), feed=conn.data_feed)
    from .ibkr import IBKRBroker
    try:
        return IBKRBroker(conn.ib_host, conn.ib_port, conn.ib_client_id, conn.mode, conn.ib_account)
    except ConnectionError as e:
        notify_startup_problem(str(e))
        raise SystemExit(f"\n{e}\n") from None


broker = make_broker()
engine = Engine(settings, broker)
STATIC = Path(__file__).resolve().parent / "static"


phone = TelegramBot.from_env(engine)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(engine.run_forever())]
    if phone:
        tasks.append(asyncio.create_task(phone.run()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Trading Bot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state(symbol: str | None = None):
    return engine.snapshot(symbol)


@app.post("/api/start")
def start():
    try:
        engine.start()
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/stop")
def stop():
    engine.stop()
    return {"ok": True}


@app.post("/api/flatten")
async def flatten():
    engine.running = False
    await engine.flatten()
    engine.log("Stopped and flattened all positions", "warn")
    return {"ok": True}


@app.post("/api/clear-halt")
def clear_halt():
    engine.reset_halt()
    return {"ok": True}


@app.post("/api/approvals/{rid}/approve")
async def approve(rid: str):
    try:
        r = await engine.approve(rid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "symbol": r["symbol"], "side": r["side"]}


@app.post("/api/approvals/{rid}/reject")
def reject(rid: str):
    try:
        engine.reject(rid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/backtest")
def backtest_page():
    return FileResponse(STATIC / "backtest.html")


JOBS = {}  # single-user app: keep only the latest few backtests in memory


@app.get("/api/backtest/meta")
def backtest_meta():
    return {"csv_symbols": available_csvs(), "data_dir": str(DATA_DIR), "settings": settings.to_dict(),
            "fx_history": (DATA_DIR / f"{FX_FILE}.csv").exists()}


@app.post("/api/backtest")
async def start_backtest(params: dict):
    job_id = uuid.uuid4().hex[:8]
    job = JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0.0, "result": None, "error": None, "cancel": False}
    for old in list(JOBS)[:-5]:
        JOBS.pop(old, None)

    def work():
        try:
            job["result"] = run_backtest(params, progress=lambda p: job.update(progress=p),
                                         cancelled=lambda: job["cancel"])
            job["status"] = "done"
        except Exception as e:
            job["status"], job["error"] = "error", str(e)

    asyncio.get_running_loop().run_in_executor(None, work)
    return {"id": job_id}


@app.get("/api/backtest/{job_id}")
def backtest_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown backtest")
    return {k: v for k, v in job.items() if k != "cancel"}


@app.post("/api/backtest/{job_id}/cancel")
def backtest_cancel(job_id: str):
    if job_id in JOBS:
        JOBS[job_id]["cancel"] = True
    return {"ok": True}


@app.post("/api/settings")
def update_settings(data: dict):
    if "strategy" in data and data["strategy"] not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy {data['strategy']}")
    try:
        settings.update(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    engine.log("Settings updated")
    return settings.to_dict()
