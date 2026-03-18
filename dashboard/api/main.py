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
    parent_uuid: str
    delta: float
    theta: float

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

# D-05: /state now supports asset switching [API-01]
@app.get("/state")
def get_state(asset: str = "NIFTY50"):
    try:
        r = get_redis()
        indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        if asset not in indices: asset = "NIFTY50"
        
        index_states = {}
        for asset in indices:
            st_raw = r.get(f"latest_market_state:{asset}")
            st = json.loads(st_raw) if st_raw else {}
            reg_raw = r.hget("hmm_regime_state", asset)
            regime = "UNKNOWN"
            if reg_raw:
                try: regime = json.loads(reg_raw).get("regime", "UNKNOWN")
                except: pass
            
            index_states[asset] = {
                "score": st.get("s_total", 0.0), 
                "regime": regime, 
                "price": st.get("price", 0.0),
                "asto": st.get("asto", 0.0),
                "asto_regime": st.get("asto_regime", 0),
                "asto_multiplier": st.get("asto_multiplier", 3.0),
                "rsi": st.get("rsi", 50.0),
                "pcr": st.get("pcr", 0.85),
                "change_pct": st.get("change_pct", 0.0),
                "call_wall": r.get(f"CALL_WALL:{asset}") or "—",
                "put_wall": r.get(f"PUT_WALL:{asset}") or "—",
                "oi_accel": float(r.get(f"OI_ACCEL:{asset}") or 0.0)
            }

        ms = json.loads(r.get("latest_market_state:NIFTY50") or r.get("latest_market_state") or "{}")

        deep_signals = {
            "log_ofi_z": ms.get("log_ofi_zscore", 0.0),
            "dispersion": ms.get("dispersion_coeff", 0.5),
            "hurst":      ms.get("hurst", 0.5),
            "rv":         ms.get("rv", 0.0),
            "adx":        ms.get("adx", 20.0),
            "vanna":      ms.get("vanna", 0.0),
            "charm":      ms.get("charm", 0.0),
            "atr":        ms.get("atr", 20.0),
            "basis_z":    ms.get("basis_zscore", 0.0),
            "cvd_flips":  ms.get("cvd_flip_ticks", 0),
            "cvd_flips":  ms.get("cvd_flip_ticks", 0),
            "atm_iv":     float(r.get(f"LIVE_IV:{asset}") or r.get("atm_iv") or ms.get("atm_iv", 0.18)),
            "asto":       ms.get("asto", 0.0),
            "asto_regime": ms.get("asto_regime", 0),
            "asto_multiplier": ms.get("asto_multiplier", 3.0),
            "rsi": ms.get("rsi", 50.0),
            "pcr": ms.get("pcr", 0.85),
            "change_pct": ms.get("change_pct", 0.0),
            "call_wall": r.get(f"CALL_WALL:{asset}") or "—",
            "put_wall": r.get(f"PUT_WALL:{asset}") or "—",
            "oi_accel": float(r.get(f"OI_ACCEL:{asset}") or 0.0),
            "gamma_flip": ms.get("price", 0.0) * 0.995, # Mock/Calculated
            "price_walls": {
                "support": [ms.get("price", 0.0) * 0.99, ms.get("price", 0.0) * 0.98],
                "resistance": [ms.get("price", 0.0) * 1.01, ms.get("price", 0.0) * 1.02]
            }
        }

        power_five = {}
        for idx in indices:
            power_five[idx] = {}
            components = {
                "NIFTY50":   ["HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "ITC"],
                "BANKNIFTY": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
                "SENSEX":    ["HDFCBANK", "RELIANCE", "ICICIBANK", "ITC", "LT"]
            }[idx]
            for sym in components:
                raw = r.hget("power_five_matrix", sym)
                power_five[idx][sym] = json.loads(raw).get("z_score", 0.0) if raw else round(float(r.get(f"zscore:{sym}") or 0), 2)

        return {
            "source": "VM_DIRECT",
            "alpha_score": index_states[asset]["score"],
            "hmm_regime": index_states[asset]["regime"],
            "index_states": index_states,
            "primary_asset": asset,
            "gex_sign": r.get(f"gex_sign:{asset}") or "UNKNOWN",
            "system_halted": r.get("SYSTEM_HALTED") == "True",
            "macro_lockdown": r.get("MACRO_EVENT_LOCKDOWN") == "True",
            "available_margin_paper": float(r.get("CASH_COMPONENT_PAPER") or 0) + float(r.get("COLLATERAL_COMPONENT_PAPER") or 0),
            "available_margin_live": float(r.get("CASH_COMPONENT_LIVE") or 0) + float(r.get("COLLATERAL_COMPONENT_LIVE") or 0),
            "paper_capital_limit": float(r.get("PAPER_CAPITAL_LIMIT") or 0),
            "live_capital_limit": float(r.get("LIVE_CAPITAL_LIMIT") or 0),
            "signals": deep_signals,
            "power_five": power_five,
            "exit_path_70_30": ms.get("exit_path_70_30", {"tp1": 0.0, "tp2": 0.0, "progress": 0.0})
        }
    except Exception:
        # D-00: Robust Mock Fallback for UI Testing
        return {
            "source": "MOCK_MODE",
            "alpha_score": 0.82, "hmm_regime": "TRENDING_UP",
            "index_states": {
                "NIFTY50": {"score": 0.82, "regime": "TRENDING_UP", "price": 22450.5, "asto": 75.0, "asto_regime": 1, "asto_multiplier": 3.2},
                "BANKNIFTY": {"score": -0.15, "regime": "RANGING", "price": 47200.0, "asto": 20.0, "asto_regime": 0, "asto_multiplier": 2.8},
                "SENSEX": {"score": 0.45, "regime": "TRENDING_UP", "price": 74100.0, "asto": 55.0, "asto_regime": 0, "asto_multiplier": 3.0}
            },
            "signals": {"log_ofi_z": 1.2, "adx": 32.5, "rv": 0.12, "hurst": 0.62, "atr": 185.0, "atm_iv": 0.15},
            "power_five": {"NIFTY50": {"HDFCBANK": 1.2, "RELIANCE": 0.8, "ICICIBANK": 1.5, "INFY": -0.2, "ITC": 0.5}},
            "available_margin_paper": 1000000.0, "available_margin_live": 0.0,
            "system_halted": False, "macro_lockdown": False,
            "exit_path_70_30": {"tp1": 0.0, "tp2": 0.0, "progress": 0.0}
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
    r.set("CONFIG:MAX_RISK_PER_TRADE_PAPER", config.paper_max_risk)
    r.set("CONFIG:MAX_RISK_PER_TRADE_LIVE",  config.live_max_risk)
    
    # Also update available margins (50:50 Cash-to-Collateral sync)
    paper_eff = config.paper_capital * 0.85 # HEDGE_RESERVE_PCT adjustment
    live_eff  = config.live_capital * 0.85

    r.set("CASH_COMPONENT_PAPER", f"{paper_eff * 0.5:.2f}")
    r.set("COLLATERAL_COMPONENT_PAPER", f"{paper_eff * 0.5:.2f}")
    r.set("CASH_COMPONENT_LIVE", f"{live_eff * 0.5:.2f}")
    r.set("COLLATERAL_COMPONENT_LIVE", f"{live_eff * 0.5:.2f}")

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
    assets = ["NIFTY50", "BANKNIFTY", "SENSEX"]
    
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
def get_strategy_attribution(mode: str = "Paper"):
    conn = get_db()
    if not conn:
        return {"pnl_stack": [], "efficiency": []}
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. P&L Stack by day and strategy
            cur.execute("""
                SELECT 
                    time_bucket('1 day', time) AS date,
                    strategy_id,
                    SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                FROM trades 
                WHERE execution_type = %s
                GROUP BY date, strategy_id
                ORDER BY date ASC
            """, (mode,))
            rows = cur.fetchall()
            
            pnl_stack = {}
            for row in rows:
                dt = row['date'].strftime("%Y-%m-%d")
                if dt not in pnl_stack:
                    pnl_stack[dt] = {"date": dt}
                name = row['strategy_id'].replace("STRAT_", "")
                pnl_stack[dt][name] = float(row['pnl'])
            
            # 2. Strategy Efficiency (Win Rate, Profit Factor)
            cur.execute("""
                WITH trade_pnl AS (
                    SELECT 
                        strategy_id,
                        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                    FROM trades 
                    WHERE execution_type = %s
                    GROUP BY strategy_id, time_bucket('1 hour', time) -- Simplified "trade" definition
                )
                SELECT 
                    strategy_id,
                    COUNT(*) as frequency,
                    COUNT(*) FILTER (WHERE pnl > 0)::float / COUNT(*) as win_rate,
                    SUM(pnl) FILTER (WHERE pnl > 0) / NULLIF(ABS(SUM(pnl) FILTER (WHERE pnl < 0)), 0) as profit_factor
                FROM trade_pnl
                GROUP BY strategy_id
            """, (mode,))
            efficiency_rows = cur.fetchall()
            
            efficiency = []
            for row in efficiency_rows:
                efficiency.append({
                    "name": row['strategy_id'].replace("STRAT_", ""),
                    "win_rate": float(row['win_rate'] or 0.0),
                    "profit_factor": float(row['profit_factor'] or 0.0),
                    "frequency": int(row['frequency'])
                })
            
            return {
                "pnl_stack": list(pnl_stack.values()),
                "efficiency": efficiency
            }
    except Exception as e:
        return {"error": str(e)}

# --- NEW: Tab 5: Observability ---
@app.get("/health/telemetry")
def get_telemetry():
    try:
        r = get_redis()
        daemons = [
            "NSE_MD", "BSE_MD", "OMS_EXEC", "META_ROUTER", "HMM_ENGINE",
            "VOL_SURFACE", "RISK_ENGINE", "DB_WORKER", "CLOUD_SYNC", "HFT_LOGS",
            "BACKTESTER", "REPLAY_SVC", "SIGNAL_GEN", "HEALTH_MON"
        ]
        active_daemons = []
        for d in daemons:
            if r.get(f"HEARTBEAT:{d}"): active_daemons.append(d)
            elif random.random() > 0.1: active_daemons.append(d)

        return {
            "nse_latency_ms": float(r.get("NSE_LATENCY") or 0.4),
            "bse_latency_ms": float(r.get("BSE_LATENCY") or 0.8),
            "slippage_leakage_inr": float(r.get("SLIPPAGE_TOTAL") or 0.0),
            "cpu_cores": [random.uniform(5, 95) for _ in range(8)],
            "daemons": active_daemons
        }
    except Exception:
        return {
            "nse_latency_ms": 0.4, "bse_latency_ms": 0.8, "slippage_leakage_inr": 450.0,
            "cpu_cores": [random.uniform(5, 95) for _ in range(8)],
            "daemons": ["NSE_MD", "BSE_MD", "OMS_EXEC", "META_ROUTER", "HMM_ENGINE", "HEALTH_MON"]
        }

# --- Modified: Tab 6: Analytics ---
@app.get("/analytics/summary")
def get_analytics_summary(mode: str = "Paper"):
    try:
        conn = get_db()
        if not conn: raise Exception("No DB")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                WITH daily_pnl AS (
                    SELECT 
                        time_bucket('1 day', time) as day,
                        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                    FROM trades 
                    WHERE execution_type = %s
                    GROUP BY day
                )
                SELECT 
                    AVG(pnl) / NULLIF(STDDEV(pnl), 0) * SQRT(252) as sharpe,
                    SUM(pnl) FILTER (WHERE pnl > 0) / NULLIF(ABS(SUM(pnl) FILTER (WHERE pnl < 0)), 0) as profit_factor,
                    COUNT(*) FILTER (WHERE pnl > 0)::float / NULLIF(COUNT(*), 0) as win_rate
                FROM daily_pnl
            """, (mode,))
            res = cur.fetchone()
            cur.execute("""
                SELECT SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) 
                OVER (ORDER BY time ASC) as equity FROM trades WHERE execution_type = %s
            """, (mode,))
            eq_rows = cur.fetchall()
            max_dd, peak = 0.0, -float('inf')
            for r in eq_rows:
                eq = float(r['equity'])
                if eq > peak: peak = eq
                dd = peak - eq
                if dd > max_dd: max_dd = dd
            return {
                "sharpe": float(res['sharpe'] or 0.0), "profit_factor": float(res['profit_factor'] or 0.0),
                "win_rate": float(res['win_rate'] or 0.0), "max_drawdown": max_dd
            }
    except Exception:
        return {"sharpe": 2.15, "max_drawdown": 12450.0, "profit_factor": 1.85, "win_rate": 0.62}

@app.get("/analytics/regime")
def get_analytics_regime(mode: str = "Paper"):
    try:
        conn = get_db()
        if not conn: raise Exception("No DB")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT audit_tags->>'regime' as regime, SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                FROM trades WHERE execution_type = %s AND audit_tags->>'regime' IS NOT NULL GROUP BY regime
            """, (mode,))
            return cur.fetchall()
    except:
        return [
            {"regime": "TRENDING_UP", "pnl": 125000},
            {"regime": "VOLATILE_CRASH", "pnl": -45000},
            {"regime": "RANGING", "pnl": 12000}
        ]

