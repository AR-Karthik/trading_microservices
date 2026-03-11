from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import redis
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

app = FastAPI(title="🦸‍♂️ PROJECT K.A.R.T.H.I.K. (Kinetic Alpha Regime Tracking & High-frequency Institutional Kernel)")

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify Tailscale IP/Host
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return {"status": "ok"}

@app.get("/state", response_model=SystemState)
def get_state():
    r = get_redis()
    state_raw = r.get("latest_market_state")
    ms = json.loads(state_raw) if state_raw else {}
    
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

    return {
        "alpha_score": ms.get("s_total", 0.0),
        "hmm_regime": r.get("hmm_regime") or "UNKNOWN",
        "gex_sign": r.get("gex_sign") or "UNKNOWN",
        "system_halted": r.get("SYSTEM_HALTED") == "True",
        "macro_lockdown": r.get("MACRO_EVENT_LOCKDOWN") == "True",
        "available_margin_paper": float(r.get("AVAILABLE_MARGIN_PAPER") or 0),
        "available_margin_live": float(r.get("AVAILABLE_MARGIN_LIVE") or 0),
        "signals": deep_signals,
        "power_five": {
            "HDFCBANK": ms.get("z_hdfc", 1.2),
            "RELIANCE": ms.get("z_ril", -0.8),
            "ICICIBANK": ms.get("z_icici", 2.1),
            "INFY": ms.get("z_infy", -0.3),
            "TCS": ms.get("z_tcs", 0.9)
        },
        "exit_path_70_30": {
            "tp1": ms.get("tp1_price", 0.0),
            "tp2": ms.get("tp2_price", 0.0),
            "progress": ms.get("exit_progress", 45) # percentage
        }
    }

@app.get("/strategies", response_model=List[StrategyStatus])
def get_strategies():
    r = get_redis()
    # Strategies tracked by MetaRouter
    targets = ["STRAT_GAMMA", "STRAT_REVERSION", "STRAT_VWAP", "STRAT_OI_PULSE", "STRAT_LEAD_LAG"]
    results = []
    
    # In a real system, we'd fetch actual process status. 
    # For now, we derive it from the last MetaRouter decision in Redis.
    regime = r.get("hmm_regime") or "RANGING"
    
    for t in targets:
        # Derivative logic to show "intended" state
        status = "PAUSED"
        if t == "STRAT_LEAD_LAG": status = "ACTIVE"
        if t == "STRAT_GAMMA" and r.get("gex_sign") == "NEGATIVE" and regime == "TRENDING": status = "ACTIVE"
        if t == "STRAT_REVERSION" and r.get("gex_sign") == "POSITIVE" and regime == "RANGING": status = "ACTIVE"
        
        results.append({
            "name": t.replace("STRAT_", ""),
            "status": status,
            "parameters": {"regime": regime}
        })
    return results

# --- NEW: Tab 2: Regime Orchestrator ---
@app.post("/regime/config")
def update_regime_config(config: RegimeConfigRequest):
    r = get_redis()
    r.set("CONFIG:REGIME_ENGINE", config.engine)
    r.set("CONFIG:HURST_THRESHOLD", config.hurst_threshold)
    r.set("CONFIG:HYBRID_CONFIDENCE", config.hybrid_confidence)
    r.set("CONFIG:VPIN_TOXICITY", config.vpin_toxicity)
    # Publish signal to MetaRouter to hot-swap
    r.publish("system_cmd", json.dumps({"cmd": "HOT_SWAP_REGIME", "data": config.dict()}))
    return {"status": "Config updated and published"}

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
    r = get_redis()
    # Mocking latency and CPU for visualization
    import random
    return {
        "nse_latency_ms": random.uniform(12.5, 18.2),
        "bse_latency_ms": random.uniform(14.1, 22.5),
        "slippage_leakage_inr": float(r.get("TOTAL_SLIPPAGE_INR") or 450.25),
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
def panic(mode: str = "Paper"):
    r = get_redis()
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
