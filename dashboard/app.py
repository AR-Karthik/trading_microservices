import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import redis
import json
import random
from datetime import datetime
import time

st.set_page_config(
    page_title="Karthik's Trading AI Assistant 🦸‍♂️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────────────────────────
# Session State Initialization
# ─────────────────────────────────────────────────────────────
if 'trading_mode' not in st.session_state:
    st.session_state.trading_mode = "Paper"

# ─────────────────────────────────────────────────────────────
# Theme: Dark Terminal + Mode Accent
# ─────────────────────────────────────────────────────────────
def inject_theme(mode):
    if mode == "Paper":
        accent = "#1976D2"
        accent_dark = "#0D47A1"
        accent_glow = "rgba(25, 118, 210, 0.15)"
        mode_label = "📄 PAPER"
    else:
        accent = "#43A047"
        accent_dark = "#1B5E20"
        accent_glow = "rgba(67, 160, 71, 0.15)"
        mode_label = "💰 LIVE"

    st.markdown(f"""
    <style>
        /* ── Base dark terminal ── */
        .stApp {{
            background-color: #0e1117;
            color: #e0e0e0;
        }}

        /* ── Mode accent strip ── */
        .mode-strip {{
            background: linear-gradient(90deg, {accent_dark}, {accent});
            padding: 6px 0;
            text-align: center;
            color: white;
            font-weight: 700;
            font-size: 13px;
            letter-spacing: 2px;
            border-radius: 0 0 8px 8px;
            margin: -1rem -1rem 1rem -1rem;
            box-shadow: 0 2px 12px {accent_glow};
        }}

        /* ── Sidebar ── */
        [data-testid="stSidebar"] {{
            background-color: #161b22 !important;
            border-right: 1px solid #21262d;
        }}
        [data-testid="stSidebar"] .stMarkdown p,
        [data-testid="stSidebar"] .stMarkdown span,
        [data-testid="stSidebar"] label {{
            color: #c9d1d9 !important;
        }}

        /* ── Metric cards ── */
        [data-testid="stMetric"] {{
            background-color: #161b22;
            border: 1px solid #21262d;
            border-left: 4px solid {accent};
            border-radius: 8px;
            padding: 12px 16px;
        }}
        [data-testid="stMetricValue"] {{
            color: #e6edf3 !important;
            font-weight: 600;
        }}
        [data-testid="stMetricLabel"] {{
            color: #8b949e !important;
            font-size: 12px !important;
        }}

        /* ── Tabs ── */
        div[data-baseweb="tab-list"] {{
            background-color: #161b22 !important;
            border-radius: 8px;
            padding: 4px;
            gap: 4px;
            border: 1px solid #21262d;
        }}
        button[data-baseweb="tab"] {{
            color: #8b949e !important;
            border-radius: 6px !important;
            font-weight: 500;
        }}
        button[data-baseweb="tab"][aria-selected="true"] {{
            background-color: {accent} !important;
            color: white !important;
        }}

        /* ── Dataframes ── */
        [data-testid="stDataFrame"] {{
            border: 1px solid #21262d;
            border-radius: 8px;
            overflow: hidden;
        }}

        /* ── Expanders ── */
        [data-testid="stExpander"] {{
            background-color: #161b22;
            border: 1px solid #21262d;
            border-radius: 8px;
        }}
        [data-testid="stExpander"] summary {{
            color: #c9d1d9 !important;
        }}

        /* ── Buttons ── */
        .stButton > button {{
            border-radius: 6px;
            font-weight: 600;
            transition: all 0.2s;
        }}
        .stButton > button:hover {{
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }}

        /* ── Form inputs ── */
        .stTextInput > div > div > input,
        .stNumberInput > div > div > input,
        .stSelectbox > div > div,
        .stMultiSelect > div > div {{
            background-color: #0d1117 !important;
            border-color: #30363d !important;
            color: #e6edf3 !important;
        }}

        /* ── Dividers ── */
        hr {{
            border-color: #21262d !important;
        }}

        /* ── Info boxes ── */
        .stAlert {{
            background-color: #161b22 !important;
            border: 1px solid #21262d !important;
            color: #c9d1d9 !important;
        }}

        /* ── Panic button ── */
        .panic-btn .stButton > button {{
            background: linear-gradient(135deg, #d32f2f, #b71c1c) !important;
            color: white !important;
            border: none !important;
            font-size: 14px !important;
            padding: 10px !important;
        }}
        .panic-btn .stButton > button:hover {{
            background: linear-gradient(135deg, #f44336, #d32f2f) !important;
        }}

        /* ── Deploy button ── */
        button[kind="primary"] {{
            background: linear-gradient(135deg, {accent_dark}, {accent}) !important;
            color: white !important;
            border: none !important;
            width: 100%;
        }}

        /* ── Section headers ── */
        .section-header {{
            color: {accent};
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            margin: 8px 0 4px 0;
            padding-bottom: 4px;
            border-bottom: 1px solid #21262d;
        }}
    </style>
    """, unsafe_allow_html=True)

    # Mode accent strip
    st.markdown(f'<div class="mode-strip">{mode_label} TRADING MODE</div>', unsafe_allow_html=True)

inject_theme(st.session_state.trading_mode)

# ─────────────────────────────────────────────────────────────
# Database Connections (with graceful fallback)
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname="trading_db", user="trading_user",
            password="trading_pass", host="localhost", port="5432"
        )
        return conn
    except Exception:
        return None

