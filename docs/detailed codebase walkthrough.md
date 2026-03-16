# K.A.R.T.H.I.K. Detailed Code Walkthrough

This document provides a line-by-line, exhaustive architectural analysis of the K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight) codebase. Every class, method, and piece of core logic is documented here for maximum technical transparency.

---

## 1. Core Utilities (`core/`)

### 1.1 [alerts.py](file:///c:/Users/karth/./scratch/trading_microservices/core/alerts.py)
**Purpose**: Centralized asynchronous alerting system using Redis as a message bus for Telegram notifications.

**Classes:**
- `CloudAlerts`: A singleton class managing the alert queue connection.
    - `_instance`: Class variable for singleton storage.
    - `_redis`: Class variable for the `redis.asyncio` connection object.

**Methods:**
- `get_instance()`: Standard class-method singleton accessor.
- `__init__()`: Sets up the Redis URL from environment variables (`REDIS_HOST`).
- `_ensure_redis()`: Asynchronously initializes the Redis connection if not already active. Uses `redis.from_url` with `decode_responses=True`.
- `alert(text, alert_type, **kwargs)`: The core logic.
    - Ensures Redis is connected.
    - Constructs a JSON payload with `message`, `type`, and UTC `timestamp`.
    - Merges any additional `kwargs` into the payload.
    - Uses `LPUSH` (O(1)) to add the message to the `telegram_alerts` list in Redis.

**Wrappers / Global Helpers:**
- `send_cloud_alert(text, alert_type, **kwargs)`: The primary entry point for the rest of the codebase. Provides a simple async function to fire-and-forget alerts.

**Logic Details:**
- **Asynchronous Execution**: Designed to work within `asyncio` loops, ensuring that sending a high-priority alert doesn't stall tick processing or order execution.
- **Fail-Safe**: If Redis is unreachable, it logs a warning but allows the main application to continue, preventing an "alert failure" from crashing the trader.

### 1.2 [db_retry.py](file:///c:/Users/karth/./scratch/trading_microservices/core/db_retry.py)
**Purpose**: Resilient database connection management via an asynchronous retry decorator.

**Decorators:**
- `with_db_retry(max_retries=3, backoff=0.5)`: Enhances stability for `asyncpg` operations.

**Logic Details:**
- **Error Handling**: Specifically targets `asyncpg` connection errors (`ConnectionDoesNotExistError`, `InterfaceError`, `InternalClientError`) and standard `OSError` exceptions.
- **Exponential Backoff**: Sleeps for `backoff * (attempt + 1)` between retries to allow the network or database server to recover.
- **Self-Healing Pool**: Incorporates a clever JIT (Just-In-Time) recovery mechanism. If the decorated method's object (`self`) exposes a `_reconnect_pool()` method, the decorator invokes it automatically upon encountering a connection error.
- **Finality**: If the `max_retries` limit is reached, it logs a `CRITICAL` error to the standard logger and re-raises the last exception, ensuring the failure is not silently ignored in high-stakes trading logic.

### 1.3 [execution_wrapper.py](file:///c:/Users/karth/./scratch/trading_microservices/core/execution_wrapper.py)
**Purpose**: Orchestrates multi-leg options execution with margin-aware sequencing and slippage-capped limit chasing.

**Hardened Features**:
- **Fill-Confirmed Sequencing**: In multi-leg baskets, the system waits for the **BUY leg confirmation** before firing the subsequent SELL leg. This strictly adheres to exchange margin rules and prevents "naked leg" rejections.
- **JIT Feed Subscription**: Automatically publishes `dynamic_subscriptions` for all basket legs (especially OTM wings) before order dispatch to ensure the DataGateway is już streaming price data.
- **Adaptive Limit Chasing**: Dynamically modifies limit prices within a 0.10% slippage cap to maximize fill probability.

**Classes:**
- `MultiLegExecutor`: The primary coordinator for complex order baskets.

**Methods:**
- `_get_best_price(symbol, side)`: Internal helper to fetch current market price for order initialization.
- `adaptive_limit_chase(order, max_slippage_pct=0.10)`: Implements the "Chaser" logic.
    - Loops up to 5 times.
    - Modifies the limit price by 0.05 (tick size) on each miss.
    - Enforces a `max_slippage_pct` hard-cap to prevent execution at extreme prices during low-liquidity spikes.
    - Raises `SlippageCapBreached` or `FillTimeout` if completion is impossible.
- `execute_legs(orders, sequential=True)`: The entry point for strategy engines.
    - **Margin Sorting**: When `sequential=True`, it sorts legs so `BUY` orders occur before `SELL` orders. This ensures the broker sees long-margin offsets before initiating short positions.
    - **Reconciler Sync**: Sends a `BASKET_ORIGINATION` ZMQ message to the `OrderReconciler` containing the `parent_uuid` and leg metadata.
    - **JIT Differentiation**: Identifies OTM (Out-of-the-Money) legs via the `is_otm` flag and applies the `adaptive_limit_chase` strategy specifically to them, while using standard execution for ITM/ATM legs.

**Logic Details:**
- **Atomic Intent**: By notifying the Reconciler *before* execution starts, the system ensures that if the daemon crashes mid-basket, the reconciler can detect the "Incomplete" state and take corrective action.
- **Error Propagation**: Any single leg failure instantly halts the loop and raises an exception for the parent strategy engine to handle.

### 1.4 [greeks.py](file:///c:/Users/karth/./scratch/trading_microservices/core/greeks.py)
**Purpose**: High-precision Black-Scholes engine for calculating option theoretical prices and risk sensitivities (Greeks).

**Classes:**
- `BlackScholes`: A static utility class for mathematical modeling.

**Static Methods:**
- `d1(S, K, T, r, sigma)` / `d2(S, K, T, r, sigma)`: The core cumulative probability components. Includes floors (`1e-9` for T, `1e-6` for sigma) to prevent `ZeroDivisionError`.
- `call_price(S, K, T, r, sigma)` / `put_price(...)`: Calculates fair value. Handles $T \le 0$ by returning intrinsic value.
- `delta(S, K, T, r, sigma, option_type)`: Rate of change in price relative to the underlying.
- `gamma(...)`: Rate of change in Delta (curvature).
- `vega(...)`: Sensitivity to Volatility (scaled per 1% move).
- `theta(...)`: Time decay, returned as a daily value (annual result / 365).

**Logic Details:**
- **Numerical Robustness**: The implementation avoids the common pitfall of crashing at the exact moment of expiry by using small epsilon offsets and explicit $T=0$ branch logic.
- **Dependency**: Relies on `scipy.stats.norm` for accurate Gaussian distribution calculations, essential for institutional-grade pricing.

### 1.5 [health.py](file:///c:/Users/karth/./scratch/trading_microservices/core/health.py)
**Purpose**: Unified health monitoring and heartbeat system for maintaining global system visibility.

