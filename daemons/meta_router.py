"""
daemons/meta_router.py
======================
HMM Dispatcher & GIL Mitigation (SRS §2.4)

Architecture:
  Main asyncio loop   → ZMQ I/O only; reads regime from Queue; dispatches commands
  HmmProcess         → Isolated multiprocessing.Process running GMM-HMM inference
  IPC                → multiprocessing.Queue

Regime states: TRENDING | RANGING | CRASH

Vetoes enforced:
  - Dispersion Veto: dispersion_coeff < 0.30 → block momentum, limit MR to 1 lot
  - OI Wall Veto: spot < 15pts from top-3 Call/Put OI strikes → block all buys
  - Macro Lockdown: MACRO_EVENT_LOCKDOWN=True → neutralise CRASH detection
"""

import asyncio
import collections
import json
import logging
import multiprocessing as mp
import sys
import time
from datetime import datetime
from typing import Any

import numpy as np
import redis

from core.mq import MQManager, Ports, Topics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("MetaRouter")

# ── HMM Compute Process ───────────────────────────────────────────────────────

def _hmm_worker(in_queue: mp.Queue, out_queue: mp.Queue):
    """
    Isolated OS process. Runs GMM-HMM inference on rolling feature matrix.
    Completely bypasses GIL for heavy matrix operations.
    """
    import logging
    import numpy as np
    logger = logging.getLogger("HmmWorker")
    logging.basicConfig(level=logging.WARNING)

    try:
        from hmmlearn.hmm import GMMHMM  # type: ignore
        _HAS_HMM = True
    except ImportError:
        _HAS_HMM = False
        logger.warning("hmmlearn not installed. HMM falling back to heuristic regime detection.")

    REGIME_STATES = {0: "RANGING", 1: "TRENDING", 2: "CRASH"}  # map HMM state → label
    FEATURE_LEN = 60  # minimum ticks before HMM inference

    # Initialize HMM (will be retrained as data accumulates)
    hmm_model = None
    feature_buffer: list[list[float]] = []

    def heuristic_regime(features: list[list[float]]) -> str:
        """Fallback when hmmlearn not available."""
        arr = np.array(features)
        ofi_z = arr[:, 0]
        rv = arr[:, 1]
        if np.abs(ofi_z.mean()) > 1.5 and rv.mean() > 0.001:
            return "TRENDING"
        if rv.mean() > 0.003:
            return "CRASH"
        return "RANGING"

    def train_hmm(X: np.ndarray) -> Any:
        """Train / retrain GMM-HMM model."""
        try:
            model = GMMHMM(
                n_components=3,      # TRENDING, RANGING, CRASH
                n_mix=2,             # Gaussian mixtures per state
                covariance_type="diag",
                n_iter=20,
                random_state=42
            )
            lengths = [len(X)]
            model.fit(X, lengths)
            return model
        except Exception as e:
            logger.error(f"HMM training failed: {e}")
            return None

    while True:
        try:
            snapshot = in_queue.get(timeout=1.0)
            if snapshot is None:
                break

            feat = snapshot.get("features")  # [log_ofi_z, rv, basis_z, vol_term_ratio]
            if not feat or len(feat) < 4:
                out_queue.put({"regime": "RANGING"})
                continue

            feature_buffer.append(feat)
            # Keep rolling window of 300 observations
            if len(feature_buffer) > 300:
                feature_buffer = feature_buffer[-300:]

            if len(feature_buffer) < FEATURE_LEN:
                out_queue.put({"regime": "RANGING"})  # cold-start fallback
                continue

            X = np.array(feature_buffer)

            if not _HAS_HMM:
                regime = heuristic_regime(feature_buffer[-30:])
                out_queue.put({"regime": regime})
                continue

            # Retrain periodically (every 50 new obs)
            if hmm_model is None or len(feature_buffer) % 50 == 0:
                hmm_model = train_hmm(X)

            if hmm_model is None:
                regime = heuristic_regime(feature_buffer[-30:])
                out_queue.put({"regime": regime})
                continue

            # Decode most probable state sequence
            try:
                log_prob, state_seq = hmm_model.decode(X, lengths=[len(X)])
                latest_state = int(state_seq[-1])
                regime = REGIME_STATES.get(latest_state, "RANGING")
            except Exception:
                regime = heuristic_regime(feature_buffer[-10:])

            out_queue.put({
                "regime": regime,
                "log_likelihood": float(log_prob) if hmm_model else 0.0
            })

        except mp.queues.Empty:
            continue
        except Exception as e:
            logger.error(f"HMM worker error: {e}")
            out_queue.put({"regime": "RANGING"})


