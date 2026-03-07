import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import redis
import json
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
        .stFormSubmitButton > button {{
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
    {"symbol": "BANKNIFTY", "strategy_id": "MeanRev_1", "quantity": -50, "avg_price": 46000.0, "realized_pnl": 2320.0, "execution_type": et},
    {"symbol": "RELIANCE", "strategy_id": "OIPulse_1", "quantity": 25, "avg_price": 2900.0, "realized_pnl": 1180.0, "execution_type": et}
])

MOCK_TRADES = lambda: pd.DataFrame([
    {"time": datetime.now(), "symbol": "NIFTY50", "strategy_id": "SMA_1", "action": "BUY", "quantity": 100, "price": 21950.0},
    {"time": datetime.now(), "symbol": "BANKNIFTY", "strategy_id": "MeanRev_1", "action": "SELL", "quantity": 50, "price": 46100.0},
    {"time": datetime.now(), "symbol": "RELIANCE", "strategy_id": "OIPulse_1", "action": "BUY", "quantity": 25, "price": 2895.0}
])

def fetch_portfolio(execution_type="Paper"):
    if not conn:
        return MOCK_PORTFOLIO(execution_type)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM portfolio WHERE execution_type = %s ORDER BY realized_pnl DESC", (execution_type,))
            return pd.DataFrame(cur.fetchall())
    except Exception:
        conn.rollback()
        return MOCK_PORTFOLIO(execution_type)