**Classes:**
- `Daemons`: A constant-mapping class for ensuring consistent daemon identifiers (e.g., `DATA_GATEWAY`, `MARKET_SENSOR`).
- `HeartbeatProvider`: The producer agent for health state.
    - `run_heartbeat(interval=5)`: An async loop that updates the `daemon_heartbeats` Hash in Redis and sets a standalone `heartbeat:NAME` key with a 30s TTL.
- `HealthAggregator`: The consumer agent for health state analysis.
    - `get_system_health()`: Fetches all heartbeats, compares them against `now()`, and returns a detailed status dictionary.

**Logic Details:**
- **Death Threshold**: A 15-second grace period is used. Any daemon failing to pulse within 15s is marked as `DEAD`.
- **System Health Score**: Calculated as the ratio of `ALIVE` daemons to `TOTAL` required daemons. This score is critical for triggering automated circuit breakers at the system level (e.g., stopping trading if the `DataGateway` is down).

### 1.6 [logger.py](file:///c:/Users/karth/./scratch/trading_microservices/core/logger.py)
**Purpose**: Centralized logging system utilizing JSON formatting and automated file rotation for institutional-grade audit trails.

**Classes:**
- `JsonFormatter`: Extends `logging.Formatter` to serialize log records into JSON strings.

**Methods:**
- `setup_logger(name, log_file=None, level=logging.INFO)`:
    - Configures a named logger.
    - Attaches a `StreamHandler` for console output.
    - Attaches a `RotatingFileHandler` (10MB limit, 5 backups) if a file path is provided.
    - Uses `JsonFormatter` for both to ensure consistency.

**Logic Details:**
- **Observability**: The JSON output includes `correlation_id`, `module`, `funcName`, and `lineNo`, allowing for deep tracing across the distributed microservice architecture.
- **Resilience**: The rotating file handler ensures that excessive logging during market volatility doesn't cause disk exhaustion.
- **Safety**: Includes `hasHandlers()` checks to prevent the "duplicate log" bug often found in Python's logging module when multiple re-imports occur.

### 1.7 [margin.py](file:///c:/Users/karth/./scratch/trading_microservices/core/margin.py)
**Purpose**: Atomic capital allocation and margin tracking using Redis Lua scripts for multi-process safety.

**Classes:**
- `MarginManager`: Coordinates asynchronous margin reservations and releases.

**Lua Scripts (Atomic):**
- `LUA_RESERVE`: Checks `available >= required`. If true, subtracts `required` and returns `1`. Otherwise, returns `0`.
- `LUA_RELEASE`: Adds `amount` back to the pool. Designed to allow the pool to grow with trading profits.

**Methods:**
- `reserve(required_margin, execution_type)`: Invokes `LUA_RESERVE`. Returns `True` if capital was successfully locked.
- `release(amount, execution_type)`: Invokes `LUA_RELEASE`. Returns the new total available margin.
- `get_available(execution_type)`: Fetches the current margin balance from Redis.

**Logic Details:**
- **Double-Spending Protection**: By moving the "Check-then-Set" logic into a Redis Lua script, the system eliminates race conditions where two strategy hunters might simultaneously attempt to use the same final ₹5,000 of capital.
- **Environment Separation**: Derives Redis keys (e.g., `AVAILABLE_MARGIN_LIVE` vs `AVAILABLE_MARGIN_PAPER`) dynamically based on the execution mode, preventing accidental cross-contamination of live and paper funds.

### 1.8 [mq.py](file:///c:/Users/karth/./scratch/trading_microservices/core/mq.py)
**Purpose**: The ZeroMQ messaging backbone, providing unified async communication patterns and context-aware request/reply tracing.

**Classes:**
- `MQManager`: Factory for ZeroMQ sockets (`PUB`, `SUB`, `PUSH`, `PULL`, `DEALER`, `ROUTER`).
- `Ports` / `Topics`: Registries for system-wide TCP port and message-type assignments.
- `NumpyEncoder`: Custom JSON encoder to handle high-precision math types (numpy integers, floats, arrays).
- `RedisLogger`: Streams mission-critical logs to Redis for real-time dashboard UI updates.

**Methods:**
- `create_publisher(port, bind)`: Standard ZMQ-PUB socket.
- `create_subscriber(port, topics, bind)`: ZMQ-SUB socket with inclusive topic filtering and a 5-second `RCVTIMEO`.
- `send_json(socket, topic, data)`: The most critical logic in MQ.
    - Retrieves a `correlation_id` from the current context or generates a new UUID.
    - Sends a 3-part multipart message: `[Topic, Header_JSON, Data_JSON]`.
- `recv_json(socket)`: Receives the 3-part message and *restores* the `correlation_id` into the thread's local context variable, allowing for end-to-end tracing.

**Logic Details:**
- **Correlation Tracing**: By wrapping `contextvars.ContextVar`, the system ensures that logs generated in a `StrategyEngine` on a specific trade Intent will carry the same UUID as the logs generated in the `LiveBridge` when the order is filled.
- **Micro-Services Networking**: In Docker environments, it defaults to binding to `0.0.0.0`, allowing containers to communicate over the internal mesh network via environment-configured hostnames (`MQ_ORDERS_HOST`, etc.).

### 1.9 [network_utils.py](file:///c:/Users/karth/./scratch/trading_microservices/core/network_utils.py)
**Purpose**: Network-layer resilience utilities, implementing industry-standard backoff and circuit-breaking patterns.

**Decorators:**
- `exponential_backoff(max_retries=5, base_delay=1, max_delay=30)`: Wraps asynchronous functions to provide jittered retries on failure.

**Classes:**
- `CircuitBreaker`: Implements a state-machine to protect the system from cascading failures in external integrations.
    - `call(func, *args, **kwargs)`: Executes a function within the circuit's protection.
    - `record_failure()`: Increments the failure count and trips the circuit to `OPEN` if the threshold is breached.

**Logic Details:**
- **State Machine**:
    - `CLOSED`: Normal operation.
    - `OPEN`: Blocked. Trips after 5 failures. Prevents "spamming" dead endpoints like a broker API during an outage.
    - `HALF_OPEN`: Probing state after 60s of inactivity. Success here resets the circuit to `CLOSED`.
- **Latency Optimization**: The `exponential_backoff` ensures that the system doesn't get stuck in a tight loop of failing requests, which would consume CPU cycles better spent on market data processing.

### 1.10 [shared_memory.py](file:///c:/Users/karth/./scratch/trading_microservices/core/shared_memory.py)
**Purpose**: High-performance, zero-copy inter-process communication (IPC) for raw tick data using Python's `multiprocessing.shared_memory`.

**Classes:**
- `TickSharedMemory`: Manages a pre-allocated RAM segment for low-latency data sharing.

**Methods:**
- `__init__(create)`: Initializes the segment. Safely unlinks previous segments to prevent data corruption.
- `write_tick(slot_index, symbol, price, volume, timestamp)`:
    - Encodes the data using a strict `struct` format (`32s d q d`).
    - Maps the symbol to a 32-byte fixed-width field.
    - Directly writes the binary buffer into the RAM slice defined by the `slot_index`.