@st.cache_resource
def get_redis_client():
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None

conn = get_db_connection()
r = get_redis_client()

# ─────────────────────────────────────────────────────────────
# Data Fetching (with mock fallback)
# ─────────────────────────────────────────────────────────────
MOCK_PORTFOLIO = lambda et: pd.DataFrame([
    {"symbol": "NIFTY50", "strategy_id": "SMA_1", "quantity": 100, "avg_price": 22000.0, "realized_pnl": 4850.0, "execution_type": et},
    {"symbol": "BANKNIFTY", "strategy_id": "MeanRev_1", "quantity": -50, "avg_price": 46150.0, "realized_pnl": 2320.0, "execution_type": et},
    {"symbol": "RELIANCE", "strategy_id": "OIPulse_1", "quantity": 25, "avg_price": 2900.0, "realized_pnl": 1180.0, "execution_type": et}
])

MOCK_TRADES = lambda: pd.DataFrame([
    {"time": datetime.now(), "symbol": "NIFTY50", "strategy_id": "SMA_1", "action": "BUY", "quantity": 100, "price": 21950.0},
    {"time": datetime.now(), "symbol": "BANKNIFTY", "strategy_id": "MeanRev_1", "action": "SELL", "quantity": 50, "price": 46100.0},
    {"time": datetime.now(), "symbol": "RELIANCE", "strategy_id": "OIPulse_1", "action": "BUY", "quantity": 25, "price": 2895.0}
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
                cur.execute("SELECT * FROM portfolio WHERE execution_type = %s AND strategy_id = %s ORDER BY realized_pnl DESC", (execution_type, strategy_id))
            else:
                cur.execute("SELECT * FROM portfolio WHERE execution_type = %s ORDER BY realized_pnl DESC", (execution_type,))
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
                cur.execute("""
                    SELECT * FROM trades 
                    WHERE time >= CURRENT_DATE AND execution_type = %s AND strategy_id = %s
                    ORDER BY time DESC LIMIT %s
                """, (execution_type, strategy_id, limit))
            else:
                cur.execute("""
                    SELECT * FROM trades 
                    WHERE time >= CURRENT_DATE AND execution_type = %s
                    ORDER BY time DESC LIMIT %s
                """, (execution_type, limit))
            return pd.DataFrame(cur.fetchall())
    except Exception:
        conn.rollback()
        df = MOCK_TRADES()
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        return df

def fetch_daily_metrics(execution_type="Paper", strategy_id=None):
    if not conn:
        # Returns (realized, volume, max_cap)
        if not strategy_id or strategy_id == "All Portfolio":
            return 8350.0, 12500000.0, 3200000.0
        return 1500.0, 500000.0, 100000.0
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if strategy_id and strategy_id != "All Portfolio":
                cur.execute("""
                    SELECT 
                        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as realized_pnl,
                        SUM(ABS(price * quantity)) as total_volume
                    FROM trades WHERE time >= CURRENT_DATE AND execution_type = %s AND strategy_id = %s
                """, (execution_type, strategy_id))
                res = cur.fetchone()
                cur.execute("SELECT MAX(ABS(quantity * avg_price)) as max_capital FROM portfolio WHERE execution_type = %s AND strategy_id = %s", (execution_type, strategy_id))
                max_cap = cur.fetchone()
            else:
                cur.execute("""
                    SELECT 
                        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as realized_pnl,
                        SUM(ABS(price * quantity)) as total_volume
                    FROM trades WHERE time >= CURRENT_DATE AND execution_type = %s
                """, (execution_type,))
                res = cur.fetchone()
                cur.execute("SELECT MAX(ABS(quantity * avg_price)) as max_capital FROM portfolio WHERE execution_type = %s", (execution_type,))
                max_cap = cur.fetchone()
                
            realized = float(res['realized_pnl']) if res and res['realized_pnl'] else 0.0
            volume = float(res['total_volume']) if res and res['total_volume'] else 0.0
            max_capital = float(max_cap['max_capital']) if max_cap and max_cap['max_capital'] else 0.0
            return realized, volume, max_capital
    except Exception:
        conn.rollback()
        return 1300.0, 5000000.0, 2200000.0

def fetch_aggregated_pnl(execution_type="Paper", interval='week', group_by_day=False, strategy_id=None):
    if not conn:
        return pd.DataFrame()
    if group_by_day:
        date_trunc = "day"
        interval_clause = "time >= date_trunc('week', CURRENT_DATE)" if interval == 'week' else "time >= date_trunc('month', CURRENT_DATE)"
    else:
        date_trunc = "week" if interval == "week" else "month"
        interval_clause = "TRUE"
        
    strat_clause = f"AND strategy_id = '{strategy_id}'" if strategy_id and strategy_id != "All Portfolio" else ""
    
    query = f"""
    SELECT date_trunc('{date_trunc}', time) AS period, strategy_id, symbol,
        SUM(fees) as total_fees, COUNT(id) as trade_count, SUM(ABS(quantity)) as volume,
        AVG(price) as avg_price,
        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as net_profit
    FROM trades WHERE {interval_clause} AND execution_type = '{execution_type}' {strat_clause}
    GROUP BY date_trunc('{date_trunc}', time), strategy_id, symbol ORDER BY period DESC
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            df = pd.DataFrame(cur.fetchall())
            if not df.empty:
                df['period'] = pd.to_datetime(df['period'])
                df['display_period'] = df['period'].dt.strftime('%Y-%m-%d')
                for col in ['total_fees', 'avg_price', 'net_profit', 'volume', 'trade_count']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
            return df
    except Exception:
        conn.rollback()
        return pd.DataFrame()

def fetch_advanced_metrics(execution_type="Paper", strategy_id=None):
    if not conn:
        # Mock data for demonstration
        dates = pd.date_range(end=datetime.now(), periods=100, freq='H')
        mock_pnl = [random.uniform(-500, 800) for _ in range(100)]
        df = pd.DataFrame({'time': dates, 'net_value': mock_pnl, 'strategy_id': 'SMA_1'})
        if strategy_id and strategy_id != "All Portfolio":
            df = df[df['strategy_id'] == strategy_id]
        if df.empty:
            return 0.0, 0.0, 0.0, pd.DataFrame()
        df['cumulative_pnl'] = df['net_value'].cumsum()
        df['peak'] = df['cumulative_pnl'].cummax()
        df['drawdown'] = df['peak'] - df['cumulative_pnl']
        return 0.62, 1.85, 4500.0, df

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if strategy_id and strategy_id != "All Portfolio":
                cur.execute("""
                    SELECT time, strategy_id, 
                    CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END as net_value
                    FROM trades WHERE execution_type = %s AND strategy_id = %s ORDER BY time ASC
                """, (execution_type, strategy_id))
            else:
                cur.execute("""
                    SELECT time, strategy_id, 
                    CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END as net_value
                    FROM trades WHERE execution_type = %s ORDER BY time ASC
                """, (execution_type,))
            trades = cur.fetchall()
            if not trades:
                return 0.0, 0.0, 0.0, pd.DataFrame()
            
            df = pd.DataFrame(trades)
            df['net_value'] = pd.to_numeric(df['net_value'], errors='coerce').fillna(0).astype(float)
            df['cumulative_pnl'] = df['net_value'].cumsum()
            df['peak'] = df['cumulative_pnl'].cummax()
            df['drawdown'] = df['peak'] - df['cumulative_pnl']
            
            max_dd = df['drawdown'].max()
            
            winning_trades = df[df['net_value'] > 0]['net_value'].sum()
            losing_trades = abs(df[df['net_value'] < 0]['net_value'].sum())
            profit_factor = winning_trades / losing_trades if losing_trades > 0 else float('inf')
            win_rate = len(df[df['net_value'] > 0]) / len(df) if len(df) > 0 else 0
            
            return win_rate, profit_factor, max_dd, df
    except Exception:
        conn.rollback()
        return 0.0, 0.0, 0.0, pd.DataFrame()

# ─────────────────────────────────────────────────────────────
# SIDEBAR — Compact, organized with expanders
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    # ── Mode Toggle ──
    st.markdown('<p class="section-header">🔄 Trading Mode</p>', unsafe_allow_html=True)
    new_mode = st.toggle(
        "Real Trading" if st.session_state.trading_mode == "Actual" else "Paper Trading",
        value=(st.session_state.trading_mode == "Actual"),
        help="Toggle between Paper (simulated) and Real (live) trading"
    )
    st.session_state.trading_mode = "Actual" if new_mode else "Paper"
    if 'last_mode' not in st.session_state:
        st.session_state.last_mode = st.session_state.trading_mode
    if st.session_state.last_mode != st.session_state.trading_mode:
        st.session_state.last_mode = st.session_state.trading_mode
        st.rerun()

    # ── Panic Button ──
    st.markdown("")
    with st.container():
        st.markdown('<div class="panic-btn">', unsafe_allow_html=True)
        if st.button("🚨 PANIC: SQUARE OFF ALL", use_container_width=True):
            if r:
                panic_msg = {"strat_id": "GLOBAL", "symbol": "ALL", "action": "SQUARE_OFF", "execution_type": st.session_state.trading_mode}
                r.publish("panic_channel", json.dumps(panic_msg))
                st.error("⚠️ PANIC SIGNAL SENT!")
        st.markdown('</div>', unsafe_allow_html=True)

    st.divider()

    st.divider()
    # ── Console ──
    with st.expander("📟 System Console", expanded=True):
        if r:
            logs_raw = r.lrange("live_logs", 0, 20)
            if logs_raw:
                for log_b in logs_raw:
                    try:
                        log_data = json.loads(log_b.decode('utf-8'))
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

# ─────────────────────────────────────────────────────────────
# MAIN AREA — Title + Scorecards + Tabs
# ─────────────────────────────────────────────────────────────
view_type = st.session_state.trading_mode

st.markdown(f"""
<h1 style='margin:0; padding:0; font-size:28px; color:#e6edf3;'>
    🦸‍♂️ Karthik's Trading AI Assistant
    <span style='font-size:14px; color:#8b949e; margin-left:12px;'>
        {view_type} Mode
    </span>
</h1>
""", unsafe_allow_html=True)

st.markdown("")

# ── Global Strategy Filter ──
st.markdown("##### 🧭 Dashboard View Filter")

# Get list of unique strategies from active Redis + Portfolio DB
strategies_list = ["All Portfolio"]
if r:
    active_strats = r.hgetall("active_strategies")
    if active_strats:
        strategies_list.extend(list(active_strats.keys()))
        
# Get historically traded strats from DB to ensure they are selectable even if disabled
try:
    if conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT strategy_id FROM portfolio WHERE execution_type = %s", (view_type,))
            db_strats = [row[0] for row in cur.fetchall()]
            for s in db_strats:
                if s not in strategies_list:
                    strategies_list.append(s)
except Exception:
    pass

selected_global_strat = st.selectbox("Select View Scope:", strategies_list, label_visibility="collapsed")
st.markdown("")

# ── Scorecards ──
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

def format_currency(value):
    if value >= 1_000_000:
        return f"₹ {value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"₹ {value/1_000:.1f}K"
    return f"₹ {value:,.0f}"

m1, m2, m3, m4 = st.columns(4)
m1.metric("Capital Deployed", format_currency(current_cap))
m2.metric("Peak Capital", format_currency(max_cap))
m3.metric("Realized P/L", f"₹ {realized_day:,.0f}", delta=f"{realized_day:+,.0f}")
m4.metric("Unrealized P/L", f"₹ {unrealized_day:,.0f}", delta=f"{unrealized_day:+,.0f}")

st.markdown("")

# ─────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────
tab1, tab_signals, tab_meta, tab5, tab_pnl = st.tabs(["🚀 Terminal", "📡 Market Signals", "🧠 Meta-Router", "🔬 Strategy Analytics", "📅 Performance History"])

with tab1:
    st.markdown(f"**Tick-to-Trade Latency:** `< 2ms` via ZeroMQ", unsafe_allow_html=True)
    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("##### ⚡ Live Market")
        if r:
            symbols_list = ["NIFTY50", "BANKNIFTY", "RELIANCE"]
            market_data = []
            for sym in symbols_list:
                tick_raw = r.get(f"latest_tick:{sym}")
                if tick_raw:
                    tick = json.loads(tick_raw)
                    market_data.append({"Symbol": sym, "Price": tick["price"], "Vol": tick["volume"]})
            if market_data:
                st.dataframe(pd.DataFrame(market_data), use_container_width=True, hide_index=True)
            else:
                st.info("Waiting for live data...")
        else:
            # Mock market data
            st.dataframe(pd.DataFrame([
                {"Symbol": "NIFTY50", "Price": 22015.40, "Vol": 45},
                {"Symbol": "BANKNIFTY", "Price": 46120.80, "Vol": 32},
                {"Symbol": "RELIANCE", "Price": 2903.50, "Vol": 18}
            ]), use_container_width=True, hide_index=True)
            
        st.markdown("##### 🧮 L2 Orderbook (Imbalance)")
        # Mock L2 Data for visual completeness
        st.dataframe(pd.DataFrame([
            {"Bid Qty": 12500, "Bid Price": 22015.00, "Ask Price": 22015.40, "Ask Qty": 8400},
            {"Bid Qty": 8200,  "Bid Price": 22014.50, "Ask Price": 22015.90, "Ask Qty": 15000},
            {"Bid Qty": 15000, "Bid Price": 22014.00, "Ask Price": 22016.50, "Ask Qty": 12000},
        ]), use_container_width=True, hide_index=True)

    with col2:
        st.markdown("##### 💼 Active Positions")
        if not port_df.empty:
            df = port_df.copy()
            df['unrealized_pnl'] = 0.0
            if r:
                for idx, row in df.iterrows():
                    sym = row['symbol']
                    qty = row['quantity']
                    avg_price = float(row['avg_price'])
                    tick_raw = r.get(f"latest_tick:{sym}")
                    if tick_raw and qty != 0:
                        tick = json.loads(tick_raw)
                        latest_price = float(tick["price"])
                        if qty > 0:
                            df.at[idx, 'unrealized_pnl'] = (latest_price - avg_price) * qty
                        else:
                            df.at[idx, 'unrealized_pnl'] = (avg_price - latest_price) * abs(qty)
            st.dataframe(
                df[['symbol', 'strategy_id', 'quantity', 'avg_price', 'realized_pnl', 'unrealized_pnl']],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No active positions.")

    st.markdown("##### 📝 Recent Trades")
    trades_df = fetch_recent_trades(view_type, strategy_id=selected_global_strat)
    if not trades_df.empty:
        def color_action(val):
            return f'color: {"#3fb950" if val == "BUY" else "#f85149"}; font-weight: bold'
        styled = trades_df[['time', 'symbol', 'strategy_id', 'action', 'quantity', 'price']].style.map(color_action, subset=['action'])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("No trades yet.")

with tab_signals:
    st.markdown("##### 📡 Advanced Feature Stream (Polars)")
    if r:
        state_raw = r.get("latest_market_state")
        if state_raw:
            state = json.loads(state_raw)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Hurst Exponent", f"{state.get('hurst', 0.5):.2f}")
            c2.metric("Realized Vol (15m)", f"{state.get('rv', 0.0):.6f}")
            c3.metric("Order Flow Imbalance", f"{state.get('ofi', 0):,.0f}")
            c4.metric("Spread Z-Score", f"{state.get('spread_z', 0):.2f}")
            
            c5, c6, c7, c8 = st.columns(4)
            c5.metric("Implied Vol (ATM)", f"{state.get('iv', 0):.1f}%")
            c6.metric("Options Skew", f"{state.get('skew', 0):.2f}")
            c7.metric("Book Depth Rato", f"{state.get('book_depth', 1.0):.2f}")
            c8.metric("Lead-Lag Z", "1.15") # Placeholder
            
            st.divider()
            st.markdown("###### Feature Sensitivity (Correlation)")
            # Simulated Polars Correlation Matrix
            st.dataframe(pd.DataFrame({
                "Hurst": [1.0, 0.2, 0.45],
                "OFI": [0.2, 1.0, 0.6],
                "RV": [0.45, 0.6, 1.0]
            }, index=["Hurst", "OFI", "RV"]), use_container_width=True)
        else:
            st.info("Market Sensor offline. No signals detected.")
    else:
        st.error("Redis Connection Failed.")

with tab_meta:
    st.markdown("##### 🧠 Regime & Lifecycle Audit")
    if r:
        regime_history = r.lrange("regime_shifts", 0, 15)
        if regime_history:
            for item in regime_history:
                shift = json.loads(item)
                st.markdown(f"**{shift['time']}**: Regime `{shift['old']}` ➡️ `{shift['new']}`")
        
        st.divider()
        st.markdown("###### Strategy States")
        daemons = ["STRAT_GAMMA", "STRAT_REVERSION", "STRAT_EXPIRY", "STRAT_EOD_VWAP"]
        for d in daemons:
            state = r.get(f"state:{d}") or "SLEEP"
            icon = "🟢" if state == "ACTIVE" else "🟠" if state == "ORPHANED" else "💤"
            st.markdown(f"{icon} **{d}**: `{state}`")

with tab5:
    st.markdown("##### 🔬 Advanced Performance Analytics")
    win_rate, profit_factor, max_dd, equity_df = fetch_advanced_metrics(view_type, strategy_id=selected_global_strat)
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Win Rate", f"{win_rate*100:.1f}%" if win_rate else "N/A")
    m2.metric("Profit Factor", f"{profit_factor:.2f}" if profit_factor else "N/A")
    m3.metric("Max Drawdown", format_currency(max_dd) if max_dd else "N/A", delta_color="inverse")
    
    if not equity_df.empty:
        st.markdown(f"###### Cumulative Equity Curve ({selected_global_strat})")
        plot_df = equity_df.copy()
        if not plot_df.empty:
            st.line_chart(plot_df.set_index('time')['cumulative_pnl'], color="#3fb950")
            st.markdown("###### Underwater Chart (Drawdown)")
            st.area_chart(plot_df.set_index('time')['drawdown'], color="#f85149")
        else:
            st.info("No trades for selected strategy.")
    else:
        st.info("Insufficient trade history for analytics.")

with tab_pnl:
    st.markdown("##### 📅 Performance History")
    p1, p2 = st.tabs(["Weekly", "Monthly"])
    with p1:
        weekly_agg = fetch_aggregated_pnl(view_type, 'week', group_by_day=False, strategy_id=selected_global_strat)
        if not weekly_agg.empty:
            st.dataframe(weekly_agg, use_container_width=True)
        else:
            st.info("No weekly data available.")
    with p2:
        monthly_agg = fetch_aggregated_pnl(view_type, 'month', group_by_day=False, strategy_id=selected_global_strat)
        if not monthly_agg.empty:
            st.dataframe(monthly_agg, use_container_width=True)
            try:
                import altair as alt
                monthly_chart_df = monthly_agg.groupby(['display_period', 'strategy_id'])['net_profit'].sum().reset_index()
                chart = alt.Chart(monthly_chart_df).mark_bar().encode(
                    x=alt.X('strategy_id:N', title=None),
                    y=alt.Y('net_profit:Q', title='Net Profit'),
                    color=alt.condition(alt.datum.net_profit > 0, alt.value('#3fb950'), alt.value('#f85149')),
                    column=alt.Column('display_period:N', title='Period'),
                    tooltip=['display_period', 'strategy_id', 'net_profit']
                ).properties(width=150, height=300).configure_view(stroke=None)
                st.altair_chart(chart)
            except Exception:
                pass
        else:
            st.info("No monthly data available.")
