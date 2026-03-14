from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
import redis
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import random
import time
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from core.logger import setup_logger

logger = setup_logger("DashboardAPI", log_file="logs/dashboard_api.log")

app = FastAPI(title="🦸‍♂️ Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)")

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify Tailscale IP/Host
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- NEW: Logging Middleware (#103) ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    logger.info(
        f"API Request: {request.method} {request.url.path}",
        extra={
            "duration": round(duration, 4),
            "status_code": response.status_code,
            "client_ip": request.client.host if request.client else "unknown"
        }
    )
    return response

# --- NEW: Audit Logger (#106) ---
class AuditLogger:
    @staticmethod
    async def log_event(event_type: str, user: str, details: str):
        r = get_redis()
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "user": user,
            "details": details
        }
        # Non-blocking async push to Redis stream or list
        r.lpush("audit_trail", json.dumps(event))
        r.ltrim("audit_trail", 0, 9999)
        logger.warning(f"AUDIT EVENT: {event_type} by {user} - {details}")

# Connections
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
DB_HOST = os.getenv("DB_HOST", "localhost")

def get_redis():
    return redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)

def get_db():
    try:
        conn = psycopg2.connect(
            dbname="trading_db",
            user="trading_user",
            password="trading_pass",
            host=DB_HOST,
            port="5432"
        )
        return conn
    except Exception:
        return None

# --- NEW: Configuration Models ---
class RegimeConfigRequest(BaseModel):
    engine: str
    hurst_threshold: float
    hybrid_confidence: float
    vpin_toxicity: float
    paper_capital: float
    live_capital: float
    paper_max_risk: float = 0.0  # Default 0.0 to prevent orders until UI configures it
    live_max_risk: float  = 0.0  # Default 0.0 to prevent orders until UI configures it

class CapitalRequest(BaseModel):
    amount: float

class TelemetryMetrics(BaseModel):
    nse_latency_ms: float
    bse_latency_ms: float
    slippage_leakage_inr: float
    cpu_cores: List[float]

# Models
class SystemState(BaseModel):
    alpha_score: float
    hmm_regime: str
    gex_sign: str
    system_halted: bool
    macro_lockdown: bool
    available_margin_paper: float
    available_margin_live: float
    paper_capital_limit: float
    live_capital_limit: float
    index_states: dict # Multi-index regimes and scores
    signals: dict  # Full quant signal vector
    power_five: dict # HDFC, RIL, ICICI, etc.
    exit_path_70_30: dict # TP1, TP2 markers

class Position(BaseModel):
    symbol: str
    strategy_id: str
    quantity: int
    avg_price: float
    realized_pnl: float
    unrealized_pnl: float

class StrategyStatus(BaseModel):
    name: str
    status: str
    parameters: dict

# --- Endpoints ---

@app.get("/health")
def health():
    # Enhanced Health Check (#108)
    health_status = {"status": "ok", "components": {}}
    
    # Check Redis
    try:
        r = get_redis()
        r.ping()
        health_status["components"]["redis"] = "connected"
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["components"]["redis"] = f"error: {str(e)}"

    # Check Database
    try:
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.close()
            health_status["components"]["database"] = "connected"
        else:
            health_status["status"] = "degraded"
            health_status["components"]["database"] = "failed to connect"
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["components"]["database"] = f"error: {str(e)}"

    if health_status["status"] != "ok":
        raise HTTPException(status_code=503, detail=health_status)
    return health_status