- `read_tick(slot_index)`: Unpacks the binary buffer back into a Python dictionary.

**Logic Details:**
- **Deterministic Efficiency**: By bypassing the ZMQ/TCP stack and JSON serialization, the Gateway can "blast" quote updates into memory where Sensors can read them with near-zero latency.
- **Fixed-Width Buffer**: Uses a 1,000-slot grid. The 32-byte padding for symbols ensures that every "row" in the memory matrix starts at a perfectly predictable byte offset, enabling O(1) random access by slot ID.

### 1.11 [shm.py](file:///c:/Users/karth/./scratch/trading_microservices/core/shm.py)
**Purpose**: Specialized Zero-Latency memory mapping for complex Signal Vectors and Quantitative State.

**Classes:**
- `SignalVector`: A dataclass defining the system's "Alpha Payload" (VPIN, OFI, Greeks, HMM state, etc.).
- `ShmManager`: Handles the platform-specific `mmap` logic (Linux `/dev/shm` vs Windows `tagname`).

**Methods:**
- `write(signals)`:
    - Packs the `SignalVector` into a 512-byte binary block.
    - Calculates a primitive but fast **CRC** (checksum) by summing all signal values.
    - Updates a high-resolution timestamp for staleness tracking.
- `read()`:
    - Unpacks the binary block.
    - **Integrity Check**: Re-calculates the CRC. If it differs from the stored CRC (due to a mid-read write), the payload is rejected.
    - **Staleness Check**: If the timestamp is > 1.0s old, it returns `None`, signaling that the data gateway or market sensor has stalled.

**Logic Details:**
- **Robustness**: If SHM initialization fails (e.g., due to OS permissions), the system logs a warning and has a code-path fallback to Redis, though SHM is the preferred high-frequency path.
- **Binary Format**: Uses a precise `struct` format (`ddddddddddddddd?ddddddddddd`) to map 15 primary signals, 1 toxic-flow veto flag, and 10 heavyweight alpha components into a single atomic memory block.

---

## 2. System Daemons (`daemons/`)

### 2.1 [cloud_publisher.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/cloud_publisher.py)
**Purpose**: Decouples the trading infrastructure from the UI by publishing discovery metadata and archival data to Google Cloud (Firestore/GCS).

**Classes:**
- `CloudPublisher`: Orchestrates cloud synchronization and remote command handling.

**Methods:**
- `_init_cloud_clients()`: Lazily initializes Firestore and Storage clients. Fetches the VM's external IP for dashboard discovery.
- `_heartbeat_loop()`: Updates `system/metadata` in Firestore every 60s with current P&L, Regime, and IP.
- `_command_watcher()`: Listens for dashboard-initiated actions.
    - `PANIC_BUTTON`: Publishes a `SQUARE_OFF_ALL` message to Redis.
    - `PAUSE/RESUME`: Toggles the `TRADING_PAUSED` flag in Redis.
- `_eod_snapshot()`: Triggered at 15:35 IST.
    - Exports `market_history` and `trades` from TimescaleDB.
    - Converts datasets to `.parquet` format via `pandas` and `pyarrow`.
    - Uploads artifacts and HMM model `.pkl` to GCS.

**Logic Details:**
- **Discovery Mechanism**: The dashboard doesn't need a static IP; it reads the latest `vm_public_ip` from Firestore, allowing K.A.R.T.H.I.K. to run on dynamic GCP spot instances or local machines.
- **Hybrid Safety**: Even if the cloud connection is lost, the main trading loop continues; the logger simply marks `Cloud publishing disabled`.
- **EOD Automation**: The 15:35 IST trigger ensures that data is backed up immediately after the Indian market close but before the VM is scheduled for shutdown.

### 2.2 [data_gateway.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/data_gateway.py)
**Purpose**: The central data ingestion hub, responsible for Shoonya API connectivity, dynamic lot-size management, and system-wide market halts.

**Classes:**
- `DataGateway`: Manages live/simulated market data streams.

**Methods:**
- `start()`: Initializes Redis/ZMQ and launches concurrent tasks for tick streaming, staleness monitoring, and PCR calculation.
- `_tick_stream()`:
    - Shoonya integration: Bridges the synchronous `NorenApi` background thread to the `asyncio.Queue`.
    - Simulation: Generates random-walk ticks for 13 underlyings and active options if market is closed.
    - ZeroMQ Broadcast: Publishes standardized tick JSONs to `TICK.{SYMBOL}` topics.
- `_dynamic_subscription_manager()`: Automatically scans and subscribes to ATM call/put options for Nifty/BankNifty as the spot price moves.
- `_staleness_watchdog()`: Forces a WebSocket reset if any subscribed symbol hasn't received a tick in >1000ms.
- `_pcr_ingestion_loop()`: Aggregates Open Interest (OI) from option chains every 5 minutes to compute the Put-Call Ratio (PCR).
- `_circuit_breaker_monitor()`: Implements the SEBI 10/15/20% halt matrix, broadcasting `SYSTEM_HALT` events to all listeners.

**Logic Details:**
- **Mode Intelligence**: Features a `_mode_controller` that automatically toggles between `LIVE` and `SIMULATED` flows based on Indian market hours (09:15 - 15:30 IST) or explicit `.env` overrides.
- **Risk-Free Rate (RFR)**: Dynamically fetches the current Indian RFR from Redis (`CONFIG:RISK_FREE_RATE`) for Black-Scholes Delta calculations, moving away from hardcoded values.
- **Data Integrity**: Maintains `tick_history:{symbol}` in Redis as a sliding window of the last 10,000 ticks for quick lookups by other daemons.

### 2.3 [data_logger.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/data_logger.py)
**Purpose**: High-throughput persistence layer that archetypes system-wide market signals into TimescaleDB for historical analysis and backtesting.

**Classes:**
- `DataLogger`: Manages the ZMQ-to-PostgreSQL pipeline.

**Methods:**
- `start()`: Initializes the `asyncpg` connection pool, subscribes to `Ports.MARKET_STATE`, and starts the heartbeat task.
- `_reconnect_pool()`: Helper method used by the `with_db_retry` decorator to gracefully restore the connection pool after network brownouts.
- `_batch_writer()`:
    - Runs in a perpetual 10-second loop.
    - Atomically swaps the current batch with an empty list using `asyncio.Lock`.
    - Performs a `copy_records_to_table` equivalent (via `executemany`) to insert market records in bulk.
    - Handles conflicts via `ON CONFLICT (time, symbol) DO NOTHING` to prevent data duplication.

**Logic Details:**
- **Signal-to-DB Mapping**: Specifically logs `price`, `log_ofi_zscore`, `cvd`, `vpin`, `basis_zscore`, and `vol_term_ratio`, creating a rich time-series dataset of engineered alpha features.
- **Graceful Throttle**: By batching writes every 10 seconds rather than per-tick, it significantly reduces IOPS on the persistent SSD, ensuring the database doesn't become a bottleneck during market "burst" events.
- **Shutdown Safety**: Uses the `finally` block to ensure the connection pool is drained and closed, preventing "Too many connections" errors on the database server during daemon restarts.

