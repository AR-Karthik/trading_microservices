"""One-shot script to append v7.0 sections to both specification docs."""
import os

BASE = os.path.join(os.path.dirname(__file__), "..", "Docs")

FS_APPEND = """

---

## 9. v7.0 System Improvements

### 9.1 A1 — Global Portfolio Heat Constraint `[meta_router.py]`

- **Trigger**: After all three Tri-Brain signals are collected in `broadcast_decisions()`, the sum of fractional Kelly weights is calculated.
- **Logic**: If `total_heat > CONFIG:MAX_PORTFOLIO_HEAT` (Redis key, default `0.5`), all weights are scaled: `weight = weight * (heat_limit / total_heat)`.
- **Status Key**: `PORTFOLIO_HEAT_CAPPED` (True/False, 60s TTL) — visible in the Risk pane.
- **Configuration**: `SET CONFIG:MAX_PORTFOLIO_HEAT 0.5` in Redis.

> **Rationale:** When NIFTY, BANKNIFTY, and SENSEX all fire simultaneously during macro events, the combined margin from three independent Half-Kelly allocations can be 3x any single position. In a 90%-correlated basket, a 10-sigma event hits all three simultaneously. The Heat Constraint treats the Tri-Brain as a single correlated portfolio and enforces a portfolio-level risk cap.

### 9.2 A2 — Slippage Budget `[liquidation_daemon.py, strategy_engine.py]`

- **Monitor**: `_monitor_fill_slippage()` coroutine subscribes to `order_confirmations` channel.
- **Trigger**: If `|fill_price - intended_price| / intended_price > 2%`, sets `SLIPPAGE_HALT = True` in Redis with 60s TTL.
- **Guard**: `StrategyEngine.run_strategies()` checks `SLIPPAGE_HALT` before every new entry dispatch. If True, signal is dropped.
- **Alert**: Telegram notification fires automatically on slippage breach.

> **Rationale:** During Panic liquidations (Barrier 3), large market orders can suffer extreme slippage in thin option books. Without a pause, the system continues firing entries into an already-deteriorating book, turning a controlled stop into cascading bad fills. The 60-second pause allows orderbook depth to reform.

### 9.3 B2 — HMM Warm-Up Gap Normalization `[strategy_engine.py]`

- **Trigger**: `_calibrate_vol_context()` runs at 09:00-09:14 pre-market, before the JIT priming phase.
- **Logic**: For each Tri-Brain index, reads `prev_close:{symbol}`, `tick_history:{symbol}` (first tick of day), and `rolling_std_20d:{symbol}`. Computes:
  ```
  gap_z = (open_price - prev_close) / rolling_std_20d
  ```
  Writes `VOL_GAP_Z:{symbol}` to Redis with 2-hour TTL. Fallback: if `rolling_std_20d` is missing, uses 50.0. If `prev_close` is missing, skips that symbol gracefully.

> **Rationale:** The HMM carries no overnight context. A +200pt NIFTY gap-up looks identical to a +200pt intraday trend, but gap-up opens frequently see mean-reversion in the first 30 minutes. Calibrating the Vol Context at open prevents the HMM from prematurely declaring TRENDING into what is structurally a reversion setup.

### 9.4 C1 — Theta-Aware Dynamic Stall Timer `[liquidation_daemon.py]`

- **Normal timer**: `STALL_TIMEOUT_SEC = 300` seconds (Barrier 2, SS6.2).
- **Expiry Days (Wed/Thu)**: Timer decays linearly as market close approaches:
  ```
  expiry_stall = max(60, timer * (minutes_to_close / 360))
  ```
  At 09:30 (360 min to close): timer unchanged = 300s. At 15:20 (10 min to close): = 60s floor.
- **Log Warning**: Emitted when stall timer drops below 120s.

> **Rationale:** On expiry days, Theta decay is exponential and concentrated in the final 60-90 minutes. A position stalling at 14:45 faces double the Theta cost per minute versus a midday stall. The standard 5-minute timer actively destroys value by 14:30. Dynamic decay ensures system patience is inversely proportional to remaining time value.

### 9.5 C2 — Automated Orphaned Order Reconciliation `[order_reconciler.py]`

Supersedes the legacy SS7.2 "Orphaned Intent Audit" with deterministic auto-resolution:

- **Trigger**: `_reboot_audit()` runs at every `OrderReconciler.start()` before the normal polling loop.
- **Logic**: Reads all entries from `pending_orders` hash. For each:

| Broker Status | Action |
|---|---|
| `COMPLETE` (Filled) | Confirm state. If `action=BUY`, publish `HANDOFF` to `LiquidationDaemon` via ZMQ. |
| `OPEN` | Cancel at broker via `api.cancel_order()`. Clean up locally via `_mark_order_failed()`. |
| `PENDING` / `PHANTOM` | Treat as phantom. Reverse margin. Push Telegram alert. |

- **Helpers**: `_handoff_to_liquidation_daemon(meta, fill_price)`, `_cancel_open_order(broker_order_id, order_id, meta)`.
- **Fallback**: If broker API is unavailable, `OPEN` orders are still cleaned locally.

> **Rationale:** Before v7.0, the boot audit only surfaced orphans via Telegram for human review. In auto-restart scenarios (GCP preemption, OOM kill), waiting for human review could mean missing the entire morning session. Deterministic reconciliation self-heals in under 10 seconds and resumes managing surviving positions immediately.
"""

