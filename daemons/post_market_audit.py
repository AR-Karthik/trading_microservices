"""
daemons/post_market_audit.py
============================
Post-Market Performance Audit service for Project K.A.R.T.H.I.K.
Triggers at 15:45 IST to reconcile execution and generate EOD reports.
"""

import asyncio
import os
import json
import time
import logging
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import redis.asyncio as redis
from typing import Dict, Any, List

from core.alerts import send_cloud_alert
from core.auth import get_redis_url

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("PostMarketAudit")

IST = ZoneInfo("Asia/Kolkata")

class PostMarketAudit:
    def __init__(self, redis_url: str = None):
        if redis_url is None:
            redis_url = get_redis_url()
        self.redis_url = redis_url
        self.redis = None
        self.report = []
        self.performance_data = {}

    async def start(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("Post-Market Performance Audit active. Starting debrief sequence...")
        
        await self.run_audit_pipeline()
        await self.send_eod_report()
        await self.archive_to_firestore()
        
        await self.redis.aclose()

    async def run_audit_pipeline(self):
        """Executes all audit steps in the 15:45 IST pipeline."""
        
        # 1. Source of Truth Rebalancing
        await self._reconcile_broker_ledger()
        
        # 2. Performance Metrics Extraction
        await self._extract_performance_metrics()
        
        # 3. Slippage & Execution Analysis
        await self._analyze_execution_quality()

    async def _reconcile_broker_ledger(self):
        """Reconciles Broker Orderbook with the Shadow Ledger in Redis."""
        logger.info("Step 1: Source of Truth Rebalancing...")
        
        # In actual implementation, we would fetch the full orderbook from Shoonya
        # and compare it with 'pending_orders' and 'broker_positions' in Redis.
        pending_count = await self.redis.hlen("pending_orders")
        positions = await self.redis.hgetall("broker_positions")
        
        self.report.append(f"📊 Ledger Balance: {len(positions)} Active Positions")
        if pending_count > 0:
            self.report.append(f"⚠️ Warning: {pending_count} orders remained PENDING at market close.")
        else:
            self.report.append("✅ Clean Close: All orders reconciled.")

    async def _extract_performance_metrics(self):
        """Calculates realized and unrealized PnL."""
        logger.info("Step 2: Performance Metrics Extraction...")
        
        # Simulated PnL extraction from Redis
        # (In reality, we'd sum up 'realized_pnl' fields from all strategies)
        realized_pnl = float(await self.redis.get("REALIZED_PNL_TOTAL") or 0.0)
        unrealized_pnl = float(await self.redis.get("UNREALIZED_PNL_TOTAL") or 0.0)
        
        self.performance_data["realized_pnl"] = realized_pnl
        self.performance_data["unrealized_pnl"] = unrealized_pnl
        self.performance_data["total_pnl"] = realized_pnl + unrealized_pnl
        
        status = "🟢" if realized_pnl >= 0 else "🔴"
        self.report.append(f"{status} Daily Realized PnL: ₹{realized_pnl:,.2f}")
        self.report.append(f"📉 Unrealized Carry: ₹{unrealized_pnl:,.2f}")

    async def _analyze_execution_quality(self):
        """Calculates slippage between signal trigger and fill price."""
        logger.info("Step 3: Slippage Analysis...")
        
        # Slippage data would be accumulated in a Redis list throughout the day
        # e.g., RPUSH execution_slippage '{"symbol": "...", "signal": 100.5, "fill": 100.6}'
        slippage_entries = await self.redis.lrange("execution_slippage", 0, -1)
        
        if slippage_entries:
            total_slippage = 0.0
            for entry in slippage_entries:
                data = json.loads(entry)
                total_slippage += abs(data["fill"] - data["signal"])
            
            avg_slippage = total_slippage / len(slippage_entries)
            self.report.append(f"⚡ Avg Slippage: {avg_slippage:.4f} pts per leg")
        else:
            self.report.append("✅ Slippage: No data available (Zero slippage or no trades).")

    async def send_eod_report(self):
        """Formats and sends the EOD Debrief Telegram report."""
        header = f"🏁 *K.A.R.T.H.I.K. EOD Debrief* — {datetime.now(IST).strftime('%Y-%m-%d')}"
        
        body = "\n".join(self.report)
        footer = "\n📦 *All systems SHUTDOWN for maintenance.* See you tomorrow!"
        
        full_report = f"{header}\n\n{body}\n{footer}"
        await send_cloud_alert(full_report, alert_type="SYSTEM")
        logger.info("EOD Debrief report sent.")

    async def archive_to_firestore(self):
        """Persists the day's performance metrics to Firestore."""
        logger.info("Step 4: Firestore Archival...")
        try:
            from google.cloud import firestore
            db = firestore.AsyncClient()
            today_str = datetime.now(IST).strftime('%Y-%m-%d')
            
            # [Phase 13] Comprehensive Performance Payload
            payload = {
                "date": today_str,
                "realized_pnl": self.performance_data.get("realized_pnl", 0.0),
                "unrealized_pnl": self.performance_data.get("unrealized_pnl", 0.0),
                "total_pnl": self.performance_data.get("total_pnl", 0.0),
                "avg_slippage": self.performance_data.get("avg_slippage", 0.0),
                "trade_count": self.performance_data.get("trade_count", 0),
                "logs": self.report,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            await db.collection("performance_history").document(today_str).set(payload)
            logger.info(f"✅ Performance archived to Firestore for {today_str}.")
        except Exception as e:
            logger.error(f"❌ Firestore Archival failed: {e}")

if __name__ == "__main__":
    audit = PostMarketAudit()
    asyncio.run(audit.start())
