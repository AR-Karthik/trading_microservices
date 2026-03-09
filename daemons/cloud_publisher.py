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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("CloudPublisher")

IST = timezone(timedelta(hours=5, minutes=30))
EOD_SNAPSHOT_HH, EOD_SNAPSHOT_MM = 15, 35
HEARTBEAT_INTERVAL_S = 10


class CloudPublisher:
    def __init__(self):
        self.redis = aioredis.from_url(
            f"redis://{os.getenv('REDIS_HOST', 'localhost')}:6379",
            decode_responses=True
        )
        self.gcs_bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
        self.firestore_db = None
        self.gcs_client = None
        self._eod_done_today = False

    async def _init_cloud_clients(self):
        """Lazily initialize Google Cloud clients."""
        try:
            from google.cloud import firestore, storage
            self.firestore_db = firestore.AsyncClient()
            self.gcs_client = storage.Client()
            logger.info("Google Cloud clients (Firestore + GCS) initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize cloud clients: {e}")
            logger.warning("Cloud publishing will be disabled. Running in local-only mode.")

    async def _heartbeat_loop(self):
        """
        Every 10 seconds, read key metrics from Redis and push to Firestore.
        The Cloud Run dashboard reads from this Firestore document.
        """
        while True:
            try:
                if not self.firestore_db:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                    continue

                # Gather state from Redis
                pipe = self.redis.pipeline()
                pipe.get("HMM_REGIME")
                pipe.get("DAILY_REALIZED_PNL_PAPER")
                pipe.get("DAILY_REALIZED_PNL_LIVE")
                pipe.get("CURRENT_MARGIN_UTILIZED")
                pipe.get("GLOBAL_CAPITAL_LIMIT")
                pipe.get("ACTIVE_LOTS_COUNT")
                pipe.get("COMPOSITE_ALPHA")
                pipe.get("STOP_DAY_LOSS_BREACHED_PAPER")
                pipe.get("STOP_DAY_LOSS_BREACHED_LIVE")
                results = await pipe.execute()

                state = {
                    "regime": results[0] or "UNKNOWN",
                    "daily_pnl_paper": float(results[1] or 0),
                    "daily_pnl_live": float(results[2] or 0),
                    "margin_utilized": float(results[3] or 0),
                    "capital_limit": float(results[4] or 0),
                    "active_lots": int(results[5] or 0),
                    "composite_alpha": float(results[6] or 0),
                    "stop_day_loss_breached_paper": results[7] == "True",
                    "stop_day_loss_breached_live": results[8] == "True",
                    "is_vm_running": True,
                    "last_heartbeat": datetime.now(IST).isoformat(),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                }

                # Write to Firestore
                doc_ref = self.firestore_db.collection("trading_state").document("live")
                await doc_ref.set(state, merge=True)

            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

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

                doc_ref = self.firestore_db.collection("trading_commands").document("active")
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
                except Exception as e:
                    logger.error(f"Tick parquet upload failed: {e}")

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
            rows = await conn.fetch(
                "SELECT * FROM market_history WHERE time >= $1::date AND time < ($1::date + interval '1 day')",
                snapshot_time
            )
            await conn.close()

            if not rows:
                logger.info("No tick data found for today. Skipping parquet export.")
                return

            df = pd.DataFrame([dict(r) for r in rows])
            parquet_path = f"/tmp/ticks_{today_str}.parquet"
            df.to_parquet(parquet_path, index=False)

            bucket = self.gcs_client.bucket(self.gcs_bucket)
            blob = bucket.blob(f"tick_history/ticks_{today_str}.parquet")
            blob.upload_from_filename(parquet_path)
            logger.info(f"✅ Tick data uploaded to GCS: gs://{self.gcs_bucket}/tick_history/ticks_{today_str}.parquet")

            os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Parquet export error: {e}")

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
