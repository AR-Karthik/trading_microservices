"""
daemons/unified_bridge.py
========================
The "Unified Intent Multiplexer" Execution Hub for Project K.A.R.T.H.I.K.

This daemon consolidates the logic of the legacy LiveBridge and PaperBridge into a 
single, high-fidelity execution engine. It ensures that both Paper and Live trades 
are processed by the exact same mathematical engine, removing "code drift" as a 
variable in Institutional Research analysis.

Rendering Process:
1. Stage 1: The Shadow Record (Counterfactual Ledger)
2. Stage 2: The Paper Matcher (Simulated fill with Regime-Aware Slippage)
3. Stage 3: The Live Strike (Real API gateway with Triple-Lock safety)
"""

import asyncio
import logging
import uuid
import json
try:
    import orjson as fast_json
except ImportError:
    fast_json = json
import os
import time
import hashlib
import random
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Any
from functools import partial

import zmq
import zmq.asyncio
import redis.asyncio as redis
import asyncpg
from dotenv import load_dotenv

from core.logger import setup_logger
from core.mq import MQManager, Ports, Topics
from core.alerts import send_cloud_alert
from core.db_retry import robust_db_connect, with_db_retry
from core.health import HeartbeatProvider
from core.auth import get_db_dsn, get_redis_url
from NorenRestApiPy.NorenApi import NorenApi

try:
    import uvloop
except ImportError:
    uvloop = None

load_dotenv()
logger = setup_logger("UnifiedBridge", log_file="logs/unified_bridge.log")

# --- Globals & Constants ---
DB_DSN = get_db_dsn()
REDIS_URL = get_redis_url()
STRICT_PAPER_ONLY = os.getenv("STRICT_PAPER_ONLY", "True") == "True"
SEBI_BATCH_SIZE = 10
INTER_BATCH_WAIT = 1.01

# --- Database Schema Initialization ---

async def init_db(pool):
    """Initializes the TimescaleDB schema for all execution realities."""
    async with pool.acquire() as conn:
        logger.info("Initializing TimescaleDB schema for Unified Bridge...")
        
        # 1. Main Trades Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id UUID,
                time TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity NUMERIC(15, 4) NOT NULL,
                price NUMERIC(15, 2) NOT NULL,
                fees NUMERIC(10, 2) NOT NULL,
                strategy_id TEXT,
                execution_type TEXT NOT NULL,
                audit_tags JSONB DEFAULT '{}',
                latency_ms NUMERIC(10, 2),
                sequence_id BIGINT,
                PRIMARY KEY (id, time)
            );
        """)
        try:
            await conn.execute("SELECT create_hypertable('trades', 'time', if_not_exists => TRUE);")
            logger.info("trades hypertable verified.")
        except Exception as e:
            logger.warning(f"Failed to verify trades hypertable: {e}")

        # 2. Portfolio State
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT,
                strategy_id TEXT,
                parent_uuid TEXT,
                underlying TEXT,
                lifecycle_class TEXT DEFAULT 'KINETIC',
                expiry_date DATE,
                quantity NUMERIC(15, 4) NOT NULL DEFAULT 0,
                avg_price NUMERIC(15, 2) DEFAULT 0.0,
                initial_credit NUMERIC(15, 2) DEFAULT 0.0,
                short_strikes JSONB DEFAULT '{}',
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                delta NUMERIC(15, 4) DEFAULT 0.0,
                theta NUMERIC(15, 4) DEFAULT 0.0,
                inception_spot NUMERIC(15, 2) DEFAULT 0.0,
                execution_type TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (symbol, strategy_id, parent_uuid, execution_type)
            );
        """)

        # 3. Counterfactual Ledger (Layer 8)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id UUID PRIMARY KEY,
                time TIMESTAMPTZ NOT NULL,
                asset TEXT NOT NULL,
                underlying TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                parent_uuid TEXT NOT NULL,
                execution_type TEXT NOT NULL,
                veto_reason TEXT DEFAULT 'NONE',
                is_live BOOLEAN DEFAULT FALSE,
                is_paper BOOLEAN DEFAULT TRUE,
                regime INTEGER,
                s27_quality DOUBLE PRECISION,
                intent_price DOUBLE PRECISION,
                execution_price DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'INTENT'
            );
        """)
        try:
            await conn.execute("SELECT create_hypertable('shadow_trades', 'time', if_not_exists => TRUE);")
            logger.info("shadow_trades hypertable verified.")
        except Exception as e:
            logger.warning(f"Failed to verify shadow_trades hypertable: {e}")

        # 4. Rejection Journal (Layer 7)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rejections (
                time TIMESTAMPTZ NOT NULL,
                asset TEXT NOT NULL,
                strategy_id TEXT,
                reason TEXT,
                alpha NUMERIC(15, 4),
                vpin NUMERIC(10, 4),
                PRIMARY KEY (time, asset)
            );
        """)

    logger.info("Database schema check complete.")

