import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import redis
import json
from datetime import datetime
import time

st.set_page_config(page_title="Antigravity Trading Engine", layout="wide", initial_sidebar_state="collapsed")

# Database Connections
@st.cache_resource
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname="trading_db",
            user="trading_user",
            password="trading_pass",
            host="localhost",
            port="5432"
        )
        return conn
    except Exception as e:
        st.error(f"Failed to connect to TimescaleDB: {e}")
        return None

@st.cache_resource
def get_redis_client():
    return redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

conn = get_db_connection()
r = get_redis_client()

def fetch_portfolio():
    if not conn: return pd.DataFrame()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM portfolio ORDER BY realized_pnl DESC")
        data = cur.fetchall()
        return pd.DataFrame(data)

def fetch_recent_trades(limit=50):
    if not conn: return pd.DataFrame()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM trades ORDER BY time DESC LIMIT %s", (limit,))
        data = cur.fetchall()
        return pd.DataFrame(data)

def fetch_aggregated_pnl(interval='week'):
    """Fetches P&L grouped by week or month."""
    if not conn: return pd.DataFrame()
    
    date_trunc = "week" if interval == "week" else "month"
    
    query = f"""
    SELECT 
        date_trunc('{date_trunc}', time) AS period,
        symbol,
        SUM(fees) as total_fees,
        COUNT(id) as trade_count
    FROM trades
    GROUP BY date_trunc('{date_trunc}', time), symbol
    ORDER BY period DESC
    """
    
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        data = cur.fetchall()
        return pd.DataFrame(data)

st.title("🚀 Antigravity Trading Monitor")

col1, col2, col3 = st.columns(3)

# Metrics caching
if 'last_refresh' not in st.session_state:
    st.session_state.last_refresh = time.time()

# 1. Live Market Data via Redis
with col1:
    st.subheader("⚡ Live Market Data")
    market_container = st.empty()
    
    def render_market_data():
        if r:
            symbols = ["NIFTY50", "BANKNIFTY", "RELIANCE"]
            data = []
            for sym in symbols:
                tick_raw = r.get(f"latest_tick:{sym}")
                if tick_raw:
                    tick = json.loads(tick_raw)
                    data.append({"Symbol": sym, "Price": tick["price"], "Volume": tick["volume"]})
            if data:
                market_container.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)
            else:
                market_container.info("Waiting for data via Redis...")

# 2. Portfolio State View
with col2:
    st.subheader("💼 Active Positions")
    portfolio_container = st.empty()
    
    def render_portfolio():
        df = fetch_portfolio()
        if not df.empty:
            df['unrealized_pnl'] = 0.0 # Calculate based on latest tick
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
            
            # Format display
            df_display = df[['symbol', 'quantity', 'avg_price', 'realized_pnl', 'unrealized_pnl']].copy()
            df_display = df_display.style.format({
                'avg_price': "{:.2f}",
                'realized_pnl': "{:.2f}",
                'unrealized_pnl': "{:.2f}"
            }).applymap(lambda x: 'color: green' if x > 0 else ('color: red' if x < 0 else ''), subset=['realized_pnl', 'unrealized_pnl'])
            
            portfolio_container.dataframe(df_display, use_container_width=True, hide_index=True)
        else:
            portfolio_container.info("No active positions.")

# 3. Overall Metrics
with col3:
    st.subheader("📈 Performance")
    metrics_container = st.empty()
    def render_metrics():
        df = fetch_portfolio()
        total_realized = float(df['realized_pnl'].sum()) if not df.empty else 0.0
        # Calculate unrealized too... (simplified for this demo)
        metrics_container.metric("Total Realized P&L", f"₹ {total_realized:,.2f}")

st.divider()

# Advanced Analytics Tabs
tab1, tab2, tab3 = st.tabs(["📝 Recent Trades", "📅 Weekly P&L", "📆 Monthly P&L"])

with tab1:
    trades_df = fetch_recent_trades()
    if not trades_df.empty:
        # Style actions
        def color_action(val):
            color = 'green' if val == 'BUY' else 'red'
            return f'color: {color}; font-weight: bold'
            
        st.dataframe(
            trades_df[['time', 'symbol', 'action', 'quantity', 'price', 'fees']].style.map(color_action, subset=['action']).format({"price": "{:.2f}", "fees": "{:.2f}"}),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No trades executed yet.")

with tab2:
    st.markdown("#### Weekly Profit & Loss Summary")
    weekly_df = fetch_aggregated_pnl('week')
    if not weekly_df.empty:
        st.bar_chart(data=weekly_df, x="period", y="total_fees", color="symbol", use_container_width=True)
        st.dataframe(weekly_df, use_container_width=True, hide_index=True)
    else:
        st.info("Insufficient data for weekly aggregation.")

with tab3:
    st.markdown("#### Monthly Profit & Loss Summary")
    monthly_df = fetch_aggregated_pnl('month')
    if not monthly_df.empty:
        st.bar_chart(data=monthly_df, x="period", y="total_fees", color="symbol", use_container_width=True)
        st.dataframe(monthly_df, use_container_width=True, hide_index=True)
    else:
        st.info("Insufficient data for monthly aggregation.")

# Force Streamlit to rerun every second to show live updates smoothly
time.sleep(1)
st.rerun()