### 2.4 [hmm_engine.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/hmm_engine.py)
**Purpose**: High-speed market regime classifier that translates raw volatility and trend signals into strategy selection verdicts (RANGING, TRENDING, etc.) using its deterministic HeuristicEngine.

**Classes:**
- `HeuristicEngine`: A deterministic state-machine that classifies the market based on RV, ADX, and PCR thresholds.

**Methods:**
- `_pin_core()`: Uses `os.sched_setaffinity` to lock the daemon to a specific CPU core for hardware-level execution determinism.
- `_calculate_realized_vol(closes)`: Computes annualized 14-day volatility.
- `_calculate_adx_approximation(closes)`: Approximates trend strength using absolute momentum.
- `classify_regime()`: The decision matrix.
    - Maps PCR thresholds (>1.5 for Overbought, <0.7 for Oversold).
    - Segments Ranging vs Trending based on RV and ADX intersections.
- `run()`:
    - Maintains a perpetual loop listening for `Ports.MARKET_STATE` updates.
    - Updates the **SignalVector** in Shared Memory (`shm.py`) with new RV/ADX/PCR values.
    - Synchronizes state to Redis for both real-time dashboarding and strategy-engine consumption.

**Logic Details:**
- **Veto Logic**: If a `REGIME_CRASH` is detected (RV > 50%), the engine sets the `veto` flag in the Signal Vector, causing all strategy engines to immediately halt new entries.
- **Deterministic Efficiency**: Replaces stochastic state inference with a O(1) mathematical matrix for absolute predictability and sub-millisecond classification.
- **Micro-batching**: Re-fetches macro parameters from Redis every 300 ticks (~5 minutes) to ensure mathematical models aren't drifting too far from reality mid-session.

### 2.5 [liquidation_daemon.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/liquidation_daemon.py)
**Purpose**: A secondary "Safety Net" process that manages open positions and applies risk-based liquidation barriers (TP, SL, and Time-Decay).

**Classes:**
- `LiquidationDaemon`: The risk-management orchestrator.

**Methods:**
- `run()`: Entry point. Hydrates current positions from the `portfolio` table in TimescaleDB to ensure persistence across restarts.
- `_monitor_fill_slippage()`: Dynamic watchdog that halts the system if slippage on any fill exceeds the volatility-adjusted budget.
- `_check_stop_day_loss()`: Monitors total daily P&L. If the breach threshold is hit, it triggers a `SQUARE_OFF_ALL` panic through Redis.
- `_evaluate_kinetic_barriers()`:
    - **TP1/TP2**: Initial partial exit and subsequent runner management.
    - **THETA_STALL**: Time-based exit for stagnant OTM options, scaled by DTE.
- `_attempt_exit()`: Robust order-publisher. Includes a built-in parser for **HTTP 400 Circuit Limit** errors, allowing the daemon to "nudge" prices until they fit within the broker's exchange-mandated bands.

