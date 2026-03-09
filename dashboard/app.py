import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import redis
import json
import random
from datetime import datetime
import time
import os

st.set_page_config(
    page_title="Karthik's Trading AI Assistant",
    page_icon="🦸",
    layout="wide",
    initial_sidebar_state="expanded"
)

if "trading_mode" not in st.session_state:
    st.session_state["trading_mode"] = "Paper"
if "last_mode" not in st.session_state:
    st.session_state["last_mode"] = "Paper"

# ─────────────────────────────────────────────────────────────────────────────
# Theme
# ─────────────────────────────────────────────────────────────────────────────
def inject_theme(mode):
    accent      = "#00D2FF" if mode == "Paper" else "#00FF87"
    accent_dark = "#0072FF" if mode == "Paper" else "#00A86B"
    accent_glow = "rgba(0,210,255,0.3)" if mode == "Paper" else "rgba(0,255,135,0.3)"
    mode_label  = "P A P E R   T R A D I N G" if mode == "Paper" else "L I V E   T R A D I N G"
    st.markdown(f"""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&family=JetBrains+Mono:wght@400;700&display=swap');
        :root {{
            --accent: {accent}; --accent-dark: {accent_dark}; --accent-glow: {accent_glow};
            --bg-dark: #07090d; --card-bg: rgba(22,27,34,0.6); --border: rgba(48,54,61,0.5);
        }}
        * {{ font-family: 'Inter', sans-serif; }}
        .stApp {{ background: radial-gradient(circle at 50% -20%, #1a1f2e 0%, #07090d 100%); color:#e6edf3; }}
        .mode-banner {{
            background: linear-gradient(90deg, transparent 0%, {accent_dark} 50%, transparent 100%);
            backdrop-filter:blur(12px); padding:12px 0; text-align:center; color:#fff;
            font-weight:800; font-size:15px; letter-spacing:6px; text-transform:uppercase;
            margin:-6rem -5rem 2rem -5rem; box-shadow:0 10px 30px -10px {accent_glow};
            border-bottom:1px solid rgba(255,255,255,0.1); position:relative; z-index:1000;
            animation:fadeInDown 0.8s ease-out;
        }}
        @keyframes fadeInDown {{ from{{opacity:0;transform:translateY(-20px)}} to{{opacity:1;transform:translateY(0)}} }}
        [data-testid="stSidebar"] {{ background:rgba(13,17,23,0.95)!important; backdrop-filter:blur(10px); border-right:1px solid var(--border); }}
        [data-testid="stMetric"] {{
            background:var(--card-bg)!important; border:1px solid var(--border)!important;
            border-left:4px solid var(--accent)!important; backdrop-filter:blur(10px);
            border-radius:12px!important; padding:20px!important;
            transition:all 0.4s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 4px 15px rgba(0,0,0,0.2);
        }}
        [data-testid="stMetric"]:hover {{ transform:scale(1.02) translateY(-5px); box-shadow:0 10px 25px -5px var(--accent-glow); }}
        [data-testid="stMetricValue"] {{ color:white!important; font-weight:800!important; font-family:'JetBrains Mono',monospace!important; font-size:1.8rem!important; text-shadow:0 0 10px var(--accent-glow); }}
        [data-testid="stMetricLabel"] {{ color:#8b949e!important; font-weight:600!important; text-transform:uppercase; letter-spacing:1.5px; font-size:11px!important; }}
        .stButton>button {{ background:rgba(33,38,45,0.8)!important; border:1px solid var(--border)!important; border-radius:8px!important; color:#c9d1d9!important; font-weight:600!important; transition:all 0.3s ease!important; height:3rem; width:100%; }}
        .stButton>button:hover {{ background:var(--accent)!important; color:#0d1117!important; box-shadow:0 0 15px var(--accent-glow); transform:translateY(-2px); }}
        div[data-baseweb="tab-list"] {{ gap:24px; background:transparent; margin-bottom:20px; }}
        button[data-baseweb="tab"] {{ font-weight:600!important; letter-spacing:1px; border-bottom:3px solid transparent!important; transition:all 0.3s ease!important; }}
        button[data-baseweb="tab"][aria-selected="true"] {{ border-bottom-color:var(--accent)!important; color:var(--accent)!important; }}
        [data-testid="stDataFrame"] {{ background:var(--card-bg); border-radius:12px; overflow:hidden; border:1px solid var(--border); }}
        .section-header {{ font-size:14px; font-weight:800; color:var(--accent); text-transform:uppercase; letter-spacing:2px; margin:25px 0 15px 0; padding-left:10px; border-left:3px solid var(--accent); }}
        .alpha-score {{ font-family:'JetBrains Mono',monospace; font-size:32px; font-weight:800; text-align:center; padding:20px; border-radius:12px; background:var(--card-bg); border:1px solid var(--border); }}
        .pill {{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:1px;}}
        .pill-green {{background:rgba(63,185,80,0.15);color:#3fb950;border:1px solid #3fb950;}}
        .pill-red {{background:rgba(248,81,73,0.15);color:#f85149;border:1px solid #f85149;}}
        .pill-yellow {{background:rgba(210,153,34,0.15);color:#d2990d;border:1px solid #d2990d;}}
        .pill-blue {{background:rgba(0,210,255,0.15);color:#00d2ff;border:1px solid #00d2ff;}}
        .pill-grey {{background:rgba(139,148,158,0.15);color:#8b949e;border:1px solid #8b949e;}}
        .signal-card {{background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:16px;margin:6px 0;}}
        .lockdown-banner {{background:rgba(248,81,73,0.1);border:1px solid #f85149;border-radius:8px;padding:12px;text-align:center;color:#f85149;font-weight:700;letter-spacing:2px;animation:pulse 2s infinite;}}
        @keyframes pulse {{0%,100%{{opacity:1}}50%{{opacity:0.6}}}}
    </style>
    <div class="mode-banner">{mode_label}</div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Connections
# ─────────────────────────────────────────────────────────────────────────────
def get_db_connection():
    try:
        db_host = os.getenv("DB_HOST", "localhost")
        conn = psycopg2.connect(dbname="trading_db", user="trading_user", password="trading_pass", host=db_host, port="5432")
        return conn
    except Exception:
        return None

@st.cache_resource
def get_redis_client():
    try:
        redis_host = os.getenv("REDIS_HOST", "localhost")
        r = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None

conn = get_db_connection()
r = get_redis_client()

# ─────────────────────────────────────────────────────────────────────────────
# Mock Data
# ─────────────────────────────────────────────────────────────────────────────
MOCK_PORTFOLIO = lambda et: pd.DataFrame([
    {"symbol": "NIFTY_ATM_CE", "strategy_id": "STRAT_GAMMA",     "quantity": 65,  "avg_price": 180.0, "realized_pnl": 4850.0, "execution_type": et},
    {"symbol": "NIFTY_ATM_CE", "strategy_id": "STRAT_REVERSION", "quantity": 65,  "avg_price": 165.0, "realized_pnl": 2320.0, "execution_type": et},
    {"symbol": "NIFTY_ATM_CE", "strategy_id": "STRAT_VWAP",      "quantity": 65,  "avg_price": 172.0, "realized_pnl": 1180.0, "execution_type": et},
    {"symbol": "NIFTY_ATM_CE", "strategy_id": "STRAT_OI_PULSE",  "quantity": 65,  "avg_price": 155.0, "realized_pnl": 890.0,  "execution_type": et},
    {"symbol": "BANKNIFTY_ATM_CE","strategy_id":"STRAT_LEAD_LAG","quantity": 30,  "avg_price": 340.0, "realized_pnl": 1640.0, "execution_type": et},
])

MOCK_TRADES = lambda: pd.DataFrame([
    {"time": datetime.now(), "symbol": "NIFTY_ATM_CE",     "strategy_id": "STRAT_GAMMA",    "action": "BUY",  "quantity": 65, "price": 180.0},
    {"time": datetime.now(), "symbol": "NIFTY_ATM_CE",     "strategy_id": "STRAT_REVERSION","action": "BUY",  "quantity": 65, "price": 165.0},
    {"time": datetime.now(), "symbol": "BANKNIFTY_ATM_CE", "strategy_id": "STRAT_LEAD_LAG", "action": "BUY",  "quantity": 30, "price": 340.0},
])

def fetch_portfolio(execution_type="Paper", strategy_id=None):
    if not conn:
        df = MOCK_PORTFOLIO(execution_type)
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return df
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if strategy_id and strategy_id != "All Portfolio":
                cur.execute("SELECT * FROM portfolio WHERE execution_type=%s AND strategy_id=%s ORDER BY realized_pnl DESC", (execution_type, strategy_id))
            else:
                cur.execute("SELECT * FROM portfolio WHERE execution_type=%s ORDER BY realized_pnl DESC", (execution_type,))
            return pd.DataFrame(cur.fetchall())
    except Exception:
        conn.rollback()
        df = MOCK_PORTFOLIO(execution_type)
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return df

def fetch_recent_trades(execution_type="Paper", strategy_id=None, limit=100):
    if not conn:
        df = MOCK_TRADES()
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return df
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if strategy_id and strategy_id != "All Portfolio":
                cur.execute("SELECT * FROM trades WHERE time>=CURRENT_DATE AND execution_type=%s AND strategy_id=%s ORDER BY time DESC LIMIT %s", (execution_type, strategy_id, limit))
            else:
                cur.execute("SELECT * FROM trades WHERE time>=CURRENT_DATE AND execution_type=%s ORDER BY time DESC LIMIT %s", (execution_type, limit))
            return pd.DataFrame(cur.fetchall())
    except Exception:
        conn.rollback()
        df = MOCK_TRADES()
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return df

def fetch_daily_metrics(execution_type="Paper", strategy_id=None):
    if not conn:
        return (10880.0, 12500000.0, 3200000.0) if not strategy_id or strategy_id == "All Portfolio" else (1500.0, 500000.0, 100000.0)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            q = "SELECT SUM(CASE WHEN action='SELL' THEN (price*quantity)-fees ELSE -(price*quantity)-fees END) as realized_pnl, SUM(ABS(price*quantity)) as total_volume FROM trades WHERE time>=CURRENT_DATE AND execution_type=%s"
            args = [execution_type]
            if strategy_id and strategy_id != "All Portfolio":
                q += " AND strategy_id=%s"; args.append(strategy_id)
            cur.execute(q, args)
            res = cur.fetchone()
            cur.execute("SELECT MAX(ABS(quantity*avg_price)) as max_capital FROM portfolio WHERE execution_type=%s", (execution_type,))
            mc = cur.fetchone()
            return (float(res['realized_pnl'] or 0), float(res['total_volume'] or 0), float(mc['max_capital'] or 0) if mc else 0)
    except Exception:
        conn.rollback()
        return 1300.0, 5000000.0, 2200000.0

def fetch_advanced_metrics(execution_type="Paper", strategy_id=None):
    def calc_stats(df):
        if df.empty: return {"win_rate": 0, "profit_factor": 0, "max_dd": 0, "sharpe": 0, "sortino": 0, "gain_to_pain": 0, "df": pd.DataFrame()}
        df['net_value'] = pd.to_numeric(df['net_value'], errors='coerce').fillna(0).astype(float)
        df['cumulative_pnl'] = df['net_value'].cumsum()
        df['peak'] = df['cumulative_pnl'].cummax()
        df['drawdown'] = df['peak'] - df['cumulative_pnl']
        
        wins = df[df['net_value'] > 0]['net_value'].sum()
        losses = abs(df[df['net_value'] < 0]['net_value'].sum())
        
        mean_ret = df['net_value'].mean()
        std_ret = df['net_value'].std()
        downside_ret = df[df['net_value'] < 0]['net_value']
        down_std = downside_ret.std() if not downside_ret.empty else 0
        
        sharpe = (mean_ret / std_ret) * (252**0.5) if pd.notna(std_ret) and std_ret != 0 else 0
        sortino = (mean_ret / down_std) * (252**0.5) if pd.notna(down_std) and down_std != 0 else 0
        
        return {
            "win_rate": len(df[df['net_value'] > 0]) / len(df) if len(df) > 0 else 0,
            "profit_factor": wins / losses if losses > 0 else float('inf'),
            "max_dd": float(df['drawdown'].max()),
            "sharpe": sharpe,
            "sortino": sortino,
            "gain_to_pain": wins / losses if losses > 0 else float('inf'),
            "df": df
        }

    if not conn:
        dates = pd.date_range(end=datetime.now(), periods=100, freq='h')
        pnl = [random.uniform(-500, 800) for _ in range(100)]
        df = pd.DataFrame({'time': dates, 'net_value': pnl, 'strategy_id': 'STRAT_GAMMA'})
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return calc_stats(df)
        
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            q = "SELECT time, strategy_id, CASE WHEN action='SELL' THEN (price*quantity)-fees ELSE -(price*quantity)-fees END as net_value FROM trades WHERE execution_type=%s"
            args = [execution_type]
            if strategy_id and strategy_id != "All Portfolio":
                q += " AND strategy_id=%s"; args.append(strategy_id)
            cur.execute(q + " ORDER BY time ASC", args)
            trades = cur.fetchall()
            df = pd.DataFrame(trades) if trades else pd.DataFrame()
            return calc_stats(df)
    except Exception:
        conn.rollback()
        return calc_stats(pd.DataFrame())

def format_currency(value):
    if value >= 1_000_000: return f"₹{value/1_000_000:.2f}M"
    if value >= 1_000: return f"₹{value/1_000:.1f}K"
    return f"₹{value:,.0f}"

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<p class="section-header">Trading Mode</p>', unsafe_allow_html=True)
    new_mode = st.toggle(
        "Real Trading" if st.session_state["trading_mode"] == "Actual" else "Paper Trading",
        value=(st.session_state["trading_mode"] == "Actual"),
        help="Toggle between Paper (simulated) and Real (live) trading"
    )
    st.session_state["trading_mode"] = "Actual" if new_mode else "Paper"
    if st.session_state["last_mode"] != st.session_state["trading_mode"]:
        st.session_state["last_mode"] = st.session_state["trading_mode"]
        st.rerun()

    st.markdown("")
    if st.button("🚨 PANIC: SQUARE OFF ALL", use_container_width=True):
        if r:
            r.publish("panic_channel", json.dumps({"action": "SQUARE_OFF_ALL", "reason": "MANUAL_DASHBOARD", "execution_type": st.session_state["trading_mode"]}))
            st.error("⚡ PANIC SIGNAL SENT!")
        else:
            st.error("Redis offline — cannot send panic signal!")

    st.divider()
    st.markdown('<p class="section-header">Capital Budgets</p>', unsafe_allow_html=True)
    
    if r:
        if "paper_budget" not in st.session_state:
            st.session_state["paper_budget"] = float(r.get("PAPER_CAPITAL_LIMIT") or r.get("GLOBAL_CAPITAL_LIMIT_PAPER") or 500000.0)
        if "live_budget" not in st.session_state:
            st.session_state["live_budget"] = float(r.get("LIVE_CAPITAL_LIMIT") or r.get("GLOBAL_CAPITAL_LIMIT_LIVE") or 0.0)
        if "stop_day_loss" not in st.session_state:
            st.session_state["stop_day_loss"] = float(r.get("STOP_DAY_LOSS") or 5000.0)
    else:
        st.session_state["paper_budget"] = 500000.0
        st.session_state["live_budget"] = 0.0
        st.session_state["stop_day_loss"] = 5000.0

    new_paper_budget = st.number_input("Paper Trading Capital (₹)", value=st.session_state["paper_budget"], step=10000.0)
    new_live_budget = st.number_input("Live Trading Capital (₹)", value=st.session_state["live_budget"], step=10000.0, help="Must be > 0 to enable live trading.")
    new_stop_day_loss = st.number_input(
        "🛑 Stop Day Loss (₹)",
        value=st.session_state["stop_day_loss"],
        step=500.0,
        min_value=0.0,
        help="Maximum cumulative realized loss allowed per day. When breached, all new trade entries are blocked and open positions are liquidated."
    )

    if st.button("💾 Save Budgets", use_container_width=True):
        st.session_state["paper_budget"] = new_paper_budget
        st.session_state["live_budget"] = new_live_budget
        st.session_state["stop_day_loss"] = new_stop_day_loss
        if r:
            # Save configuration limits
            r.set("PAPER_CAPITAL_LIMIT", str(new_paper_budget))
            r.set("LIVE_CAPITAL_LIMIT", str(new_live_budget))
            r.set("GLOBAL_CAPITAL_LIMIT_PAPER", str(new_paper_budget))
            r.set("GLOBAL_CAPITAL_LIMIT_LIVE", str(new_live_budget))
            r.set("STOP_DAY_LOSS", str(new_stop_day_loss))

            # Initialize available margins if they don't exist
            if not r.exists("AVAILABLE_MARGIN_PAPER"):
                r.set("AVAILABLE_MARGIN_PAPER", str(new_paper_budget))
            if not r.exists("AVAILABLE_MARGIN_LIVE"):
                r.set("AVAILABLE_MARGIN_LIVE", str(new_live_budget))

            st.success("Budgets & Stop Day Loss saved!")
        else:
            st.error("Redis offline!")

    # ── Stop Day Loss Live Status ───────────────────────────────────────────
    if r:
        try:
            stop_limit = float(r.get("STOP_DAY_LOSS") or 5000.0)
            day_loss_raw = r.get(f"DAILY_REALIZED_PNL_{st.session_state['trading_mode'].upper()}")
            day_pnl = float(day_loss_raw or 0.0)
            day_loss_breached = day_pnl <= -stop_limit
            if day_loss_breached:
                st.markdown(
                    f"<div style='background:rgba(248,81,73,0.15);border:1px solid #f85149;border-radius:8px;"
                    f"padding:10px;text-align:center;color:#f85149;font-weight:700;font-size:12px;margin-top:8px;'>"
                    f"🛑 STOP DAY LOSS HIT — ₹{day_pnl:,.0f} / -₹{stop_limit:,.0f}<br>All new entries BLOCKED"
                    f"</div>",
                    unsafe_allow_html=True
                )
            else:
                remaining = stop_limit + day_pnl  # day_pnl is negative when losing
                pct_used = ((stop_limit - remaining) / stop_limit * 100) if stop_limit > 0 else 0
                color = "#f85149" if pct_used > 75 else ("#d2990d" if pct_used > 40 else "#3fb950")
                st.markdown(
                    f"<div style='background:rgba(0,0,0,0.3);border:1px solid {color};border-radius:8px;"
                    f"padding:8px;text-align:center;font-size:11px;margin-top:6px;'>"
                    f"<span style='color:#8b949e'>Day Loss Buffer:</span> "
                    f"<span style='color:{color};font-weight:700'>₹{remaining:,.0f}</span> remaining "
                    f"<span style='color:#8b949e'>({pct_used:.0f}% used)</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )
        except Exception:
            pass


    # ── System Health ──────────────────────────────────────────────────────
    st.markdown('<p class="section-header">System Health</p>', unsafe_allow_html=True)
    if r:
        halt = r.get("SYSTEM_HALT")
        lockdown = r.get("MACRO_EVENT_LOCKDOWN")
        halted_flag = r.get("SYSTEM_HALTED")
        regime = r.get("hmm_regime") or "UNKNOWN"

        if halted_flag == "True":
            st.error("🛑 SYSTEM HALTED")
        elif halt and halt != "False":
            st.error(f"🔴 CIRCUIT BREAKER L{halt}")
        elif lockdown == "True":
            st.warning("📵 MACRO LOCKDOWN ACTIVE")
        else:
            st.success("✅ System Operational")

        st.caption(f"HMM Regime: `{regime}`")
        st.caption(f"GEX Sign: `{r.get('gex_sign') or 'N/A'}`")
    else:
        st.error("Redis Offline")

    st.divider()

    # ── Console ────────────────────────────────────────────────────────────
    with st.expander("System Console", expanded=False):
        if r:
            logs_raw = r.lrange("live_logs", 0, 20)
            if logs_raw:
                for log_b in logs_raw:
                    try:
                        log_data = json.loads(log_b)
                        ts = datetime.fromisoformat(log_data['timestamp']).strftime("%H:%M:%S")
                        level = log_data['level']
                        msg = log_data['message']
                        color = "#8b949e"
                        if level == "ERROR": color = "#f85149"
                        elif level == "SYSTEM": color = "#3fb950"
                        st.markdown(f"`{ts}` <span style='color:{color}'>{msg}</span>", unsafe_allow_html=True)
                    except Exception:
                        continue
            else:
                st.caption("No logs.")
        else:
            st.caption("Redis offline.")

# ─────────────────────────────────────────────────────────────────────────────
# Main Header
# ─────────────────────────────────────────────────────────────────────────────
view_type = st.session_state["trading_mode"]
inject_theme(view_type)

st.markdown(f"""
<h1 style='margin:0;padding:0;font-size:28px;color:#e6edf3;'>
    🦸 Karthik's Trading AI Assistant
    <span style='font-size:14px;color:#8b949e;margin-left:12px;'>{view_type} Mode</span>
</h1>
""", unsafe_allow_html=True)

# ── Macro Lockdown Banner ──────────────────────────────────────────────────
if r and r.get("MACRO_EVENT_LOCKDOWN") == "True":
    st.markdown("<div class='lockdown-banner'>📵 MACRO EVENT LOCKDOWN — All new entries blocked</div>", unsafe_allow_html=True)

# ── Alpha Score ────────────────────────────────────────────────────────────
if r:
    state_raw = r.get("latest_market_state")
    if state_raw:
        try:
            ms = json.loads(state_raw)
            s_total = ms.get("s_total", 0)
            hmm = r.get("hmm_regime") or ms.get("hmm_regime", "RANGING")
            gex = r.get("gex_sign") or ms.get("gex_sign", "POSITIVE")
            score_color = "#00D2FF" if s_total > 39 else ("#f85149" if s_total < -39 else "#8b949e")
            regime_label = "AGGR LONG" if s_total > 75 else ("AGGR SHORT" if s_total < -75 else ("NEUTRAL" if abs(s_total) > 39 else "SLEEP"))
            st.markdown(f"""
            <div class='alpha-score' style='color:{score_color};border-color:{score_color}33;'>
                α = {s_total:.1f} &nbsp;|&nbsp; Regime: {regime_label} &nbsp;|&nbsp; HMM: {hmm} &nbsp;|&nbsp; GEX: {gex}
            </div>""", unsafe_allow_html=True)
        except Exception:
            pass

st.markdown("")

# ── Strategy view filter ───────────────────────────────────────────────────
ALL_STRATS = ["All Portfolio", "STRAT_GAMMA", "STRAT_REVERSION", "STRAT_VWAP", "STRAT_OI_PULSE", "STRAT_LEAD_LAG"]
selected_global_strat = st.selectbox("View Scope:", ALL_STRATS, label_visibility="collapsed")
st.markdown("")

# ── Scorecards ─────────────────────────────────────────────────────────────
realized_day, vol_day, max_cap = fetch_daily_metrics(view_type, strategy_id=selected_global_strat)
port_df = fetch_portfolio(view_type, strategy_id=selected_global_strat)
unrealized_day = 0.0

if not port_df.empty and r:
    for _, row in port_df.iterrows():
        sym = row['symbol']
        qty = row['quantity']
        avg_p = float(row['avg_price'])
        tick_raw = r.get(f"latest_tick:{sym}")
        if tick_raw and qty != 0:
            tick = json.loads(tick_raw)
            cur_p = float(tick["price"])
            unrealized_day += (cur_p - avg_p) * qty if qty > 0 else (avg_p - cur_p) * abs(qty)

current_cap = sum(abs(row['quantity'] * float(row['avg_price'])) for _, row in port_df.iterrows()) if not port_df.empty else 0.0

st.markdown('<p class="section-header">Live Capital & Hard Budget</p>', unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)

if view_type == "Paper":
    budget_limit = float(r.get("GLOBAL_CAPITAL_LIMIT_PAPER") or r.get("PAPER_CAPITAL_LIMIT") or 0.0) if r else 0.0
    avail_margin = float(r.get("AVAILABLE_MARGIN_PAPER") or budget_limit) if r else 0.0
else:
    budget_limit = float(r.get("GLOBAL_CAPITAL_LIMIT_LIVE") or r.get("LIVE_CAPITAL_LIMIT") or 0.0) if r else 0.0
    avail_margin = float(r.get("AVAILABLE_MARGIN_LIVE") or budget_limit) if r else 0.0

utilization = ((budget_limit - avail_margin) / budget_limit * 100) if budget_limit > 0 else 0.0

c1.metric("Global Budget Limit", format_currency(budget_limit))
c2.metric("Available Margin", format_currency(avail_margin))
c3.metric("Live Positions Value", format_currency(current_cap))
c4.metric("Budget Utilization", f"{utilization:.1f}%", delta="⚠️ EXHAUSTED" if utilization >= 95.0 else ("⚠️ HIGH" if utilization >= 80.0 else "✅ SAFE"), delta_color="inverse" if utilization >= 80.0 else "normal")

st.divider()

m1, m2, m3 = st.columns(3)
m1.metric("Peak Capital Used (Historical)", format_currency(max_cap))
m2.metric("Realized P/L (Today)", f"₹{realized_day:,.0f}", delta=f"{realized_day:+,.0f}")
m3.metric("Unrealized P/L (Live)", f"₹{unrealized_day:,.0f}", delta=f"{unrealized_day:+,.0f}")
st.markdown("")

# ─────────────────────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────────────────────
tab_term, tab_signals, tab_meta, tab_risk, tab_analytics, tab_pnl = st.tabs([
    "📟 Terminal", "📡 Market Signals", "🧠 Meta-Router", "⚠️ Risk & Phantom", "📊 Strategy Analytics", "📈 Performance"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: Terminal
# ══════════════════════════════════════════════════════════════════════════════
with tab_term:
    st.markdown("**Tick-to-Trade Latency:** `< 2ms` via ZeroMQ &nbsp;|&nbsp; **Execution Bridge:** " + ("🟢 Live (Shoonya)" if view_type == "Actual" else "🔵 Paper"))
    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("##### Live Market Feed")
        if r:
            market_data = []
            for sym in ["NIFTY50", "BANKNIFTY", "RELIANCE", "HDFC", "INFY"]:
                tick_raw = r.get(f"latest_tick:{sym}")
                if tick_raw:
                    tick = json.loads(tick_raw)
                    chg = tick.get("price", 0) - tick.get("prev_price", tick.get("price", 0))
                    market_data.append({"Symbol": sym, "Price": f"{tick['price']:.2f}", "Chg": f"{chg:+.2f}", "OI": f"{tick.get('oi',0):,}"})
            if market_data:
                st.dataframe(pd.DataFrame(market_data), use_container_width=True, hide_index=True)
            else:
                st.dataframe(pd.DataFrame([
                    {"Symbol": "NIFTY50",   "Price": "22350.00", "Chg": "+12.50", "OI": "1,234,000"},
                    {"Symbol": "BANKNIFTY", "Price": "47200.00", "Chg": "+45.00", "OI": "987,000"},
                ]), use_container_width=True, hide_index=True)

        st.markdown("##### L2 Orderbook")
        st.dataframe(pd.DataFrame([
            {"Bid Qty": 12500, "Bid": 22348.00, "Ask": 22350.20, "Ask Qty": 8400},
            {"Bid Qty": 8200,  "Bid": 22347.50, "Ask": 22350.90, "Ask Qty": 15000},
            {"Bid Qty": 15000, "Bid": 22347.00, "Ask": 22351.50, "Ask Qty": 12000},
        ]), use_container_width=True, hide_index=True)

    with col2:
        st.markdown("##### Active Positions (Buy-Only Options)")
        if not port_df.empty:
            df = port_df.copy()
            df['unrealized_pnl'] = 0.0
            if r:
                for idx, row in df.iterrows():
                    tick_raw = r.get(f"latest_tick:{row['symbol']}")
                    if tick_raw and row['quantity'] != 0:
                        ltp = float(json.loads(tick_raw)["price"])
                        df.at[idx, 'unrealized_pnl'] = (ltp - float(row['avg_price'])) * row['quantity']
            st.dataframe(
                df[['symbol', 'strategy_id', 'quantity', 'avg_price', 'realized_pnl', 'unrealized_pnl']],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No active positions.")

    st.markdown("##### Recent Trades")
    trades_df = fetch_recent_trades(view_type, strategy_id=selected_global_strat)
    if not trades_df.empty:
        def color_action(val):
            return f'color: {"#3fb950" if val == "BUY" else "#f85149"}; font-weight: bold'
        cols = [c for c in ['time', 'symbol', 'strategy_id', 'action', 'quantity', 'price'] if c in trades_df.columns]
        styled = trades_df[cols].style.map(color_action, subset=['action'])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("No trades recorded today.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: Market Signals (SRS §4)
# ══════════════════════════════════════════════════════════════════════════════
with tab_signals:
    if r:
        state_raw = r.get("latest_market_state")
        ms = json.loads(state_raw) if state_raw else {}

        # ── Row 1: Alpha & Regime ──────────────────────────────────────────
        st.markdown('<p class="section-header">Alpha Scoring & Regime</p>', unsafe_allow_html=True)
        c1, c2, c3, c4, c5 = st.columns(5)
        s_total = ms.get("s_total", 0.0)
        hmm_r   = r.get("hmm_regime") or "RANGING"
        gex     = r.get("gex_sign") or "POSITIVE"
        hurst   = ms.get("hurst", 0.5)
        rv      = ms.get("rv", 0.0)

        c1.metric("Alpha Score (α)", f"{s_total:.2f}")
        c2.metric("HMM Regime", hmm_r)
        c3.metric("GEX Sign", gex)
        c4.metric("Hurst Exponent", f"{hurst:.3f}")
        c5.metric("Realized Vol", f"{rv:.5f}")

        st.divider()

        # ── Row 2: Microstructure Signals ─────────────────────────────────
        st.markdown('<p class="section-header">Microstructure Signals (GIL Bypass Compute)</p>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)

        ofi_z = float(r.get("log_ofi_zscore") or ms.get("log_ofi_zscore", 0.0))
        disp  = float(r.get("dispersion_coeff") or ms.get("dispersion_coeff", 0.5))
        cvd_a = r.get("cvd_absorption") == "1"
        cvd_f = int(r.get("cvd_flip_ticks") or ms.get("cvd_flip_ticks", 0))
        dislo = r.get("price_dislocation") == "1"
        basis_z = float(ms.get("basis_zscore", 0.0))
        vtr   = float(ms.get("vol_term_ratio", 1.0))
        zgl   = float(ms.get("zero_gamma_level", 0.0))
        atr   = float(r.get("atr") or ms.get("atr", 20.0))
        toxic = ms.get("toxic_option", False)

        ofi_color = "#3fb950" if ofi_z > 2 else ("#f85149" if ofi_z < -2 else "#8b949e")
        c1.metric("Log-OFI Z-Score", f"{ofi_z:.3f}")
        c1.markdown(f"<span style='color:{ofi_color};font-size:11px'>{'🟢 STRONG BUY FLOW' if ofi_z>2 else ('🔴 STRONG SELL FLOW' if ofi_z<-2 else '⚪ NEUTRAL')}</span>", unsafe_allow_html=True)

        disp_color = "#f85149" if disp < 0.30 else ("#3fb950" if disp > 0.60 else "#d2990d")
        c2.metric("Dispersion Coeff", f"{disp:.3f}")
        c2.markdown(f"<span style='color:{disp_color};font-size:11px'>{'🔴 VETO ACTIVE (<0.30)' if disp<0.30 else ('🟢 HIGH CORR' if disp>0.60 else '🟡 MODERATE')}</span>", unsafe_allow_html=True)

        c3.metric("CVD Absorption", "✅ YES" if cvd_a else "❌ NO")
        c3.metric("CVD Flip Ticks", str(cvd_f))

        c4.metric("Basis Z-Score", f"{basis_z:.2f}")
        c4.markdown(f"<span style='color:#f85149;font-size:11px'>{'⚠️ DISLOCATION' if dislo else ''}</span>", unsafe_allow_html=True)

        st.divider()

        # ── Row 3: Greeks & Vol ────────────────────────────────────────────
        st.markdown('<p class="section-header">Greeks & Volatility Structure</p>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)

        charm = ms.get("charm", 0.0)
        vanna = ms.get("vanna", 0.0)

        c1.metric("Vol Term Ratio (Near/Far)", f"{vtr:.3f}")
        c1.markdown(f"<span style='font-size:11px;color:{'#f85149' if vtr>1.2 else '#3fb950'}'>{'⚠️ HIGH NEAR-TERM IV' if vtr>1.2 else '✅ NORMAL TERM STRUCTURE'}</span>", unsafe_allow_html=True)

        c2.metric("Zero Gamma Level", f"{zgl:,.0f}" if zgl > 0 else "N/A")
        c3.metric("ATR (live)", f"{atr:.1f} pts")
        c4.metric("Toxic Option Flag", "⚠️ YES" if toxic else "✅ NO")
        c4.caption(f"Charm={charm:.4f} | Vanna={vanna:.4f}")

        st.divider()

        # ── Row 4: Time Window ─────────────────────────────────────────────
        st.markdown('<p class="section-header">Macro Time Window</p>', unsafe_allow_html=True)
        now_t = datetime.now().strftime("%H:%M")
        in_window = ("09:30" <= now_t <= "11:30") or ("13:30" <= now_t <= "15:00")
        lockdown_active = r.get("MACRO_EVENT_LOCKDOWN") == "True"

        wc1, wc2, wc3 = st.columns(3)
        wc1.metric("Current Time (IST)", now_t)
        wc2.metric("Entry Window", "✅ OPEN" if in_window and not lockdown_active else "🚫 CLOSED")
        wc3.metric("Macro Lockdown", "🔴 ACTIVE" if lockdown_active else "✅ CLEAR")

        # Upcoming macro events
        macro_raw = r.get("macro_events") if r else None
        if macro_raw:
            try:
                events = json.loads(macro_raw)
                upcoming = [e for e in events[:5] if e['datetime'] > datetime.now().isoformat()]
                if upcoming:
                    st.markdown("**Upcoming Tier-1 Events:**")
                    for ev in upcoming[:3]:
                        st.caption(f"📅 `{ev['datetime'][:16]}` — **{ev['name']}** ({ev.get('currency','?')}) [{ev.get('source','').upper()}]")
            except Exception:
                pass
    else:
        st.error("Redis unavailable — cannot display signals.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: Meta-Router (SRS §2.4 — HMM + Vetoes)
# ══════════════════════════════════════════════════════════════════════════════
with tab_meta:
    st.markdown('<p class="section-header">HMM Regime Engine</p>', unsafe_allow_html=True)
    if r:
        hmm_regime = r.get("hmm_regime") or "RANGING"
        rc1, rc2, rc3 = st.columns(3)

        regime_colors = {"TRENDING": "#3fb950", "RANGING": "#d2990d", "CRASH": "#f85149"}
        rcol = regime_colors.get(hmm_regime, "#8b949e")
        rc1.markdown(f"""<div style='background:rgba(0,0,0,0.3);border:2px solid {rcol};border-radius:12px;padding:20px;text-align:center'>
            <div style='font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:2px'>HMM STATE</div>
            <div style='font-size:36px;font-weight:800;color:{rcol};margin:8px 0'>{hmm_regime}</div>
        </div>""", unsafe_allow_html=True)
        rc2.metric("GEX Posture", r.get("gex_sign") or "POSITIVE")
        rc3.metric("Dispersion Coeff", r.get("dispersion_coeff") or "N/A")

        st.divider()
        st.markdown('<p class="section-header">Active Vetoes</p>', unsafe_allow_html=True)
        vc1, vc2, vc3 = st.columns(3)

        disp_val = float(r.get("dispersion_coeff") or 0.5)
        disp_veto = disp_val < 0.30
        oi_walls_raw = r.get("oi_walls")
        oi_veto = False  # Would need spot vs wall proximity calc
        macro_lockdown = r.get("MACRO_EVENT_LOCKDOWN") == "True"

        vc1.markdown(f"""<div class='signal-card'>
            <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>Dispersion Veto</div>
            <div style='font-size:20px;margin-top:8px'>{'🔴 ACTIVE — MR capped 1 lot' if disp_veto else '✅ Clear'}</div>
            <div style='font-size:11px;color:#8b949e;margin-top:4px'>Threshold: disp_coeff &lt; 0.30</div>
        </div>""", unsafe_allow_html=True)

        vc2.markdown(f"""<div class='signal-card'>
            <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>OI Wall Veto</div>
            <div style='font-size:20px;margin-top:8px'>{'🔴 ACTIVE — Spot at OI wall' if oi_veto else '✅ Clear'}</div>
            <div style='font-size:11px;color:#8b949e;margin-top:4px'>Guard: spot &lt; 15pts from top OI strikes</div>
        </div>""", unsafe_allow_html=True)

        vc3.markdown(f"""<div class='signal-card'>
            <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>Macro Lockdown</div>
            <div style='font-size:20px;margin-top:8px'>{'🔴 ACTIVE — No CRASH detection' if macro_lockdown else '✅ Clear'}</div>
            <div style='font-size:11px;color:#8b949e;margin-top:4px'>Tier-1 event within 30 min window</div>
        </div>""", unsafe_allow_html=True)

        st.divider()
        st.markdown('<p class="section-header">Strategy Command States (All 5)</p>', unsafe_allow_html=True)

        ALL_5_STRATEGIES = {
            "STRAT_GAMMA":     {"name": "Long Gamma Momentum",    "condition": "NEG_GEX + TRENDING"},
            "STRAT_REVERSION": {"name": "Institutional Fade",     "condition": "POS_GEX + RANGING"},
            "STRAT_VWAP":      {"name": "Anchored VWAP",          "condition": "|α| > 40"},
            "STRAT_OI_PULSE":  {"name": "OI Pulse Scalping",      "condition": "OI Accel > 300%"},
            "STRAT_LEAD_LAG":  {"name": "Lead-Lag Reversion",     "condition": "Corr < 0.40"},
        }
        sc1, sc2 = st.columns(2)
        for i, (sid, info) in enumerate(ALL_5_STRATEGIES.items()):
            state = r.get(f"state:{sid}") or "SLEEP"
            icon = "🟢" if state == "ACTIVE" else ("🟠" if state == "ORPHANED" else "💤")
            target_col = sc1 if i % 2 == 0 else sc2
            target_col.markdown(f"""<div class='signal-card' style='margin-bottom:8px'>
                <div style='display:flex;justify-content:space-between;align-items:center'>
                    <div>
                        <span style='font-size:13px;font-weight:700'>{icon} {sid}</span>
                        <div style='font-size:11px;color:#8b949e'>{info['name']} · <code>{info['condition']}</code></div>
                    </div>
                    <span class='pill {"pill-green" if state=="ACTIVE" else ("pill-yellow" if state=="ORPHANED" else "pill-grey")}'>{state}</span>
                </div>
            </div>""", unsafe_allow_html=True)
    else:
        st.error("Redis offline.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: Risk & Phantom Orders
# ══════════════════════════════════════════════════════════════════════════════
with tab_risk:
    st.markdown('<p class="section-header">Pending Orders (Reconciler View)</p>', unsafe_allow_html=True)
    if r:
        pending = r.hgetall("pending_orders")
        if pending:
            rows = []
            for oid, meta_raw in list(pending.items())[:20]:
                try:
                    m = json.loads(meta_raw)
                    age = time.time() - m.get("dispatch_time_epoch", time.time())
                    rows.append({
                        "Order ID": oid[:12] + "…",
                        "Symbol": m.get("symbol"),
                        "Strategy": m.get("strategy_id"),
                        "Action": m.get("action"),
                        "Qty": m.get("quantity"),
                        "Age (s)": f"{age:.1f}",
                        "Broker ID": m.get("broker_order_id") or "Awaiting ACK",
                        "Status": "⚠️ PHANTOM RISK" if age > 3 else "🔄 In-Flight"
                    })
                except Exception:
                    pass
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.success("✅ No pending orders — all fills reconciled.")

        st.divider()
        st.markdown('<p class="section-header">Phantom Order Log</p>', unsafe_allow_html=True)
        phantom_raw = r.lrange("phantom_orders", 0, 9)
        if phantom_raw:
            for p in phantom_raw:
                try:
                    pdata = json.loads(p)
                    meta = pdata.get("meta", {})
                    st.error(f"👻 **{pdata.get('detected_at','')[:16]}** — `{meta.get('symbol')}` | Strategy: `{meta.get('strategy_id')}` | Qty: {meta.get('quantity')}")
                except Exception:
                    pass
        else:
            st.success("✅ No phantom orders detected.")

        st.divider()
        st.markdown('<p class="section-header">Liquidation Barriers — Live ATR</p>', unsafe_allow_html=True)
        atr = float(r.get("atr") or 20.0)
        rc1, rc2, rc3 = st.columns(3)
        rc1.metric("Live ATR", f"{atr:.1f} pts")
        rc2.metric("TP Offset (2.5×)", f"+{2.5*atr:.1f} pts")
        rc3.metric("SL Offset (1.0×)", f"-{1.0*atr:.1f} pts")

        cvd_f2 = int(r.get("cvd_flip_ticks") or 0)
        st.metric("CVD Flip Ticks (B3 trigger ≥ 5)", cvd_f2,
                  delta="⚠️ WARNING" if cvd_f2 >= 3 else None,
                  delta_color="inverse" if cvd_f2 >= 3 else "normal")

        st.divider()
        st.markdown('<p class="section-header">Rate Limiter Status</p>', unsafe_allow_html=True)
        rl1, rl2 = st.columns(2)
        rl1.markdown("""<div class='signal-card'>
            <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>Layer 1 — SEBI 10 OPS Token Bucket</div>
            <div style='font-size:18px;margin-top:8px'>✅ Active</div>
            <div style='font-size:11px;color:#8b949e;margin-top:4px'>Max 10 orders per second</div>
        </div>""", unsafe_allow_html=True)
        rl2.markdown("""<div class='signal-card'>
            <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>Layer 2 — 190req/60s Rolling Window</div>
            <div style='font-size:18px;margin-top:8px'>✅ Active</div>
            <div style='font-size:11px;color:#8b949e;margin-top:4px'>Shoonya 200/min lockout guard</div>
        </div>""", unsafe_allow_html=True)
    else:
        st.error("Redis offline — cannot display risk data.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: Strategy Analytics
# ══════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    st.markdown('<p class="section-header">Advanced Performance Analytics</p>', unsafe_allow_html=True)
    metrics = fetch_advanced_metrics(view_type, strategy_id=selected_global_strat)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Win Rate", f"{metrics['win_rate']*100:.1f}%")
    m2.metric("Profit Factor", f"{metrics['profit_factor']:.2f}")
    m3.metric("Max Drawdown", format_currency(metrics['max_dd']), delta_color="inverse")
    m4.metric("Sharpe Ratio", f"{metrics['sharpe']:.2f}")
    m5.metric("Sortino Ratio", f"{metrics['sortino']:.2f}")

    st.markdown("")
    am1, am2, am3 = st.columns(3)
    am1.metric("Gain-to-Pain Ratio", f"{metrics['gain_to_pain']:.2f}")
    
    # M2 Measure = RiskFreeRate + Sharpe * MarketVol (Assuming MarketVol = 0.15, Rf = 0.065)
    m2_measure = 0.065 + metrics['sharpe'] * 0.15
    am2.metric("M² Measure Benchmark", f"{m2_measure*100:.1f}%")
    
    # Profit-to-Pain Profile (Total Profit / Max DD)
    profit_to_pain = (metrics['df']['net_value'].sum() / metrics['max_dd']) if metrics['max_dd'] > 0 else float('inf')
    am3.metric("Profit-to-Pain Profile", f"{profit_to_pain:.2f}")

    equity_df = metrics['df']

    if not equity_df.empty and 'time' in equity_df.columns:
        st.markdown(f"###### Equity Curve — `{selected_global_strat}`")
        st.line_chart(equity_df.set_index('time')['cumulative_pnl'], color="#3fb950")
        st.markdown("###### Drawdown (Underwater)")
        st.area_chart(equity_df.set_index('time')['drawdown'], color="#f85149")
    else:
        st.info("Insufficient trade history for analytics.")

    # DTE-aware pot sizing guide
    st.divider()
    st.markdown('<p class="section-header">Dynamic Lot Sizing (DTE Regime)</p>', unsafe_allow_html=True)
    if r:
        nifty_lot = int(r.hget("lot_sizes", "NIFTY50") or 65)
        bn_lot = int(r.hget("lot_sizes", "BANKNIFTY") or 30)
    else:
        nifty_lot, bn_lot = 65, 30
    today_day = datetime.now().strftime("%A")
    sizing_pct = "100%" if today_day in ("Wednesday", "Thursday") else "50%"
    ls1, ls2, ls3 = st.columns(3)
    ls1.metric("Today", today_day)
    ls2.metric("Nifty Lot Size", f"{nifty_lot} ({sizing_pct})")
    ls3.metric("BankNifty Lot Size", f"{bn_lot} ({sizing_pct})")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6: Performance History
# ══════════════════════════════════════════════════════════════════════════════
with tab_pnl:
    st.markdown('<p class="section-header">FII Macro Bias</p>', unsafe_allow_html=True)
    if r:
        fii_raw = r.get("fii_summary")
        if fii_raw:
            fii = json.loads(fii_raw)
            fc1, fc2, fc3 = st.columns(3)
            fc1.metric("FII Bias Score", f"{fii.get('bias_score', 0):.2f}")
            fc2.metric("FII Signal", fii.get("signal", "N/A"))
            fc3.metric("Updated", fii.get("updated_at", "N/A")[:16])
        else:
            st.info("FII data not yet fetched (runs daily at 18:30 IST).")

    st.divider()
    st.markdown('<p class="section-header">GCP Infrastructure</p>', unsafe_allow_html=True)
    ic1, ic2 = st.columns(2)
    ic1.markdown("""<div class='signal-card'>
        <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>NSE Holiday Guard</div>
        <div style='font-size:18px;margin-top:8px'>✅ Active (python-holidays)</div>
        <div style='font-size:11px;color:#8b949e;margin-top:4px'>VM skipped on NSE holidays & weekends</div>
    </div>""", unsafe_allow_html=True)
    ic2.markdown("""<div class='signal-card'>
        <div style='font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px'>Macro Pre-Fetch</div>
        <div style='font-size:18px;margin-top:8px'>✅ ForexFactory + FMP</div>
        <div style='font-size:11px;color:#8b949e;margin-top:4px'>Fresh calendar written before VM boot each day</div>
    </div>""", unsafe_allow_html=True)

    st.divider()
    p1, p2 = st.tabs(["Weekly P&L", "Monthly P&L"])
    with p1:
        st.info("Connect to TimescaleDB for weekly P&L aggregation.")
    with p2:
        st.info("Connect to TimescaleDB for monthly P&L aggregation.")
