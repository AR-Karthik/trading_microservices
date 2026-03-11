# Technical Specifications: Project K.A.R.T.H.I.K.
## *(Kinetic Algorithmic Real-Time High-Intensity Knight)*
**Version 0.9**

This document represents the absolute ground-truth technical architecture for the trading system, derived from a line-by-line analysis of the current active codebase (`v0.9`).

## 1. Architectural Blueprint
The system follows a heavily decoupled, asynchronous microservices architecture that prioritizes Zero-Copy memory sharing, Queue-based multiprocessing, and Pub-Sub messaging.

### 1.1 Core Daemons

| Daemon | Core Pin | Role |
|--------|---------|------|
| `cpp_gateway` | Core 0 | C++ WebSocket feed parser. Publishes clean ticks to ZMQ `MARKET_DATA`. |
| `market_sensor.py` | Core 1 | Multiprocessing alpha generation. Writes SHM + Redis state. |
| `strategy_engine.py` | Core 2 | Execution matrix. Reads SHM. Dispatches ZMQ orders. |
| `meta_router.py` | Core 3 | Tri-Brain orchestrator. Reads Redis state, publishes regime commands. |
| `liquidation_daemon.py` | Any | Autonomous three-barrier exit manager for orphaned positions. |
| `hmm_engine.py` | Any | HMM inference. Reads `market_history` from TimescaleDB. Writes to Redis. |
| `system_controller.py` | Any | Lifecycle: GCP preemption, macro lockdown, EOD hard stop, HMM sync. |
| `live_bridge.py` | Any | Live order execution via Shoonya REST API. |
| `paper_bridge.py` | Any | Simulated paper execution with slippage model. Writes to TimescaleDB. |
| `cloud_publisher.py` | Any | One-way telemetry mirror to Firestore. Publishes heartbeats every 10s. |

### 1.2 Startup Sequence
The intended boot order is enforced by `docker-compose.yml` health checks:

1. **Redis** (health check: `PING`)
2. **TimescaleDB** (health check: `pg_isready`)
3. `system_controller` → initializes Redis capital keys, audits `Pending_Journal`
4. `cpp_gateway` → C++ WebSocket connects to Shoonya broker feed
5. `hmm_engine` → loads pickled model, enters warm-up mode
6. `market_sensor` → starts Compute subprocess, begins tick ingestion from ZMQ
7. `meta_router` → waits for all 3 HMM regime states (`hmm_regime_state` hash has 3 keys)
8. `strategy_engine` → pre-flight JIT warmup (1,000 synthetic ticks), then live loop
9. `live_bridge` / `paper_bridge` → authenticate, arm execution listeners
10. `liquidation_daemon` → arm barrier monitors
11. `cloud_publisher` → begin 10s Firestore heartbeat cycle

> **Rationale:** This strict ordering prevents a race-condition where `strategy_engine` reads SHM before `market_sensor` has written valid data, or `meta_router` dispatches a `TRENDING` regime before `hmm_engine` has completed its hidden-state warm-up. The `HMM_WARM_UP` Redis flag enforces this gate.

## 2. Shared Memory IPC (SHM) `[core/shm.py]`
To bypass ZeroMQ messaging overhead for high-frequency tick signals, the system implements a direct RAM pipeline.

- **Storage Target**: Mapped to `/dev/shm/trading_alpha` (tmpfs on Linux) to prevent physical disk writes.
- **Size**: **128 bytes**.
- **Struct Layout**: `ddddddddd?d`

| Offset | Field | Type | Description |
|--------|-------|------|-------------|
| 0 | `ts` | `double` | Unix timestamp of last write |
| 8 | `alpha` | `double` | `CompositeAlphaScorer.get_total_score()` — the unified trading signal |
| 16 | `vpin` | `double` | Current VPIN reading from the 50-sample rolling window |
| 24 | `ofi_z` | `double` | Log-OFI Z-score (standardized order-flow imbalance) |
| 32 | `vanna` | `double` | Black-Scholes Vanna (dDelta/dVol) for the ATM strike |
| 40 | `charm` | `double` | Black-Scholes Charm (dDelta/dTime) — Theta-adjusted delta decay |
| 48 | `s_env` | `double` | Environmental sub-score (FII, VIX slope, IVP) |
| 56 | `s_str` | `double` | Structural sub-score (basis slope, max pain, PCR) |
| 64 | `s_div` | `double` | Divergence sub-score (price/CVD/PCR divergence) |
| 72 | `toxic_veto` | `bool` | True if VPIN > 0.8 (informed flow detected) |
| 73–127 | CRC | `double` | Summation checksum of all preceding fields |