# --- Rate Limiting & Safety ---

class TokenBucketRateLimiter:
    """Shoonya API Compliance: Max 10 requests/sec per vendor spec."""
    def __init__(self, rate=8, per=1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
    
    async def acquire(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.per))
        self.last_refill = now
        
        while self.tokens < 1:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.per))
            self.last_refill = now
        self.tokens -= 1

# --- API Handler & Execution Logic ---

class ShoonyaHandler:
    """Stage 3: Physical Strike Gateway."""
    def __init__(self):
        load_dotenv()
        self.api = NorenApi(host=os.getenv("SHOONYA_HOST"), 
                           websocket=os.getenv("SHOONYA_HOST", "").replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP') + "/")
        self.user = os.getenv("SHOONYA_USER")
        self.pwd = os.getenv("SHOONYA_PWD")
        self.factor2 = os.getenv("SHOONYA_FACTOR2")
        self.vc = os.getenv("SHOONYA_VC")
        self.app_key = os.getenv("SHOONYA_APP_KEY")
        self.imei = os.getenv("SHOONYA_IMEI", "abc1234")
        self.authenticated = False
        self.rate_limiter = TokenBucketRateLimiter(rate=8, per=1.0)

    async def authenticate(self):
        if self.authenticated: return True
        try:
            pwd = hashlib.sha256(str(self.pwd).encode('utf-8')).hexdigest()
            u_app_key = '{0}|{1}'.format(self.user, self.app_key)
            app_key = hashlib.sha256(u_app_key.encode('utf-8')).hexdigest()
            
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, partial(self.api.login, 
                userid=self.user, password=pwd, twoFA=self.factor2, 
                vendor_code=self.vc, api_secret=app_key, imei=self.imei))
            
            if res and res.get('stat') == 'Ok':
                self.authenticated = True
                logger.info("✅ Shoonya API Authenticated.")
                return True
            else:
                logger.error(f"❌ Shoonya API Auth Failed: {res}")
                return False
        except Exception as e:
            logger.error(f"Execution API Exception: {e}")
            return False

    def get_exchange(self, symbol: str) -> str:
        if any(suffix in symbol for suffix in ["CE", "PE", "FUT"]):
            return 'BFO' if any(idx in symbol for idx in ["SENSEX", "BANKEX"]) else 'NFO'
        return 'NSE'

    async def place_order(self, order: dict, redis_client) -> Optional[dict]:
        """Triple-Lock Live Order Placement."""
        if STRICT_PAPER_ONLY:
            logger.critical("🚨 TRIPLE-LOCK BLOCK: STRICT_PAPER_ONLY is True.")
            return None

        if not self.authenticated:
            if not await self.authenticate(): return None

        await self.rate_limiter.acquire()
        action_map = {"BUY": "B", "SELL": "S"}
        action = action_map.get(order['action'], 'B')
        exchange = self.get_exchange(order['symbol'])
        symbol = order['symbol']

        # [Reconciler Audit §3] Inject parent_uuid into remarks for O(1) basket routing
        p_uuid = order.get('parent_uuid', 'NONE')
        oid = order.get('order_id', '')
        remarks = f"K7:{p_uuid}:{oid}"

        logger.warning(f"🚀 [LIVE STRIKE] {action} {order['quantity']} {symbol} (remarks={remarks[:30]})")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: self.api.place_order(
            buy_or_sell=action, product_type='I', exchange=exchange, tradingsymbol=symbol,
            quantity=order['quantity'], disclosedquantity=0, price_type=order.get('order_type', 'MKT'),
            price=order.get('price', 0), retention='DAY', remarks=remarks
        ))

        if res and res.get('stat') == 'Ok':
            broker_oid = res['norenordno']
            # [Reconciler Audit §4] Write Hash-based order status for fast reconciliation
            try:
                await redis_client.hset(f"order_status:{oid}", mapping={
                    "status": "COMPLETE", "filled_qty": str(order['quantity']),
                    "remaining_qty": "0", "broker_orderno": broker_oid,
                    "parent_uuid": p_uuid,
                })
            except Exception as e:
                logger.error(f"Hash status write failed: {e}")
            return {
                "id": broker_oid, "time": datetime.now(timezone.utc),
                "symbol": symbol, "action": order['action'], "quantity": float(order['quantity']),
                "price": float(order.get('price', 0.0)), "fees": 20.0, "strategy_id": order['strategy_id'],
                "execution_type": "Actual", "parent_uuid": p_uuid,
                "broker_oid": broker_oid
            }
        
        # Regime Aware Nudging for Rejections
        error_msg = res.get('emsg', 'Unknown')
        if "400" in error_msg or "REJECTED" in (res.get('stat') or ''):
            regime_raw = await redis_client.get("s18_state")
            regime = int(regime_raw) if regime_raw else 0
            nudge = 0.003 if regime in [1, 3] else 0.001
            logger.warning(f"⚠️ API REJECTION in Regime {regime}. Nudging by {nudge*100}%...")
            
            new_price = float(order.get('price', 0)) * (1 + (nudge if action == 'B' else -nudge))
            res = await loop.run_in_executor(None, lambda: self.api.place_order(
                buy_or_sell=action, product_type='I', exchange=exchange, tradingsymbol=symbol, 
                quantity=order['quantity'], disclosedquantity=0, price_type='LMT', price=new_price,
                retention='DAY', remarks=remarks
            ))
            if res and res.get('stat') == 'Ok':
                nudge_oid = res['norenordno']
                try:
                    await redis_client.hset(f"order_status:{oid}", mapping={
                        "status": "COMPLETE", "filled_qty": str(order['quantity']),
                        "remaining_qty": "0", "broker_orderno": nudge_oid,
                        "parent_uuid": p_uuid,
                    })
                except Exception as e:
                    logger.error(f"Hash status write (nudge) failed: {e}")
                return {
                    "id": nudge_oid, "time": datetime.now(timezone.utc),
                    "symbol": symbol, "action": order['action'], "quantity": float(order['quantity']),
                    "price": new_price, "fees": 20.0, "strategy_id": order['strategy_id'],
                    "execution_type": "Actual", "parent_uuid": p_uuid,
                }

        logger.error(f"❌ Live Order Failed: {res}")
        # [Reconciler Audit §4] Write REJECTED status for fast reconciliation
        try:
            await redis_client.hset(f"order_status:{oid}", mapping={
                "status": "REJECTED", "filled_qty": "0", "remaining_qty": "0",
                "parent_uuid": p_uuid, "error": str(res.get('emsg', 'Unknown')),
            })
        except Exception as e:
            logger.error(f"Hash status write (reject) failed: {e}")
        return None

    async def get_order_status(self, broker_oid: str) -> str:
        """Physical status check with the broker [SRS §14.9]."""
        if not self.authenticated:
            if not await self.authenticate(): return "ERROR"
        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, self.api.get_order_history, broker_oid)
            # Shoonya returns a list of dictionaries for order history
            if isinstance(res, list) and len(res) > 0:
                # The first entry is generally the latest status
                return res[0].get('status', 'UNKNOWN')
            return "UNKNOWN"
        except Exception as e:
            logger.error(f"Status check failed for {broker_oid}: {e}")
            return "ERROR"

