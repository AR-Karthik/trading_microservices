"""
Cloud Run Dashboard API — Project K.A.R.T.H.I.K. V8.0 (Hybrid)
============================================================
Smart Proxy Architecture:
1. READS from VM (Public IP) when ONLINE.
2. FALLS BACK to BigQuery/GCS/Firestore when OFFLINE.
3. WRITES commands directly to VM when ONLINE.
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import os
import json
import httpx
import asyncio

app = FastAPI(title="🦸‍♂️ PROJECT K.A.R.T.H.I.K. — Pro Cloud Dashboard")

# ─── Auth Middleware ────────────────────────────────────────────────────────
# ─── Auth Middleware ────────────────────────────────────────────────────────
def get_dashboard_access_key():
    """Fetches the access key from Secret Manager (Priority) or Environment."""
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
        print(f"Secret Manager fetch failed: {e}. Falling back to default.")
        return "K_A_R_T_H_I_K_2026_PRO"

ACCESS_KEY = get_dashboard_access_key()

@app.middleware("http")
async def check_access_key(request: Request, call_next):
    # Allow health check and static assets without key for basic functioning
    if request.url.path in ["/health"] or request.url.path.startswith("/assets/"):
        return await call_next(request)
    
    # Authenticate via Query Param, Header, or Session Cookie
    requested_key = request.query_params.get("key") or request.headers.get("X-Access-Key")
    session_cookie = request.cookies.get("dashboard_session")
    
    is_authenticated = (requested_key == ACCESS_KEY) or (session_cookie == ACCESS_KEY)
    
    if not is_authenticated:
        if request.url.path == "/":
             return JSONResponse(
                 status_code=401, 
                 content={"detail": "Unauthorized. Please provide ?key=YOUR_SECRET_KEY in the URL."}
             )
        return JSONResponse(status_code=401, content={"detail": "Invalid Access Key"})
        
    response = await call_next(request)
    
    # If authenticated via fresh key, drop a session cookie for the SPA to use on next calls
    if requested_key == ACCESS_KEY:
        response.set_cookie(
            key="dashboard_session",
            value=ACCESS_KEY,
            httponly=True,
            samesite="lax",
            max_age=86400  # 24 hours
        )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Cloud Clients ──────────────────────────────────────────────────────────
GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
GCS_BUCKET = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")

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

# ─── Hybrid Proxy Core ──────────────────────────────────────────────────────
async def get_vm_ip():
    """Fetch the latest VM Public IP from Firestore metadata."""
    try:
        db = get_firestore()
        doc = db.collection("system").document("metadata").get()
        if doc.exists:
            data = doc.to_dict()
            if data.get("status") == "ONLINE":
                return data.get("vm_public_ip")
    except:
        pass
    return None

async def smart_proxy(path: str, method: str = "GET", body: dict = None, params: dict = None):
    """Attempts to route request to VM, otherwise returns None."""
    vm_ip = await get_vm_ip()
    if not vm_ip:
        return None
    
    url = f"http://{vm_ip}:8000/{path}"
    try:
        async with httpx.AsyncClient() as client:
            if method == "GET":
                resp = await client.get(url, params=params, timeout=2.0)
            elif method == "POST":
                resp = await client.post(url, json=body, timeout=3.0)
            
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"Proxy failed to {url}: {e}")
    return None

# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "source": "cloud_run_proxy"}

@app.get("/state")
async def get_state():
    # 1. Try VM Direct
    vm_data = await smart_proxy("state")
    if vm_data:
        vm_data["source"] = "VM_DIRECT"
        return vm_data
    
    # 2. Fallback to Firestore (Last Sync)
    db = get_firestore()
    doc = db.collection("system").document("config").get() # Using config sync for regime/capital
    meta = db.collection("system").document("metadata").get()
    
    cfg = doc.to_dict() if doc.exists else {}
    m = meta.to_dict() if meta.exists else {}

    return {
        "alpha_score": m.get("live_alpha", 0.0),
        "hmm_regime": m.get("live_regime", "UNKNOWN"),
        "is_vm_running": m.get("status") == "ONLINE",
        "last_heartbeat": m.get("last_heartbeat", ""),
        "source": "FIRESTORE_SYNC",
        "available_margin_paper": cfg.get("paper_capital_limit", 0.0),
        "available_margin_live": cfg.get("live_capital_limit", 0.0)
    }

@app.get("/portfolio")
async def get_portfolio(mode: str = "Paper"):
    vm_data = await smart_proxy("portfolio", params={"mode": mode})
    if vm_data is not None:
        return vm_data
    
    # Fallback: Historical view (Empty if VM is off for now, or use BQ if needed)
    return []

@app.post("/regime/config")
async def update_regime_config(config: dict):
    # Try VM Direct
    vm_data = await smart_proxy("regime/config", method="POST", body=config)
    if vm_data:
        return vm_data
    
    # Fallback: Queue in Firestore for VM to pick up on boot
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
    
    # Fallback: Firestore flag for CloudPublisher watcher
    db = get_firestore()
    db.collection("commands").document("latest").set({
        "PANIC_BUTTON": True,
        "mode": mode,
        "timestamp": datetime.utcnow().isoformat()
    }, merge=True)
    return {"status": "Panic signal QUEUED"}

# ─── Advanced v8.0 Endpoints (Discovery & Proxy) ──────────────────────────

@app.get("/regime/simulation")
async def get_regime_sim():
    vm_data = await smart_proxy("regime/simulation")
    return vm_data if vm_data else {"engine_ranking": [], "equity_growth": []}

@app.get("/models/evolution")
async def get_evolution():
    vm_data = await smart_proxy("models/evolution")
    return vm_data if vm_data else {"leaderboard": [], "transition_matrix": []}

@app.get("/health/telemetry")
async def get_telemetry():
    vm_data = await smart_proxy("health/telemetry")
    return vm_data if vm_data else {"nse_latency_ms": 0, "cpu_cores": [0,0,0]}

@app.get("/attribution/strategy")
async def get_attribution():
    vm_data = await smart_proxy("attribution/strategy")
    if vm_data: return vm_data
    
    # Fallback: Pull from BigQuery (TBD)
    return {"pnl_stack": [], "efficiency": []}

@app.get("/barriers/attribution")
async def get_barriers():
    vm_data = await smart_proxy("barriers/attribution")
    return vm_data if vm_data else {"summary": {}, "feed": []}

@app.get("/sizing/inspector")
async def get_sizing():
    vm_data = await smart_proxy("sizing/inspector")
    return vm_data if vm_data else {"assets": []}

# ─── Analytics (BigQuery) ──────────────────────────────────────────────────
@app.get("/analytics")
def get_analytics(mode: str = "Paper"):
    try:
        bq = get_bigquery()
        dataset = "trading_analytics"
        table = f"{GCP_PROJECT}.{dataset}.trade_history_external"
        
        query = f"""
            SELECT 
                DATE(time) as trade_date,
                SUM(CASE WHEN action = 'SELL' THEN (price * quantity) - fees 
                         ELSE -(price * quantity) - fees END) as daily_pnl,
                COUNT(*) as trades
            FROM `{table}`
            WHERE execution_type = '{mode}'
            GROUP BY trade_date
            ORDER BY trade_date ASC
        """
        query_job = bq.query(query)
        results = query_job.result()
        
        curve = []
        cumulative_pnl = 0.0
        total_trades = 0
        for row in results:
            cumulative_pnl += float(row.daily_pnl)
            total_trades += row.trades
            curve.append({"date": row.trade_date.isoformat(), "pnl": round(cumulative_pnl, 2)})
            
        return {
            "equity_curve": curve,
            "total_pnl": round(cumulative_pnl, 2),
            "trade_count": total_trades,
            "source": "bigquery_analytics"
        }
    except Exception as e:
        return {"error": str(e), "equity_curve": [], "total_pnl": 0.0}

# --- Static Files & SPA Support ---
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Mount /assets specifically
assets_dir = os.path.join(STATIC_DIR, "assets")
if os.path.isdir(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

# Mount root for the rest of the files (index.html, etc)
# NOTE: Mount this LAST to allow API routes priority
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

@app.exception_handler(404)
async def custom_404_handler(request: Request, __):
    """Fallback to index.html for React SPA routing on 404s."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path) and not request.url.path.startswith("/api/"):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