def fetch_recent_trades(execution_type="Paper", limit=100):
    if not conn:
        return MOCK_TRADES()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM trades 
                WHERE time >= CURRENT_DATE AND execution_type = %s
                ORDER BY time DESC LIMIT %s
            """, (execution_type, limit))
            return pd.DataFrame(cur.fetchall())
    except Exception:
        conn.rollback()
        return MOCK_TRADES()

def fetch_daily_metrics(execution_type="Paper"):
    if not conn:
        return 8350.0, 12500000.0, 3200000.0
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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

def fetch_aggregated_pnl(execution_type="Paper", interval='week', group_by_day=False):
    if not conn:
        return pd.DataFrame()
    if group_by_day:
        date_trunc = "day"
        interval_clause = "time >= date_trunc('week', CURRENT_DATE)" if interval == 'week' else "time >= date_trunc('month', CURRENT_DATE)"
    else:
        date_trunc = "week" if interval == "week" else "month"
        interval_clause = "TRUE"
    query = f"""
    SELECT date_trunc('{date_trunc}', time) AS period, strategy_id, symbol,
        SUM(fees) as total_fees, COUNT(id) as trade_count, SUM(ABS(quantity)) as volume,
        AVG(price) as avg_price,
        SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as net_profit
    FROM trades WHERE {interval_clause} AND execution_type = '{execution_type}'
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

    # ── Deploy Strategy (compact form) ──
    st.markdown('<p class="section-header">🚀 Deploy Strategy</p>', unsafe_allow_html=True)
    strat_type = st.selectbox("Strategy Type", ["SMA", "MeanReversion", "OIPulse", "GammaScalping", "Custom"], label_visibility="collapsed")

    with st.form("strategy_form"):
        # Row 1: ID + Symbols
        c1, c2 = st.columns(2)
        strat_id = c1.text_input("Strategy ID", f"MY_{strat_type}_1")
        symbols = c2.multiselect("Symbols", ["NIFTY50", "BANKNIFTY", "RELIANCE"], default=["NIFTY50"])

        # Row 2: Capital (full width, no Mode radio)
        max_capital = st.number_input("Max Capital ₹", min_value=1000.0, value=1000000.0, step=10000.0)

        # Strategy Parameters (collapsible)
        params = {}
        with st.expander("⚙️ Strategy Parameters", expanded=False):
            if strat_type == "SMA":
                params['period'] = st.number_input("SMA Period", min_value=1, value=10)
            elif strat_type == "MeanReversion":
                p1, p2 = st.columns(2)
                params['period'] = p1.number_input("Window", min_value=1, value=5)
                params['threshold'] = p2.number_input("Threshold", value=0.001, format="%.4f")
            elif strat_type == "OIPulse":
                params['oi_threshold'] = st.number_input("OI Surge %", value=2.0, step=0.1)
            elif strat_type == "GammaScalping":
                g1, g2 = st.columns(2)
                params['strike'] = g1.number_input("Strike", value=22000.0, step=50.0)
                params['expiry_days'] = g2.number_input("Expiry Days", min_value=1.0, value=30.0)
                g3, g4 = st.columns(2)
                params['iv'] = g3.number_input("IV", value=0.15, step=0.01, format="%.2f")
                params['r'] = g4.number_input("R-free", value=0.07, step=0.01, format="%.2f")
                params['hedge_threshold'] = st.number_input("Hedge Threshold", value=0.10, step=0.01)
            elif strat_type == "Custom":
                code_str = st.text_area("Python Code", "def on_tick(symbol, history, position, price):\n    return None", height=120)
                params['code'] = code_str

        # Scheduling — multiple time slots per day
        with st.expander("🕒 Schedule", expanded=False):
            exec_days = st.multiselect("Active Days", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], default=["Mon", "Tue", "Wed", "Thu", "Fri"])
            day_map = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}

            num_slots = st.number_input("Number of time slots", min_value=1, max_value=5, value=1, step=1)
            time_slots = []
            for i in range(int(num_slots)):
                st.caption(f"Slot {i+1}")
                s1, s2 = st.columns(2)
                default_starts = ["09:15", "14:00", "10:30", "15:00", "11:00"]
                default_ends   = ["11:30", "15:30", "12:00", "15:30", "12:30"]
                slot_start = s1.time_input(f"Start #{i+1}", value=datetime.strptime(default_starts[i], "%H:%M").time(), key=f"slot_start_{i}")
                slot_end   = s2.time_input(f"End #{i+1}", value=datetime.strptime(default_ends[i], "%H:%M").time(), key=f"slot_end_{i}")
                time_slots.append({"start": slot_start.strftime("%H:%M"), "end": slot_end.strftime("%H:%M")})

        enabled = st.checkbox("Strategy Enabled", value=True)
        submitted = st.form_submit_button("🚀 Deploy Strategy", use_container_width=True)

        if submitted:
            config = {
                "id": strat_id, "type": strat_type, "symbols": symbols,
                "max_capital": max_capital, "enabled": enabled,
                "execution_type": st.session_state.trading_mode,
                "schedule": {
                    "days": [day_map.get(d, d) for d in exec_days],
                    "slots": time_slots
                },
                **params
            }
            if r:
                r.hset("active_strategies", strat_id, json.dumps(config))
                st.success(f"✅ {strat_id} deployed!")
            else:
                st.info(f"✅ {strat_id} configured (Redis offline)")

    # ── Active Strategies ──
    with st.expander("📋 Active Strategies", expanded=False):
        if r:
            active_strats = r.hgetall("active_strategies")
            if active_strats:
                for strat, config_raw in active_strats.items():
                    config = json.loads(config_raw)
                    is_active = config.get('enabled', True)
                    icon = "🟢" if is_active else "🔴"
                    st.markdown(f"{icon} **{strat}** · `{config['type']}`")
                    c1, c2 = st.columns(2)
                    if c1.button("🗑️", key=f"del_{strat}", help="Delete"):
                        r.hdel("active_strategies", strat)
                        st.rerun()
                    if c2.button("🛑" if is_active else "▶️", key=f"tog_{strat}", help="Toggle"):
                        config['enabled'] = not is_active
                        r.hset("active_strategies", strat, json.dumps(config))
                        st.rerun()
            else:
                st.caption("No strategies deployed.")
        else:
            st.caption("Redis offline.")

    # ── Risk Controls ──
    with st.expander("🛡️ Risk Controls", expanded=False):
        with st.form("risk_form"):
            stop_day_loss = st.number_input("Stop Day Loss ₹", min_value=0.0, value=50000.0, step=5000.0)
            if st.form_submit_button("Update Risk", use_container_width=True):
                risk_config = {"stop_day_loss": stop_day_loss}
                if r:
                    r.set("global_risk_settings", json.dumps(risk_config))
                    st.success("Risk settings updated!")

    # ── System Console ──
    with st.expander("📟 Console", expanded=False):
        if r:
            logs_raw = r.lrange("live_logs", 0, 10)
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

