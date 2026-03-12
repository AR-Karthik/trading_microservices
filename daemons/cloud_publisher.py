"""
Cloud Publisher Daemon — Project K.A.R.T.H.I.K. V5.4
=====================================================
Decouples the dashboard from the trading VM by publishing state to cloud services.

Responsibilities:
1. Heartbeat (Every 10s): Push current P&L, Active Lots, Regime, and Greeks
   to Google Firestore for real-time Cloud Run dashboard consumption.
2. EOD Snapshot (15:35 IST): Export full day's tick log as .parquet to GCS
   and upload the latest HMM model .pkl to GCS.
3. Command Watcher: Listen to Firestore for remote commands (e.g., PANIC_BUTTON).
"""
import os
import sys
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import redis.asyncio as aioredis
from dotenv import load_dotenv
import httpx

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("CloudPublisher")

IST = timezone(timedelta(hours=5, minutes=30))
EOD_SNAPSHOT_HH, EOD_SNAPSHOT_MM = 15, 35
HEARTBEAT_INTERVAL_S = 60  # Reduced frequency for hybrid pipeline


class CloudPublisher:
    def __init__(self):
        self.redis = aioredis.from_url(
            f"redis://{os.getenv('REDIS_HOST', 'localhost')}:6379",
            decode_responses=True
        )
        self.gcs_bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
        self.firestore_db = None
        self.gcs_client = None
        self.external_ip = None
        self._eod_done_today = False

    async def _init_cloud_clients(self):
        """Lazily initialize Google Cloud clients."""
        try:
            from google.cloud import firestore, storage
            self._firestore_module = firestore
            self.firestore_db = firestore.AsyncClient()
            self.gcs_client = storage.Client()
            logger.info("Google Cloud clients (Firestore + GCS) initialized.")
            
            # Fetch External IP for Hybrid Proxy logic
            self.external_ip = await self._fetch_external_ip()
            logger.info(f"Detected External IP: {self.external_ip}")
        except Exception as e:
            logger.error(f"Failed to initialize cloud clients: {e}")
            logger.warning("Cloud publishing will be disabled. Running in local-only mode.")

    async def _fetch_external_ip(self):
        """Fetch VM's public IP from GCP metadata server."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip",
                    headers={"Metadata-Flavor": "Google"},
                    timeout=5.0
                )
                if resp.status_code == 200:
                    return resp.text.strip()
        except Exception as e:
            logger.error(f"Failed to fetch external IP: {e}")
        return "127.0.0.1" # Fallback

    async def _heartbeat_loop(self):
        """
        Every 60 seconds, update VM status and IP in Firestore.
        Real-time metrics are now pulled directly from the VM API by the dashboard.
        """
        while True:
            try:
                if not self.firestore_db:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                    continue

                # Fetch live metrics from Redis for fallback visibility
                alpha = await self.redis.get("COMPOSITE_ALPHA") or "0.0"
                regime = await self.redis.get("HMM_REGIME") or "UNKNOWN"
                
                # Minimal state for discovery
                state = {
                    "vm_public_ip": self.external_ip,
                    "status": "ONLINE",
                    "last_heartbeat": datetime.now(IST).isoformat(),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "system_health": "HEALTHY",
                    "live_alpha": float(alpha),
                    "live_regime": regime
                }

                # Push to Firestore Metadata (Discovery)
                doc_ref = self.firestore_db.collection("system").document("metadata")
                await doc_ref.set(state, merge=True)
                
                # Also sync current operating config for visibility when VM is OFF
                await self._sync_config_to_firestore()

            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _sync_config_to_firestore(self):
        """Syncs the latest trading configuration from Redis to Firestore."""
        try:
            if not self.firestore_db or not self._firestore_module:
                logger.warning("Firestore DB or module not initialized. Skipping config sync.")
                return

            # Match keys used in dashboard/api/main.py
            paper_cap = await self.redis.get("PAPER_CAPITAL_LIMIT") or "0"
            live_cap = await self.redis.get("LIVE_CAPITAL_LIMIT") or "0"
            regime = await self.redis.get("CONFIG:REGIME_ENGINE") or "UNKNOWN"
            risk_paper = await self.redis.get("CONFIG:MAX_RISK_PER_TRADE_PAPER") or "0"
            risk_live = await self.redis.get("CONFIG:MAX_RISK_PER_TRADE_LIVE") or "0"

            config_data = {
                "paper_capital_limit": float(paper_cap),
                "live_capital_limit": float(live_cap),
                "regime_engine": regime,
                "max_risk_paper": float(risk_paper),
                "max_risk_live": float(risk_live),
                "updated_at": self._firestore_module.SERVER_TIMESTAMP
            }
            self.firestore_db.collection("system").document("config").set(config_data, merge=True)
            # logger.info("Config synced to Firestore.")
        except Exception as e:
            logger.error(f"Config sync failed: {e}")

    async def _command_watcher(self):
        """
        Watch Firestore for remote commands from the Cloud Run dashboard.
        Supports: PANIC_BUTTON, PAUSE_TRADING, RESUME_TRADING.
        """
        while True:
            try:
                if not self.firestore_db:
                    await asyncio.sleep(5)
                    continue

                if not self.firestore_db:
                    logger.error("Firestore DB not initialized. Skipping command check.")
                    return

                doc_ref = self.firestore_db.collection("commands").document("latest")
                doc = await doc_ref.get()

                if doc.exists:
                    data = doc.to_dict()

                    # PANIC BUTTON — Remote Square-Off
                    if data.get("PANIC_BUTTON"):
                        logger.critical("🚨 PANIC BUTTON received from Cloud Dashboard!")
                        await self.redis.publish("panic_channel", "SQUARE_OFF_ALL")
                        # Clear the command after execution
                        await doc_ref.update({"PANIC_BUTTON": False, "panic_ack": True})

                    # PAUSE TRADING — Disable new entries
                    if data.get("PAUSE_TRADING"):
                        logger.warning("⏸️ PAUSE_TRADING command received.")
                        await self.redis.set("TRADING_PAUSED", "True")
                        await doc_ref.update({"PAUSE_TRADING": False, "pause_ack": True})

                    # RESUME TRADING
                    if data.get("RESUME_TRADING"):
                        logger.info("▶️ RESUME_TRADING command received.")
                        await self.redis.delete("TRADING_PAUSED")
                        await doc_ref.update({"RESUME_TRADING": False, "resume_ack": True})

            except Exception as e:
                logger.error(f"Command watcher error: {e}")

            await asyncio.sleep(3)  # Poll every 3 seconds

    async def _eod_snapshot(self):
        """
        At 15:35 IST, export today's tick data as .parquet to GCS
        and upload the latest HMM model.
        """
        while True:
            now = datetime.now(IST)
            if now.hour == EOD_SNAPSHOT_HH and now.minute == EOD_SNAPSHOT_MM and not self._eod_done_today:
                logger.info("📦 EOD Snapshot triggered (15:35 IST).")
                self._eod_done_today = True

                try:
                    await self._upload_tick_parquet(now)
                    await self._upload_trades_parquet(now)
                except Exception as e:
                    logger.error(f"EOD Parquet export failed: {e}")

                try:
                    await self._upload_hmm_model()
                except Exception as e:
                    logger.error(f"HMM model upload failed: {e}")

                try:
                    await self._mark_vm_shutdown_pending()
                except Exception as e:
                    logger.error(f"VM shutdown mark failed: {e}")

            # Reset flag at midnight
            if now.hour == 0 and now.minute == 0:
                self._eod_done_today = False

            await asyncio.sleep(30)

    async def _upload_tick_parquet(self, snapshot_time: datetime):
        """Export today's ticks from TimescaleDB to .parquet and upload to GCS."""
        if not self.gcs_client:
            return

        try:
            import asyncpg
            import pandas as pd

            conn = await asyncpg.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=5432,
                user="trading_user",
                password="trading_pass",
                database="trading_db"
            )

            today_str = snapshot_time.strftime("%Y-%m-%d")
            # Enforce fixed column selection and order for BigQuery compatibility
            rows = await conn.fetch("""
                SELECT time, symbol, price, log_ofi_zscore, cvd, vpin, basis_zscore, vol_term_ratio 
                FROM market_history 
                WHERE time >= $1::date AND time < ($1::date + interval '1 day')
            """, snapshot_time)
            await conn.close()

            if not rows:
                logger.info("No tick data found for today. Skipping parquet export.")
                return

            df = pd.DataFrame([dict(r) for r in rows])
            
            # Ensure types are correct for BigQuery
            df['time'] = pd.to_datetime(df['time'])
            df['symbol'] = df['symbol'].astype(str)
            for col in ['price', 'log_ofi_zscore', 'cvd', 'vpin', 'basis_zscore', 'vol_term_ratio']:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

            parquet_path = f"/tmp/ticks_{today_str}.parquet"
            df.to_parquet(parquet_path, index=False)

            if not self.gcs_client:
                logger.error("GCS client not initialized. Skipping tick export.")
                return

            bucket = self.gcs_client.bucket(self.gcs_bucket)
            blob = bucket.blob(f"tick_history/ticks_{today_str}.parquet")
            blob.upload_from_filename(parquet_path)
            logger.info(f"✅ Tick data uploaded to GCS: gs://{self.gcs_bucket}/tick_history/ticks_{today_str}.parquet")

            os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Tick Parquet export error: {e}")

    async def _upload_trades_parquet(self, snapshot_time: datetime):
        """Export today's trades from TimescaleDB to .parquet and upload to GCS."""
        if not self.gcs_client:
            return

        try:
            import asyncpg
            import pandas as pd

            conn = await asyncpg.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=5432,
                user="trading_user",
                password="trading_pass",
                database="trading_db"
            )

            today_str = snapshot_time.strftime("%Y-%m-%d")
            rows = await conn.fetch("""
                SELECT id::text, time, symbol, action, quantity, price, fees, strategy_id, execution_type 
                FROM trades 
                WHERE time >= $1::date AND time < ($1::date + interval '1 day')
            """, snapshot_time)
            await conn.close()

            if not rows:
                logger.info("No trades found for today.")
                return

            df = pd.DataFrame([dict(r) for r in rows])
            df['time'] = pd.to_datetime(df['time'])
            
            parquet_path = f"/tmp/trades_{today_str}.parquet"
            df.to_parquet(parquet_path, index=False)

            bucket = self.gcs_client.bucket(self.gcs_bucket)
            blob = bucket.blob(f"trade_history/trades_{today_str}.parquet")
            blob.upload_from_filename(parquet_path)
            logger.info(f"✅ Trade data uploaded to GCS: gs://{self.gcs_bucket}/trade_history/trades_{today_str}.parquet")

            os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Trade Parquet export error: {e}")

    async def _upload_hmm_model(self):
        """Upload the latest HMM model to GCS for persistence."""
        if not self.gcs_client:
            return

        model_path = "data/models/hmm_v_latest.pkl"
        if not os.path.exists(model_path):
            model_path = "data/models/hmm_generic.pkl"

        if not os.path.exists(model_path):
            logger.warning("No HMM model found to upload.")
            return

        bucket = self.gcs_client.bucket(self.gcs_bucket)
        blob = bucket.blob(os.path.basename(model_path))
        blob.upload_from_filename(model_path)
        logger.info(f"✅ HMM model uploaded to GCS: gs://{self.gcs_bucket}/{os.path.basename(model_path)}")

    async def _mark_vm_shutdown_pending(self):
        """Update Firestore to indicate EOD processing is complete."""
        if not self.firestore_db:
            return

        doc_ref = self.firestore_db.collection("trading_state").document("live")
        await doc_ref.update({
            "eod_snapshot_complete": True,
            "eod_snapshot_time": datetime.now(IST).isoformat(),
        })
        logger.info("Firestore marked: EOD snapshot complete.")

    async def run(self):
        """Main entry point."""
        logger.info("=" * 60)
        logger.info("  Project K.A.R.T.H.I.K. — Cloud Publisher Starting")
        logger.info("=" * 60)

        await self._init_cloud_clients()

        await asyncio.gather(
            self._heartbeat_loop(),
            self._command_watcher(),
            self._eod_snapshot(),
        )


if __name__ == "__main__":
    publisher = CloudPublisher()
    asyncio.run(publisher.run())