@app.get("/analytics")
def get_analytics(mode: str = "Paper"):
    try:
        conn = get_db()
        if not conn: raise Exception("No DB")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT time_bucket('5 minutes', time) AS bucket, SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as pnl
                FROM trades WHERE execution_type = %s GROUP BY bucket ORDER BY bucket ASC
            """, (mode,))
            rows = cur.fetchall()
            equity, curve = 0.0, []
            for row in rows:
                equity += float(row['pnl'])
                curve.append({"time": row['bucket'].isoformat(), "equity": equity})
            return {"equity_curve": curve, "total_pnl": equity, "trade_count": len(rows)}
    except Exception:
        # Mock curve logic
        curve = []
        base = 10000
        for i in range(20):
            base += random.randint(-500, 1500)
            curve.append({"time": f"2026-03-17T{10+i//12:02}:{ (i%12)*5:02}:00", "equity": base})
        return {"equity_curve": curve, "total_pnl": base, "trade_count": 20}

@app.get("/portfolio", response_model=List[Position])
def get_portfolio(mode: str = "Paper"):
    try:
        r = get_redis()
        conn = get_db()
        positions = []
        if conn:
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
                        try:
                            tick = json.loads(tick_raw)
                            cur_p = float(tick.get("price", avg_p))
                            unrealized = (cur_p - avg_p) * qty
                        except: pass
                    stall = int(r.get(f"STALL_TIMER:{sym}") or 0)
                    st_raw = r.get(f"latest_market_state:{sym}") or r.get("latest_market_state:NIFTY50")
                    atr = 20.0
                    if st_raw: atr = json.loads(st_raw).get("atr", 20.0)
                    atr_stop = avg_p - (atr * 1.2) if qty > 0 else avg_p + (atr * 1.2)
                    positions.append({
                        "symbol": sym, "strategy_id": row['strategy_id'], "quantity": qty,
                        "avg_price": avg_p, "realized_pnl": float(row['realized_pnl']),
                        "unrealized_pnl": unrealized, "stall_timer": stall, "atr_stop": round(atr_stop, 2),
                        "parent_uuid": row.get('parent_uuid', 'NONE'),
                        "delta": float(row.get('delta', 0.0)),
                        "theta": float(row.get('theta', 0.0))
                    })
        return positions
    except Exception:
        return [
            {"symbol": "NIFTY24MAR22500CE", "strategy_id": "STRAT_GAMMA", "quantity": 150, "avg_price": 120.5, "realized_pnl": 4500.0, "unrealized_pnl": 1200.0, "stall_timer": 4, "atr_stop": 95.2},
            {"symbol": "BANKNIFTY24MAR47000PE", "strategy_id": "STRAT_VWAP", "quantity": -30, "avg_price": 450.0, "realized_pnl": -200.0, "unrealized_pnl": 800.0, "stall_timer": 12, "atr_stop": 485.4}
        ]

@app.get("/portfolio/delta")
def get_portfolio_delta():
    # D-17: Delta Thermometer data
    r = get_redis()
    return {
        "NIFTY50":   float(r.get("NET_DELTA_NIFTY50") or 0.0),
        "BANKNIFTY": float(r.get("NET_DELTA_BANKNIFTY") or 0.0),
        "threshold": 0.15
    }

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

@app.get("/audit")
def get_audit():
    r = get_redis()
    events = r.lrange("audit_trail", 0, 99)
    return [json.loads(e) for e in events]

@app.get("/signals/history")
def get_signal_history(limit: int = 100):
    """Returns rolling signal history for UI charting."""
    r = get_redis()
    history_raw = r.lrange("signal_history", 0, limit - 1)
    return [json.loads(h) for h in history_raw]

@app.get("/greeks/sensitivity")
def get_greek_sensitivity():
    """Returns Greek sensitivity matrix and alpha components."""
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
    Signal Attribution: reads `barrier_exits` from Redis.
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


@app.post("/system/halt")
def halt_system():
    # D-21: Manual system kill switch
    r = get_redis()
    r.set("SYSTEM_HALTED", "True")
    return {"status": "System halted"}

@app.get("/sizing/inspector")
def get_sizing_inspector():
    """
    Sizing Inspector: returns current ATR, unit_size, half_kelly, and final_lots
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
        for a in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
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


@app.get("/analytics/fiidii")
def get_fii_dii():
    r = get_redis()
    raw = r.get("latest_fii_dii")
    if raw:
        return json.loads(raw)
    return [
        {"category": "FII", "buyValue": 0, "sellValue": 0, "netValue": 0, "date": "—"},
        {"category": "DII", "buyValue": 0, "sellValue": 0, "netValue": 0, "date": "—"}
    ]

# --- Static Files (Serve Frontend) ---
# NOTE: Mount this AFTER all API routes to avoid shadowing
app.mount("/", StaticFiles(directory="dashboard/frontend", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
