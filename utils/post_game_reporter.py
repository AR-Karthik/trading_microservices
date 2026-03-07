import asyncio
import os
import json
import logging
import asyncpg
from datetime import datetime
import requests # For Telegram fallback or simple webhooks

logger = logging.getLogger(__name__)

async def generate_daily_report():
    """
    Fetches P&L stats for both Live and Shadow strategies.
    Sends an automated summary via Telegram/Webhook.
    --- Recommendation 9: Automated Performance Dashboard ---
    """
    db_uri = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/trading_db")
    
    try:
        conn = await asyncpg.connect(db_uri)
    except Exception as e:
        logger.error(f"Failed to connect to DB for reporting: {e}")
        return

    today = datetime.now().date()
    
    # Query performance metrics
    query = """
        SELECT 
            execution_type, 
            SUM(realized_pnl) as total_pnl,
            COUNT(*) as trade_count,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as win_count
        FROM trades
        WHERE DATE(execution_time) = $1
        GROUP BY execution_type
    """
    
    rows = await conn.fetch(query, today)
    await conn.close()
    
    if not rows:
        report_text = f"📊 *Daily Trading Report ({today})*\nNo trades executed today."
    else:
        report_text = f"📊 *Daily Trading Report ({today})*\n\n"
        for row in rows:
            win_rate = (row['win_count'] / row['trade_count'] * 100) if row['trade_count'] > 0 else 0
            report_text += (
                f"*{row['execution_type']} Mode:*\n"
                f"- P&L: {'🟢' if row['total_pnl'] >= 0 else '🔴'} ₹{row['total_pnl']:.2f}\n"
                f"- Trades: {row['trade_count']}\n"
                f"- Win Rate: {win_rate:.1f}%\n\n"
            )

    # Simplified Telegram Alert (if token provided)
    tele_token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if tele_token and chat_id:
        url = f"https://api.telegram.org/bot{tele_token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": report_text, "parse_mode": "Markdown"})
        logger.info("Daily report sent to Telegram.")
    else:
        logger.info(f"Daily Report (console fallback):\n{report_text}")

if __name__ == "__main__":
    asyncio.run(generate_daily_report())