# ── Scorecards ──
realized_day, vol_day, max_cap = fetch_daily_metrics(view_type)
port_df = fetch_portfolio(view_type)
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

m1, m2, m3, m4 = st.columns(4)
m1.metric("Capital Deployed", f"₹ {current_cap:,.0f}")
m2.metric("Peak Capital", f"₹ {max_cap:,.0f}")
m3.metric("Realized P/L", f"₹ {realized_day:,.0f}", delta=f"{realized_day:+,.0f}")
m4.metric("Unrealized P/L", f"₹ {unrealized_day:,.0f}", delta=f"{unrealized_day:+,.0f}")

st.markdown("")

# ─────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(["📈 Terminal", "🛡️ Risk Monitor", "📅 Weekly P&L", "📆 Monthly P&L"])

with tab1:
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
    trades_df = fetch_recent_trades(view_type)
    if not trades_df.empty:
        def color_action(val):
            return f'color: {"#3fb950" if val == "BUY" else "#f85149"}; font-weight: bold'
        styled = trades_df[['time', 'symbol', 'strategy_id', 'action', 'quantity', 'price']].style.map(color_action, subset=['action'])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("No trades yet.")

with tab2:
    st.markdown("##### ⚡ Greeks & Risk")
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Portfolio Delta", "-0.45", delta="0.02")
    g2.metric("Portfolio Gamma", "12.8")
    g3.metric("Portfolio Vega", "₹ 4,250")
    g4.metric("Portfolio Theta", "-₹ 1,200", delta_color="inverse")

    st.markdown("")
    if not port_df.empty:
        st.markdown("##### 📊 Capital Allocation")
        cap_data = port_df.copy()
        cap_data['abs_value'] = abs(cap_data['quantity'] * cap_data['avg_price'].astype(float))
        try:
            import altair as alt
            chart = alt.Chart(cap_data).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X('abs_value:Q', title="Capital (₹)"),
                y=alt.Y('symbol:N', sort='-x', title=""),
                color=alt.Color('abs_value:Q', scale=alt.Scale(scheme='viridis'), legend=None),
                tooltip=['symbol', 'abs_value']
            ).properties(height=200).configure_view(stroke=None).configure_axis(
                labelColor='#8b949e', titleColor='#8b949e'
            )
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            st.bar_chart(cap_data.set_index('symbol')['abs_value'])

with tab3:
    st.markdown("##### Weekly Performance")
    weekly_agg = fetch_aggregated_pnl(view_type, 'week', group_by_day=False)
    weekly_daily = fetch_aggregated_pnl(view_type, 'week', group_by_day=True)
    if not weekly_agg.empty:
        import altair as alt
        weekly_chart_df = weekly_agg.groupby(['display_period', 'strategy_id'])['net_profit'].sum().reset_index()
        chart = alt.Chart(weekly_chart_df).mark_bar().encode(
            x=alt.X('strategy_id:N', title=None),
            y=alt.Y('net_profit:Q', title='Net Profit'),
            color=alt.condition(alt.datum.net_profit > 0, alt.value('#3fb950'), alt.value('#f85149')),
            column=alt.Column('display_period:N', title='Period'),
            tooltip=['display_period', 'strategy_id', 'net_profit']
        ).properties(width=150, height=300).configure_view(stroke=None)
        st.altair_chart(chart)
        st.dataframe(weekly_daily.style.format({"total_fees": "{:.2f}", "avg_price": "{:.2f}", "net_profit": "{:.2f}"}), use_container_width=True, hide_index=True)
    else:
        st.info("No weekly data available.")

with tab4:
    st.markdown("##### Monthly Performance")
    monthly_agg = fetch_aggregated_pnl(view_type, 'month', group_by_day=False)
    monthly_daily = fetch_aggregated_pnl(view_type, 'month', group_by_day=True)
    if not monthly_agg.empty:
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
        st.dataframe(monthly_daily.style.format({"total_fees": "{:.2f}", "avg_price": "{:.2f}", "net_profit": "{:.2f}"}), use_container_width=True, hide_index=True)
    else:
        st.info("No monthly data available.")