**Logic Details:**
- **Triple Barrier System**: Combines ATR-based volatility stops, time-based decay exits, and structural breaches (e.g., spot crossing an option's short strike).
- **A2 Architecture**: Features a "Slippage Budget". If the market is too thin/fast and fills are diverging from intended prices, the daemon sets a 60s global lockout to prevent further account depletion.
- **Hydration Resilience**: By querying the database at startup, the daemon avoids "losing" positions if the strategy engine crashes, making it the system's "Absolute Truth" for open risk.

### 2.6 [live_bridge.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/live_bridge.py)
**Purpose**: The real-money execution gateway. Bridges the internal ZMQ "Order Intent" messaging with the Shoonya (Noren) REST APIs.

**Classes:**
- `TokenBucketRateLimiter`: Ensures the system never exceeds the broker's API limits (default: 8 requests/sec).
- `LiveExecutionEngine`: Coordinates auth, order routing, and portfolio journaling.

**Methods:**
- `authenticate()`: Performs hashing-based login (SHA256) to secure the API session.
- `execute_live_order(order)`:
    - Acquires a rate-limit token.
    - Determines the exchange (NSE vs NFO vs BFO) based on symbol suffix.
    - Blocks the async loop temporarily to perform the synchronous POST request through `run_in_executor`.
- `handle_order()`:
    - **Double-Tap Guard**: Sets a Redis lock per symbol to prevent race conditions from duplicate signals.
    - **Journaling**: Records intent to `Pending_Journal` in Redis *before* sending to the broker, ensuring recovery if the VM dies mid-request.
    - **Transaction Safety**: Updates the `portfolio` table using a `FOR UPDATE` row lock, preventing P&L inaccuracies.
- `panic_listener()`: A dedicated high-priority task that liquidates the entire account if a `SQUARE_OFF_ALL` signal is received via Redis Pub/Sub.

**Logic Details:**
- **Rate-Limit Enforcement**: Crucial for HFT; without the `TokenBucket`, a high-frequency sensor burst could get the account suspended by the exchange.
- **Micro-Services Feedback**: Publishes confirmation events to `EXEC.{SYMBOL}` on `Ports.TRADE_EVENTS` for strategy engines to acknowledge fills.
- **Rollback Resilience**: Implements `_handle_basket_rollback` for multi-leg orders where one leg failed, allowing the bridge to immediately "undo" partial fills.

### 2.7 [market_sensor.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/market_sensor.py)
**Purpose**: The system's "Analytical Brain". Performs real-time feature engineering, calculating complex market microstructure signals (OFI, VPIN, CVD, GEX).

**Classes:**
- `MarketSensor`: Orchestrates data ingestion and distributes heavy compute.
- `CompositeAlphaScorer`: A weighted logic engine that fuses multiple signals into a single `S_TOTAL` alpha score.

**Methods:**
- `start()`: Launches an isolated **ComputeProcess** via `multiprocessing`. This ensures heavy math (Greeks/Dispersion) doesn't block the main tick-ingestion loop.
- `_classify_trade()`: Implements **Lee-Ready classification** (optionally in Rust) to determine if a tick was an aggressive buy or sell based on bid-ask midpoints.
- `_ofi()` / `_update_vpin()`: Computes Order Flow Imbalance and Volume-Synchronized Probability of Informed Trading.
- `_publish_market_state()`:
    - Assembles the 50-field `state` JSON.
    - Writes the 512-byte **SignalVector** to Shared Memory.
    - Populates the **Power Five Matrix** in Redis (tracking the top 5 heavyweights like RELIANCE and HDFCBANK).

**Logic Details:**
- **Hybrid Compute Model**: By offloading `scipy` and `numpy` operations to a sub-process, the sensor maintains <1ms jitter for the main tick-to-signal pipeline.
- **Rust Tick Engine**: If detected, the sensor bypasses Python logic for trade classification and VPIN, achieving sub-microsecond metric updates.
- **Sentiment Fusion**: Blends news/social sentiment (from Redis) into the final alpha score, allowing a +/- 20% bias based on external macro news.
- **Power Five Observation**: Isolates performance of the top 5 heavyweights to detect "Index Drag" or "Dislocated Rallies".

### 2.8 [meta_router.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/meta_router.py)
**Purpose**: The central "Allocator". It translates market regimes and alpha scores into specific position sizes and order intents for 13+ assets.

**Classes:**
- `BaseStrategyLogic`: Implements the core **ATR-Normalized + Half-Kelly** sizing math.
- `MetaRouter`: Orchestrates parallel brains for NIFTY, BANKNIFTY, SENSEX, and heavyweights.

**Methods:**
- `_sync_settings()`: Hot-swaps risk parameters (MAX_PORTFOLIO_HEAT, HURST_THRESHOLD) from Redis in real-time.
- `_calculate_portfolio_greeks()`: Every 10s, it reconciles the database portfolio to calculate **Net Delta** per index, allowing for automated hedging decisions.
- `_check_cross_index_divergence()`: Vetoes trades if the market is "fractured" (e.g., NIFTY is trending while SENSEX is crashing).
- `_enrich_with_atm_symbol()`: Resolves index names to valid exchange ticker symbols (e.g., `NIFTY26MAR22350CE`) based on the current ATM strike and nearest expiry.
- `_build_basket_intent()`: Constructs complex multi-leg payloads for POSITIONAL strategies like `IronCondor` (4 legs) and `DirectionalCredit` (2 legs). Uses a high-performance JIT Delta-search to find optimal short and wing strikes.
- `broadcast_decisions()`:
    - Calculates total "Portfolio Heat".
    - Scales lot sizes if the combined heat exceeds the account's risk capacity.
    - Bifurcates logic: single-leg intents for KINETIC/ZERO_DTE and multi-leg baskets for POSITIONAL.
    - Dispatches ZMQ `ORDER_INTENT` messages to the execution bridges.

**Logic Details:**
- **Hybrid Sizing**: Sizing is determined by `Risk-at-Stop` (ATR) rather than capital percentage, ensuring a ₹2,500 loss on a 1-lot NIFTY trade is mathematically similar to a 1-lot BANKNIFTY trade.
- **JIT Basket Priming**: For multi-leg trades, the router immediately publishes `dynamic_subscriptions` for ALL legs (shorts + wings), ensuring the `DataGateway` is already tracking the OTM prices before the first trade fires.
- **Multi-Leg Atomicity**: By assigning a shared `parent_uuid` to all legs in the basket payload, the router provides the necessary relational glue for the `OrderReconciler` to manage fill integrity.
- **Fracture Veto**: A safety mechanism that prevents the system from getting "chopped up" when indices lose correlation during high-volatility events.

### 2.9 [order_reconciler.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/order_reconciler.py)
**Purpose**: The system's "Transaction Monitor". Ensures that multi-leg complex orders either fill completely or are safely rolled back to prevent naked tail risk (e.g., being long a call without the protective wing).

**Classes:**
- `OrderReconciler`: The watchdog for in-flight transaction integrity.

**Methods:**
- `_watchdog_loop()`: Constantly checks for "Stale Baskets" — orders that haven't reached a terminal state (Filled/Rejected) within 3 seconds.
- `reconcile_basket(p_uuid)`: The primary decision logic.
    - Evaluates if all legs are `COMPLETE`.
    - If a **Partial Fill** is detected, it triggers `force_fill()`.
    - If a **Rejection** is detected, it triggers `trigger_rollback()`.
- `force_fill()`: Secures the remainder of an order by firing aggressive market orders.
- `trigger_rollback()`: The circuit-breaker for failed multi-leg trades. It identifies any partially executed legs and fires inverse market orders to close them, returning the account to a flat state for that specific trade.

**Logic Details:**
- **Asymmetrical Risk Mitigation**: In option spreads (Condors/Butterflies), missing one "wing" can lead to uncapped loss. The Reconciler ensures no "limping" positions are left open for more than 3 seconds.
- **Rollback Protocol**: Uses `SYSTEM_CMD` to tell the execution bridges to immediately wipe any state associated with the failed `parent_uuid`.
- **Inter-Service Handshake**: Uses Redis `order_status` keys as the source of truth, creating a decoupled but highly consistent state machine between the Bridges and the Reconciler.

### 2.10 [paper_bridge.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/paper_bridge.py)
**Purpose**: The "Shadow Broker". Provides a simulated execution environment that mirrors live-trading behavior for forward-testing and strategy validation.

**Classes:**
- `PaperBridge`: The core simulation and ledger-keeping engine.

**Methods:**
- `init_db()`: Sets up the TimescaleDB **Hypertable** for paper trades, enabling high-performance historical analysis.
- `execute_orders()`:
    - Listens for intents on `Ports.ORDERS`.
    - Applies a random **0.3 - 0.5 rupee slippage** via `calculate_slippage_and_fees()` to ensure simulation results aren't overly optimistic.
    - Maintains a local 10 orders/sec rate limit.
- `update_portfolio()`: Records trades into the `portfolio` table with `execution_type='Paper'`, maintaining a clean separation from real capital.
- `_handle_basket_rollback()`: Mirrored logic from the `LiveBridge` to ensure multi-leg simulation integrity.

**Logic Details:**
- **Slippage Realism**: By injecting stochastic price noise into every fill, the Paper Bridge forces strategies to handle bid-ask bounce and market impact before moving to real money.
- **Shared Database Truth**: Since it uses the same TimescaleDB instance as the live system, the dashboard can present a unified view of "Total Active Risk" across both paper and live environments.
- **Handoff Parity**: Signals are handed off to the `LiquidationDaemon` in exactly the same way as live trades, ensuring the safety net is equally tested.

### 2.11 [shoonya_gateway.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/shoonya_gateway.py)
**Purpose**: The primary market data connector for live environments. Feeds the system with high-frequency ticks from the Shoonya (Noren) WebSocket.

**Classes:**
- `ShoonyaDataStreamer`: Manages the WebSocket lifecycle and data distribution.

**Methods:**
- `open_callback()`: Logic triggered upon connection. It immediately subscribes to the "Power 10" heavyweights and queries Redis to dynamically subscribe to any option tokens required by active strategies.
- `event_handler_feed_update()`: The raw tick callback. It bridges the broker's background socket thread to the system's `asyncio` loop using `run_coroutine_threadsafe`.
- `_broadcast_tick()`:
    - Writes the latest price/volume to **Shared Memory** (`TickSharedMemory`) for zero-copy IPC.
    - Maintains a 100-tick **Time-Travel Buffer** in Redis for fast daemon recovery.
    - Publishes the tick to ZeroMQ for event-driven engine triggers.

**Logic Details:**
- **Double-Buffered Reliability**: By combining Shared Memory (for speed) and ZeroMQ (for event signaling), the gateway ensures that consumers get the data with <100μs latency while maintaining a robust, decoupled architecture.
- **Dynamic Token Resolution**: Unlike static gateways, this component can "learn" new tokens to watch mid-session if a user activates a new strategy via the dashboard.
- **Microstructure Enrichment**: Uses the `shoonya_master` utility to resolve raw exchange tokens back into human-readable symbols (e.g., `26000` -> `NIFTY50`) before broadcasting.

### 2.12 [strat_eod_vwap.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_eod_vwap.py)
**Status**: `[DEPRECATED]`
**Purpose**: Retail mean-reversion engine (Legacy). Replaced by the 4-Pillar institutional suite.

**Classes:**
- `AnchoredVWAPStrategy`: The strategy state machine.

**Methods:**
- `_compute_vwap()`: Uses **Polars** for lightning-fast volume-weighted average price calculation, bypassing pandas overhead.
- `_maybe_reanchor()`: Intelligent gap handling. Re-anchors at 09:30 AM if the opening gap is >1.0% to prevent "anchoring bias" from overnight news.
- `evaluate()`: Triple-barrier entry check:
    1. Alpha Score > 40.
    2. Spot within ±0.2% of VWAP.
    3. SMA(5) > SMA(20) to confirm the turn.
- `_update_candle()`: Tracks 1-minute OHLC. If a candle closes entirely below VWAP, it triggers a "Hard Invalidation".

**Logic Details:**
- **Institutional Handoff**: This strategy "enters and orphans". Once a buy order is filled, it immediately hands off the position to the `LiquidationDaemon`, freeing up the strategy engine to scan for the next setup.
- **Dynamic Sizing**: Lot sizes are automatically doubled on Wednesdays and Thursdays to exploit high-gamma environments near index expiry.
- **Margin Safety**: Integrated with `MarginManager` to ensure no trade is fired if it would breach the sub-account's capital limit.

### 2.13 [strat_expiry.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_expiry.py)
**Purpose**: A specialized high-speed "Expiry Harvesting" engine. Designed to capture rapid theta-decay and gamma-bursts on 0-DTE option contracts.

**Classes:**
- `ExpiryScalper`: Manages rapid scalp-entries during terminal expiry windows.

**Methods:**
- `handle_commands()`: Listens for systemic state transitions (`ACTIVATE`, `PAUSE`, `ORPHAN`).
- `on_tick()`: Event-driven entry logic. When activated, it immediately enters ATM/OTM scalps on market ticks to capture micro-momentum.
- `handoff_positions()`: Safety protocol that "pushes" open positions to the `LiquidationDaemon` via the `HANDOFF` channel.

**Logic Details:**
- **Regime Constraint**: This strategy is only unlocked by the `MetaRouter` when the market is classified as `EXPIRY` (Wednesday/Thursday afternoon).
- **Infinite Loop Safety**: Includes a `try-except` block to prevent the scalper from crashing during high-message-rate periods (typical on expiry days).
- **Decoupled Execution**: Uses `Topics.ORDER_INTENT` to dispatch orders asynchronously, ensuring the tick-processing loop doesn't stall on network I/O.

### 2.14 [strat_gamma.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_gamma.py)
**Purpose**: The system's "High-Octane" engine. Exploits Negative Dealer Gamma and HMM Trending regimes to capture explosive breakout moves in ATM options.

**Classes:**
- `LongGammaMomentumStrategy`: A sophisticated options scalper with 6-layer entry validation.

**Methods:**
- `evaluate()`: The primary "Veto" logic. It checks:
    1. **Structural/Regime**: Negative GEX + HMM Trending.
    2. **Sentiment**: Vetoes if sentiment is bearish (< -0.3).
    3. **Microstructure**: log-OFI Z-Score > 2.0.
    4. **Correlation**: Dispersion Coeff > 0.5 (ensures the whole index moves together).
- `_check_breakout()`: Uses a 5-minute rolling window (via `collections.deque`) to detect price breakouts before entry.
- `dispatch_buy()`:
    - Reserves margin via `AsyncMarginManager`.
    - Registers the order in `pending_orders` for the Reconciler.
    - Dispatches a market order intent for ATM options.

**Logic Details:**
- **Gamma Pinning Strategy**: By targeting `DTE <= 2` and `Negative GEX`, the strategy bets on dealer delta-hedging which accelerates the trend (the "Gamma Effect").
- **Double Filtering**: The strategy requires both internal microstructure (OFI) and external weightings (Top-5 correlation) to align, significantly reducing the probability of "False Breakouts".
- **Clean Handoff**: Once a trade is initiated, it is "orphaned" to the `LiquidationDaemon`. The strategy purely focuses on the "Inception" of the move, not the "Exit".

### 2.15 [strat_lead_lag.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_lead_lag.py)
**Status**: `[DEPRECATED]`
**Purpose**: Cross-index arbitrage engine (Legacy). Replaced by the 4-Pillar institutional suite.

**Classes:**
- `LeadLagReversionStrategy`: Tracks index decoupling via rolling Pearson correlation.

**Methods:**
- `_compute_correlation()`: Real-time 1-minute rolling correlation between indices.
- `_compute_ratio_zscore()`: Determines if the Nifty/BankNifty ratio has deviated by more than **2.5 standard deviations** from its mean and identifies which index is the "Laggard".
- `_check_volume_confirm()`: Ensures the laggard index is seeing a volume surge (>120% of average) before entry.
- `check_invalidation()`: Monitors the "Snap-Back". If correlation rises above 0.80, it signals that the indices are back in sync and the trade is closed.

**Logic Details:**
- **Pair-Trading Logic**: Unlike single-index strategies, Lead-Lag looks for "fractured correlation". When the two indices diverge, it buys the one that has fallen behind.
- **Statistical Safety**: By requiring a 2.5 Z-score, the strategy waits for a 99% probability level event before entering, ensuring high win-rate reversion trades.
- **Laggard Sensitivity**: lot sizes are dynamically adjusted based on which index is being traded (Nifty vs BankNifty), ensuring consistent margin usage.

### 2.16 [strat_oi_pulse.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_oi_pulse.py)
**Status**: `[DEPRECATED]`
**Purpose**: Unusual Options Activity engine (Legacy). Replaced by the 4-Pillar institutional suite.

**Classes:**
- `OIPulseStrategy`: Monitors strike-level OI depth for NIFTY ATM options.

**Methods:**
- `_compute_oi_acceleration()`: Calculates the ratio of the latest OI against its 10-minute rolling average. Requires a **3x multiplier** to trigger.
- `evaluate()`:
    - Identifies the highest-accelerating strike in the ATM ±100pt range.
    - Verifies that spot price is actively trending *toward* that strike.
- `check_invalidation()`: The "Wall Breach" monitor. If the price pushes through the high-OI strike and stays there for 30s, the strategy exits, assuming the "Resistance Wall" has failed.

**Logic Details:**
- **Anticipatory Scalping**: This strategy seeks to front-run institutional positioning that shows up in OI data before it fully translates into a large price move.
- **Expiry Efficiency**: By focusing on `DTE <= 3`, the engine captures the period where gamma-hedging by dealers is most sensitive to OI changes.
- **Margin Check**: Uses the standard `MarginManager` to block entries that would exceed the account's localized strategy budget.

### 2.17 [strat_reversion.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strat_reversion.py)
**Purpose**: An "Institutional Fade" engine. It identifies historical price extremes and plays the "Snap-Back" to the 15-minute mean.

**Classes:**
- `InstitutionalFadeStrategy`: The mean-reversion controller.

**Methods:**
- `evaluate()`:
    - Ensures a `POSITIVE GEX` and `RANGING` regime.
    - Checks for a **Spot Z-Score > |2.5|** from the 15-minute mean.
    - Confirms **CVD Absorption** is active (passive buyers stepping in).
- `check_invalidation()`:
    - **Liquidity Vacuum**: Exits if the Bid-Ask spread expands >300%.
    - **Trend Conflict**: Exits if `log_ofi_zscore` spikes >3.0 against the trade.

**Logic Details:**
- **Contrarian Momentum**: This strategy intentionally bets against the short-term trend when it deems the move to be "Over-Extended".
- **Liquidity Guard**: By monitoring spread expansion, the strategy avoids being "trapped" in one-sided markets where the exit cost would wipe out the alpha.
- **Statistical Fidelity**: Relies on a 15-minute lookback for mean calculation, providing a stable "Fair Value" line for intraday scalping.

### 2.18 [strategy_engine.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/strategy_engine.py)
**Purpose**: The system's "Unified Execution Hub". It hosts multiple strategies simultaneously and provides shared infrastructure for risk, margin, and high-speed data access.

**Classes:**
- `BaseStrategy`: The standard interface for all logical engines.
- `StrategyEngine`: The orchestrator that manages the tick-to-signal lifecycle.

**Methods:**
- `warmup_engine()`: Fires 1000 synthetic ticks at startup to prime the Python VM and trigger JIT optimization, ensuring the first "real" tick doesn't experience "cold-start" latency.
- `config_subscriber()`: Polls Redis every 2s for strategy updates. Allows users to enable/disable specific strategies or change their parameters (e.g. lot sizes) while the engine is running.
- `_calibrate_vol_context()`: Performs the **B2 Calibration** — calculating the overnight gap's Z-score to set the opening sensitivity for the HMM engines.
- **Alpha Bias Vetoes**: Implements institutional risk-vetoing (e.g., blocking a Buy if global Alpha < -20).
- **Institutional Strategies**: Hosts the 4-Pillar suite: `GammaScalping`, `IronCondor`, `DirectionalCredit`, and `TastyTrade0DTE`.
- **Atomic Capital Locking**: Enforces hard margin checks via `AsyncMarginManager`.

**Logic Details:**
- **Dynamic Hot-Swapping**: Strategy logic can be updated on-the-fly via the `active_strategies` hash in Redis, making the system highly adaptable to changing market conditions without restarts.
- **Latency Hardening**: By reading `shm_alpha` and `shm_ticks`, the engine bypasses the JSON serialization path for time-critical decision metrics.
- **Slippage Protection**: Integrated with a `SLIPPAGE_HALT` guard that blocks all new entries if the system detects order book instability or catastrophic slippage spikes.

### 2.19 [system_controller.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/system_controller.py)
**Purpose**: The system's "Operational Master". Orchestrates the lifecycle of the entire stack, from morning history sync to evening VM termination.

**Institutional Updates**:
- **Selective Shutdown**: At 15:20 IST, it only liquidates `KINETIC` and `ZERO_DTE` trades. `POSITIONAL` trades hibernate securely in the database.
- **50:50 Margin Enforcement**: Ensures compliance with NSE's Cash-to-Collateral requirements on boot.
- **SEBI Guard**: Automated settlement halts on quarterly Fridays.

**Classes:**
- `SystemController`: Manages time-based triggers, cloud infrastructure safety, and SEBI compliance.

**Methods:**
- `_three_stage_eod_scheduler()`:
    1. **15:00 IST**: Origination Halt.
    2. **15:20 IST**: Kinetic/Intraday Square-off.
    3. **16:00 IST**: Final Liquidation & VM Shutdown.
- `_preemption_poller()`: Polls GCP metadata every 5s. Triggers emergency exit if the Spot VM is being killed.
- `_macro_lockdown_watcher()`: Freezes trading during tier-1 economic events (e.g. RBI/Fed announcements).
- `_hard_state_sync()`: The **Ghost Fill Sync**. Reconciles Broker Net-Qty with DB state every 15 minutes to eliminate "Stale Ghosts" or "Orphan Positions".
- `_exchange_health_monitor()`: A high-precision (200ms) latency checker that kills all routing if the NSE feed stalls for 500ms.
- `_eod_summary_report()`: Generates a comprehensive P&L and risk report sent to the Telegram alerts channel.

**Logic Details:**
- **Failure Isolation**: The controller runs independently of the execution engines, ensuring that even if a strategy crashes, the EOD square-off and safety guards remain active.
- **Margin Reserves**: Automatically partitions capital into "Hedge Reserves" (15%) and "Available Margin" (85%) to prevent margin-calls during rapid deployment.
- **T-1 Calendar Guard**: Prevents massive margin spikes by liquidating positions expiring tomorrow at 15:15 the day before.

### 2.20 [tiny_recorder.py](file:///c:/Users/karth/./scratch/trading_microservices/daemons/tiny_recorder.py)
**Purpose**: A low-overhead telemetry service designed to record raw tick-by-tick (TBT) market data into a highly compressed binary format for offline post-hoc analysis.

**Classes:**
- `TinyRecorder`: Manages the websocket lifecycle and the binary file-sink.

**Methods:**
- `compress_tick()`: Uses `struct.pack` to transform a verbose JSON tick into a minimalist **21-byte binary chunk**.
- `connect_and_stream()`:
    - Opens an **LZMA-compressed** file stream.
    - Manages the 09:00 AM login and 15:45 PM shutdown.
- `push_to_gcs()`: Executes a `gsutil` shell command to upload the day's archive to the Google Cloud "Sanctum" bucket.

**Logic Details:**
- **Zero-Waste Storage**: By using binary packing + LZMA, the recorder can capture a full day of high-frequency data for multiple indices on a tiny shared-CPU VM without exhausting disk space.
- **Cross-Index Coverage**: Records both NSE and BSE ticks for the top market-moving heavyweights (Reliance, HDFC Bank, etc.), enabling post-market correlation studies.
- **Secure Handoff**: Integrated with Telegram to notify the administrator once the data has been successfully "sharded" to the cloud data lake.

### 2.21 [fii_data_extractor.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/fii_data_extractor.py)
**Purpose**: A daily scheduled task that scrapes institutional positioning data from the NSE to compute a "Macro Sentiment" score.

**Classes:**
- `FIIDataExtractor`: Manages the NSE scraping and bias computation.

**Methods:**
- `_fetch_nse_data()`: Uses `httpx` with session-cookie handling to safely retrieve the Derivative Position Limits JSON from the NSE API.
- `_compute_bias()`:
    - Aggregates Future and Option positions for Foreign Institutional Investors (FII).
    - Normalizes the net positioning into a **Bullish/Bearish score from -15 to +15**.
- `_wait_for_fetch_time()`: Sleeps until 18:30 IST, the standard time NSE releases this data.

**Logic Details:**
- **Institutional Weighting**: This score is consumed by the `CompositeAlphaScorer` to adjust dynamic entry thresholds. If FIIs are heavily bearish (-10), long strategies require much stronger signals to fire.
- **Bot Protection Bypass**: Implements a two-step HTTP handshake (Homepage -> API) to maintain valid session headers, preventing automated IP blacklisting.
- **Fail-Safe Neutrality**: In cases of API outages, the system defaults to a neutral `0` bias, ensuring the strategy engine doesn't derive false confidence from stale data.

### 2.23 [hmm_trainer.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/hmm_trainer.py)
**Status**: `[LEGACY / RESEARCH ONLY]`
**Purpose**: The nightly "Self-Correction" engine (Legacy). Formerly used to refine HMM transitions; now retained for offline research to compare heuristic vs. stochastic performance.

**Classes:**
- `HMMTrainer`: Handles model training, validation, and promotion.

**Methods:**
- `fetch_data()`: Queries TimescaleDB for the last 30 days of high-fidelity microstructure signals (OFI, VPIN, Basis, etc.).
- `preprocess()`: Generates **Ground Truth labels** by calculating forward returns, allowing the model to link hidden states to future price outcomes.
- `validate()`: Performs a comparative **Log-Likelihood** check. Only "Promotes" the new model if its statistical fit is superior to the current one.
- `save_model()`: Serializes the winner and synchronizes it to GCS for the `hmm_engine.py` daemon to pick up on the next reload.

**Logic Details:**
- **Microstructure Fidelity**: Unlike the Cold Start tool, this trainer uses real microstructure features (`VPIN`, `OFI`), allowing for much higher-precision state identification.
- **Regression Prevention**: The promotion logic acts as a "Circuit Breaker", ensuring that a bad data day don't poison the model transition matrix.
- **Model Evolution**: By retraining nightly, the system's "Brain" adapts to long-term changes in market volatility and participant behavior.

### 2.24 [macro_event_fetcher.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/macro_event_fetcher.py)
**Purpose**: The system's "Economic Radar". It scrapes global and domestic financial calendars to identify periods of high systemic risk.

**Methods:**
- `fetch_forex_factory()`: Pulls high-impact global macro events.
- `fetch_fmp_economic_calendar()`: Pulls detailed US and Indian economic prints (CPI, GDP, etc.) using the FMP API.
- `deduplicate()`: Merges the two feeds, resolving overlaps for major US data releases.
- `fetch_and_write()`: The main entry point that synchronizes the merged calendar to both disk and Redis.

**Logic Details:**
- **Network Hardening**: Overrides Python's DNS resolver with a local cache to eliminate lookup overhead and implements exponential backoff for all API calls.
- **Risk Mitigation**: This utility populates the `macro_events` list, which the `system_controller.py` uses to freeze trading activity during volatile news windows (e.g., 30 mins before a Tier-1 print).
- **Redundancy**: By merging ForexFactory and FMP, the system ensures that it remains aware of Indian macro events even if one data provider experiences an outage.

### 2.25 [shoonya_master.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/shoonya_master.py)
**Purpose**: The "Instrument Registry". It downloads, parses, and caches the Shoonya NFO symbol master list, enabling micro-second lookups between human-readable strike names and broker-specific tokens.

**Methods:**
- `download_and_extract()`: Pulls the latest compressed symbol master from the Shoonya server using an memory-efficient chunked stream.
- `parse_and_cache()`:
    - Parses the massive text file into a Pandas DataFrame.
    - Performs **Chunked Redis Insertion** (10,000 items at a time) to populate the `shoonya_nfo_tokens` and `shoonya_nfo_symbols` hashes.
- `get_token()` / `get_symbol()`: Helper functions used by gateways and strategies to translate between names and IDs.

**Logic Details:**
- **Zero-Wait Lookups**: By mirroring the entire NFO universe in Redis on boot, the system ensures that dynamic subscriptions (e.g. subscribing to a new strike during a breakout) experience zero I/O latency.
- **Data Integrity**: Uses strict string-typing during CSV parsing to prevent the truncation of numeric broker tokens that start with leading zeros.
- **Resource Management**: Implements careful chunked insertion to ensure Redis remains responsive to high-speed market data ticks while the symbol master is being re-synced in the background.

### 2.26 [telegram_bot.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/telegram_bot.py)
**Purpose**: The system's "Outbound Voice". It monitors a Redis queue for urgent notifications and relays them to the user's mobile device.

**Classes:**
- `TelegramAlerter`: An asynchronous dispatcher that drains the `telegram_alerts` queue.

**Methods:**
- `start()`: Establishes the Redis connection and begins the `BRPOP` loop.
- `_dispatch()`: Formats the raw alert JSON into a stylized HTML message, appending live margin usage statistics for context.
- `push_alert()`: A static helper used throughout the codebase to asynchronously enqueue messages from any daemon.

**Logic Details:**
- **Context Enrichment**: Every notification sent via the bot includes a snapshot of current `Live` and `Paper` capital utilization, allowing the trader to assess risk immediately without opening a dashboard.
- **Safety Throttling**: Limits outgoing messages to 1 per second, preventing the bot from being banned by Telegram's flood-control filters during "Flash Crash" scenarios where hundreds of alerts might trigger.
- **Micro-Categorization**: Uses a lookup table to prefix messages with context-specific emojis (e.g. ⚡ for Cloud Preemption, 👻 for Order Ghosts), making the notification stream highly readable.

### 2.22 [hmm_cold_start.py](file:///c:/Users/karth/./scratch/trading_microservices/utils/hmm_cold_start.py)
**Status**: `[LEGACY / RESEARCH ONLY]`
**Purpose**: The "Pre-Flight" initialization tool (Legacy). Formerly used to build baseline HMMs; now used to generate historical benchmarks for the Heuristic Engine.

**Classes:**
- `HMMColdStarter`: Manages data ingestion from Kaggle and Yahoo Finance.

**Methods:**
- `download_kaggle_data()`: Connects to KaggleHub to pull long-term Nifty historical 1-minute datasets.
- `engineering_features()`: Creates **Proxy Features** (Velocity Z-Score and Realized Volatility) to simulate real-time signals when using offline OHLC data.
- `train_and_save()`: Fits a 3-state Gaussian HMM and serializes the state-transition matrix to `hmm_generic.pkl`.

**Logic Details:**
- **Kaggle-YF Bridge**: Combines multi-year Kaggle data with the most recent 7 days of Yahoo Finance 1-min data to provide the HMM with both structural depth and recent volatility contexts.
- **Dimensionality Padding**: Pads the feature matrix to match the 4-dimensional input expected by the live engine, allowing the "Generic" model to be hot-swapped into the `daemons/hmm_engine.py` without code changes.
- **Cloud Model Lake**: Automatically synchronizes the trained model to GCS, allowing multiple trading VM instances to share the same structural baseline.
