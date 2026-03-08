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

app = FastAPI(title="Karthik's Trading API")

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

# Models
class SystemState(BaseModel):
    alpha_score: float
    hmm_regime: str
    gex_sign: str
    system_halted: bool
    macro_lockdown: bool
    available_margin_paper: float
    available_margin_live: float

class Position(BaseModel):
    symbol: str
    strategy_id: str
    quantity: int
    avg_price: float
    realized_pnl: float
    unrealized_pnl: float

# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/state", response_model=SystemState)
def get_state():
    r = get_redis()
    state_raw = r.get("latest_market_state")
    ms = json.loads(state_raw) if state_raw else {}
    
    return {
        "alpha_score": ms.get("s_total", 0.0),
        "hmm_regime": r.get("hmm_regime") or "UNKNOWN",
        "gex_sign": r.get("gex_sign") or "UNKNOWN",
        "system_halted": r.get("SYSTEM_HALTED") == "True",
        "macro_lockdown": r.get("MACRO_EVENT_LOCKDOWN") == "True",
        "available_margin_paper": float(r.get("AVAILABLE_MARGIN_PAPER") or 0),
        "available_margin_live": float(r.get("AVAILABLE_MARGIN_LIVE") or 0),
    }

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
                        unrealized = (cur_p - avg_p) * qty if qty > 0 else (avg_p - cur_p) * abs(qty)
                    
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
        "reason": "MANUAL_REACT_DASHBOARD",
        "execution_type": mode
    }
    r.publish("panic_channel", json.dumps(payload))
    return {"status": "Panic signal sent"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