# --- Main Unified Bridge ---

class UnifiedExecutionBridge:
    def __init__(self):
        self.mq = MQManager()
        self.redis = None
        self.pool = None
        self.live_handler = ShoonyaHandler()
        self.total_realized_pnl = {"Paper": 0.0, "Actual": 0.0}
        self.is_kill_switch_triggered = False
        self.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "-16000.0"))
        
        # Rate limiting for Paper orders (8 per sec to match Live)
        self.paper_timestamps = collections.deque()

    def _get_underlying(self, symbol: str) -> str:
        """Derives the underlying index for inception spot tracking."""
        if "NIFTY" in symbol: return "NIFTY50"
        if "BANKNIFTY" in symbol: return "BANKNIFTY"
        if "SENSEX" in symbol: return "SENSEX"
        return symbol.split()[0]

    async def setup(self):
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=20)
        await init_db(self.pool)
        
        # Subscriptions
        self.order_sub = self.mq.create_pull(Ports.ORDERS, bind=True)
        self.trade_pub = self.mq.create_publisher(Ports.TRADE_EVENTS)
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe("panic_channel")
        
        # Recovery: Daily realized PnL from Redis
        paper_pnl = await self.redis.get("DAILY_REALIZED_PNL_PAPER_TOTAL")
        live_pnl = await self.redis.get("DAILY_REALIZED_PNL_LIVE_TOTAL")
        if paper_pnl: self.total_realized_pnl["Paper"] = float(paper_pnl)
        if live_pnl: self.total_realized_pnl["Actual"] = float(live_pnl)
        
        logger.info(f"🛡️ Unified Execution Bridge Active | Kill Switch: ₹{self.daily_loss_limit:,.2f}")

    async def _log_shadow_ledger(self, intent: dict):
        """Stage 1: Shadow Record (Journaling for Efficacy Metrics)."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO shadow_trades (
                        id, time, asset, underlying, strategy_id, action, 
                        quantity, price, parent_uuid, execution_type, 
                        veto_reason, is_live, is_paper, regime, s27_quality
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    ON CONFLICT (id) DO NOTHING
                """, uuid.uuid4(), datetime.now(timezone.utc), intent.get("asset"), 
                     intent.get("asset"), intent.get("strategy_id"), intent.get("action"),
                     float(intent.get("lots", 0.0)), float(intent.get("price", 0.0)), 
                     intent.get("parent_uuid"), "SHADOW",
                     intent.get("veto_reason", "NONE"), intent.get("is_live", False), 
                     intent.get("is_paper", True), intent.get("regime"), intent.get("s27"))
        except Exception as e:
            logger.error(f"Shadow Ledger log failed: {e}")

    async def calculate_paper_fill(self, order: dict) -> dict:
        """Stage 2: Paper Matcher (Regime-Aware Institutional Slippage)."""
        symbol = order['symbol']
        ltp_raw = await self.redis.get(f"ltp:{symbol}")
        ltp = float(ltp_raw) if ltp_raw else float(order.get("price", 100.0))
        
        # S18 Regime Scaling
        regime_raw = await self.redis.get("s18_state")
        regime = int(regime_raw) if regime_raw else 0
        regime_mult = {0: 1, 1: 2, 2: 1, 3: 5}.get(regime, 1) # Regime 3 (Volatile) = 5x Slippage
        
        base_slip = random.randint(2, 5) * 0.05
        final_slip = base_slip * regime_mult
        
        exec_price = ltp + (final_slip if order['action'] == 'BUY' else -final_slip)
        
        return {
            "id": str(uuid.uuid4()), "time": datetime.now(timezone.utc),
            "symbol": symbol, "action": order['action'], "quantity": float(order.get('lots', 0) or order.get('quantity', 0)),
            "price": round(float(exec_price), 2), "fees": 20.0, "strategy_id": order['strategy_id'],
            "execution_type": "Paper", "parent_uuid": order.get('parent_uuid', 'NONE')
        }

    @with_db_retry(max_retries=3, backoff=0.5)
    async def update_portfolio_universal(self, conn, execution, fees):
        """Standardized Portfolio State Engine for ALL Realities."""
        symbol = execution['symbol']
        strat_id = execution['strategy_id']
        exec_type = execution['execution_type']
        p_uuid = execution.get('parent_uuid', 'NONE')
        qty = float(execution['quantity']) if execution['action'] == 'BUY' else -float(execution['quantity'])
        price = float(execution['price'])
        
        # Atomic Redis Settling for Capital Allocation UI
        total_p = (abs(qty) * price) + fees
        cash_key = f"CASH_COMPONENT_{exec_type.upper()}"
        quarantine_key = f"QUARANTINE_PREMIUM_{exec_type.upper()}"
        
        if execution['action'] == 'BUY':
            await self.redis.decrbyfloat(cash_key, total_p)
        else:
            await self.redis.incrbyfloat(quarantine_key, total_p)
        
        # DB Persistence
        rec = await conn.fetchrow("""
            SELECT quantity, avg_price, realized_pnl 
            FROM portfolio 
            WHERE symbol = $1 AND strategy_id = $2 AND execution_type = $3 AND parent_uuid = $4 
            FOR UPDATE
        """, symbol, strat_id, exec_type, p_uuid)
        
        if not rec:
            await conn.execute("""
                INSERT INTO portfolio (
                    symbol, strategy_id, parent_uuid, underlying, lifecycle_class, 
                    quantity, avg_price, execution_type, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """, symbol, strat_id, p_uuid, symbol, "KINETIC", qty, price, exec_type, execution['time'])
            return

        curr_q = float(rec['quantity'])
        avg_p = float(rec['avg_price'])
        prev_p = float(rec['realized_pnl'])
        
        new_q = curr_q + qty
        new_avg_p = avg_p
        realized_p = prev_p
        
        # PnL Math (Common Engine)
        if curr_q > 0 and qty < 0:
            c = min(curr_q, abs(qty)); realized_p += c * (price - avg_p) - fees
        elif curr_q < 0 and qty > 0:
            c = min(abs(curr_q), qty); realized_p += c * (avg_p - price) - fees
        
        if abs(new_q) > abs(curr_q):
            new_avg_p = (abs(curr_q) * avg_p + abs(qty) * price) / abs(new_q)
        elif abs(new_q) < 0.0001:
            new_avg_p = 0.0
            
        # Inception Spot Tracking (Fortress Fix 4.1)
        underlying = self._get_underlying(symbol)
        inception_spot = float(await self.redis.get(f"ltp:{underlying}") or 0.0)

        await conn.execute("""
            UPDATE portfolio
            SET quantity = $1, avg_price = $2, realized_pnl = $3, delta = $4, theta = $5, 
                updated_at = $6, inception_spot = COALESCE(NULLIF($7, 0.0), inception_spot)
            WHERE symbol = $8 AND strategy_id = $9 AND execution_type = $10 AND parent_uuid = $11
        """, new_q, new_avg_p, realized_p, 
             float(execution.get('delta', 0.0)), float(execution.get('theta', 0.0)),
             execution['time'], inception_spot,
             symbol, strat_id, exec_type, p_uuid)
        
        # Real-time PnL Aggregation
        self.total_realized_pnl[exec_type] += (realized_p - prev_p)
        await self.redis.set(f"DAILY_REALIZED_PNL_{exec_type.upper()}_TOTAL", str(self.total_realized_pnl[exec_type]))

    async def handle_intent_rendering(self, intent: dict):
        """Stages 1-3 Rendering Pipeline."""
        try:
            # Stage 1: The Shadow Record (Counterfactual Reality Update)
            shadow_fill = await self.calculate_paper_fill(intent)
            shadow_fill["execution_type"] = "Shadow"
            
            # Log both intent and simulated execution for slippage analysis
            await self._log_shadow_ledger(intent, shadow_fill)
            
            async with self.pool.acquire() as conn:
                await self.update_portfolio_universal(conn, shadow_fill, 0.0)
            
            # Stage 0: Basic Guard (Lots Check)
            if float(intent.get("lots", 0.0)) <= 0: return

            # Stage 0.5: Atomic Double-Tap & Latency Lock
            lock_key = f"lock:{intent['symbol']}"
            if await self.redis.exists(lock_key): return
            await self.redis.setex(lock_key, 10, "LOCKED")
            
            tick_raw = await self.redis.get(f"latest_tick:{intent['symbol']}")
            if tick_raw:
                tick = fast_json.loads(tick_raw)
                tick_ts = datetime.fromisoformat(tick.get("timestamp")).astimezone(timezone.utc)
                if (datetime.now(timezone.utc) - tick_ts).total_seconds() > 0.25:
                    logger.critical(f"🛑 FEED LATENCY BLOCK for {intent['symbol']}")
                    await self.redis.delete(lock_key); return

            # Stage 2: The Paper Matcher (Constant Baseline Reality)
            if intent.get("is_paper", True):
                paper_fill = await self.calculate_paper_fill(intent)
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("""
                            INSERT INTO trades (
                                id, time, symbol, action, quantity, price, fees, 
                                strategy_id, execution_type, audit_tags, latency_ms, sequence_id
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                        """, uuid.UUID(paper_fill['id']), paper_fill['time'], paper_fill['symbol'], paper_fill['action'], 
                             paper_fill['quantity'], paper_fill['price'], 20.0, 
                             paper_fill['strategy_id'], "Paper", '{}', 0.0, 0)
                        await self.update_portfolio_universal(conn, paper_fill, 20.0)
                logger.info(f"📝 [PAPER] {intent['symbol']} fill @ {paper_fill['price']}")
                
                # Broadcast for UI
                await self.mq.send_json(self.trade_pub, f"EXEC.{intent['symbol']}", {"type": "EXECUTION", "execution_type": "Paper", **paper_fill})

            # Stage 3: The Live Strike (Experimental Reality)
            if intent.get("is_live", False):
                if await self.redis.get("SYSTEM_HALT") == "True" or self.is_kill_switch_triggered:
                    logger.warning("🚨 [LIVE BLOCKED] System Halt.")
                else:
                    # Final Bridge-Level Margin Check via LUA
                    margin_req = float(intent.get("price", 100.0)) * float(intent.get("lots", 1)) * 4.0 # 4x Buffer
                    LUA = """
                        local c = tonumber(redis.call('get', KEYS[1]) or '0')
                        if c >= tonumber(ARGV[1]) then 
                            redis.call('set', KEYS[1], tostring(c - ARGV[1]))
                            return 1
                        end 
                        return 0
                    """
                    margin_ok = await self.redis.eval(LUA, 1, "CASH_COMPONENT_LIVE", margin_req)
                    if not margin_ok:
                        logger.error(f"❌ [BRIDGE MARGIN REJECTION] {intent['symbol']}")
                    else:
                        live_fill = await self.live_handler.place_order(intent, self.redis)
                        if live_fill:
                            async with self.pool.acquire() as conn:
                                async with conn.transaction():
                                    await conn.execute("""
                                        INSERT INTO trades (
                                            id, time, symbol, action, quantity, price, fees, 
                                            strategy_id, execution_type, audit_tags, latency_ms, sequence_id
                                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                                    """, uuid.UUID(live_fill['id']), live_fill['time'], live_fill['symbol'], live_fill['action'], 
                                         live_fill['quantity'], live_fill['price'], 20.0, 
                                         live_fill['strategy_id'], "Actual", '{}', 
                                         float(intent.get('latency_ms', 0.0)), 0)
                                    await self.update_portfolio_universal(conn, live_fill, 20.0)
                            
                            # Institutional Step 19.1: The 3s Bailout Audit
                            async def delayed_fill_audit(f_id, p_uuid):
                                await asyncio.sleep(3.0)
                                status = await self.live_handler.get_order_status(f_id)
                                logger.info(f"🧐 AUDITING FILL: {f_id} | Status: {status}")
                                
                                if status not in ["COMPLETE", "FILLED"]:
                                    logger.critical(f"⚠️ PARTIAL FILL DETECTED: {f_id} is {status}. Triggering Rollback.")
                                    # [SRS §19.2] Atomic Rollback to prevent unhedged liability
                                    await self._handle_basket_rollback(p_uuid)
                                    # Add to rejections ledger for analysis
                                    rejection_ev = {
                                        "time": datetime.now(timezone.utc).isoformat(),
                                        "asset": intent['symbol'],
                                        "strategy_id": intent['strategy_id'],
                                        "reason": f"PARTIAL_FILL_BAILOUT_{status}",
                                        "alpha": 0.0, "vpin": 0.0
                                    }
                                    await self.mq.send_json(self.trade_pub, f"REJECTION.{intent['symbol']}", rejection_ev)
                            
                            asyncio.create_task(delayed_fill_audit(live_fill['id'], live_fill.get('parent_uuid', 'NONE')))
                            
                            logger.info(f"🔥 [LIVE STRIKE SUCCESS] {intent['symbol']} ID: {live_fill['id']}")
                            await self.mq.send_json(self.trade_pub, f"EXEC.{intent['symbol']}", {"type": "EXECUTION", "execution_type": "Actual", **live_fill})
                            await self.check_kill_switch_breach()

            await self.redis.delete(lock_key)

        except Exception as e:
            logger.error(f"Intent Rendering Crash: {e}", exc_info=True)

    async def check_kill_switch_breach(self):
        """Hard Bound Protection ($4.3)."""
        if self.total_realized_pnl["Actual"] <= self.daily_loss_limit:
            self.is_kill_switch_triggered = True
            logger.critical(f"🛑 DAILY LOSS BREACH: ₹{self.total_realized_pnl['Actual']} <= ₹{self.daily_loss_limit}")
            await self.redis.set("SYSTEM_HALT", "True")
            await self.redis.publish("panic_channel", json.dumps({"action": "SQUARE_OFF_ALL", "execution_type": "ALL", "reason": "KILL_SWITCH"}))
            await send_cloud_alert("⛔ KILL SWITCH TRIGGERED. SQUARING OFF ALL POSITIONS.", alert_type="CRITICAL")

    async def panic_listener(self):
        """Out-of-band Global Square Off."""
        logger.info("Panic listener active on 'panic_channel'.")
        async for message in self.pubsub.listen():
            if message['type'] == 'message':
                try:
                    data = fast_json.loads(message['data'])
                    action = data.get('action', '')
                    exec_type = data.get('execution_type', 'ALL')
                    
                    if action in ["SQUARE_OFF", "SQUARE_OFF_ALL"]:
                        logger.critical(f"🆘 PANIC SIGNAL RECEIVED: {data['reason'] if 'reason' in data else 'UI INITIATED'}")
                        asyncio.create_task(self.liquidate_all_realities(exec_type))
                    elif action == "SQUARE_OFF_SIDE":
                        asyncio.create_task(self.liquidate_selective(data.get("symbol"), data.get("side"), exec_type))
                except Exception as e:
                    logger.error(f"Panic handler error: {e}")

    async def liquidate_all_realities(self, target_reality):
        """SEBI-Compliant Market Wipeout."""
        logger.critical(f"🛑 GLOBAL WIPE: Reality={target_reality}")
        async with self.pool.acquire() as conn:
            query = "SELECT symbol, strategy_id, parent_uuid, quantity, execution_type FROM portfolio WHERE quantity != 0"
            if target_reality != "ALL": query += f" AND execution_type = '{target_reality}'"
            positions = await conn.fetch(query)
            
            if not positions:
                logger.info("No open positions to liquidate.")
                return

            # Batch by 10 per SEBI rule 10-ops-per-sec
            for i in range(0, len(positions), SEBI_BATCH_SIZE):
                batch = positions[i:i + SEBI_BATCH_SIZE]
                for pos in batch:
                    action = "SELL" if pos['quantity'] > 0 else "BUY"
                    qty = abs(pos['quantity'])
                    logger.critical(f"PANIC BLAST: {action} {qty} {pos['symbol']} Reality:{pos['execution_type']}")
                    
                    if pos['execution_type'] == "Actual":
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, lambda: self.live_handler.api.place_order(
                            buy_or_sell=action[0], product_type='I', 
                            exchange=self.live_handler.get_exchange(pos['symbol']), 
                            tradingsymbol=pos['symbol'], quantity=int(qty), disclosedquantity=0, 
                            price_type='MKT', price=0, retention='DAY'))
                    
                    # Update internal registers
                    fill = {"symbol": pos['symbol'], "action": action, "quantity": qty, "price": 0.0, 
                             "strategy_id": pos['strategy_id'], "execution_type": pos['execution_type'], 
                             "parent_uuid": pos['parent_uuid'], "time": datetime.now(timezone.utc)}
                    await self.update_portfolio_universal(conn, fill, 20.0)
                    await self.redis.delete(f"lock:{pos['symbol']}")
                
                if i + SEBI_BATCH_SIZE < len(positions):
                    await asyncio.sleep(INTER_BATCH_WAIT)
        
        logger.info("✅ Global Wipeout Procedures Completed.")

    async def _log_shadow_ledger(self, intent: dict, fill_result: dict = None):
        """
        Layer 8 Journaling: The Counterfactual Alpha Baseline (SRS §14.8).
        Captures the intent vs. simulated execution for PhD-level slippage analysis.
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO shadow_trades (
                        id, time, asset, underlying, strategy_id, action, quantity, price, 
                        parent_uuid, execution_type, veto_reason, is_live, is_paper,
                        regime, s27_quality, intent_price, execution_price, status
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                """, 
                uuid.uuid4(), datetime.now(timezone.utc), intent.get('symbol', 'UNKNOWN'),
                self._get_underlying(intent.get('symbol', '')), intent.get('strategy_id', 'KINETIC'),
                intent.get('action', 'BUY'), float(intent.get('lots', 0.0)), float(intent.get('price', 0.0)),
                intent.get('parent_uuid', 'NONE'), "Shadow", intent.get('veto_reason', 'NONE'),
                intent.get('is_live', False), intent.get('is_paper', True),
                int(intent.get('regime', 0)), float(intent.get('s27_quality', 0.0)),
                float(intent.get('price', 0.0)), float(fill_result.get('price', 0.0) if fill_result else 0.0),
                "COMPLETE"
                )
        except Exception as e:
            logger.error(f"Shadow Journaling Error: {e}")

    async def liquidate_selective(self, symbol, side, reality):
        """TRAP/Nudge Selective Liquidation."""
        logger.warning(f"🎯 Selective Liquidate: {symbol} Side:{side} Reality:{reality}")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT symbol, strategy_id, parent_uuid, quantity, execution_type 
                FROM portfolio 
                WHERE symbol = $1 AND quantity != 0 AND ($2 = 'ALL' OR execution_type = $2)
            """, symbol, reality)
            
            for row in rows:
                qty = float(row['quantity'])
                if (side == "S" and qty > 0) or (side == "B" and qty < 0) or side == "ALL":
                    action = "SELL" if qty > 0 else "BUY"
                    abs_q = abs(qty)
                    if row['execution_type'] == "Actual":
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, lambda: self.live_handler.api.place_order(
                            buy_or_sell=action[0], product_type='I', 
                            exchange=self.live_handler.get_exchange(symbol), 
                            tradingsymbol=symbol, quantity=int(abs_q), disclosedquantity=0, 
                            price_type='MKT', price=0, retention='DAY'))
                    
                    fill = {"symbol": symbol, "action": action, "quantity": abs_q, "price": 0.0, 
                             "strategy_id": row['strategy_id'], "execution_type": row['execution_type'], 
                             "parent_uuid": row['parent_uuid'], "time": datetime.now(timezone.utc)}
                    await self.update_portfolio_universal(conn, fill, 20.0)

    async def _handle_basket_rollback(self, p_uuid):
        """Selective Circuit Breaker for fragmented multi-leg fills."""
        if not p_uuid or p_uuid == "NONE": return
        logger.warning(f"🚨 BASKET ROLLBACK: {p_uuid}")
        await self.liquidate_all_realities("ALL") # Simplified for safely, should be specific to p_uuid

    async def rejection_listener(self):
        """Layer 7 Journaling."""
        sub = self.mq.create_subscriber(Ports.TRADE_EVENTS, topics=["REJECTION."])
        while True:
            try:
                topic, ev = await self.mq.recv_json(sub)
                if not ev: continue
                async with self.pool.acquire() as conn:
                    await conn.execute("INSERT INTO rejections (time, asset, strategy_id, reason, alpha, vpin) VALUES ($1,$2,$3,$4,$5,$6)",
                        datetime.fromisoformat(ev['time']).astimezone(timezone.utc), ev['asset'], ev['strategy_id'], ev['reason'], float(ev.get('alpha', 0.0)), float(ev.get('vpin', 0.0)))
            except Exception: await asyncio.sleep(1)

    async def shadow_listener(self):
        """Layer 8 Counterfactual Journaling."""
        sub = self.mq.create_subscriber(Ports.TRADE_EVENTS, topics=["SHADOW."])
        while True:
            try:
                topic, ev = await self.mq.recv_json(sub)
                if not ev: continue
                await self._log_shadow_ledger(ev)
            except Exception: await asyncio.sleep(1)

    async def liveliness_pulse(self):
        hb = HeartbeatProvider("UnifiedBridge", self.redis)
        asyncio.create_task(hb.run_heartbeat())
        while True:
            try:
                msg = {"component": "UnifiedBridge", "status": "ALIVE", "pnl": self.total_realized_pnl, "ts": datetime.now().isoformat()}
                await self.redis.set("LH:UnifiedBridge", json.dumps(msg), ex=30)
                await asyncio.sleep(5)
            except Exception: await asyncio.sleep(5)

    async def run_loop(self):
        await self.setup()
        
        # Parallel Listeners
        asyncio.create_task(self.panic_listener())
        asyncio.create_task(self.rejection_listener())
        asyncio.create_task(self.shadow_listener())
        asyncio.create_task(self.liveliness_pulse())
        
        logger.info("🚀 Unified execution pipeline fully operational.")
        
        while True:
            try:
                topic, intent = await self.mq.recv_json(self.order_sub)
                if intent: asyncio.create_task(self.handle_intent_rendering(intent))
            except zmq.Again:
                await asyncio.sleep(0.01)
            except Exception as e:
                logger.error(f"Main loop crash: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    if uvloop: uvloop.install()
    elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    bridge = UnifiedExecutionBridge()
    asyncio.run(bridge.run_loop())
