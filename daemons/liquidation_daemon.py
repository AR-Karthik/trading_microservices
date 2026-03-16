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
                "expiry_date": row['expiry_date'],
                "initial_credit": float(row['initial_credit'] or 0.0),
                "short_strikes": row['short_strikes'] or {},
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
                "expiry_date": row['expiry_date'],
                "initial_credit": float(row['initial_credit'] or 0.0),
                "short_strikes": row['short_strikes'] or {},
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
            # Fetch the user-configured limit (default ₹16,000 = 2% of ₹8,00,000 per spec §4.3)
            stop_limit = float(await self._redis.get("STOP_DAY_LOSS") or 16000.0)  # [F9-02]

            # Check both paper and live daily realized P&L + unrealized MTM
            for mode_suffix in ["PAPER", "LIVE"]:
                day_pnl_raw = await self._redis.get(f"DAILY_REALIZED_PNL_{mode_suffix}")
                day_pnl = float(day_pnl_raw or 0.0)
                # [F9-02] Include unrealized MTM as spec requires realized + unrealized
                unrealized_raw = await self._redis.get(f"DAILY_UNREALIZED_PNL_{mode_suffix}")
                unrealized = float(unrealized_raw or 0.0)
                total_pnl = day_pnl + unrealized
                breach_key = f"STOP_DAY_LOSS_BREACHED_{mode_suffix}"

                limit_val = float(stop_limit)
                if total_pnl <= -limit_val:
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

        vix_raw = await self._redis.get("vix")
        vix = float(vix_raw) if vix_raw else 15.0
        atr_raw = await self._redis.get("atr")
        atr = float(atr_raw) if atr_raw else 20.0
        hmm_raw = await self._redis.get("hmm_regime")
        current_hmm = str(hmm_raw) if hmm_raw else "RANGING"

        # ── Barrier 0: Systemic Vol Panic (RV-based) ──────────────────────────
        # [Audit 14.2] Applied to ALL lifecycle classes for 3-sigma safety
        try:
            rv_raw = await self._redis.get("rv")
            rv = float(rv_raw or 0.0)
            if rv > 0.002:
                price = tick.get("price", 0.0)
                action = pos.get("action", "BUY")
                entry = pos.get("price", price)
                aggressive_price = float(price * 0.99 if action == "BUY" else price * 1.01)
                exit_reason = f"PANIC_VOL_EXIT: RV {rv:.5f} > 3σ"
                await self._record_barrier_exit(symbol, "PANIC", exit_reason, aggressive_price, float(entry))
                await self._attempt_exit(pos, symbol, price=aggressive_price, reason=exit_reason)
                return
        except Exception: 
            rv = 0.0

        lifecycle = str(pos.get("lifecycle_class") or "KINETIC")
        if lifecycle == "POSITIONAL":
            await self._evaluate_positional_barriers(symbol, tick, state, pos, rv, vix, atr, current_hmm)
        elif lifecycle == "ZERO_DTE":
            # [S-03] ZERO_DTE-specific exit logic
            await self._evaluate_zero_dte_barriers(symbol, tick, state, pos, rv, vix, atr, current_hmm)
        else:
            await self._evaluate_kinetic_barriers(symbol, tick, state, pos, rv, vix, atr, current_hmm)

    async def _evaluate_kinetic_barriers(self, symbol: str, tick: dict, state: dict, pos: dict, rv: float, vix: float, atr: float, current_hmm: str):
        price = tick.get("price", 0.0)
        entry = pos.get("price", price)
        elapsed = time.time() - pos.get("entry_time", time.time())
        action = pos.get("action", "BUY")



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

        # Phase 13.3: Microstructure CVD Flip Exit
        try:
            cvd_raw = await self._redis.get("cvd_score")
            cvd = float(cvd_raw or 0.0)
            if (action == "BUY" and cvd < -CVD_FLIP_EXIT_THRESHOLD) or \
               (action == "SELL" and cvd > CVD_FLIP_EXIT_THRESHOLD):
                exit_reason = f"CVD_FLIP_EXIT: Score {cvd:.2f}"
                await self._record_barrier_exit(symbol, "CVD_FLIP", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return
        except Exception: pass

        
        if action == "BUY":
            if not runner_active and price >= tp1:
                exit_reason = f"TP1_HIT: {price:.2f} >= {tp1:.2f}"; is_partial = True
            elif runner_active:
                if price >= tp2: exit_reason = f"TP2_HIT: {price:.2f} >= {tp2:.2f}"
                elif price <= sl: exit_reason = f"INV_HUNT_SL: {price:.2f} <= {sl:.2f}"
                elif current_hmm != entry_hmm and current_hmm in ["RANGING", "CRASH"]:
                    exit_reason = f"HMM_SHIFT: {entry_hmm} -> {current_hmm}"
            elif price <= sl: exit_reason = f"SL_HIT: {price:.2f} <= {sl:.2f}"
        
        elif action == "SELL":
            # SELL logic (Short side)
            sl_short = entry + sl_mult * atr
            tp1_short = entry - tp1_mult * atr
            tp2_short = entry - tp2_mult * atr
            
            if not runner_active and price <= tp1_short:
                exit_reason = f"TP1_HIT_SHORT: {price:.2f} <= {tp1_short:.2f}"; is_partial = True
            elif runner_active:
                if price <= tp2_short: exit_reason = f"TP2_HIT_SHORT: {price:.2f} <= {tp2_short:.2f}"
                elif price >= sl_short: exit_reason = f"INV_HUNT_SL_SHORT: {price:.2f} >= {sl_short:.2f}"
                elif current_hmm != entry_hmm and current_hmm in ["RANGING", "BOOM"]:
                    exit_reason = f"HMM_SHIFT_SHORT: {entry_hmm} -> {current_hmm}"
            elif price >= sl_short: exit_reason = f"SL_HIT_SHORT: {price:.2f} >= {sl_short:.2f}"

        # [C4-04] Stagnation exit: must check 0.1 ATR price band + time elapsed
        if not exit_reason and elapsed >= stall_timer:
            # Check price range within the stall window
            tick_store_key = f"tick_history:{symbol}"  # [F3-04] Fixed: was 'ticks:', actual key is 'tick_history:'
            try:
                recent_prices_raw = await self._redis.lrange(tick_store_key, -50, -1)
                if recent_prices_raw and len(recent_prices_raw) >= 2:
                    recent_prices = [float(p) for p in recent_prices_raw]
                    price_range = max(recent_prices) - min(recent_prices)
                    if price_range < 0.1 * atr:
                        exit_reason = f"THETA_STALL: Range {price_range:.2f} < 0.1*ATR({atr:.2f}) within {elapsed:.0f}s"
                    # else: price moved enough, don't stall-exit
                else:
                    # Fallback: if we can't check range, use time-only
                    exit_reason = f"THETA_STALL: {elapsed:.0f}s >= {stall_timer:.0f}s (no range data)"
            except Exception:
                exit_reason = f"THETA_STALL: {elapsed:.0f}s >= {stall_timer:.0f}s"

        if exit_reason:
            await self._record_barrier_exit(symbol, "KINETIC", exit_reason, float(price), float(entry))
            if is_partial:
                await self._attempt_partial_exit(pos, symbol, float(price), exit_reason, 0.70)
            else:
                await self._attempt_exit(pos, symbol, float(price), exit_reason)

    async def _evaluate_positional_barriers(self, symbol: str, tick: dict, state: dict, pos: dict, rv: float, vix: float, atr: float, current_hmm: str):
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

        # 2. DTE Vertical Barrier (Spec 11.3)
        expiry_date = pos.get("expiry_date")
        if expiry_date:
            now = datetime.now(timezone.utc).date()
            dte = (expiry_date - now).days
            if dte < 3:
                exit_reason = f"DTE_VERTICAL: DTE={dte} < 3 days. Clearing positional risk."
                await self._record_barrier_exit(symbol, "TIME_LIMIT", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return

        # 3. Spread Decay (Take Profit): Value <= 40% of initial credit (= 60% decayed)
        initial_credit = float(pos.get("initial_credit", 0.0))
        strategy_id = str(pos.get("strategy_id", ""))
        if initial_credit > 0:
            current_value = price 
            # [S-04] DirectionalCredit uses 70% profit target (30% remaining)
            if strategy_id == "DirectionalCredit":
                tp_threshold = 0.30  # Exit when value <= 30% of credit (70% profit)
            else:
                tp_threshold = 0.40  # Iron Condor: 60% profit (40% remaining)
            
            if current_value / initial_credit <= tp_threshold:
                exit_reason = f"SPREAD_DECAY_TP: {current_value:.2f} <= {tp_threshold*100:.0f}% of {initial_credit:.2f}"
                await self._record_barrier_exit(symbol, "PROFIT_TAKING", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return
            
            # [S-04] DirectionalCredit 2× credit stop-loss
            if strategy_id == "DirectionalCredit":
                max_loss_value = initial_credit * 2.0
                if current_value >= max_loss_value:
                    exit_reason = f"CREDIT_STOP: Value {current_value:.2f} >= 2× credit {initial_credit:.2f}"
                    await self._record_barrier_exit(symbol, "STOP_LOSS", exit_reason, price, entry)
                    await self._attempt_exit(pos, symbol, price, exit_reason)
                    return

        # 4. Delta Tolerance Waterfall (Spec 11.4)
        net_delta_raw = state.get("net_delta")
        net_delta = float(net_delta_raw) if net_delta_raw is not None else 0.0
        if abs(net_delta) > 0.15:
            await self._execute_hedge_waterfall(pos, net_delta, rv)

    async def _evaluate_zero_dte_barriers(self, symbol: str, tick: dict, state: dict, pos: dict, rv: float, vix: float, atr: float, current_hmm: str):
        """
        [S-03] ZERO_DTE Exit Logic:
        - Profit Target: 50% of credit received
        - Aggressive Stop: Exit if underlying moves > 0.5% from inception
        """
        price = float(tick.get("price", 0.0))
        entry = float(pos.get("price", 0.0))

        # 1. Credit-based Take Profit (50%)
        initial_credit = float(pos.get("initial_credit", 0.0))
        if initial_credit > 0:
            current_value = price
            if current_value / initial_credit <= 0.50:  # 50% decayed = 50% profit
                exit_reason = f"ZERO_DTE_TP: Value {current_value:.2f} <= 50% of credit {initial_credit:.2f}"
                await self._record_barrier_exit(symbol, "ZERO_DTE_TP", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return

        # 2. Underlying Move Stop (0.5% from inception)
        inception_spot = float(pos.get("inception_spot", entry))
        if inception_spot > 0:
            move_pct = abs(price - inception_spot) / inception_spot * 100
            if move_pct > 0.5:
                exit_reason = f"ZERO_DTE_STOP: Spot moved {move_pct:.2f}% > 0.5% from inception"
                await self._record_barrier_exit(symbol, "ZERO_DTE_STOP", exit_reason, price, entry)
                await self._attempt_exit(pos, symbol, price, exit_reason)
                return

        # 3. Fallback to kinetic barriers for time-based stall
        elapsed = time.time() - pos.get("entry_time", time.time())
        if elapsed > 300:  # 5 min stall for 0DTE
            exit_reason = f"ZERO_DTE_STALL: {elapsed:.0f}s > 300s"
            await self._record_barrier_exit(symbol, "ZERO_DTE_STALL", exit_reason, price, entry)
            await self._attempt_exit(pos, symbol, price, exit_reason)

    async def _execute_hedge_waterfall(self, pos: dict, delta: float, rv: float):
        """Hedge Waterfall Protocol (Spec 11.4)"""
        # Step 1: Rapid assessment
        symbol = pos['symbol']
        
        logger.warning(f"🌊 Waterfall Protocol for {symbol} | Delta={delta:.2f} | RV={rv:.4f}")

        if abs(delta) > 0.30 or rv > 0.003:
             # Critical breach: liquidate immediately rather than hedging
             exit_reason = f"WATERFALL_PANIC: Delta {delta:.2f} or RV {rv:.4f} too high."
             await self._attempt_exit(pos, symbol, 0, exit_reason)
             return

        # Moderate breach: Dispatch neutralizing Hedge Order (Micro-Futures)
        # Sizing: Target 0.0 delta. Since we are at ±0.15, we need to buy/sell ~0.15 of underlying notional.
        # [Audit 11.4] Hedge intent must specify reason to avoid reconciler collisions
        hedge_action = "BUY" if delta < 0 else "SELL"
        hedge_qty = max(1, int(abs(delta) * 50)) # Proxy multiplier for micro-lot conversion
        
        hedge_order = {
            "order_id": f"hedge_{uuid.uuid4().hex[:8]}",
            "symbol": f"{pos.get('underlying', 'NIFTY50')}-FUT",
            "action": hedge_action,
            "quantity": hedge_qty,
            "order_type": "MARKET",
            "strategy_id": "DELTA_HEDGE",
            "execution_type": pos.get("execution_type", "Paper"),
            "parent_uuid": pos.get("parent_uuid"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": f"WATERFALL_HEDGE: Delta={delta:.2f}"
        }

        try:
            await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, hedge_order)
            logger.info(f"🛡️ STEP 1 HEDGE DISPATCHED: {hedge_action} {hedge_qty} Micro-FUT for {symbol}")
            # Mark that we've hedged to prevent spamming
            pos["last_hedge_ts"] = time.time()
            pos["net_delta_at_hedge"] = delta
        except Exception as e:
            logger.error(f"Step 1 hedge dispatch failure: {e}")

        # [C4-02] Step 2: If margin unavailable, roll untested IC side
        # [F3-01] Fixed: use actual Redis keys (CASH_COMPONENT_LIVE + COLLATERAL_COMPONENT_LIVE)
        cash_raw = await self._redis.get("CASH_COMPONENT_LIVE")
        coll_raw = await self._redis.get("COLLATERAL_COMPONENT_LIVE")
        margin_avail = float(cash_raw or 0) + float(coll_raw or 0)
        if margin_avail < 10000:  # Not enough margin for futures hedge
            logger.warning(f"🔄 WATERFALL STEP 2: Margin insufficient ({margin_avail:.0f}). Attempting IC Roll.")
            # Publish a roll intent for the untested wing
            roll_side = "PE" if delta > 0 else "CE"  # Roll opposite to breach direction
            roll_order = {
                "order_id": f"roll_{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "action": "ROLL",
                "side": roll_side,
                "parent_uuid": pos.get("parent_uuid"),
                "strategy_id": "DELTA_ROLL",
                "execution_type": pos.get("execution_type", "Paper"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": f"WATERFALL_ROLL: Delta={delta:.2f}, margin={margin_avail:.0f}"
            }
            try:
                await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, roll_order)
                logger.info(f"🔄 ROLL DISPATCHED: {roll_side} wing for {symbol}")
            except Exception as e:
                logger.error(f"Step 2 roll failure: {e}")

        # [C4-03] Step 3: Pro-rata slash if delta still extreme or margin > 90%
        total_cap_raw = await self._redis.get("LIVE_CAPITAL_LIMIT")
        total_cap = float(total_cap_raw or 800000)
        # [F3-02] Compute margin utilization from (total capital - available margin)
        cash_avail_raw = await self._redis.get("CASH_COMPONENT_LIVE")
        coll_avail_raw = await self._redis.get("COLLATERAL_COMPONENT_LIVE")
        margin_available = float(cash_avail_raw or 0) + float(coll_avail_raw or 0)
        margin_util = max(0, total_cap - margin_available)
        if abs(delta) >= 0.25 or (total_cap > 0 and (margin_util / total_cap) > 0.90):
            logger.warning(f"🔪 WATERFALL STEP 3: Pro-rata 25% slash. Delta={delta:.2f}, Margin%={margin_util/max(total_cap,1)*100:.0f}%")
            await self._attempt_partial_exit(pos, symbol, 0, f"PRORATA_SLASH: Delta={delta:.2f}", 0.25)

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