# ── Meta Router ───────────────────────────────────────────────────────────────

class MetaRouter:
    def __init__(self, test_mode: bool = False):
        self.mq = MQManager()
        self.test_mode = test_mode

        if not test_mode:
            self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)
            self._redis = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

        self.current_regime: str = "RANGING"
        self._orphaned_lunch = False
        self._orphaned_eod = False

        # HMM subprocess
        self._hmm_in: mp.Queue = mp.Queue(maxsize=30)
        self._hmm_out: mp.Queue = mp.Queue(maxsize=30)
        self._hmm_proc: mp.Process | None = None

        # Feature rolling buffer for HMM input
        self._feature_buf: collections.deque = collections.deque(maxlen=300)

    # ── Macro Windows ─────────────────────────────────────────────────────────

    def check_macro_windows(self) -> tuple[bool, bool]:
        """
        Returns (is_entry_allowed, should_orphan).
        Active windows: 09:30–11:30 and 13:30–15:00.
        """
        now = datetime.now()
        t = now.strftime("%H:%M")

        is_entry_allowed = ("09:30" <= t <= "11:30") or ("13:30" <= t <= "15:00")

        should_orphan = (
            (t == "11:30" and not self._orphaned_lunch) or
            (t == "15:00" and not self._orphaned_eod)
        )
        if t == "11:30":
            self._orphaned_lunch = True
        if t == "15:00":
            self._orphaned_eod = True
        if t == "12:00":
            self._orphaned_lunch = False
        if t == "16:00":
            self._orphaned_eod = False

        return is_entry_allowed, should_orphan

    # ── Veto Checks ───────────────────────────────────────────────────────────

    def _check_dispersion_veto(self) -> tuple[bool, bool]:
        """
        Returns (momentum_vetoed, mr_restricted_to_1lot).
        True if dispersion_coeff < 0.30 (correlated market = momentum strategies unreliable).
        """
        try:
            disp = float(self._redis.get("dispersion_coeff") or 0.5)
        except Exception:
            disp = 0.5
        if disp < 0.30:
            return True, True
        return False, False

    def _check_oi_wall_veto(self, spot: float) -> bool:
        """
        Returns True if spot is < 15pts from any top-3 Call/Put OI wall.
        Blocks fresh buy entries near OI walls.
        """
        try:
            walls_raw = self._redis.get("oi_walls")
            if not walls_raw:
                return False
            walls: list[float] = json.loads(walls_raw)
            for wall in walls[:6]:
                if abs(spot - wall) < 15:
                    logger.warning(f"OI_WALL_VETO: Spot {spot:.0f} within 15pts of wall {wall:.0f}")
                    return True
        except Exception:
            pass
        return False

    def _check_macro_lockdown(self) -> bool:
        """Returns True if MACRO_EVENT_LOCKDOWN is active in Redis."""
        try:
            val = self._redis.get("MACRO_EVENT_LOCKDOWN")
            return str(val).lower() == "true"
        except Exception:
            return False

    # ── HMM Output Drain ─────────────────────────────────────────────────────

    def _drain_hmm_output(self):
        """Non-blocking drain of HMM output queue."""
        while True:
            try:
                result = self._hmm_out.get_nowait()
                raw_regime = result.get("regime", "RANGING")

                # Macro lockdown neutralises CRASH detection
                macro_locked = self._check_macro_lockdown() if not self.test_mode else False
                if raw_regime == "CRASH" and macro_locked:
                    logger.info("MACRO_LOCKDOWN active: suppressing CRASH regime → RANGING")
                    raw_regime = "RANGING"

                if raw_regime != self.current_regime:
                    logger.info(f"Regime transition: {self.current_regime} → {raw_regime}")
                    self.current_regime = raw_regime
                    if not self.test_mode:
                        self._redis.set("hmm_regime", raw_regime)
            except mp.queues.Empty:
                break

    # ── Command Broadcast ─────────────────────────────────────────────────────

    async def broadcast(
        self,
        state: dict,
        should_orphan: bool = False,
        momentum_vetoed: bool = False,
        mr_1lot: bool = False,
        oi_wall_veto: bool = False
    ) -> list[dict]:
        commands: list[dict] = []
        spot = state.get("spot", 0.0)
        gex_sign = state.get("gex_sign", "POSITIVE")
        regime = self.current_regime

        if should_orphan:
            logger.warning("MACRO BOUNDARY: Issuing ORPHAN to all strategies.")
            commands = [
                {"target": "STRAT_GAMMA", "command": "ORPHAN"},
                {"target": "STRAT_REVERSION", "command": "ORPHAN"},
                {"target": "STRAT_VWAP", "command": "ORPHAN"},
                {"target": "STRAT_OI_PULSE", "command": "ORPHAN"},
                {"target": "STRAT_LEAD_LAG", "command": "ORPHAN"},
            ]
        else:
            # Long Gamma Momentum: NEG GEX + TRENDING
            if gex_sign == "NEGATIVE" and regime == "TRENDING" and not momentum_vetoed and not oi_wall_veto:
                commands.append({"target": "STRAT_GAMMA", "command": "ACTIVATE", "regime": regime})
            else:
                commands.append({"target": "STRAT_GAMMA", "command": "PAUSE"})

            # Institutional Fade: POS GEX + RANGING
            if gex_sign == "POSITIVE" and regime == "RANGING" and not oi_wall_veto:
                cmd = {"target": "STRAT_REVERSION", "command": "ACTIVATE",
                       "lot_override": 1 if mr_1lot else None, "regime": regime}
                commands.append(cmd)
            else:
                commands.append({"target": "STRAT_REVERSION", "command": "PAUSE"})

            # VWAP: activated on high alpha (|s_total| > 40)
            s_total = abs(state.get("s_total", 0))
            if s_total > 40 and not oi_wall_veto:
                commands.append({"target": "STRAT_VWAP", "command": "ACTIVATE", "regime": regime})
            else:
                commands.append({"target": "STRAT_VWAP", "command": "PAUSE"})

            # OI Pulse & Lead-Lag: all regimes (if no OI wall veto on spot)
            oi_cmd = "PAUSE" if oi_wall_veto else "ACTIVATE"
            commands.append({"target": "STRAT_OI_PULSE", "command": oi_cmd, "regime": regime})
            commands.append({"target": "STRAT_LEAD_LAG", "command": "ACTIVATE", "regime": regime})

        if not self.test_mode:
            for cmd in commands:
                target = cmd.get("target", "ALL")
                await self.mq.send_json(self.cmd_pub, cmd, topic=target)

        return commands

    # ── Main Run Loop ─────────────────────────────────────────────────────────

    async def run(self):
        if not self.test_mode:
            self._hmm_proc = mp.Process(
                target=_hmm_worker,
                args=(self._hmm_in, self._hmm_out),
                daemon=True,
                name="HmmWorker"
            )
            self._hmm_proc.start()
            logger.info(f"HMM subprocess started (PID: {self._hmm_proc.pid})")

        logger.info("MetaRouter active. Subscribing to MARKET_STATE...")
        sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[Topics.MARKET_STATE, "STATE"])
        tick_count = 0

        try:
            while True:
                try:
                    _, state = await self.mq.recv_json(sub)
                    if not state:
                        await asyncio.sleep(0.1)
                        continue

                    tick_count += 1

                    # Drain HMM results (non-blocking — doesn't block I/O loop)
                    if not self.test_mode:
                        self._drain_hmm_output()

                    # Feed features to HMM process every 10 states
                    if tick_count % 10 == 0 and not self.test_mode:
                        feat = [
                            state.get("log_ofi_zscore", 0.0),
                            state.get("rv", 0.0),
                            state.get("basis_zscore", 0.0),
                            state.get("vol_term_ratio", 1.0)
                        ]
                        try:
                            self._hmm_in.put_nowait({"features": feat})
                        except mp.queues.Full:
                            pass

                    # Veto checks
                    spot = state.get("spot", 22000.0)
                    momentum_vetoed, mr_1lot, oi_wall_veto = False, False, False
                    if not self.test_mode:
                        momentum_vetoed, mr_1lot = self._check_dispersion_veto()
                        oi_wall_veto = self._check_oi_wall_veto(spot)

                    # Macro window check
                    is_entry_allowed, should_orphan = self.check_macro_windows()
                    if not is_entry_allowed and not should_orphan:
                        await asyncio.sleep(0.5)
                        continue

                    await self.broadcast(state, should_orphan, momentum_vetoed, mr_1lot, oi_wall_veto)

                    await asyncio.sleep(0.25)

                except Exception as e:
                    logger.error(f"MetaRouter loop error: {e}")
                    await asyncio.sleep(1)
        finally:
            if self._hmm_proc and self._hmm_proc.is_alive():
                self._hmm_in.put(None)
                self._hmm_proc.join(timeout=5)
            sub.close()


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    router = MetaRouter()
    asyncio.run(router.run())