- **Integrity**: CRC is recalculated on every read. If `CRC != sum(fields)` or `now - ts > 1.0 second`, the SHM is marked stale and the strategy engine defaults to `WAITING`.

> **Rationale:** The 128-byte size is chosen to fit in exactly **two 64-byte L1 cache lines**, making the entire struct readable in a single memory bus transaction. This eliminates CPU cache-miss penalties that would occur with a larger struct.

## 3. The `market_sensor` GIL Mitigation
Standard Python threads share a Global Interpreter Lock, choking high-frequency engines attempting matrix math. The system solves this via standard POSIX OS-level process isolation:
- **I/O Process**: The main `asyncio` loop handles ZMQ networking and lightweight tick buffering. Publishes a snapshot every **20 ticks** to the Compute subprocess via `multiprocessing.Queue(maxsize=50)`.
- **Compute Process**: Heavy operations (`scipy.stats.norm`, `brentq`, Hurst exponent regression, Black-Scholes Greeks) run in a completely isolated worker. Writes results back via a second `multiprocessing.Queue(maxsize=50)`.
- **Queue Overflow Policy**: If the compute queue is full (backpressure signal), the I/O process calls `put_nowait()` and **silently discards** the snapshot. This prioritizes tick ingestion continuity over compute throughput.

### 3.1 Optional Rust Tick Engine
Set `USE_RUST_ENGINE=1` to replace Python-based CVD and VPIN with the compiled Rust extension `tick_engine`:

| Operation | Python Path | Rust Path | Latency |
|-----------|-------------|-----------|---------|
| Lee-Ready Classification | `_classify_trade()` per tick | `TickEngine.classify_trade()` | ~100ns vs ~2µs |
| VPIN Bucket Update | Python counter loop | `TickEngine.update_vpin()` atomic | ~50ns vs ~1µs |
| CVD Update | `self.cvd += sign * vol` | Embedded in Rust struct | ~10ns vs ~500ns |

> **Rationale:** At NSE opening (09:15 – 09:30 IST), tick rates can exceed 800/sec on heavily traded options contracts. At this rate, the Python CVD loop takes ~400ms per second of ticks — consuming 40% of a single-core budget just for tick classification. The Rust extension brings this to ~40ms (4%), leaving ample headroom for Hurst computation and OFI windowing.

## 4. Middleware & Messaging `[core/mq.py]`
For instructions requiring "many-to-many" broadcast patterns, the system utilizes raw ZeroMQ over sockets.

### 4.1 Socket Topology

| Pipeline | Pattern | Binding Convention | Port |
|---------|---------|-------------------|------|
| Tick Feed (C++ → Python) | `PUB/SUB` | `cpp_gateway` binds PUB | `MARKET_DATA` |
| Market State (sensor → daemons) | `PUB/SUB` | `market_sensor` binds PUB | `MARKET_STATE` |
| Order Intent (strategy → bridges) | `PUSH/PULL` | bridges bind PULL | `ORDERS` |
| System Commands (router → all) | `PUB/SUB` | `meta_router` binds PUB | `SYSTEM_CMD` |

**Critical rule:** Order Intent uses `PUSH/PULL` not `PUB/SUB`. This guarantees that when both `live_bridge` and `liquidation_daemon` are listening, each order is consumed by exactly **one** recipient — preventing double-execution.

> **Rationale:** A common mistake in trading system messaging is using PUB/SUB for order routing, resulting in both the paper bridge and live bridge processing the same order. PUSH/PULL acts as a work queue: only one consumer gets each message.

