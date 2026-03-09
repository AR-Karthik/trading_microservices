"""
Cloud Run Dashboard API — Project K.A.R.T.H.I.K.
==================================================
This is the Cloud Run version of the dashboard API.
Instead of reading from Redis/TimescaleDB (which live on the VM),
it reads from Firestore (live state) and GCS (historical analytics).

The Panic Button writes to Firestore, which the VM's CloudPublisher watches.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import os
import json

app = FastAPI(title="🦸‍♂️ PROJECT K.A.R.T.H.I.K. — Cloud Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Firestore Client ────────────────────────────────────────────────────────
GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
GCS_BUCKET = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")

_firestore_client = None
_gcs_client = None


def get_firestore():
    global _firestore_client
    if _firestore_client is None:
        from google.cloud import firestore
        _firestore_client = firestore.Client(project=GCP_PROJECT)
    return _firestore_client


def get_gcs():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage
        _gcs_client = storage.Client(project=GCP_PROJECT)
    return _gcs_client


# ─── Models ──────────────────────────────────────────────────────────────────
class SystemState(BaseModel):
    alpha_score: float = 0.0
    hmm_regime: str = "UNKNOWN"
    gex_sign: str = "UNKNOWN"
    system_halted: bool = False
    macro_lockdown: bool = False
    available_margin_paper: float = 0.0
    available_margin_live: float = 0.0
    signals: dict = {}
    is_vm_running: bool = False
    last_heartbeat: str = ""


class StrategyStatus(BaseModel):
    name: str
    status: str
    parameters: dict


class Position(BaseModel):
    symbol: str
    strategy_id: str
    quantity: int
    avg_price: float
    realized_pnl: float
    unrealized_pnl: float


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "source": "cloud_run"}


@app.get("/state", response_model=SystemState)
def get_state():
    """Read live trading state from Firestore (published by VM's CloudPublisher)."""
    try:
        db = get_firestore()
        doc = db.collection("trading_state").document("live").get()

        if not doc.exists:
            return SystemState()

        data = doc.to_dict()
        return SystemState(
            alpha_score=data.get("composite_alpha", 0.0),
            hmm_regime=data.get("regime", "UNKNOWN"),
            gex_sign=data.get("gex_sign", "UNKNOWN"),
            system_halted=data.get("stop_day_loss_breached_paper", False),
            macro_lockdown=data.get("macro_lockdown", False),
            available_margin_paper=data.get("capital_limit", 0.0) - data.get("margin_utilized", 0.0),
            available_margin_live=0.0,
            signals={},
            is_vm_running=data.get("is_vm_running", False),
            last_heartbeat=data.get("last_heartbeat", ""),
        )
    except Exception as e:
        print(f"Firestore read error: {e}")
        return SystemState()


@app.get("/strategies", response_model=List[StrategyStatus])
def get_strategies():
    """Derive strategy status from Firestore state."""
    try:
        db = get_firestore()
        doc = db.collection("trading_state").document("live").get()
        data = doc.to_dict() if doc.exists else {}

        regime = data.get("regime", "RANGING")
        targets = ["GAMMA", "REVERSION", "VWAP", "OI_PULSE", "LEAD_LAG"]
        results = []

        for t in targets:
            status = "PAUSED"
            if t == "LEAD_LAG":
                status = "ACTIVE"
            if t == "GAMMA" and regime == "TRENDING":
                status = "ACTIVE"
            if t == "REVERSION" and regime == "RANGING":
                status = "ACTIVE"

            results.append(StrategyStatus(
                name=t,
                status=status,
                parameters={"regime": regime}
            ))
        return results
    except Exception as e:
        print(f"Strategy read error: {e}")
        return []


@app.get("/analytics")
def get_analytics(mode: str = "Paper"):
    """
    Read historical analytics from GCS parquet files.
    Falls back to Firestore daily P&L if no parquet data exists.
    """
    try:
        db = get_firestore()
        doc = db.collection("trading_state").document("live").get()
        data = doc.to_dict() if doc.exists else {}

        daily_pnl = data.get("daily_pnl_paper", 0.0) if mode == "Paper" else data.get("daily_pnl_live", 0.0)

        return {
            "equity_curve": [],
            "total_pnl": daily_pnl,
            "trade_count": 0,
            "source": "firestore_live"
        }
    except Exception as e:
        return {"equity_curve": [], "total_pnl": 0.0, "error": str(e)}


@app.get("/portfolio", response_model=List[Position])
def get_portfolio(mode: str = "Paper"):
    """
    Read active positions from Firestore.
    The VM's CloudPublisher pushes position snapshots to Firestore.
    """
    try:
        db = get_firestore()
        doc = db.collection("trading_state").document("positions").get()

        if not doc.exists:
            return []

        data = doc.to_dict()
        positions_raw = data.get("active", [])

        return [Position(
            symbol=p.get("symbol", ""),
            strategy_id=p.get("strategy_id", ""),
            quantity=p.get("quantity", 0),
            avg_price=p.get("avg_price", 0.0),
            realized_pnl=p.get("realized_pnl", 0.0),
            unrealized_pnl=p.get("unrealized_pnl", 0.0),
        ) for p in positions_raw]

    except Exception as e:
        print(f"Portfolio read error: {e}")
        return []


@app.post("/panic")
def panic(mode: str = "Paper"):
    """
    Write PANIC_BUTTON command to Firestore.
    The VM's CloudPublisher watches this and triggers SQUARE_OFF_ALL.
    """
    try:
        db = get_firestore()
        doc_ref = db.collection("trading_commands").document("active")
        doc_ref.set({
            "PANIC_BUTTON": True,
            "source": "cloud_dashboard",
            "mode": mode,
            "timestamp": datetime.utcnow().isoformat(),
        }, merge=True)
        return {"status": "Panic signal sent via Firestore"}
    except Exception as e:
        return {"status": f"Failed: {e}"}


@app.post("/pause")
def pause_trading():
    """Write PAUSE_TRADING command to Firestore."""
    try:
        db = get_firestore()
        db.collection("trading_commands").document("active").set({
            "PAUSE_TRADING": True,
            "timestamp": datetime.utcnow().isoformat(),
        }, merge=True)
        return {"status": "Pause command sent"}
    except Exception as e:
        return {"status": f"Failed: {e}"}


@app.post("/resume")
def resume_trading():
    """Write RESUME_TRADING command to Firestore."""
    try:
        db = get_firestore()
        db.collection("trading_commands").document("active").set({
            "RESUME_TRADING": True,
            "timestamp": datetime.utcnow().isoformat(),
        }, merge=True)
        return {"status": "Resume command sent"}
    except Exception as e:
        return {"status": f"Failed: {e}"}


# ─── Serve Frontend ──────────────────────────────────────────────────────────
# Mount static files (frontend assets)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")


@app.get("/")
def serve_frontend():
    """Serve the React SPA."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"error": "Frontend not found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
