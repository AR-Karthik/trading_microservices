"""
Cloud Run Dashboard API — Project K.A.R.T.H.I.K. V8.1 (Unified Hybrid)
========================================================================
Smart Proxy Architecture:
  1. Caches VM IP from Firestore (60s TTL) — D-02
  2. Tries VM direct-read for live data (2s timeout) — D-04 heartbeat age check
  3. Falls back to GCS Parquet + Firestore when VM is offline — D-06
  4. Firestore field names standardised to match cloud_publisher writes — D-13
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import os, json, time, httpx, asyncio

app = FastAPI(title="🦸 PROJECT K.A.R.T.H.I.K. — Pro Cloud Dashboard")

# ─── Auth Middleware ─────────────────────────────────────────────────────────
def get_dashboard_access_key():
    env_key = os.getenv("DASHBOARD_ACCESS_KEY")
    if env_key and env_key != "K_A_R_T_H_I_K_2026_PRO":
        return env_key
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
        name = f"projects/{project_id}/secrets/DASHBOARD_ACCESS_KEY/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"Secret Manager fetch failed: {e}. Using default.")
        return "K_A_R_T_H_I_K_2026_PRO"

ACCESS_KEY = get_dashboard_access_key()

@app.middleware("http")
async def check_access_key(request: Request, call_next):
    if request.url.path in ["/health"] or request.url.path.startswith("/assets/"):
        return await call_next(request)
    requested_key = request.query_params.get("key") or request.headers.get("X-Access-Key")
    session_cookie = request.cookies.get("dashboard_session")
    is_authenticated = (requested_key == ACCESS_KEY) or (session_cookie == ACCESS_KEY)
    if not is_authenticated:
        if request.url.path == "/":
            return JSONResponse(status_code=401,
                content={"detail": "Unauthorized. Please provide ?key=YOUR_SECRET_KEY in the URL."})
        return JSONResponse(status_code=401, content={"detail": "Invalid Access Key"})
    response = await call_next(request)
    if requested_key == ACCESS_KEY:
        response.set_cookie(key="dashboard_session", value=ACCESS_KEY,
            httponly=True, samesite="lax", max_age=86400)
    return response

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"])

# ─── Cloud Clients ────────────────────────────────────────────────────────────
GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
GCS_BUCKET  = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")

_firestore_client = None
_bq_client = None

def get_firestore():
    global _firestore_client
    if _firestore_client is None:
        from google.cloud import firestore
        _firestore_client = firestore.Client(project=GCP_PROJECT)
    return _firestore_client

def get_bigquery():
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery
        _bq_client = bigquery.Client(project=GCP_PROJECT)
    return _bq_client

# ─── VM IP Cache (D-02) ──────────────────────────────────────────────────────
_vm_ip_cache: dict = {"ip": None, "expires_at": 0.0}

async def get_vm_ip() -> Optional[str]:
    """Cache Firestore VM-IP lookup with 60-second TTL."""
    now = time.monotonic()
    if _vm_ip_cache["ip"] and now < _vm_ip_cache["expires_at"]:
        return _vm_ip_cache["ip"]
    try:
        db = get_firestore()
        doc = db.collection("system").document("metadata").get()
        if doc.exists:
            data = doc.to_dict()
            # D-04: Only treat VM as ONLINE if heartbeat is within 90 seconds
            last_hb_str = data.get("last_heartbeat", "")
            vm_online = False
            if last_hb_str:
                try:
                    last_hb = datetime.fromisoformat(last_hb_str.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - last_hb).total_seconds()
                    vm_online = age < 90
                except Exception:
                    vm_online = data.get("status") == "ONLINE"
            else:
                vm_online = data.get("status") == "ONLINE"

            ip = data.get("vm_public_ip") if vm_online else None
            _vm_ip_cache["ip"] = ip
            _vm_ip_cache["expires_at"] = now + 60.0
            return ip
    except Exception as e:
        print(f"VM IP Firestore lookup failed: {e}")
    return None

async def smart_proxy(path: str, method: str = "GET", body: dict = None, params: dict = None):
    """Route to VM if ONLINE (2s timeout), else return None."""
    vm_ip = await get_vm_ip()
    if not vm_ip:
        return None
    url = f"http://{vm_ip}:8000/{path}"
    try:
        async with httpx.AsyncClient() as client:
            if method == "GET":
                resp = await client.get(url, params=params, timeout=2.0)
            elif method == "POST":
                resp = await client.post(url, json=body, params=params, timeout=3.0)
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"Proxy failed to {url}: {e}")
        # Reset cache on connection failure so next call re-checks
        _vm_ip_cache["ip"] = None
    return None

# ─── GCS Parquet Fallback helpers (D-06) ─────────────────────────────────────
def _gcs_read_parquet(blob_prefix: str, columns: list = None):
    """Read GCS parquet files via gcsfs + pyarrow. Returns pandas DataFrame."""
    try:
        import gcsfs, pyarrow.parquet as pq
        fs = gcsfs.GCSFileSystem(project=GCP_PROJECT)
        path = f"{GCS_BUCKET}/{blob_prefix}"
        files = fs.glob(f"{path}/*.parquet")
        if not files:
            return None
        import pandas as pd
        dfs = [pq.read_table(f"gs://{f}", filesystem=fs, columns=columns).to_pandas() for f in files[:5]]
        return pd.concat(dfs, ignore_index=True) if dfs else None
    except Exception as e:
        print(f"GCS Parquet read failed for {blob_prefix}: {e}")
        return None

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "source": "cloud_run_proxy"}

@app.get("/state")
async def get_state():
    # 1. Try VM Direct
    vm_data = await smart_proxy("state")
    if vm_data:
        vm_data["source"] = "VM_DIRECT"
        vm_data["is_vm_running"] = True
        return vm_data

    # 2. Fallback to Firestore — D-13: field names matched to cloud_publisher.py writes
    try:
        db = get_firestore()
        meta = db.collection("system").document("metadata").get()
        cfg  = db.collection("system").document("config").get()
        m = meta.to_dict() if meta.exists else {}
        c = cfg.to_dict()  if cfg.exists  else {}

        # D-04: Real heartbeat age check
        last_hb_str = m.get("last_heartbeat", "")
        vm_running = False
        if last_hb_str:
            try:
                last_hb = datetime.fromisoformat(last_hb_str.replace("Z", "+00:00"))
                vm_running = (datetime.now(timezone.utc) - last_hb).total_seconds() < 90
            except Exception:
                vm_running = m.get("status") == "ONLINE"

        return {
            # D-13: cloud_publisher writes live_alpha + live_regime into metadata
            "alpha_score":            float(m.get("live_alpha", 0.0)),
            "hmm_regime":             m.get("live_regime", "UNKNOWN"),
            "is_vm_running":          vm_running,
            "last_heartbeat":         last_hb_str,
            "source":                 "FIRESTORE_SYNC",
            # D-13: capital limits are written under paper_capital_limit / live_capital_limit
            "available_margin_paper": float(c.get("paper_capital_limit", 0.0)),
            "available_margin_live":  float(c.get("live_capital_limit",  0.0)),
            "paper_capital_limit":    float(c.get("paper_capital_limit", 0.0)),
            "live_capital_limit":     float(c.get("live_capital_limit",  0.0)),
            "power_five":             {},
            "exit_path_70_30":        {"tp1": 0, "tp2": 0, "progress": 0},
            "signals":                {},
            "gex_sign":               "UNKNOWN",
            "system_halted":          c.get("SYSTEM_HALTED", False),
            "macro_lockdown":         False,
            "index_states":           {},
        }
    except Exception as e:
        return {"source": "OFFLINE", "is_vm_running": False, "alpha_score": 0.0,
                "hmm_regime": "UNKNOWN", "error": str(e)}

@app.get("/portfolio")
async def get_portfolio(mode: str = "Paper"):
    vm_data = await smart_proxy("portfolio", params={"mode": mode})
    if vm_data is not None:
        return vm_data

    # D-06: GCS Parquet fallback
    df = _gcs_read_parquet("trade_history", columns=["symbol", "strategy_id", "quantity",
                                                       "avg_price", "realized_pnl", "execution_type"])
    if df is not None:
        filtered = df[df["execution_type"] == mode] if "execution_type" in df.columns else df
        return filtered.head(50).to_dict("records")
    return []

@app.post("/regime/config")
async def update_regime_config(config: dict):
    vm_data = await smart_proxy("regime/config", method="POST", body=config)
    if vm_data:
        return vm_data
    # Queue in Firestore
    db = get_firestore()
    db.collection("commands").document("latest").set({
        "HOT_SWAP_REGIME": config,
        "timestamp": datetime.utcnow().isoformat(),
        "status": "QUEUED"
    }, merge=True)
    return {"status": "Command QUEUED in Firestore (VM Offline)"}

@app.post("/panic")
async def panic(mode: str = "Paper"):
    vm_data = await smart_proxy("panic", method="POST", params={"mode": mode})
    if vm_data:
        return vm_data
    db = get_firestore()
    db.collection("commands").document("latest").set({
        "PANIC_BUTTON": True, "mode": mode,
        "timestamp": datetime.utcnow().isoformat()
    }, merge=True)
    return {"status": "Panic signal QUEUED in Firestore"}

@app.post("/system/halt")
async def system_halt():
    vm_data = await smart_proxy("system/halt", method="POST")
    if vm_data:
        return vm_data
    db = get_firestore()
    db.collection("commands").document("latest").set({
        "SYSTEM_HALT": True, "timestamp": datetime.utcnow().isoformat()
    }, merge=True)
    return {"status": "Halt QUEUED in Firestore"}

@app.get("/regime/simulation")
async def get_regime_sim():
    vm_data = await smart_proxy("regime/simulation")
    return vm_data if vm_data else {"engine_ranking": [], "equity_growth": []}

@app.get("/analytics/summary")
async def get_analytics_summary(mode: str = "Paper"):
    vm_data = await smart_proxy("analytics/summary", params={"mode": mode})
    return vm_data if vm_data else {"sharpe": 0, "max_drawdown": 0, "profit_factor": 0, "win_rate": 0, "source": "OFFLINE"}

@app.get("/analytics/regime")
async def get_analytics_regime(mode: str = "Paper"):
    vm_data = await smart_proxy("analytics/regime", params={"mode": mode})
    return vm_data if vm_data else []

@app.get("/portfolio/delta")
async def get_portfolio_delta():
    vm_data = await smart_proxy("portfolio/delta")
    return vm_data if vm_data else {"NIFTY50": 0, "BANKNIFTY": 0, "threshold": 0.15}

@app.get("/models/evolution")
async def get_evolution():
    vm_data = await smart_proxy("models/evolution")
    return vm_data if vm_data else {"leaderboard": [], "transition_matrix": [], "data_mix": {"seed": 50, "live": 50}}

@app.get("/health/telemetry")
async def get_telemetry():
    vm_data = await smart_proxy("health/telemetry")
    if vm_data:
        return vm_data
    return {"nse_latency_ms": 0, "bse_latency_ms": 0, "cpu_cores": [0, 0, 0],
            "slippage_leakage_inr": 0, "source": "OFFLINE"}

@app.get("/attribution/strategy")
async def get_attribution():
    vm_data = await smart_proxy("attribution/strategy")
    if vm_data:
        return vm_data
    return {"pnl_stack": [], "efficiency": [], "source": "OFFLINE"}

@app.get("/barriers/attribution")
async def get_barriers():
    vm_data = await smart_proxy("barriers/attribution")
    return vm_data if vm_data else {"summary": {"UPPER":0,"LOWER":0,"VERTICAL":0,"PANIC":0}, "feed": [], "total": 0}

@app.get("/sizing/inspector")
async def get_sizing():
    vm_data = await smart_proxy("sizing/inspector")
    return vm_data if vm_data else {"assets": [], "current_atr": 20, "unit_size_base": 125}

@app.get("/analytics")
async def get_analytics(mode: str = "Paper"):
    # Try VM first
    vm_data = await smart_proxy("analytics", params={"mode": mode})
    if vm_data:
        return vm_data

    # D-06: GCS Parquet fallback for after-hours analytics
    df = _gcs_read_parquet("trade_history",
        columns=["time", "action", "price", "quantity", "fees", "execution_type"])
    if df is not None and not df.empty:
        import pandas as pd
        df = df[df["execution_type"] == mode] if "execution_type" in df.columns else df
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time")
        # Compute cumulative P&L per minute
        df["pnl"] = df.apply(lambda r: (r["price"]*r["quantity"] - r.get("fees",0))
                             if r["action"] == "SELL" else -(r["price"]*r["quantity"] + r.get("fees",0)), axis=1)
        df["cum_pnl"] = df["pnl"].cumsum()
        curve = [{"time": str(row["time"]), "equity": round(row["cum_pnl"], 2)}
                 for _, row in df.iterrows()]
        return {"equity_curve": curve, "total_pnl": round(df["cum_pnl"].iloc[-1], 2) if len(df) > 0 else 0,
                "trade_count": len(df), "source": "GCS_PARQUET"}
    return {"equity_curve": [], "total_pnl": 0.0, "trade_count": 0, "source": "OFFLINE"}

# ─── Static Files & SPA Support ───────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

assets_dir = os.path.join(STATIC_DIR, "assets")
if os.path.isdir(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

@app.exception_handler(404)
async def custom_404_handler(request: Request, __):
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path) and not request.url.path.startswith("/api/"):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
