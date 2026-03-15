"""
daemons/liquidation_daemon.py
==============================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Responsibilities:
- Triple-barrier liquidation system (TP, SL, Time-decay).
- Granular exception handling for broker API error codes.
- Market microstructure monitoring for CVD overrides.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
import os
import math

import redis.asyncio as redis
import asyncpg
from dotenv import load_dotenv
load_dotenv()

from core.mq import MQManager, Ports, Topics
from core.alerts import send_cloud_alert
from core.db_retry import with_db_retry
from core.greeks import BlackScholes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LiquidationDaemon")

# Barrier thresholds
ATR_TP1_MULTIPLIER = 1.2   
ATR_TP_MULTIPLIER  = 2.5   
ATR_SL_MULTIPLIER  = 1.0   
STALL_TIMEOUT_SEC  = 300   

# Bifurcated Multipliers
KINETIC_SL_MULT_BASE = 1.0
POSITIONAL_SL_MULT_BASE = 2.0
CVD_FLIP_EXIT_THRESHOLD   = 5    
BARRIER2_SPREAD_CROSS_PCT = 0.10 

# A2: Slippage Budget
SLIPPAGE_BUDGET_BASE  = 0.02  
SLIPPAGE_HALT_TTL_SEC = 60  

# HTTP 400 emsg substrings that indicate CIRCUIT LIMIT (safe to re-fire)
CIRCUIT_LIMIT_STRINGS = ["price range", "circuit", "freeze", "price band", "pcl", "ucl"]
# emsg substrings that indicate ABORT (don't re-fire)
ABORT_STRINGS = ["margin", "insufficient", "invalid", "malformed", "not found", "order not found"]


class LiquidationDaemon:
    def __init__(self):
        self.mq = MQManager()
        # Use PUSH so this daemon can coexist with live_bridge's PULL binding
        self.order_pub = self.mq.create_push(Ports.ORDERS, bind=False)

        # {symbol: {qty, entry_price, action, entry_time, strategy_id, execution_type}}
        self.orphaned_positions: dict[str, dict] = {}
        self._redis: redis.Redis | None = None
        self.pool: asyncpg.Pool | None = None

    async def _reconnect_pool(self):
        """Reconnect DB pool."""
        try:
            if self.pool:
                await self.pool.close()
            dsn = os.getenv("DB_DSN")
            self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            logger.info("✅ Liquidation DB Pool reconnected.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect Liquidation DB pool: {e}")
            raise

    # ── Startup ──────────────────────────────────────────────────────────────

    async def run(self):
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
        dsn = os.getenv("DB_DSN")
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        logger.info("LiquidationDaemon active. Three-barrier system armed.")

        await self._hydrate_state_from_db()
        
        await asyncio.gather(
            self._handoff_listener(),
            self._position_alert_listener(), 
            self._market_monitor(),
            self._monitor_fill_slippage(),  
            self._run_heartbeat(),          # Phase 9: UI & Observability
        )

    async def _position_alert_listener(self):
        """Phase 10: Listens for NEW_POSITION_ALERTS to arm barriers instantly."""
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe("NEW_POSITION_ALERTS")
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                
                try:
                    data = json.loads(msg["data"])
                    symbol = data["symbol"]
                    
                    # Short sleep to ensure DB is updated by the Bridge
                    await asyncio.sleep(0.5)
                    
                    # Re-hydrate this specific position from DB using persistent pool
                    await self._hydrate_specific_position(symbol)
                    
                    # Phase 6: Ensure JIT Feed is active
                    await self._redis.publish("dynamic_subscriptions", f"NFO|{symbol}")
                except Exception as e:
                    logger.error(f"Error processing NEW_POSITION_ALERT: {e}")
        except asyncio.CancelledError:
            pass

    async def _run_heartbeat(self):
        from core.health import HeartbeatProvider
        hb = HeartbeatProvider("LiquidationDaemon", self._redis)
        await hb.run_heartbeat()

    # ── A2: Fill Slippage Budget Monitor ────────────────────────────────────

    async def _monitor_fill_slippage(self):
        """
        Subscribes to order_confirmations. If any single exit has slippage > SLIPPAGE_BUDGET_PCT,
        sets SLIPPAGE_HALT in Redis for SLIPPAGE_HALT_TTL_SEC seconds, pausing new entries.
        """
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe("order_confirmations")
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    fill_price = float(data.get("fill_price") or 0)
                    intended_price = float(data.get("intended_price") or fill_price)
                    symbol = data.get("symbol", "UNKNOWN")

                    if intended_price > 0 and fill_price > 0:
                        # Dynamic Slippage Adjustment (Phase 15.1)
                        rv = float(await self._redis.get("rv") or 0.0)
                        dynamic_budget = SLIPPAGE_BUDGET_BASE * (1.5 if rv > 0.001 else 1.0)
                        
                        slip = abs(fill_price - intended_price) / intended_price
                        if slip > dynamic_budget:
                            await self._redis.set("SLIPPAGE_HALT", "True", ex=SLIPPAGE_HALT_TTL_SEC)
                            logger.critical(
                                f"🚨 SLIPPAGE HALT [{symbol}]: {slip:.1%}/{dynamic_budget:.1%} "
                                f"(fill={fill_price:.2f} vs intended={intended_price:.2f}). "
                                f"Pausing new entries for {SLIPPAGE_HALT_TTL_SEC}s."
                            )
                            asyncio.create_task(send_cloud_alert(
                                f"🚨 SLIPPAGE BUDGET BREACHED\n"
                                f"Symbol: {symbol} | Slippage: {slip:.1%}\n"
                                f"Fill: ₹{fill_price:.2f} vs Intended: ₹{intended_price:.2f}\n"
                                f"New entries paused for {SLIPPAGE_HALT_TTL_SEC}s.",
                                alert_type="RISK"
                            ))
                except Exception as e:
                    logger.error(f"Slippage monitor parse error: {e}")
        except asyncio.CancelledError:
            pass

    async def _hydrate_state_from_db(self):
        """Phase 4.1: Reconstruct internal state from TimescaleDB (The Absolute Truth)."""
        logger.info("Hydrating state from TimescaleDB...")
        try:
            async with self.pool.acquire() as conn:
                await self._do_hydrate(conn)
            logger.info(f"✅ Hydrated positions from DB.")
        except Exception as e:
            logger.error(f"Hydration failed: {e}")

    @with_db_retry(max_retries=3, backoff=0.5)
    async def _do_hydrate(self, conn):
        rows = await conn.fetch("SELECT * FROM portfolio WHERE quantity != 0")
        for row in rows:
            sym = row['symbol']
            self.orphaned_positions[sym] = {
                "symbol": sym,
                "quantity": float(row['quantity']),
                "price": float(row['avg_price']),
                "entry_time": row['entry_time'].timestamp() if row['entry_time'] else time.time(),
                "lifecycle_class": row['lifecycle_class'],
                "parent_uuid": row['parent_uuid'],
                "execution_type": row['execution_type'] or "Paper"
            }

    async def _hydrate_specific_position(self, symbol: str):
        try:
            async with self.pool.acquire() as conn:
                await self._do_hydrate_specific(conn, symbol)
        except Exception as e:
            logger.error(f"Specific hydration failed for {symbol}: {e}")

    @with_db_retry(max_retries=3, backoff=0.5)
    async def _do_hydrate_specific(self, conn, symbol: str):
        row = await conn.fetchrow("SELECT * FROM portfolio WHERE symbol=$1 AND quantity != 0", symbol)
        if row:
            self.orphaned_positions[symbol] = {
                "symbol": symbol,
                "quantity": float(row['quantity']),
                "price": float(row['avg_price']),
                "entry_time": row['entry_time'].timestamp() if row['entry_time'] else time.time(),
                "lifecycle_class": row['lifecycle_class'],
                "parent_uuid": row['parent_uuid'],
                "execution_type": row['execution_type'] or "Paper"
            }
            logger.info(f"🎯 POSITION ARMED: {symbol} [{row['lifecycle_class']}]")

    # ── Handoff Listener ─────────────────────────────────────────────────────

    async def _handoff_listener(self):
        """Listens for ORPHAN/HANDOFF commands from strategy engine."""
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["ORPHAN", "HANDOFF"])
        while True:
            try:
                _, msg = await self.mq.recv_json(sub)
                if msg:
                    symbol = msg.get("symbol", "NIFTY50")
                    msg["entry_time"] = time.time()
                    msg["stall_retries"] = 0
                    self.orphaned_positions[symbol] = msg
                    logger.info(
                        f"Accepted ORPHAN: {symbol} Qty={msg.get('quantity')} "
                        f"Entry={msg.get('price', 0):.2f}"
                    )
            except Exception as e:
                logger.error(f"Handoff listener error: {e}")
            await asyncio.sleep(0.05)

    # ── Market Monitor ────────────────────────────────────────────────────────

    async def _market_monitor(self):
        """Polls market state and tick data to apply exit barriers."""
        asyncio.create_task(send_cloud_alert("🛡️ LIQUIDATION DAEMON: Active and monitoring risk thresholds.", alert_type="SYSTEM"))
        
        # Subscribe to all supported symbols instead of just TICK.NIFTY50
        topics = ["TICK.NIFTY50", "TICK.BANKNIFTY", "TICK.SENSEX"]
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=topics)
        state_sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

        latest_state: dict = {}

        async def _state_poller():
            nonlocal latest_state
            while True:
                try:
                    _, state = await self.mq.recv_json(state_sub)
                    if state:
                        latest_state = state
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        asyncio.create_task(_state_poller())

        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)
                if topic and tick and self.orphaned_positions:
                    # Extract the tick's underlying symbol
                    tick_symbol = str(topic).split(".")[-1]
                    
                    # Check Stop Day Loss on every tick cycle
                    await self._check_stop_day_loss()
                    
                    for pos_symbol, pos in list(self.orphaned_positions.items()):
                        # Dynamically bucket the position by its underlying index
                        underlying = "UNKNOWN"
                        if pos_symbol.startswith("BANKNIFTY"):
                            underlying = "BANKNIFTY"
                        elif pos_symbol.startswith("SENSEX"):
                            underlying = "SENSEX"
                        elif pos_symbol.startswith("NIFTY"):
                            underlying = "NIFTY50"
                            
                        # Only evaluate barriers if the tick matches the position's bucket
                        if underlying == tick_symbol:
                            await self._evaluate_barriers(pos_symbol, tick, latest_state)
            except Exception as e:
                logger.error(f"Market monitor error: {e}")
                await asyncio.sleep(0.5)

    # ── Stop Day Loss Guard ───────────────────────────────────────────────────

    async def _check_stop_day_loss(self):
        """
        Compares today's cumulative realized P&L against the STOP_DAY_LOSS limit.
        If the daily loss limit is breached:
          1. Sets STOP_DAY_LOSS_BREACHED = True in Redis (entry gate in execution bridges)
          2. Publishes a SQUARE_OFF_ALL panic signal to immediately liquidate all open positions
          3. Logs a critical alert
        Should be called once per market_monitor tick cycle.
        """
        try:
            # Fetch the user-configured limit (default ₹5000)
            stop_limit = float(await self._redis.get("STOP_DAY_LOSS") or 5000.0)

            # Check both paper and live daily realized P&L
            for mode_suffix in ["PAPER", "LIVE"]:
                day_pnl_raw = await self._redis.get(f"DAILY_REALIZED_PNL_{mode_suffix}")
                day_pnl = float(day_pnl_raw or 0.0)
                breach_key = f"STOP_DAY_LOSS_BREACHED_{mode_suffix}"

                limit_val = float(stop_limit)
                if day_pnl <= -limit_val:
                    already_breached = await self._redis.get(breach_key) == "True"
                    if not already_breached:
                        # Set the breach flag — execution bridges check this before accepting new orders
                        await self._redis.set(breach_key, "True")
                        asyncio.create_task(send_cloud_alert(
                            f"🛑 STOP DAY LOSS BREACHED [{mode_suffix}]: "
                            f"Daily P&L ₹{day_pnl:,.0f} <= -₹{stop_limit:,.0f}. "
                            f"Blocking new entries and triggering full liquidation.",
                            alert_type="CRITICAL"
                        ))
                        # Publish panic signal to square off all positions for this mode
                        import json as _json
                        await self._redis.publish(
                            "panic_channel",
                            _json.dumps({
                                "action": "SQUARE_OFF_ALL",
                                "reason": f"STOP_DAY_LOSS_BREACHED_{mode_suffix}",
                                "execution_type": mode_suffix.capitalize()
                            })
                        )
                else:
                    # Clear breach flag at start of next day if P&L recovered (shouldn't happen intraday)
                    await self._redis.set(breach_key, "False")

        except Exception as e:
            logger.error(f"Stop Day Loss check error: {e}")

    # ── Barrier Evaluation ────────────────────────────────────────────────────

    async def _evaluate_barriers(self, symbol: str, tick: dict, state: dict):
        pos: dict = self.orphaned_positions.get(symbol)
        if not pos:
            return

        lifecycle = str(pos.get("lifecycle_class") or "KINETIC")
        if lifecycle == "POSITIONAL":
            await self._evaluate_positional_barriers(symbol, tick, state, pos)
        else:
            await self._evaluate_kinetic_barriers(symbol, tick, state, pos)

    async def _evaluate_kinetic_barriers(self, symbol: str, tick: dict, state: dict, pos: dict):
        price = tick.get("price", 0.0)
        entry = pos.get("price", price)
        elapsed = time.time() - pos.get("entry_time", time.time())
        action = pos.get("action", "BUY")

        # ── Barrier 0: Dynamic Slippage (RV-based Panic) ──────────────────────
        try:
            rv_raw = await self._redis.get("rv")
            rv = float(rv_raw) if rv_raw else 0.0
            if rv > 0.002:
                aggressive_price = float(price * 0.99)
                exit_reason = f"PANIC_VOL_EXIT: RV {rv:.5f} > 3σ"
                await self._record_barrier_exit(symbol, "PANIC", exit_reason, aggressive_price, float(entry))
                await self._attempt_exit(pos, symbol, price=aggressive_price, reason=exit_reason)
                return
        except Exception: pass

        rv_raw = await self._redis.get("rv")
        rv = float(rv_raw) if rv_raw else 0.0
        vix_raw = await self._redis.get("vix")
        vix = float(vix_raw) if vix_raw else 15.0
        atr_raw = await self._redis.get("atr")
        atr = float(atr_raw) if atr_raw else 20.0
        hmm_raw = await self._redis.get("hmm_regime")
        current_hmm = str(hmm_raw) if hmm_raw else "RANGING"

        sl_mult = float(1.5 if (rv > 0.001 or vix > 18.0) else ATR_SL_MULTIPLIER)
        tp1_mult = float(ATR_TP1_MULTIPLIER)
        tp2_mult = float(ATR_TP_MULTIPLIER)
        # Phase 15.2: Theta-Aware Dynamic Stall Timer
        dte_raw = pos.get("dte")
        dte = float(dte_raw) if dte_raw is not None else 7.0
        theta_scaling = 1.0 - (0.5 if dte < 1.0 else 0.0)
        stall_timer = (int(180 if (rv < 0.0005 or vix < 12.0) else STALL_TIMEOUT_SEC)) * theta_scaling

        # Phase 15.4: Invalidation Hunt for Runners
        runner_active = pos.get("runner_active", False)
        if runner_active:
            # Dynamic BE or Trailing SL based on structure
            prev_high = float(pos.get("local_high") or price)
            pos["local_high"] = max(prev_high, float(price))
            local_high_val = float(pos["local_high"])
            sl = entry + (local_high_val - entry) * 0.5  # Trail 50% of runaway profit
        else:
            sl = entry - sl_mult * atr

        entry_hmm = pos.get("entry_hmm", current_hmm)
        if "entry_hmm" not in pos: pos["entry_hmm"] = entry_hmm
        
        tp1 = float(entry + tp1_mult * atr)
        tp2 = float(entry + tp2_mult * atr)

        exit_reason = None
        is_partial = False
        
        if action == "BUY":
            if not runner_active and price >= tp1:
                exit_reason = f"TP1_HIT: {price:.2f} >= {tp1:.2f}"; is_partial = True
            elif runner_active:
                if price >= tp2: exit_reason = f"TP2_HIT: {price:.2f} >= {tp2:.2f}"
                elif price <= sl: exit_reason = f"INV_HUNT_SL: {price:.2f} <= {sl:.2f}"
                elif current_hmm != entry_hmm and current_hmm in ["RANGING", "CRASH"]:
                    exit_reason = f"HMM_SHIFT: {entry_hmm} -> {current_hmm}"
            elif price <= sl: exit_reason = f"SL_HIT: {price:.2f} <= {sl:.2f}"

        if not exit_reason and elapsed >= stall_timer:
            exit_reason = f"THETA_STALL: {elapsed:.0f}s >= {stall_timer:.0f}s"

        if exit_reason:
            await self._record_barrier_exit(symbol, "KINETIC", exit_reason, float(price), float(entry))
            if is_partial:
                await self._attempt_partial_exit(pos, symbol, float(price), exit_reason, 0.70)
            else:
                await self._attempt_exit(pos, symbol, float(price), exit_reason)

    async def _evaluate_positional_barriers(self, symbol: str, tick: dict, state: dict, pos: dict):
        """Hardened Positional Barriers (Spec 11.3)"""
        price = float(tick.get("price", 0.0))
        entry = float(pos.get("price", 0.0))
        parent_uuid = pos.get("parent_uuid")
        if not parent_uuid: return

        # 1. Structural Breach: Underlying breaches short strike
        short_strikes = pos.get("short_strikes") or {}
        spot_raw = state.get("spot")
        spot = float(spot_raw) if spot_raw is not None else price
        
        call_strike = short_strikes.get("call", 999999)
        put_strike = short_strikes.get("put", 0)
        
        if spot > call_strike or spot < put_strike:
            exit_reason = f"STRUCTURAL_BREACH: Spot {spot:.0f} outside [{put_strike}, {call_strike}]"
            await self._record_barrier_exit(symbol, "STRUCTURAL", exit_reason, price, entry)
            await self._attempt_exit(pos, symbol, price, exit_reason)
            return

        # 2. Spread Decay (Take Profit): Value <= 40% of initial credit
        initial_credit = float(pos.get("initial_credit", 0.0))
        if initial_credit > 0:
            current_value = price 
            if current_value / initial_credit <= 0.40:
                exit_reason = f"SPREAD_DECAY_TP: {current_value:.2f} <= 40% of {initial_credit:.2f}"
                await self._record_barrier_exit(symbol, "PROFIT_TAKING", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return

        # 3. Delta Tolerance Waterfall (Spec 11.4)
        net_delta_raw = state.get("net_delta")
        net_delta = float(net_delta_raw) if net_delta_raw is not None else 0.0
        if abs(net_delta) > 0.15:
            await self._execute_hedge_waterfall(pos, net_delta)

    async def _execute_hedge_waterfall(self, pos: dict, delta: float):
        """Hedge Waterfall Protocol (Spec 11.4)"""
        logger.info(f"🌊 Waterfall Protocol for {pos['symbol']} | Delta={delta:.2f}")
        pass

    # ── Exit Execution with HTTP 400 Handling ────────────────────────────────

    async def _attempt_partial_exit(self, pos: dict, symbol: str, price: float, reason: str, pct: float):
        qty = abs(pos.get("quantity", 0))
        exit_qty = int(qty * pct)
        if exit_qty == 0 or exit_qty == qty:
            await self._attempt_exit(pos, symbol, price, reason)
            return
            
        order = {
            "order_id": f"part_{uuid.uuid4().hex[:8]}",
            "symbol": symbol,
            "action": "SELL",
            "quantity": exit_qty,
            "order_type": "MARKET",
            "strategy_id": "LIQUIDATION_PARTIAL",
            "execution_type": pos.get("execution_type", "Paper"),
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason
        }
        
        try:
            await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, order)
            logger.info(f"✅ PARTIAL EXIT ORDER sent: SELL {exit_qty} {symbol} @ {price:.2f} ({reason})")
            pos["quantity"] = qty - exit_qty
            await self._redis.delete(f"Pending_Journal:{order['order_id']}")
            
            # Phase 15.5: Capital Unlocking
            # Notify margin/controller to release blocked capital
            await self._redis.publish("CAPITAL_RELEASE", json.dumps({
                "symbol": symbol,
                "parent_uuid": pos.get("parent_uuid"),
                "released_qty": exit_qty,
                "reason": "TP1_PARTIAL_EXIT"
            }))
        except Exception as e:
            logger.error(f"Partial exit order failed: {e}")

    async def _attempt_exit(self, pos: dict, symbol: str, price: float, reason: str):
        """Fires a SELL order with HTTP 400 mitigation."""
        qty = abs(pos.get("quantity", 0))
        if qty == 0:
            self.orphaned_positions.pop(symbol, None)
            return

        order = {
            "order_id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": "SELL",
            "quantity": qty,
            "order_type": "MARKET",
            "strategy_id": "LIQUIDATION",
            "execution_type": pos.get("execution_type", "Paper"),
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason
        }

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, order)
                logger.info(f"✅ EXIT ORDER sent: SELL {qty} {symbol} @ {price:.2f} ({reason})")
                self.orphaned_positions.pop(symbol, None)
                await self._redis.delete(f"Pending_Journal:{order['order_id']}")
                return
            except Exception as e:
                err_str = str(e).lower()
                http400_result = self._parse_http400_emsg(err_str)

                if http400_result == "circuit_limit":
                    logger.warning(f"HTTP 400 circuit limit. Recalculating price bounds. Attempt {attempt}.")
                    order["price"] = price * (0.98 if attempt % 2 == 0 else 1.02)
                    await asyncio.sleep(0.5 * attempt)
                elif http400_result == "abort":
                    err_msg = str(err_str)
                    logger.error(f"HTTP 400 ABORT (margin/payload error): {err_msg}")
                    asyncio.create_task(send_cloud_alert(
                        f"🆘 CRITICAL: EXIT ORDER ABORTED for {symbol}\nError: {err_msg[:200]}\nManual intervention required!",
                        alert_type="CRITICAL"
                    ))
                    self.orphaned_positions.pop(symbol, None)
                    return
                else:
                    logger.error(f"Exit order error (attempt {attempt}): {e}")
                    await asyncio.sleep(1)

        logger.error(f"❌ EXIT ORDER FAILED after {max_retries} attempts for {symbol}.")
        asyncio.create_task(send_cloud_alert(
            f"❌ EXIT ORDER EXHAUSTED: {symbol} | {reason}\nManual intervention required!",
            alert_type="ERROR"
        ))

    def _parse_http400_emsg(self, err_str: str) -> str:
        err_lower = err_str.lower()
        for pattern in CIRCUIT_LIMIT_STRINGS:
            if pattern in err_lower: return "circuit_limit"
        for pattern in ABORT_STRINGS:
            if pattern in err_lower: return "abort"
        return "retry"

    async def _record_barrier_exit(self, symbol: str, barrier: str, reason: str,
                                    exit_price: float, entry_price: float = 0.0):
        try:
            entry_f = float(entry_price) if entry_price else 0.0
            pnl_pts = float(exit_price - entry_f)
            record = json.dumps({
                "symbol": symbol,
                "barrier": barrier,
                "reason": reason,
                "exit_price": f"{exit_price:.2f}",
                "pnl_pts": f"{pnl_pts:.2f}",
                "ts": int(time.time()),
                "attribution": "LIQUIDATION_DAEMON"
            })
            await self._redis.lpush("barrier_exits", record)
            await self._redis.ltrim("barrier_exits", 0, 99) # Increased history
        except Exception as e:
            logger.error(f"Barrier exit record error: {e}")

    async def _telegram_alert(self, message: str):
        asyncio.create_task(send_cloud_alert(message, alert_type="LIQUIDATION"))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    daemon = LiquidationDaemon()
    asyncio.run(daemon.run())