TS_APPEND = """

---

## 10. v7.0 Technical Additions

### 10.1 A1 — Portfolio Heat Constraint `[meta_router.py]`

**New constant:**
```python
DEFAULT_MAX_PORTFOLIO_HEAT = 0.5
```

**New Redis Keys:**
| Key | Type | TTL | Written By | Read By |
|-----|------|-----|-----------|---------|
| `CONFIG:MAX_PORTFOLIO_HEAT` | String | persistent | UI/Ops | `meta_router.broadcast_decisions()` |
| `PORTFOLIO_HEAT_CAPPED` | String | 60s | `meta_router` | dashboard Risk pane |

**Code path:** `broadcast_decisions()` -> sum weights -> compare to `CONFIG:MAX_PORTFOLIO_HEAT` -> optional proportional scale-down -> publish commands.

### 10.2 A2 — Slippage Budget `[liquidation_daemon.py, strategy_engine.py]`

**New constants:**
```python
SLIPPAGE_BUDGET_PCT   = 0.02   # 2% slippage threshold
SLIPPAGE_HALT_TTL_SEC = 60     # seconds to pause new entries
```

**New coroutine:** `LiquidationDaemon._monitor_fill_slippage()` — subscribes to Redis PubSub `order_confirmations`, calculates slippage per fill, sets `SLIPPAGE_HALT` with TTL.

**Guard added to:** `StrategyEngine.run_strategies()` — `await redis.get("SLIPPAGE_HALT")` before every `_place_order()` call.

**New Redis Keys:**
| Key | Type | TTL | Written By | Read By |
|-----|------|-----|-----------|---------|
| `SLIPPAGE_HALT` | String | 60s | `liquidation_daemon` | `strategy_engine` |

### 10.3 B2 — HMM Gap Calibration `[strategy_engine.py]`

**New function:** `_calibrate_vol_context(redis)` — async, called from `start_engine()` before warmup.

**Input Redis keys:**
- `prev_close:{symbol}` — previous session closing price
- `tick_history:{symbol}` — list; index 0 = first tick of day as JSON `{"price": float}`
- `rolling_std_20d:{symbol}` — 20-day rolling std dev (fallback: 50.0 if absent)

**Output Redis keys written:**
| Key | Type | TTL | Formula |
|-----|------|-----|---------|
| `VOL_GAP_Z:NIFTY50` | String | 7200s | `(open - prev_close) / std` |
| `VOL_GAP_Z:BANKNIFTY` | String | 7200s | same |
| `VOL_GAP_Z:SENSEX` | String | 7200s | same |

### 10.4 C1 — Theta Stall Timer `[liquidation_daemon.py]`

Applied inside `_evaluate_barriers()`. No new constants or Redis keys:
```python
is_expiry_day = now.weekday() in (2, 3)  # Wednesday=2, Thursday=3
if is_expiry_day:
    minutes_to_close = max(0, (15*60+30) - (now.hour*60 + now.minute))
    stall_timer = max(60, int(STALL_TIMEOUT_SEC * (minutes_to_close / 360.0)))
```

### 10.5 C2 — Reboot Audit `[order_reconciler.py]`

**New methods on `OrderReconciler`:**
| Method | Signature | Purpose |
|--------|-----------|---------|
| `_reboot_audit()` | `async -> None` | Entry point, called from `start()` |
| `_handoff_to_liquidation_daemon()` | `async (meta, fill_price) -> None` | ZMQ publish `HANDOFF` command |
| `_cancel_open_order()` | `async (broker_id, order_id, meta) -> None` | `api.cancel_order()` + `_mark_order_failed()` |

**Updated startup sequence:**
```
OrderReconciler.start()
  -> _reboot_audit()          [NEW: deterministic reconciliation first]
  -> _reconciliation_cycle()  [then normal 500ms polling]
```

**Complete v7.0 Redis Key Registry additions:**
| Key | Type | TTL | Written By | Read By |
|-----|------|-----|-----------|---------|
| `CONFIG:MAX_PORTFOLIO_HEAT` | String | persistent | UI/Ops | `meta_router` |
| `PORTFOLIO_HEAT_CAPPED` | String | 60s | `meta_router` | dashboard |
| `SLIPPAGE_HALT` | String | 60s | `liquidation_daemon` | `strategy_engine` |
| `VOL_GAP_Z:NIFTY50` | String | 2h | `strategy_engine` | `hmm_engine` |
| `VOL_GAP_Z:BANKNIFTY` | String | 2h | `strategy_engine` | `hmm_engine` |
| `VOL_GAP_Z:SENSEX` | String | 2h | `strategy_engine` | `hmm_engine` |

### 10.6 Test Coverage Added (v7.0)

| Test File | Covers |
|-----------|--------|
| `tests/test_v70_features.py` | A1 heat constraint (4 cases), A2 slippage budget (5 cases), B2 gap Z-score (5 cases), C1 theta timer (6 cases), C2 reboot audit (5 cases) |
| `tests/test_shoonya_integration.py` | Live login with TOTP, market data (quotes, option chain, time series), account info (limits, positions, order book), order lifecycle (place->cancel->history), WebSocket touchline |
"""

fs_path = os.path.join(BASE, "functional_specifications.md")
ts_path = os.path.join(BASE, "technical_specifications.md")

with open(fs_path, "a", encoding="utf-8") as f:
    f.write(FS_APPEND)
print("functional_specifications.md appended OK")

with open(ts_path, "a", encoding="utf-8") as f:
    f.write(TS_APPEND)
print("technical_specifications.md appended OK")
