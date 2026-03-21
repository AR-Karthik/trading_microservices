"""
Cloud Dashboard Publisher
Transmits real-time operational metrics and P&L snapshots to Google Cloud Firestore.
Handles EOD data archival to GCS and remote command monitoring.
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
HEARTBEAT_INTERVAL_S = 5  # Wave 2: Standardizing on High-Frequency Heartbeats


class CloudPublisher:
    def __init__(self):
        from core.auth import get_redis_url
        self.redis = aioredis.from_url(
            get_redis_url(),
            decode_responses=True
        )
        self.gcs_bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
        self.firestore_db = None
        self.gcs_client = None
        self.external_ip = None
        self._eod_done_today = False
        self._firestore_module = None

    async def _init_cloud_clients(self):
        """Lazily initialize Google Cloud clients."""
        try:
            from google.cloud import firestore, storage
            
            # [Audit-Fix] Detect if GOOGLE_APPLICATION_CREDENTIALS is a directory (Docker mount glitch)
            creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if creds_path:
                if os.path.isdir(creds_path) or (os.path.isfile(creds_path) and os.path.getsize(creds_path) == 0):
                    logger.error(f"❌ GCP Credentials Error: {creds_path} is invalid (dir or empty)! Unsetting to prevent metadata-service hang.")
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            
            from google.cloud import firestore, storage
            self._firestore_module = firestore
            
            # [Audit 3.8] Final hardening: If Metadata server returns 404, AsyncClient() can raise RefreshError.
            # We catch it here to stay in local-only mode.
            try:
                self.firestore_db = firestore.AsyncClient()
                self.gcs_client = storage.Client()
                logger.info("✅ Google Cloud clients (Firestore + GCS) initialized successfully.")
            except Exception as auth_err:
                logger.warning(f"⚠️ GCP Authentication failed (Check SA attachment): {auth_err}")
                self.firestore_db = None
                self.gcs_client = None
            
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
        Maintains an aggressive outbound polling loop, transmitting deep portfolio and 
        market heatmaps without interrupting zero-zmq databuses.
        """
        while True:
            try:
                if not self.firestore_db:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                    continue

                # Fetch live metrics from Redis for fallback visibility
                alpha = await self.redis.get("COMPOSITE_ALPHA") or "0.0"
                # Aggregate distributed subsystem indicators into a monolithic Command Center object
                # Retrieve global regime from NIFTY50 as default
                nifty_reg_raw = await self.redis.hget("hmm_regime_state", "NIFTY50")
                regime = "UNKNOWN"
                if nifty_reg_raw:
                    try: regime = json.loads(nifty_reg_raw).get("regime", "UNKNOWN")
                    except: pass

                indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
                index_states = {}
                all_deep_signals = {}
                
                for asset in indices:
                    # Fetch market state
                    st_raw = await self.redis.get(f"latest_market_state:{asset}")
                    st = json.loads(st_raw) if st_raw else {}
                    
                    # Fetch regime
                    asset_reg_raw = await self.redis.hget("hmm_regime_state", asset)
                    asset_reg = "UNKNOWN"
                    s18, s27 = 0, 0.0
                    if asset_reg_raw:
                        try:
                            reg_data = json.loads(asset_reg_raw)
                            asset_reg = reg_data.get("regime", "UNKNOWN")
                            s18 = int(reg_data.get("s18", 0))
                            s27 = float(reg_data.get("s27", 0.0))
                        except: pass
                    
                    index_states[asset] = {
                        "score": st.get("s_total", 0.0), 
                        "regime": asset_reg,
                        "s18": s18, # [Audit-Fix] Component 1: Integer State Visibility
                        "s27": s27, # [Audit-Fix] Component 1: Quality Score Visibility
                        "price": st.get("price", 0.0)
                    }

                    # Fetch Deep Signals for this asset
                    all_deep_signals[asset] = {
                        "log_ofi_z": st.get("log_ofi_zscore", 0.0),
                        "rv":         st.get("rv", 0.0),
                        "adx":        st.get("adx", 20.0),
                        "atm_iv":     float(await self.redis.get(f"LIVE_IV:{asset}") or st.get("atm_iv", 0.18)),
                        "asto":       st.get("asto", 0.0),
                        "asto_multiplier": st.get("asto_multiplier", 3.0),
                        "asto_regime": st.get("asto_regime", 0),
                        "rsi":        st.get("rsi", 50.0),
                        "pcr":        st.get("pcr", 0.85)
                    }

                # Legacy/default signal for NIFTY50
                deep_signals = all_deep_signals.get("NIFTY50", {})


                power_five = {}
                for idx in indices:
                    power_five[idx] = {}
                    components = {
                        "NIFTY50":   ["HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "ITC"],
                        "BANKNIFTY": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
                        "SENSEX":    ["HDFCBANK", "RELIANCE", "ICICIBANK", "ITC", "LT"]
                    }[idx]
                    for sym in components:
                        raw_z = await self.redis.hget("power_five_matrix", sym)
                        if raw_z:
                            try:
                                # Standardize: power_five_matrix stores JSON with z_score
                                power_five[idx][sym] = float(json.loads(raw_z).get("z_score", 0.0))
                            except:
                                power_five[idx][sym] = 0.0
                        else:
                            # Fallback to legacy zscore key
                            z = await self.redis.get(f"zscore:{sym}") or "0.0"
                            try: power_five[idx][sym] = float(z)
                            except: power_five[idx][sym] = 0.0

                portfolio_delta = {}
                for idx in indices:
                    d = await self.redis.get(f"NET_DELTA_{idx}") or "0.0"
                    portfolio_delta[idx] = float(d)

                # [Audit-Fix] Component 2: Real-Time Realized P&L Tracker
                realized_pnl_live = float(await self.redis.get("DAILY_REALIZED_PNL_LIVE") or 0.0)
                realized_pnl_paper = float(await self.redis.get("DAILY_REALIZED_PNL_PAPER") or 0.0)

                # [Audit-Fix] Component 4: Exchange Lag Detection (Core 1 Handshake)
                network_lag_ms = 0.0
                nifty_tick_raw = await self.redis.get("latest_tick:NIFTY50")
                if nifty_tick_raw:
                    try:
                        nifty_tick = json.loads(nifty_tick_raw)
                        exch_ts_str = nifty_tick.get("exchange_ts")
                        if exch_ts_str:
                            # Shoonya LTT is usually HH:MM:SS
                            # For precise lag, we assume the tick happened today
                            now_ist = datetime.now(IST)
                            exch_time = datetime.strptime(exch_ts_str, "%H:%M:%S").replace(
                                year=now_ist.year, month=now_ist.month, day=now_ist.day, tzinfo=IST
                            )
                            lag = (datetime.now(IST) - exch_time).total_seconds()
                            network_lag_ms = max(0.0, lag * 1000.0)
                    except: pass

                # Minimal state for discovery
                state = {
                    "vm_public_ip": self.external_ip,
                    "status": "ONLINE",
                    "last_heartbeat": datetime.now(IST).isoformat(),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "system_health": "HEALTHY",
                    "live_alpha": float(alpha),
                    "live_regime": regime,
                    "realized_pnl_live": realized_pnl_live,
                    "realized_pnl_paper": realized_pnl_paper,
                    "network_lag_ms": network_lag_ms,
                    "signals": deep_signals, # Backward compat
                    "all_signals": all_deep_signals, # Wave 3: Multi-index
                    "power_five": power_five,
                    "portfolio_delta": portfolio_delta,
                    "index_states": index_states,
                    "exit_path_70_30": all_deep_signals.get("NIFTY50", {}).get("exit_path_70_30", {"tp1": 0.0, "tp2": 0.0, "progress": 0.0}),
                    
                    # Wave 2: Enhanced Indicators
                    "indicators": {
                        "slippage_audit": float(await self.redis.get("METRIC:AVG_SLIPPAGE") or 0.0),
                        "portfolio_heat": float(await self.redis.get("METRIC:MARGIN_UTIL") or 0.0),
                        "vm_up_to_date": bool(network_lag_ms < 500.0), # Stale Dashboard Veto signal
                        "bid_ask_heatmap": await self._fetch_spread_heatmap(),
                        # [Audit-Fix] Component 3: The "Veto Monitor"
                        "system_vetos": {
                            "toxic_vpin": await self.redis.get("VETOR:VPIN_ACTIVE") == "True",
                            "vix_spike": await self.redis.get("VIX_SPIKE_DETECTED") == "True",
                            "margin_low": await self.redis.get("MARGIN_HALT") == "True",
                            "trading_paused": await self.redis.get("TRADING_PAUSED") == "True"
                        }
                    }
                }

                # Wave 4.1: Catch RefreshError during runtime publishing
                try:
                    doc_ref = self.firestore_db.collection("system").document("metadata")
                    await doc_ref.set(state, merge=True)
                except Exception as e:
                    # Catch google.auth.exceptions.RefreshError or similar 404s from metadata
                    if "RefreshError" in str(e) or "404" in str(e):
                        logger.error(f"❌ GCP Auth Refresh Failed (Metadata 404): Disabling Firestore publishing. Error: {e}")
                        self.firestore_db = None
                    else:
                        logger.error(f"Firestore metadata update error: {e}")
                
                # Also sync current operating config for visibility when VM is OFF
                await self._sync_config_to_firestore()

            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")

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
            # Hardened Wave 4.2: Catch auth failures during config sync
            try:
                self.firestore_db.collection("system").document("config").set(config_data, merge=True)
            except Exception as e:
                if "RefreshError" in str(e) or "404" in str(e):
                    logger.error(f"❌ config_sync: GCP Auth Refresh Failed (Metadata 404): Disabling Firestore. Error: {e}")
                    self.firestore_db = None
                else:
                    logger.error(f"Firestore config sync error: {e}")
            # logger.info("Config synced to Firestore.")
        except Exception as e:
            logger.error(f"Config sync preparation error: {e}")

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
                    
                    # [Audit-Fix] Advisory 3: Piggyback on EOD Snapshot to backup constituents
                    if self.firestore_db and self.gcs_client:
                        doc = await self.firestore_db.collection("system").document("index_constituents").get()
                        if doc.exists:
                            data = doc.to_dict()
                            bucket = self.gcs_client.bucket(self.gcs_bucket)
                            blob = bucket.blob("config/index_constituents.json")
                            await asyncio.to_thread(blob.upload_from_string, json.dumps(data, indent=2))
                            logger.info("✅ Index constituents backed up to GCS (EOD Snapshot).")
                except Exception as e:
                    logger.error(f"EOD Snapshot logic failed: {e}")

                # Legacy HMM model upload removed: system is now purely deterministic.

                try:
                    await self._mark_vm_shutdown_pending()
                except Exception as e:
                    logger.error(f"VM shutdown mark failed: {e}")

            # Reset flag at midnight
            if now.hour == 0 and now.minute == 0:
                self._eod_done_today = False

            await asyncio.sleep(30)

    async def _upload_tick_parquet(self, snapshot_time: datetime):
        """Export today's ticks from TimescaleDB to .parquet using chunked streaming."""
        if not self.gcs_client:
            return

        parquet_path = f"/tmp/ticks_{snapshot_time.strftime('%Y-%m-%d')}.parquet"
        
        try:
            import asyncpg
            import pyarrow as pa
            import pyarrow.parquet as pq

            conn = await asyncpg.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=5432,
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASS"),
                database=os.getenv("DB_NAME", "trading_db")
            )

            # [Performance Audit-Fix] Use Server-Side Cursor for OOM prevention
            query = """
                SELECT time, symbol, price, log_ofi_zscore, cvd, vpin, basis_zscore, vol_term_ratio, exit_path_70_progress, asto, asto_regime, asto_multiplier
                FROM market_history 
                WHERE time >= $1::date AND time < ($1::date + interval '1 day')
            """

            # Define schema explicitly for PyArrow
            schema = pa.schema([
                ('time', pa.timestamp('us', tz='UTC')),
                ('symbol', pa.string()),
                ('price', pa.float64()),
                ('log_ofi_zscore', pa.float64()),
                ('cvd', pa.float64()),
                ('vpin', pa.float64()),
                ('basis_zscore', pa.float64()),
                ('vol_term_ratio', pa.float64()),
                ('exit_path_70_progress', pa.float64()),
                ('asto', pa.float64()),
                ('asto_regime', pa.int32()),
                ('asto_multiplier', pa.float64())
            ])

            CHUNK_SIZE = 50000
            writer = pq.ParquetWriter(parquet_path, schema, compression='snappy')
            
            async with conn.transaction():
                chunk = []
                async for record in conn.cursor(query, snapshot_time):
                    # Mapping record to dict or list matching schema
                    chunk.append(dict(record))
                    if len(chunk) >= CHUNK_SIZE:
                        table = pa.Table.from_pandas(pd.DataFrame(chunk), schema=schema)
                        writer.write_table(table)
                        chunk = []
                
                # Flush remaining
                if chunk:
                    table = pa.Table.from_pandas(pd.DataFrame(chunk), schema=schema)
                    writer.write_table(table)
            
            writer.close()
            await conn.close()

            # Upload to GCS
            bucket = self.gcs_client.bucket(self.gcs_bucket)
            blob = bucket.blob(f"tick_history/ticks_{snapshot_time.strftime('%Y-%m-%d')}.parquet")
            blob.upload_from_filename(parquet_path)
            logger.info(f"✅ Tick data uploaded to GCS (Chunked): {blob.name}")

            if os.path.exists(parquet_path):
                os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Tick Parquet chunked export error: {e}")
            if os.path.exists(parquet_path): os.remove(parquet_path)

    async def _upload_trades_parquet(self, snapshot_time: datetime):
        """Export today's trades from TimescaleDB to .parquet using chunked streaming."""
        if not self.gcs_client:
            return

        parquet_path = f"/tmp/trades_{snapshot_time.strftime('%Y-%m-%d')}.parquet"

        try:
            import asyncpg
            import pyarrow as pa
            import pyarrow.parquet as pq

            conn = await asyncpg.connect(
                host=os.getenv("DB_HOST", "localhost"),
                port=5432,
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASS"),
                database=os.getenv("DB_NAME", "trading_db")
            )

            query = """
                SELECT id::text, time, symbol, action, quantity, price, fees, strategy_id, execution_type, audit_tags 
                FROM trades 
                WHERE time >= $1::date AND time < ($1::date + interval '1 day')
            """

            schema = pa.schema([
                ('id', pa.string()),
                ('time', pa.timestamp('us', tz='UTC')),
                ('symbol', pa.string()),
                ('action', pa.string()),
                ('quantity', pa.int64()),
                ('price', pa.float64()),
                ('fees', pa.float64()),
                ('strategy_id', pa.string()),
                ('execution_type', pa.string()),
                ('audit_tags', pa.string())
            ])

            writer = pq.ParquetWriter(parquet_path, schema, compression='snappy')
            CHUNK_SIZE = 10000
            
            async with conn.transaction():
                chunk = []
                async for record in conn.cursor(query, snapshot_time):
                    chunk.append(dict(record))
                    if len(chunk) >= CHUNK_SIZE:
                        table = pa.Table.from_pandas(pd.DataFrame(chunk), schema=schema)
                        writer.write_table(table)
                        chunk = []
                
                if chunk:
                    table = pa.Table.from_pandas(pd.DataFrame(chunk), schema=schema)
                    writer.write_table(table)
            
            writer.close()
            await conn.close()

            bucket = self.gcs_client.bucket(self.gcs_bucket)
            blob = bucket.blob(f"trade_history/trades_{snapshot_time.strftime('%Y-%m-%d')}.parquet")
            blob.upload_from_filename(parquet_path)
            logger.info(f"✅ Trade data uploaded to GCS (Chunked): {blob.name}")

            if os.path.exists(parquet_path):
                os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Trade Parquet chunked export error: {e}")
            if os.path.exists(parquet_path): os.remove(parquet_path)

        except Exception as e:
            logger.error(f"Trade Parquet chunked export error: {e}")
            if os.path.exists(parquet_path): os.remove(parquet_path)

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

    async def _fetch_spread_heatmap(self):
        """Wave 2: Fetches current bid-ask spread for top symbols."""
        symbols = ["NIFTY50", "BANKNIFTY", "SENSEX", "RELIANCE", "HDFCBANK", "ICICIBANK"]
        heatmap = {}
        for sym in symbols:
            try:
                # We expect MarketSensor/Bridge to store 'bid' and 'ask' in Redis
                bid = float(await self.redis.get(f"bid:{sym}") or 0.0)
                ask = float(await self.redis.get(f"ask:{sym}") or 0.0)
                if bid > 0 and ask > 0:
                    spread = (ask - bid) / bid * 100 # spread in percentage
                    heatmap[sym] = round(spread, 4)
            except:
                pass
        return heatmap

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
    # [Audit-Fix] Component 5: Core 0 Pinning for Management affinity
    try:
        if hasattr(os, 'sched_setaffinity'):
            os.sched_setaffinity(0, {0})
            logger.info("🎯 CORE PINNING: CloudPublisher pinned to Core 0 (Management).")
    except Exception as e:
        logger.warning(f"Could not pin to Core 0: {e}")

    publisher = CloudPublisher()
    asyncio.run(publisher.run())
