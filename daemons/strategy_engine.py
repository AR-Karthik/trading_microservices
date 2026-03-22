"""
Strategy Engine Daemon (Refactored) — Thin Launcher
Delegates all logic to core/strategy/ modules:
  - SignalDispatcher: SHM reads + snapshot construction
  - KnightRegistry: Config loading, signal dispatch, vetoes, sizing
  - JITWarmupManager: Pre-subscription for nearby strikes

All strategy classes have been extracted into core/strategy/*_knight.py.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone

import zmq
import zmq.asyncio

import redis.asyncio as redis
from core.mq import MQManager, Ports, RedisLogger, Topics, NumpyEncoder
from core.shared_memory import TickSharedMemory, SYMBOL_TO_SLOT
from core.shm import ShmManager, RegimeShm
from core.margin import AsyncMarginManager
from core.strategy.signal_dispatcher import SignalDispatcher
from core.strategy.registry import KnightRegistry
from core.options.jit_manager import JITWarmupManager
from core.options.chain_utils import INDEX_METADATA

try:
    import uvloop
except ImportError:
    uvloop = None

try:
    from core.logger import setup_logger
    logger = setup_logger("StrategyEngine", log_file="logs/strategy_engine.log")
except Exception:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger = logging.getLogger("StrategyEngine")

redis_logger = RedisLogger()

IS_SHADOW_MODE = "--shadow" in sys.argv

# ── Vol Gap Calibration (B2) ──────────────────────────────────────────────────

async def _calibrate_vol_context(redis_client):
    """B2: Pre-market Rolling Z-Score normalization of the overnight gap."""
    symbols = ["NIFTY50", "BANKNIFTY", "SENSEX"]
    calibrated = 0
    for symbol in symbols:
        try:
            prev_close_raw = await redis_client.get(f"prev_close:{symbol}")
            if not prev_close_raw: continue
            prev_close = float(prev_close_raw)
            if prev_close <= 0: continue

            tick_raw = await redis_client.lindex(f"tick_history:{symbol}", -1)
            if not tick_raw: continue
            tick = json.loads(tick_raw)
            open_price = float(tick.get("price", 0) or 0)
            if open_price <= 0: continue

            rolling_std_raw = await redis_client.get(f"rolling_std_20d:{symbol}")
            rolling_std = float(rolling_std_raw or 50.0)
            if rolling_std <= 0: rolling_std = 50.0

            gap_z = (open_price - prev_close) / rolling_std
            await redis_client.set(f"VOL_GAP_Z:{symbol}", round(gap_z, 4), ex=7200)
            logger.info(f"📊 Vol Gap Calibration [{symbol}]: Z={gap_z:.2f}")
            calibrated += 1
        except Exception as e:
            logger.warning(f"B2 calibration skipped for {symbol}: {e}")

    if calibrated > 0:
        logger.info(f"✅ B2 Vol Context: {calibrated}/{len(symbols)} symbols calibrated.")
    else:
        logger.warning("⚠️ B2 Vol Context: No symbols calibrated. Proceeding with defaults.")


# ── Main Engine Loop ──────────────────────────────────────────────────────────

async def run_engine(
    sub_socket, push_socket, cmd_socket, hedge_socket,
    mq_manager: MQManager, registry: KnightRegistry,
    dispatcher: SignalDispatcher, jit_manager: JITWarmupManager,
    redis_client, shm_ticks, asset_filter=None,
):
    """Main tick loop: reads ticks, builds snapshots, dispatches to Knights."""
    logger.info("Starting Strategy Engine loop... (v6.0 Knight Architecture)")

    shm_slots = {}
    last_shm_sync = 0
    last_sequence_ids = {}
    last_config_sync = 0

    async def handle_commands():
        while True:
            try:
                topic, cmd = await mq_manager.recv_json(cmd_socket)
                if cmd:
                    registry.set_strategy_state(
                        cmd.get("target"),
                        cmd.get("command", "ACTIVATE"),
                        cmd.get("lots", 1),
                    )
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Command Handler Error: {e}")
                await asyncio.sleep(0.1)

    asyncio.create_task(handle_commands())

    while True:
        try:
            topic, tick_msg = await mq_manager.recv_json(sub_socket)
            if not tick_msg:
                continue

            # ── Execution Reports → Position Updates ──────────────────
            if topic.startswith("EXEC."):
                qty = int(tick_msg.get("quantity", 0))
                action = tick_msg.get("action", "BUY")
                strat_id = tick_msg.get("strategy_id")
                symbol = tick_msg.get("symbol")
                change = qty if action == "BUY" else -qty
                registry.update_position(strat_id, symbol, change)

                # Record inception_spot
                try:
                    underlying = "NIFTY50"
                    if "BANKNIFTY" in symbol: underlying = "BANKNIFTY"
                    elif "SENSEX" in symbol: underlying = "SENSEX"
                    spot_raw = await redis_client.get(f"ltp:{underlying}")
                    if spot_raw:
                        await redis_client.hset(f"POSITION_STATE:{symbol}", "inception_spot", spot_raw)
                except Exception as e:
                    logger.error(f"Inception spot error: {e}")
                continue

            # ── SHM Slot Sync ─────────────────────────────────────────
            if time.time() - last_shm_sync > 10:
                shm_slots_raw = await redis_client.hgetall("shm_slots")
                shm_slots = {k: int(v) for k, v in shm_slots_raw.items()}
                last_shm_sync = time.time()

            # ── Config Sync (every 2s) ────────────────────────────────
            if time.time() - last_config_sync > 2:
                await registry.sync_config()
                last_config_sync = time.time()

            symbol = tick_msg.get("symbol")
            if asset_filter and symbol != asset_filter:
                continue

            # ── SHM Tick Read ─────────────────────────────────────────
            tick = tick_msg
            if shm_ticks and symbol in shm_slots:
                shm_tick = shm_ticks.read_tick(shm_slots[symbol])
                if shm_tick:
                    tick = shm_tick

            # ── Sequence Guard ────────────────────────────────────────
            curr_seq = tick.get("sequence_id", 0)
            last_seq = last_sequence_ids.get(symbol, 0)
            if curr_seq < last_seq:
                logger.warning(f"🔄 GATEWAY_RESTART: {symbol} sequence reset")
                last_sequence_ids[symbol] = curr_seq
            elif 0 < curr_seq <= last_seq:
                continue
            else:
                last_sequence_ids[symbol] = curr_seq

            # ── Build Snapshot (single SHM read for all Knights) ──────
            signal = await dispatcher.build_snapshot(symbol, tick)

            # ── JIT Warmup ────────────────────────────────────────────
            await jit_manager.check_and_warmup(symbol, signal.asto, signal.price)

            # ── Dispatch to Knights ───────────────────────────────────
            orders = await registry.dispatch(symbol, signal)

            for order in orders:
                action = order.get("action", "WAIT")

                # Route special actions
                if action == "HEDGE_REQUEST":
                    await mq_manager.send_json(hedge_socket, Topics.HEDGE_REQUEST, order)
                    continue
                elif action == "TRAP_ALERT":
                    await redis_client.publish("panic_channel", json.dumps(order))
                    continue

                # ── Slippage Halt Guard ────────────────────────────────
                if await redis_client.get("SLIPPAGE_HALT") == "True":
                    logger.warning(f"⏸️ SLIPPAGE HALT: {order.get('strategy_id')} blocked")
                    continue

                # ── Margin Check ──────────────────────────────────────
                price = order.get("price", 0)
                qty = order.get("quantity", 1)
                exec_type = order.get("execution_type", "Paper")
                if action == "BUY":
                    margin_mgr = AsyncMarginManager(redis_client)
                    if not await margin_mgr.reserve(price * qty, exec_type):
                        logger.error(f"❌ MARGIN REJECTED: {order.get('strategy_id')}")
                        continue

                # ── Dispatch to Bridge ────────────────────────────────
                dispatch_meta = order.copy()
                dispatch_meta["dispatch_time_epoch"] = time.time()
                dispatch_meta["broker_order_id"] = None

                await mq_manager.send_json(push_socket, Topics.ORDER_INTENT, order)
                await redis_client.hset("pending_orders", order["order_id"],
                                         json.dumps(dispatch_meta, cls=NumpyEncoder))
                logger.info(f"DISPATCHED {action} {qty} {symbol} @ {price} | {order.get('lifecycle_class')}")

        except zmq.Again:
            continue
        except Exception as e:
            logger.error(f"Engine Loop Error: {e}")
            await asyncio.sleep(0.1)


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def start_engine():
    import argparse
    parser = argparse.ArgumentParser(description="K.A.R.T.H.I.K. Strategy Engine (v6.0)")
    parser.add_argument("--asset", type=str, help="Target index (e.g. NIFTY50)")
    parser.add_argument("--core", type=int, help="CPU core to pin")
    parser.add_argument("--shadow", action="store_true", help="Shadow mode")
    args = parser.parse_args()

    # Core pinning
    if args.core is not None:
        try:
            import psutil
            psutil.Process().cpu_affinity([args.core])
            logger.info(f"🎯 CORE PINNED to Core {args.core}")
        except Exception as e:
            logger.warning(f"Core Pinning failed: {e}")

    mq = MQManager()
    from core.auth import get_redis_url
    redis_url = get_redis_url()
    redis_client = redis.from_url(redis_url, decode_responses=True)

    # ── SHM Managers ──────────────────────────────────────────────────
    target_assets = [args.asset] if args.asset else list(INDEX_METADATA.keys())
    shm_alpha = {idx: ShmManager(asset_id=idx, mode='r') for idx in target_assets}
    shm_regime = {idx: RegimeShm(asset_id=idx, mode='r') for idx in target_assets}
    shm_alpha["GLOBAL"] = ShmManager(asset_id="GLOBAL", mode='r')

    try:
        shm_ticks = TickSharedMemory(create=False)
    except Exception as e:
        logger.warning(f"SHM Ticks not found: {e}")
        shm_ticks = None

    # ── MQ Sockets ────────────────────────────────────────────────────
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.", "EXEC."])
    push_socket = mq.create_push(Ports.ORDERS)
    hedge_socket = mq.create_push(Ports.HEDGE_REQUEST)
    cmd_socket = mq.create_subscriber(Ports.SYSTEM_CMD, topics=["STRAT_", "ALL"], bind=True)

    # ── Core Components ───────────────────────────────────────────────
    dispatcher = SignalDispatcher(shm_alpha, shm_regime, redis_client)
    registry = KnightRegistry(redis_client, mq)
    jit_manager = JITWarmupManager(redis_client)

    # ── Heartbeat ─────────────────────────────────────────────────────
    from core.health import HeartbeatProvider
    hb = HeartbeatProvider("StrategyEngine", redis_client)
    asyncio.create_task(hb.run_heartbeat())

    # ── B2: Vol Gap Calibration ───────────────────────────────────────
    await _calibrate_vol_context(redis_client)

    # ── Initial Config Load ───────────────────────────────────────────
    await registry.sync_config()

    try:
        await run_engine(
            sub_socket, push_socket, cmd_socket, hedge_socket,
            mq, registry, dispatcher, jit_manager,
            redis_client, shm_ticks, asset_filter=args.asset,
        )
    finally:
        await redis_client.aclose()
        sub_socket.close()
        push_socket.close()
        hedge_socket.close()
        cmd_socket.close()
        mq.context.term()


if __name__ == "__main__":
    try:
        if uvloop:
            uvloop.install()
        elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(start_engine())
    except KeyboardInterrupt:
        pass