### 4.2 Topic Namespacing
- Tick data: `TICK.{SYMBOL}` (e.g., `TICK.NIFTY50`, `TICK.BANKNIFTY25000CE`)
- Execution events: `EXEC.{SYMBOL}`
- Regime commands: asset name as topic (e.g., `NIFTY`, `BANKNIFTY`)
- Orphan handoffs: `ORPHAN` or `HANDOFF`

## 5. Persistence & State Management

### 5.1 Redis Key Registry (The Blackboard)

| Key | Type | Written By | Read By |
|-----|------|-----------|---------|
| `PAPER_CAPITAL_LIMIT` | String | UI/Dashboard | `system_controller`, `strategy_engine` |
| `LIVE_CAPITAL_LIMIT` | String | UI/Dashboard | `system_controller`, `strategy_engine` |
| `GLOBAL_CAPITAL_LIMIT_PAPER` | String | `system_controller` | `cloud_publisher` |
| `GLOBAL_CAPITAL_LIMIT_LIVE` | String | `system_controller` | `cloud_publisher` |
| `AVAILABLE_MARGIN_PAPER` | String | `system_controller`, `margin_manager` | `strategy_engine` |
| `AVAILABLE_MARGIN_LIVE` | String | `system_controller`, `margin_manager` | `strategy_engine` |
| `CURRENT_MARGIN_UTILIZED` | String | `margin_manager` | `cloud_publisher` |
| `DAILY_REALIZED_PNL_PAPER` | String | `paper_bridge` (from TimescaleDB) | `liquidation_daemon`, `cloud_publisher` |
| `DAILY_REALIZED_PNL_LIVE` | String | `live_bridge` | `liquidation_daemon`, `cloud_publisher` |
| `ACTIVE_LOTS_COUNT` | String | `live_bridge`, `paper_bridge` (INCR/DECR) | `cloud_publisher`, dashboard |
| `STOP_DAY_LOSS` | String | UI/Dashboard | `liquidation_daemon`, bridges |
| `STOP_DAY_LOSS_BREACHED_PAPER` | String | `liquidation_daemon`, `paper_bridge` | bridges, `cloud_publisher` |
| `STOP_DAY_LOSS_BREACHED_LIVE` | String | `liquidation_daemon`, `live_bridge` | bridges, `cloud_publisher` |
| `SYSTEM_HALTED` | String | `system_controller` | All daemons |
| `HMM_WARM_UP` | String | `system_controller` (09:00–09:15 IST) | `strategy_engine`, `meta_router` |
| `LOGGER_STOP` | String | `system_controller` (after 15:25 IST) | `market_sensor` |
| `MACRO_EVENT_LOCKDOWN` | String | `system_controller` | `strategy_engine` |
| `hmm_regime_state` | Hash | `hmm_engine` (per-asset key) | `meta_router`, `market_sensor` |
| `HMM_REGIME` | String | `market_sensor` (flat, from NIFTY's state) | `cloud_publisher`, dashboard |
| `latest_market_state` | String (JSON) | `market_sensor` | `meta_router`, `cloud_publisher`, dashboard |
| `COMPOSITE_ALPHA` | String | `market_sensor` | `cloud_publisher`, dashboard |
| `latest_attributions` | String (JSON) | `meta_router` | dashboard, analytics |
| `active_strategies` | Hash | UI/Dashboard | `strategy_engine`, bridges |
| `active_regime_engine` | String | UI/Dashboard | `meta_router` |
| `lock:{symbol}` | String (TTL 10s) | bridges | bridges (double-tap guard) |
| `Pending_Journal:{uuid}` | String | bridges | `system_controller` (boot audit), `liquidation_daemon` |
| `telegram_alerts` | List | All daemons | `cloud_publisher` / `telemetry_alerter` |
| `panic_channel` | PubSub | UI, `system_controller`, `liquidation_daemon` | bridges, `liquidation_daemon` |

### 5.2 TimescaleDB Schema (trading_db)

| Table | Hypertable | Written By | Read By |
|-------|-----------|-----------|---------|
| `trades` | Yes (`time`) | `live_bridge`, `paper_bridge` | Dashboard analytics, P&L calcs |
| `portfolio` | No | `live_bridge`, `paper_bridge` | Dashboard positions, Unrealized P&L |
| `market_history` | Yes (`time`) | `market_sensor` | `hmm_engine` (training data) |

- **Unrealized P&L Query**: Dashboard reads `portfolio` table (`symbol`, `quantity`, `avg_price`) and cross-references with `latest_tick:{symbol}` to compute `(ltp - avg_price) * quantity` at display time.
- **LOGGER_STOP**: At 15:25 IST, `market_sensor` stops writing to `market_history` to prevent MOC auction noise from contaminating HMM training data.

### 5.3 Pending Journal (Write-Ahead Log)
An order intent is saved to `Pending_Journal:{uuid}` immediately before dispatch. It is cleared:
- **Normal path**: By the bridge after a successful broker ACK.
- **Liquidation path**: By `liquidation_daemon` after confirmed exit fill.
- **Boot path**: If found orphaned on `system_controller` startup, a Telegram alert fires and the key is flagged for manual review.

## 6. Meta Router Inference & Routing `[meta_router.py]`
The system dynamically weights strategy allocation percentages using Fractional Kelly calculations.
- **Deterministic Provider**: Uses thresholds (`hurst > 0.55`, `adx > 25`, `kaufman_er > 0.6`) to yield a "TRENDING" or "RANGING" state. Probability is calculated as `0.5 + (hurst - 0.5) + (adx - 20)/100`.
- **HMM Provider**: Reads real-time serialized GMM-HMM state from `hmm_regime_state` hash in Redis.
- **Hybrid Provider**: Blends them at a `(0.4 * HMM) + (0.6 * Deterministic)` ratio. Requires a confidence score `> 0.70` for long authorization, AND both providers must independently classify `TRENDING`.
- **Engine Selection**: The active engine (`HMM`, `DETERMINISTIC`, or `HYBRID`) is switchable at runtime via `active_regime_engine` Redis key without restarting any daemon.

## 7. Application UI Architecture & Observability
The system deploys a modular frontend hosted on **Google Cloud Run** (`cloudrun/main.py`). It fully decouples the UI from the execution loop.

### 7.1 Zero-Interference Observer Pattern
The UI operates strictly outside the quantitative event loop. It does not ping the backend engines via REST APIs, which could cause blocking or latency spikes on the trading nodes. 
Instead, the app acts as a **passive subscriber** to the Redis plane and Firestore. If the UI crashes, lags, or is bombarded by a thousand users, the Python trading daemons and C++ gateway remain physically unbothered.

### 7.2 Data Ingestion (Read Flow)
- **Live State (on-premise)**: Reads Redis keys (`latest_market_state`, `COMPOSITE_ALPHA`, `HMM_REGIME`) at its own framerate. 
- **Live State (off-premise)**: Reads from Firestore heartbeat document (pushed every 10s by `cloud_publisher`). Contains regime, PnL, margin, and breach flags.
- **Analytical State**: Deep metrics (Drawdowns, Sharpe Ratios, Equity Curves) queried from TimescaleDB `trades` table via `asyncpg`.

### 7.3 Command Dispatch (Write Flow)
- **Configuration Hot-Reloading**: Strategy configs (`active_strategies`) and budgets (`PAPER_CAPITAL_LIMIT`) pushed into Redis hashes.
- **Panic Action**: UI publishes to `panic_channel` → all bridges and `liquidation_daemon` react in sub-milliseconds.
- **Regime Engine Switch**: Write `HMM`, `DETERMINISTIC`, or `HYBRID` to `active_regime_engine` key → `meta_router._sync_settings()` reads it on next 100ms cycle.

### 7.4 Optimization & Iteration via Observability
By broadcasting raw mathematical coefficients (`Log-OFI Z-Score`, `Basis Z-score`, `Hurst`, `VPIN`) alongside the Meta-Router's final classification, the researcher can watch decisions unfold live. This enables "mental-model backtesting" — verifying whether a misclassification was caused by HMM lag or Deterministic threshold miss — without parsing log files.

## 8. Cloud Deployability (GCP Spot Optimization)
The daemon topology is specifically hardened for running on ephemeral, hyper-cheap Preemptible/Spot instances via Docker:
- `system_controller.py` polls `http://metadata.google.internal/computeMetadata/v1/instance/preempted` every **5 seconds**.
- `httpx` is used as the async HTTP client for this poll. Falls back to `urllib` if `httpx` is not installed.
- If preemption triggers, the app publishes `SQUARE_OFF_ALL` to `panic_channel`, waits **15 seconds** for fill receipts, then sets `SYSTEM_HALTED=True` and dies gracefully.

## 9. Engineering Institutional-Grade Insulation & Sub-Millisecond Latency
Every component of this architecture was specifically selected to guarantee **three properties**: True Process Insulation, Predictable Sub-Millisecond Latency, and Crash Resilience.

### 9.1 How Insulation Protects Execution
- **OS-Level Process Sharding**: A memory leak in the `dashboard` UI or an uncaught exception in the `market_sensor`'s Scipy matrix math *cannot* crash the `strategy_engine` or the `liquidation_daemon`. They exist as entirely decoupled OS processes communicating strictly via IPC/Redis.
- **Stateless Bridges**: The `cpp_gateway` and `live_bridge` hold zero state. If the WebSocket connection drops, the microservice dies, the Docker daemon immediately restarts it, and it reconnects without pulling the HMM Regime or the Liquidation Daemon out of memory.
- **Queue Overflow Resilience**: The compute subprocess `multiprocessing.Queue(maxsize=50)` is bounded. If the compute process falls behind and the queue fills, new snapshots are silently dropped — the I/O loop is never blocked. This means tick ingestion is always real-time even if Black-Scholes computation falls 1-2 seconds behind.

### 9.2 How Sub-Millisecond Latency is Achieved
- **Zero-Copy SHM vs Networking**: Instead of sending dense Alpha matrices over TCP/IP or ZeroMQ, `market_sensor.py` writes directly to physical RAM via `core/shm.py` (`/dev/shm`). `strategy_engine` reads the `128-byte C-struct` natively. **Result**: Tick-to-Signal lookup drops from `~200µs` (ZMQ) to `< 1µs` (SHM).
- **Python GIL Bypass**: Because Python's Global Interpreter Lock (GIL) halts asyncio execution during heavy math, `market_sensor` boots a completely isolated `multiprocessing.Process` for all Black-Scholes/Greeks calculations. **Result**: The main event loop never drops a tick while waiting for a CPU cycle.
- **The C++ Edge**: The `cpp_gateway` handles the incredibly noisy incoming WebSocket stream from the broker. Using `simdjson` (CPU vectorization) it parses the JSON ticks hundreds of times faster than Python can, filtering noise out on the C++ side *before* passing the clean payload to Python.
- **Optional Rust Extension**: `USE_RUST_ENGINE=1` replaces Python-level Lee-Ready classification and VPIN buckets with a compiled Rust crate (`tick_engine`), reducing per-tick classification from ~2µs to ~100ns.

### 9.3 How Infrastructure/Hardware Optimizes the Software
- **CPU Affinity (Core Pinning)**: The Docker engines configure container affinity. Core 0 handles the C++ Gateway (I/O bursty), Core 1 handles the heavy `market_sensor` math. **Result**: Eliminates CPU context-switching overhead (~30µs penalty per thread swap).
- **Three-Disk I/O Shattering**: By splitting I/O across 3 physical Google Cloud disks (Standard OS Disk, Extreme NVMe for Redis WAL, Persistent SSD for PostgreSQL), a massive tick-ingestion DB write sequence physically cannot starve the CPU or block the Redis memory write that authorizes a trade entry.
- **gVNIC & Transparent HugePages (THP)**: By utilizing GCP's Custom Virtual Network Interfaces (gVNIC) and forcing Linux `THP=always`, the kernel is optimized for large block matrix calculations (HMM Inference) and high-throughput network bursting.