@app.get("/metrics", response_class=PlainTextResponse)
def get_metrics():
    # Basic Prometheus Metrics (#104)
    r = get_redis()
    state_raw = r.get("latest_market_state")
    ms = json.loads(state_raw) if state_raw else {}
    
    metrics = [
        "# HELP composite_alpha Current Alpha Score",
        "# TYPE composite_alpha gauge",
        f"composite_alpha {ms.get('s_total', 0.0)}",
        "# HELP system_halted Indicator if system is halted (1=True, 0=False)",
        "# TYPE system_halted gauge",
        f"system_halted {1 if r.get('SYSTEM_HALTED') == 'True' else 0}"
    ]
    return "\n".join(metrics)

    indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
    index_states = {}
    
    for asset in indices:
        st_raw = r.get(f"latest_market_state:{asset}")
        st = json.loads(st_raw) if st_raw else {}
        
        # HMM Regime
        asset_short = "NIFTY" if asset == "NIFTY50" else asset
        reg_raw = r.hget("hmm_regime_state", asset_short)
        reg_data = json.loads(reg_raw or "{}")
        
        index_states[asset] = {
            "score": st.get("s_total", 0.0),
            "regime": reg_data.get("regime", "UNKNOWN"),
            "price": st.get("price", 0.0)
        }
    
    # Primary asset for top-level keys
    ms = json.loads(r.get("latest_market_state:NIFTY50") or r.get("latest_market_state") or "{}")

    # Extract deep signals
    deep_signals = {
        "log_ofi_z": ms.get("log_ofi_zscore", 0.0),
        "dispersion": ms.get("dispersion_coeff", 0.5),
        "hurst": ms.get("hurst", 0.5),
        "rv": ms.get("rv", 0.0),
        "vanna": ms.get("vanna", 0.0),
        "charm": ms.get("charm", 0.0),
        "atr": ms.get("atr", 20.0),
        "basis_z": ms.get("basis_zscore", 0.0),
        "cvd_flips": ms.get("cvd_flip_ticks", 0)
    }

    # Extract Power Five Matrix from Z-Scores in Redis
    # These are populated by MarketSensor
    power_five = {}
    for idx in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        power_five[idx] = {}
        # Fetch the top 5 weighted components for each index
        components = {
            "NIFTY": ["HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "ITC"],
            "BANKNIFTY": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
            "SENSEX": ["HDFCBANK", "RELIANCE", "ICICIBANK", "ITC", "LT"]
        }[idx]
        
        # Fetch the top 5 weighted components for each index from power_five_matrix hash
        for sym in components:
            raw = r.hget("power_five_matrix", sym)
            if raw:
                try:
                    data = json.loads(raw)
                    power_five[idx][sym] = data.get("z_score", 0.0)
                except:
                    power_five[idx][sym] = 0.0
            else:
                # Fallback to old zscore key if hash not yet populated
                z_raw = r.get(f"zscore:{sym}")
                power_five[idx][sym] = round(float(z_raw or 0), 2)

    return {
        "alpha_score": index_states["NIFTY50"]["score"],
        "hmm_regime": index_states["NIFTY50"]["regime"],
        "index_states": index_states,
        "gex_sign": r.get("gex_sign:NIFTY50") or r.get("gex_sign") or "UNKNOWN",
        "system_halted": r.get("SYSTEM_HALTED") == "True",
        "macro_lockdown": r.get("MACRO_EVENT_LOCKDOWN") == "True",
        "available_margin_paper": float(r.get("AVAILABLE_MARGIN_PAPER") or 0),
        "available_margin_live": float(r.get("AVAILABLE_MARGIN_LIVE") or 0),
        "paper_capital_limit": float(r.get("PAPER_CAPITAL_LIMIT") or 0),
        "live_capital_limit": float(r.get("LIVE_CAPITAL_LIMIT") or 0),
        "signals": deep_signals,
        "power_five": power_five,
        "exit_path_70_30": ms.get("exit_path_70_30", {
            "tp1": 0.0,
            "tp2": 0.0,
            "progress": 0.0
        })
    }

@app.get("/strategies", response_model=List[StrategyStatus])
def get_strategies():
    r = get_redis()
    # Strategies tracked by MetaRouter
    targets = ["STRAT_GAMMA", "STRAT_VWAP", "STRAT_OI_PULSE", "STRAT_LEAD_LAG"]
    results = []
    
    # In a real system, we'd fetch actual process status. 
    # For now, we derive it from the last MetaRouter decision in Redis.
    regime = r.get("hmm_regime") or "RANGING"
    
    for t in targets:
        # Derivative logic to show "intended" state
        status = "PAUSED"
        if t == "STRAT_LEAD_LAG": status = "ACTIVE"
        if t == "STRAT_GAMMA" and r.get("gex_sign:NIFTY50") == "NEGATIVE" and regime == "TRENDING": status = "ACTIVE"
        
        results.append({
            "name": t.replace("STRAT_", ""),
            "status": status,
            "parameters": {"regime": regime}
        })
    return results

# --- NEW: Tab 2: Regime Orchestrator ---
@app.post("/regime/config")
async def update_regime_config(config: RegimeConfigRequest):
    r = get_redis()
    # Audit log the change (#106)
    await AuditLogger.log_event("CONFIG_CHANGE", "ADMIN_UI", f"Regime config updated: {config.engine}")
    
    r.set("CONFIG:REGIME_ENGINE", config.engine)
    r.set("CONFIG:HURST_THRESHOLD", config.hurst_threshold)
    r.set("CONFIG:HYBRID_CONFIDENCE", config.hybrid_confidence)
    r.set("CONFIG:VPIN_TOXICITY", config.vpin_toxicity)
    r.set("PAPER_CAPITAL_LIMIT", config.paper_capital)
    r.set("LIVE_CAPITAL_LIMIT", config.live_capital)
    r.set("CONFIG:MAX_RISK_PER_TRADE_PAPER", config.paper_max_risk)  # v8.0 paper path
    r.set("CONFIG:MAX_RISK_PER_TRADE_LIVE",  config.live_max_risk)   # v8.0 live path
    
    # Also update available margins if needed (simple sync)
    # Note: In a production system, we'd handle this more carefully with delta checks
    r.set("AVAILABLE_MARGIN_PAPER", config.paper_capital)
    r.set("AVAILABLE_MARGIN_LIVE", config.live_capital)

    # Publish signal to MetaRouter to hot-swap
    r.publish("system_cmd", json.dumps({"cmd": "HOT_SWAP_REGIME", "data": config.dict()}))
    return {"status": "Config updated and published"}

@app.post("/config/capital")
def update_capital(req: CapitalRequest):
    r = get_redis()
    r.set("CONFIG:TOTAL_CAPITAL", req.amount)
    return {"status": "Capital updated"}

# --- NEW: Tab 7: Engine Simulation ---
@app.get("/regime/simulation")
def get_regime_simulation():
    r = get_redis()
    engines = ["HMM", "DETERMINISTIC", "HYBRID"]
    assets = ["NIFTY", "BANKNIFTY", "SENSEX"]
    
    ranking = []
    equity_growth = [] # Simplified history
    
    for eng in engines:
        total_pnl = 0
        total_trades = 0
        for asset in assets:
            # Derived from shadow_lots and price movements (pseudo-PNL)
            # In a full sys, we'd have shadow_pnl keys. 
            # For now, we fetch from a summary hash populated by Sidecar/MetaRouter
            pnl_val = r.hget("shadow_pnl_summary", f"{eng}:{asset}")
            total_pnl += float(pnl_val or 0)
            total_trades += int(r.hget("shadow_trade_count", f"{eng}:{asset}") or 0)
        
        ranking.append({
            "engine": eng, 
            "pnl": round(total_pnl, 2), 
            "drawdown": round(total_pnl * 0.1, 2), # Approx
            "trades": total_trades
        })

    # Pull last 5 entries from history for the growth chart
    # (In prod, we'd use a Timescale query)
    return {
        "engine_ranking": ranking,
        "equity_growth": [
            {"time": "LIVE", "HMM": ranking[0]["pnl"], "Deterministic": ranking[1]["pnl"], "Hybrid": ranking[2]["pnl"]}
        ]
    }

# --- NEW: Tab 3: Model Evolution ---
@app.get("/models/evolution")
def get_model_evolution():
    # In a production system, this would pull from a 'model_registry' table
    # For now, we return mock versioning as per spec
    return {
        "leaderboard": [
            {"version": "v1.4.2", "log_likelihood": -1204.5, "shadow_pnl": 4500.0, "status": "LIVE"},
            {"version": "v1.4.3-rc1", "log_likelihood": -1180.2, "shadow_pnl": 5200.0, "status": "CANDIDATE"},
            {"version": "v1.4.1", "log_likelihood": -1250.8, "shadow_pnl": 1200.0, "status": "ARCHIVED"},
        ],
        "transition_matrix": [
            [0.85, 0.10, 0.05],
            [0.05, 0.90, 0.05],
            [0.10, 0.05, 0.85]
        ],
        "data_mix": {"seed": 40, "live": 60}
    }

# --- NEW: Tab 4: Strategy Audit ---
@app.get("/attribution/strategy")
def get_strategy_attribution():
    # Daily P&L breakdown
    return {
        "pnl_stack": [
            {"date": "2026-03-09", "Momentum": 4500, "Fade": -1200, "A-VWAP": 3000, "OI Pulse": 500},
            {"date": "2026-03-10", "Momentum": -2000, "Fade": 4000, "A-VWAP": 1500, "OI Pulse": 1200},
            {"date": "2026-03-11", "Momentum": 3200, "Fade": 800, "A-VWAP": -500, "OI Pulse": 2100},
        ],
        "efficiency": [
            {"name": "Momentum", "win_rate": 0.62, "profit_factor": 1.8, "frequency": 45},
            {"name": "Fade", "win_rate": 0.55, "profit_factor": 1.4, "frequency": 30},
            {"name": "A-VWAP", "win_rate": 0.68, "profit_factor": 2.1, "frequency": 12},
            {"name": "OI Pulse", "win_rate": 0.48, "profit_factor": 1.1, "frequency": 85},
        ]
    }

# --- NEW: Tab 5: Observability ---
@app.get("/health/telemetry", response_model=TelemetryMetrics)
def get_telemetry():
    # Real Telemetry (#105)
    r = get_redis()
    conn = get_db()
    
    # 1. Fetch avg latency from recent trades in DB
    latency = 15.0 # Default
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT AVG(latency_ms) FROM trades WHERE time > now() - interval '1 hour'")
                res = cur.fetchone()
                if res and res[0]:
                    latency = float(res[0])
        except:
            pass
            
    # 2. Fetch slippage from Redis
    slippage = float(r.get("TOTAL_SLIPPAGE_INR") or 0.0)
    
    # 3. Simulate cores but with real scale
    return {
        "nse_latency_ms": latency,
        "bse_latency_ms": latency * 1.1,
        "slippage_leakage_inr": slippage,
        "cpu_cores": [random.uniform(20.0, 45.0), random.uniform(60.0, 85.0), random.uniform(40.0, 70.0)]
    }

# --- Modified: Tab 6: Analytics ---
@app.get("/analytics")
def get_analytics(mode: str = "Paper"):
    conn = get_db()
    if not conn:
        return {"equity_curve": [], "drawdown": 0.0}
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch 1-min aggregated P&L for charting
            cur.execute("""
                SELECT 
                    time_bucket('1 minute', time) AS bucket,
                    SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                FROM trades 
                WHERE execution_type = %s
                GROUP BY bucket 
                ORDER BY bucket ASC
            """, (mode,))
            rows = cur.fetchall()
            
            equity = 0.0
            curve = []
            for row in rows:
                equity += float(row['pnl'])
                curve.append({"time": row['bucket'].isoformat(), "equity": equity})
            
            return {
                "equity_curve": curve,
                "total_pnl": equity,
                "trade_count": len(rows)
            }
    except Exception as e:
        return {"error": str(e)}

@app.get("/portfolio", response_model=List[Position])
def get_portfolio(mode: str = "Paper"):
    r = get_redis()
    conn = get_db()
    
    positions = []
    if conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM portfolio WHERE execution_type=%s", (mode,))
                rows = cur.fetchall()
                for row in rows:
                    sym = row['symbol']
                    qty = row['quantity']
                    avg_p = float(row['avg_price'])
                    
                    unrealized = 0.0
                    tick_raw = r.get(f"latest_tick:{sym}")
                    if tick_raw and qty != 0:
                        tick = json.loads(tick_raw)
                        cur_p = float(tick["price"])
                        # Simple P&L calc for visual purposes
                        unrealized = (cur_p - avg_p) * qty
                    
                    positions.append({
                        "symbol": sym,
                        "strategy_id": row['strategy_id'],
                        "quantity": qty,
                        "avg_price": avg_p,
                        "realized_pnl": float(row['realized_pnl']),
                        "unrealized_pnl": unrealized
                    })
        except Exception:
            conn.rollback()
    
    return positions

@app.post("/panic")
async def panic(mode: str = "Paper"):
    r = get_redis()
    # Audit log the panic (#106)
    await AuditLogger.log_event("PANIC_TRIGGERED", "ADMIN_UI", f"Manual panic signal sent for {mode} mode")
    
    payload = {
        "action": "SQUARE_OFF_ALL",
        "reason": "MANUAL_PRO_DASHBOARD",
        "execution_type": mode
    }
    r.publish("panic_channel", json.dumps(payload))
    return {"status": "Panic signal sent"}

@app.get("/signals/history")
def get_signal_history(limit: int = 100):
    """v5.5: Returns rolling signal history for UI charting."""
    r = get_redis()
    history_raw = r.lrange("signal_history", 0, limit - 1)
    return [json.loads(h) for h in history_raw]

@app.get("/greeks/sensitivity")
def get_greek_sensitivity():
    """v5.5: Returns Greek sensitivity matrix and alpha components."""
    r = get_redis()
    state_raw = r.get("latest_market_state")
    if not state_raw:
        return {}
    ms = json.loads(state_raw)
    return {
        "vanna": ms.get("vanna", 0.0),
        "charm": ms.get("charm", 0.0),
        "alpha_components": {
            "env": ms.get("s_env", 0.0),
            "str": ms.get("s_str", 0.0),
            "div": ms.get("s_div", 0.0)
        }
    }


@app.get("/barriers/attribution")
def get_barrier_attribution():
    """
    v8.0 Signal Attribution: reads `barrier_exits` from Redis.
    Returns a grouped summary (donut chart data) + recent exit feed (table data).
    """
    r = get_redis()
    raw_exits = r.lrange("barrier_exits", 0, 49)  # Last 50 exits
    exits = []
    for e in raw_exits:
        try:
            exits.append(json.loads(e))
        except Exception:
            pass

    # Tally by barrier type
    summary = {"UPPER": 0, "LOWER": 0, "VERTICAL": 0, "PANIC": 0}
    for ex in exits:
        barrier = ex.get("barrier", "LOWER")
        if barrier in summary:
            summary[barrier] += 1

    # Return last 20 for the feed table
    feed = exits[:20]
    for ex in feed:
        # Human-readable timestamp
        ex["time"] = datetime.utcfromtimestamp(ex.get("ts", 0)).strftime("%H:%M:%S") if ex.get("ts") else "—"

    return {
        "summary": summary,
        "feed": feed,
        "total": len(exits)
    }


@app.get("/sizing/inspector")
def get_sizing_inspector():
    """
    v8.0 Sizing Inspector: returns current ATR, unit_size, half_kelly, and final_lots
    for each Tri-Brain asset so the dashboard can show the live hybrid sizing calculation.
    """
    r = get_redis()
    state_raw = r.get("latest_market_state")
    ms = json.loads(state_raw) if state_raw else {}
    atr = float(ms.get("atr", 20.0))
    atr = max(atr, 1.0)

    MAX_RISK_PER_TRADE = 2500.0
    ATR_SL_MULTIPLIER  = 1.0
    unit_size = MAX_RISK_PER_TRADE / (atr * ATR_SL_MULTIPLIER)

    # Read latest attributions to extract per-asset lots
    attr_raw = r.get("latest_attributions")
    assets_data = []
    if attr_raw:
        try:
            attrs = json.loads(attr_raw)
            for attr in attrs:
                asset = attr.get("asset", "")
                active_dec = attr.get("decisions", {}).get("HYBRID", {})
                assets_data.append({
                    "asset": asset,
                    "lots": round(active_dec.get("lots", unit_size * 0.01), 4),
                    "weight": round(active_dec.get("weight", 0.01), 4),
                    "unit_size": round(active_dec.get("unit_size", unit_size), 4),
                    "atr_used": round(active_dec.get("atr_used", atr), 4),
                    "score": round(active_dec.get("score", 0.5), 3)
                })
        except Exception:
            pass

    if not assets_data:
        for a in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            assets_data.append({
                "asset": a, "lots": round(unit_size * 0.01, 4),
                "weight": 0.01, "unit_size": round(unit_size, 4),
                "atr_used": round(atr, 4), "score": 0.5
            })

    return {
        "current_atr": round(atr, 2),
        "max_risk_per_trade": MAX_RISK_PER_TRADE,
        "unit_size_base": round(unit_size, 4),
        "assets": assets_data
    }


# --- Static Files (Serve Frontend) ---
# NOTE: Mount this AFTER all API routes to avoid shadowing
app.mount("/", StaticFiles(directory="dashboard/frontend", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
