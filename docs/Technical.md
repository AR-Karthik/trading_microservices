@
# Technical Documentation
Exhaustive architectural and implementation details for the microservices suite.
---
@
---
### [EXHAUSTIVE] core/__init__.py (Modular Boundary)

**Technical Architecture**:
Marks the `core/` directory as a Python namespace package. This is essential for the `import core.alerts` and `from core import ...` syntax used throughout the microservices.

**Technical Decomposition**:
- **Role**: Namespace initialization.
- **Impact**: Without this file, the Python interpreter would treat `core/` as a generic directory, leading to `ModuleNotFoundError` when the daemons attempt to access shared utilities.

**Logic Flow for Recreation**:
1. Create a file named `__init__.py`.
2. Leave it empty or add a descriptive comment.
3. Place it in the root of any directory that needs to be imported by other Python scripts.

---
### [EXHAUSTIVE] core/alerts.py (Asynchronous Notification Bus)

**Technical Architecture**:
This component follows the **Producer-Consumer pattern**. The `CloudAlerts` class is the 'Producer,' pushing tasks into a Redis-backed queue (`telegram_alerts`). A separate microservice (the Telegram/Dashboard consumer) acts as the 'Consumer.'

**Method-by-Method Technical Breakdown**:
1. **`CloudAlerts.get_instance()`**: Uses a class-level `_instance` variable. This is a thread-safe-ish (in asyncio) singleton provider.
2. **`CloudAlerts._ensure_redis()`**:
   - Uses `redis.from_url` with `decode_responses=True`.
   - **Fault Tolerance**: Wrapped in a `try-except` block to prevent service-level cascading failures if Redis is unreachable.
3. **`CloudAlerts.alert()`**:
   - **Concurrency Model**: Mark as `async`. It uses `await self._redis.lpush` which is a non-blocking I/O operation.
   - **Data Structure**: Uses a Redis **LIST** (via `lpush`). This guarantees **First-In-First-Out (FIFO)** order for alerts, ensuring sequence integrity.
   - **Serialization**: Uses `json.dumps()` for cross-language compatibility (e.g., if the consumer is written in Rust or Go).

**Logic Flow for Recreation**:
1. Check if a singleton instance exists; if not, create it.
2. Store the Redis configuration string.
3. When `alert` is called:
   - Check/Initialize Redis connection if null.
   - Construct a dictionary with `message`, `type`, and `timestamp`.
   - Use `kwargs.update()` to allow strategies to attach custom metadata (like order IDs).
   - Convert the dictionary to a JSON string.
   - Use `LPUSH` to put the string at the front of the `telegram_alerts` key.
   - Catch and log any Redis errors to prevent parent caller disruption.

---
---
### [EXHAUSTIVE] core/auth.py (Credential Resolver)

**Technical Architecture**:
A utility module for **Environment Injection**. It uses the `python-dotenv` library to load configuration into the OS environment before the system boots.

**Method-by-Method Technical Breakdown**:
1. **`get_redis_url()`**: 
   - Dynamically assembles a URI string. 
   - Uses `os.getenv` with sensible defaults (e.g., 'redis' host).
   - Implements conditional formatting to handle password-less (local) vs password-protected (cloud) Redis deployments.
2. **`get_db_dsn()`**: Assembles a `postgres://` RFC-compliant URI. Used primarily for tools that expect a single connection string.
3. **`get_db_config()`**: Returns a configuration mapping. This is designed for `asyncpg.connect()` which prefers kwargs over DSNs for fine-grained control over connection parameters (like SSL).

**Variable Dictionary & Constants**:
- `HEDGE_RESERVE_PCT`: Set to `0.15`. This is a system-wide risk constant. Changing this affects the global margin reservation logic in the `MetaRouter`.
- `load_dotenv()`: Called at the module level. This ensures that any subsequent imports (like `core.alerts`) immediately see the updated environment variables.

**Logic Flow for Recreation**:
1. Initialize `dotenv` to read `.env` file.
2. Define functions to read `os.environ` keys like `DB_USER`, `REDIS_HOST`, etc.
3. Construct standard URI strings: `protocol://user:pass@host:port/resource`.
4. Provide a hardcoded risk constant for margin hedging.

---
### [EXHAUSTIVE] core/db_retry.py (Resiliency Decorators)

**Technical Architecture**:
Implements a **Retry with Jitter/Backoff** pattern to handle transient network state failures. It integrates tightly with `asyncpg` to detect specific connection dropped states.

**Method-by-Method Technical Breakdown**:
1. **`with_db_retry(max_retries=3, backoff=0.5)`**:
   - **Pattern**: Function Decorator using `functools.wraps`.
   - **Error Handling**: Explicitly catches `ConnectionDoesNotExistError`, `InterfaceError`, and `InternalClientError`.
   - **Intrinsic Recovery**: Checks for an `_reconnect_pool` method on the decorated instance (`self`). This allows for **stateful recovery** where the class can rebuild its entire connection pool if the decorator detects a 'Dead' connection.
2. **`robust_db_connect()`**:
   - Uses an infinite-style `while True` loop gated by `max_retries`.
   - **Wait Logic**: Implements linear-plus-static backoff (`min(2 * retry_count, 30)`).
   - **Docker Readiness**: Specifically designed to handle the `ECONNREFUSED` state seen when services are booting in parallel.

**Logic Flow for Recreation**:
1. Define a decorator that iterates `N` times.
2. Inside the loop, `try` the async function.
3. If a specific DB exception occurs:
   - Increment attempt counter.
   - If parent class has a recovery method, call it.
   - `sleep` for `backoff * attempt_count`.
4. If attempts are exhausted, `raise` the last error to the caller (Terminal Failure).

---
---
### [EXHAUSTIVE] core/execution_wrapper.py (Atomic Order Manager)

**Technical Architecture**:
A high-level execution orchestration layer. It bridges the gap between raw broker API calls and strategic 'Intents.'

**Method-by-Method Technical Breakdown**:
1. **`adaptive_limit_chase()`**:
   - **Goal**: Resolve the 'Price Improvement' problem.
   - **Logic**: A 5-iteration loop. After each iteration, if unfulfilled, it modifies the limit price by 1 `tick_size` (0.05).
   - **Slippage Cap**: If `abs(current - best) / best > max_slippage_pct`, it triggers a **SLIPPAGE_HALT** via Redis. This is a global system circuit breaker.
2. **`execute_legs()`**:
   - **Sorting Logic**: `sorted(orders, key=lambda x: 0 if x == "BUY" else 1)`. 
   - **Sequential Mode**: Implements a **Pub/Sub Watcher**. It subscribes to `order_confirmations` and blocks (waits) until the broker confirms the long leg is set.
   - **Latency Trade-off**: Sequential execution is safer (margin) but slower. Atomic (parallel) is faster but requires full margin upfront.
   - **Redis Integration**: Uses Redis as a real-time signaling bus for `order_confirmations` and `dynamic_subscriptions`.

**Logic Flow for Recreation**:
1. Sort orders so BUYS go before SELLS.
2. Publish `BASKET_ORIGINATION` to the ZMQ command bus.
3. For each order:
   - Identify if it needs 'Chasing' (OTM) or 'Direct Placement'.
   - Fire the `place_order` command.
   - If sequential and it's a 'Cover' (Buy) leg, start a 5s `while` loop monitoring Redis PubSub for that specific symbol's 'COMPLETE' status.
   - Move to next leg once confirmed or timed out.
4. Return a list of all order IDs or Exceptions.

---
### [EXHAUSTIVE] core/greeks.py (Numerical Options Engine)

**Technical Architecture**:
A computational library leveraging `scipy.stats.norm` for Cumulative Distribution Function (CDF) and Probability Density Function (PDF) calculations.

**Method-by-Method Technical Breakdown**:
1. **`d1()` and `d2()`**: Implements the base stochastic differential components.
   - **Resiliency**: Uses `1e-9` as a zero-division guard for `T` (Time) and `sigma` (Volatility) to prevent `NaN` results during near-expiration states.
2. **`delta()`**: Provides the first derivative of price. 
   - **Edge Cases**: Explicitly handles `T <= 0` (expiration) to return boolean-style result (1.0 if ITM, 0.0 if OTM).
3. **`gamma()`**: Second derivative. Normalizes the PDF by `S * sigma * sqrt(T)`.
4. **`vega()`**: Sensitivity to sigma. Scaled by `/ 100` to provide the 'Per 1% move' metric used by professional trader desks.
5. **`theta()`**: Time decay. Implements the complex 5-term differential. Normalizes results by `/ 365` to provide a daily dollar-decay value.

**Logic Flow for Recreation**:
1. Import `math` and `scipy.stats.norm`.
2. Define `d1` as `(ln(S/K) + (r + 0.5 * sigma^2) * T) / (sigma * sqrt(T))`.
3. Use `norm.cdf(d1)` for Call Delta and `norm.cdf(d1) - 1` for Put Delta.
4. Implement a `max(T, 1e-9)` guard on all functions to allow for calculation on the day of expiration.

---
### [EXHAUSTIVE] core/health.py (Distributed Health Tracking)

**Technical Architecture**:
Implements a **Distributed Heartbeat** pattern using Redis Hashes and TTL (Time-To-Live) keys.

**Method-by-Method Technical Breakdown**:
1. **`HeartbeatProvider.run_heartbeat()`**:
   - **Implementation**: Infinite `async` loop.
   - **Primary Store**: `hset("daemon_heartbeats", self.name, timestamp)`. This provides a single 'Big Picture' view for auditing.
   - **Secondary Store**: `set(f"LH:{self.name}")` with an `ex=30` (30 second expiry). This allows the Dashboard to monitor health via simple 'Key Exists' checks.
2. **`HealthAggregator.get_system_health()`**:
   - Fetches all heartbeats in a single `hgetall` call for efficiency.
   - **Business Logic**: Compares current system time against each daemon's last heartbeat. If the gap is `> 15.0s`, the daemon is marked **DEAD**.
   - Returns a serialized dict for the SystemController to broadcast.

**Logic Flow for Recreation**:
1. Define a list of all required service names (Enums).
2. Create a class that runs a loop: `await redis.hset("heartbeats", my_name, current_time)`.
3. Create an aggregator that:
   - Reads the heartbeat hash.
   - Loops through required names.
   - If `current_time - record_time < 15`, increment healthy count.
   - Return (healthy_count / total_required).

---
### [EXHAUSTIVE] core/logger.py (JSON Log Aggregator)

**Technical Architecture**:
A wrapper around the standard Python `logging` module that overrides default formatting to produce structured, machine-indexable JSON.

**Method-by-Method Technical Breakdown**:
1. **`JsonFormatter.format()`**:
   - Maps `logging.LogRecord` attributes to a flat dictionary.
   - **Correlation Tracking**: Specifically looks for a `correlation_id` attribute (set via `extra={...}` during log calls) to enable trace-level debugging across microservices.
   - **Fidelity**: Uses `formatException` to ensure full multi-line tracebacks are captured as a single string field in the JSON.
2. **`setup_logger()`**:
   - **Idempotency**: Uses `logger.hasHandlers()` to prevent duplicate logs if re-initialized.
   - **File Rotation**: Implements `RotatingFileHandler` with `maxBytes=10MB` and `backupCount=5` to bound local storage growth.

**Logic Flow for Recreation**:
1. Subclass `logging.Formatter`.
2. Overwrite `format(record)`:
   - Build a dict with key fields: time, level, logger, message, module.
   - Use `json.dumps()` for the final return.
3. Define a setup function that:
   - creates a `logging.getLogger`.
   - Adds a `StreamHandler` (Stdout).
   - Adds a `RotatingFileHandler` (Disk).
   - Sets the level to `INFO`.

---
---
### [EXHAUSTIVE] core/margin.py (LUA-Atomic Risk Controller)

**Technical Architecture**:
Standardizes capital management using server-side **LUA scripts** executed within Redis. This ensures **Linearizability** (thread-safety) for capital operations across multiple distributed strategy instances.

**Method-by-Method Technical Breakdown**:
1. **`LUA_RESERVE`**:
   - Implements a **Priority-Based Drain** strategy. 
   - Logic: `coll_use = min(coll_avail, req * 0.5)`. This prioritizes using collateral (stocks) to satisfy the 'Non-Cash' portion of the margin requirement, preserving liquid cash.
   - **Safety**: Hard-blocks the trade (`return -2`) if the 50:50 cash-to-total margin ratio is breached.
2. **`LUA_RELEASE`**:
   - Implements **Accounting Rectification**.
   - Logic: Profit is extracted as `amount_back - original`. All profit is added to the `CASH` key.
   - **Ceiling Guard**: `if (new_cash + new_coll) > (limit * 1.02)`. This prevents "phantom money" from appearing due to double-release bugs, capping the balance at 102% of the initial limit.
3. **`MarginManager.get_state()`**: Performs an atomic multi-key read using the `LUA_GET_STATE` script to prevent 'Partial Read' (Dirty Read) inconsistencies.

**Logic Flow for Recreation**:
1. Define keys for `CASH`, `COLLATERAL`, and `LIMIT` in Redis (namespaced per execution type).
2. Write a LUA script to:
   - READ current values.
   - CHECK if (Cash+Coll) >= Required.
   - SUBTRACT required from balances using the 50:50 logic.
3. Provide a Python wrapper using `redis.register_script` to manage state-switching between 'PAPER' and 'LIVE' modes.

---
### [EXHAUSTIVE] core/mq.py (ZeroMQ Messaging Fabric)

**Technical Architecture**:
A multi-protocol wrapper (PUB/SUB, PUSH/PULL, ROUTER/DEALER) for **Context-Aware Messaging**. It uses `zmq.asyncio` for non-blocking I/O.

**Method-by-Method Technical Breakdown**:
1. **`MQManager`**:
   - Manages a shared `zmq.asyncio.Context`.
   - Maps logical service names (e.g., `market_data`) to physical IP/Port pairs defined in environment variables.
2. **`send_json()` and `recv_json()`**:
   - Implements **Multipart Messaging**. 
   - Frame 1: Topic. Frame 2: Metadata Header (JSON). Frame 3: Data (JSON).
   - **Context Tracking**: Uses `contextvars` to propagate the `correlation_id` across the `await` boundary, ensuring logs from different services can be grouped visually.
3. **`NumpyEncoder`**: Overrides the default JSON encoder to handle `numpy.float64` and `numpy.ndarray`, which are common outputs from the Regime and Sensor engines.

**Logic Flow for Recreation**:
1. Initialize a `zmq.asyncio.Context`.
2. Define a port-to-host mapping.
3. Implement `create_publisher` and `create_subscriber` with `setsockopt(zmq.LINGER, 1000)` to prevent ghost processes during shutdown.
4. Implement a 3-part multipart sender/receiver that handles JSON serialization and correlation-ID injection.

---
### [EXHAUSTIVE] core/network_utils.py (Fault Isolation Utilities)

**Technical Architecture**:
Implements higher-order functions and state machines for **Fail-Fast** and **Retry** behavior.

**Method-by-Method Technical Breakdown**:
1. **`exponential_backoff`**:
   - **Implementation**: Decorator pattern with `asyncio.sleep`.
   - **Delay Logic**: `min(base_delay * (2 ** (retries - 1)), max_delay)`. This provide a progressive 'Cooldown' for the system.
2. **`CircuitBreaker`**:
   - **State Machine**: 
     - **CLOSED**: Requests flow normally. Failures increment a counter.
     - **OPEN**: Failures exceed `threshold`. All requests are immediately rejected with an `Exception`. Start recovery timer.
     - **HALF_OPEN**: Recovery timer expired. Allow **one** request. If successful, transition to **CLOSED**. If failed, transition back to **OPEN**.
   - **Purpose**: Prevents 'Cascading Failures' where one slow service times out all others.

**Logic Flow for Recreation**:
1. Create a decorator that wraps a function in a `while` loop.
2. Use `try/except` to catch failures and `sleep` with exponential wait.
3. Create a state machine class with `CLOSED`, `OPEN`, `HALF_OPEN` logic based on failure timestamps.

---
### [EXHAUSTIVE] core/shared_memory.py (IPC Optimizer)

**Technical Architecture**:
Leverages the **Python 3.8+ `shared_memory` module** for zero-copy, cross-process data exchange. It uses binary serialization via the `struct` module for memory-mapped alignment.

**Method-by-Method Technical Breakdown**:
1. **`TICK_STRUCT_FORMAT`**: `32s d q d d Q Q d d`. 
   - Ensures fixed-width memory layout.
   - `32s`: Symbol name. `d`: Double-precision float (Price). `q`: 64-bit Int (Volume). `Q`: Unsigned 64-bit Int (Seq/Hires Time).
2. **`write_tick()`**:
   - Calculates memory offset: `slot_index * TICK_STRUCT_SIZE`.
   - Packs data into a binary buffer.
   - Uses `shm.buf[offset : offset + size] = packed_data` for a direct memory write.
3. **`read_tick()`**:
   - Uses `struct.unpack` to convert the binary buffer back into a Python dictionary.
   - Strips the `\0` padding from the symbol string.

**Logic Flow for Recreation**:
1. Define a fixed-size binary structure representing a price tick.
2. Initialize `shared_memory.SharedMemory` with a unique name.
3. Create a deterministic mapping (Map) between symbols (e.g., NIFTY) and integer indices (Slots).
4. Provide `read` and `write` methods that calculate byte offsets based on the slot index and format size.

---
---
### [EXHAUSTIVE] core/shm.py (Shared Memory Signal Bus)

**Technical Architecture**:
Implements a **Shared Memory mapped IPC** mechanism using the `mmap` module. Optimized for Windows (`tagname`) and Linux (`/dev/shm`) cross-compatibility. It uses fixed-offset binary structures to minimize serialization overhead.

**Method-by-Method Technical Breakdown**:
1. **`SignalVector` Dataclass**: Define the schema for the quantitative state vector. Includes 31 double-precision floats and 3 booleans.
2. **`ShmManager.write()`**:
   - **Checksum Calculation**: Performs a floating-point sum of all vector components. This serves as a lightweight **Consistency Guard** to detect 'Partial Writes' (Races).
   - **Serialization**: Uses `struct.pack` with the `dddd...` format (31 doubles). 
   - **Memory Write**: Uses `self.shm.write` at offset 0.
3. **`ShmManager.read()`**:
   - **Atomicity Guard 1 (Timestamp)**: Rejects the packet if `time.time() - write_ts > 1.0`.
   - **Atomicity Guard 2 (CRC)**: Recalculates the sum of all fields and compares against the stored CRC. Rejects if the difference `> 1e-4`. 
   - **Hydration**: Returns a new `SignalVector` instance or `None` if invalid.

**Logic Flow for Recreation**:
1. Define a large `dataclass` for all quantitative features.
2. Open an `mmap` buffer of 1024 bytes.
3. On Write:
   - Pack the features into binary using `struct`.
   - Calculate a sum-based CRC of all features.
   - Write (timestamp + features + CRC) to the buffer.
4. On Read:
   - Unpack the binary buffer.
   - Check if current time is roughly equal to the stored timestamp.
   - Verify the CRC sum.
   - Return the reconstructed dataclass.

---
---
### [EXHAUSTIVE] core/proto/messages.proto (Binary Protocol Schema)

**Technical Architecture**:
Defines the **Google Protocol Buffers (proto3)** schema for cross-service communication. Protobuf is chosen over JSON for internal ZMQ traffic because it is **Language-Neutral**, **Compact (Binary)**, and supports **Strong Typing**.

**Message Definitions**:
1. **`TickData`**: Optimized for high-frequency consumption. 
   - **Fields**: Symbol (string), Price (double), Bid/Ask (double), Volumes (int64), Timestamp (string), OI (double), VWAP (double).
   - **Rationale**: Double precision is used to maintain price accuracy for instruments with small tick sizes (e.g., 0.05).
2. **`AlphaSignal`**: Encapsulates the quantitative verdict.
   - **Fields**: Regime (string), GEX Sign (string), Toxicity Veto (bool).
3. **`StrategyConfig`**: Used for management plane operations (lifecycle).
   - **Fields**: State (Enum-like string: ACTIVE, SLEEP), Target/Stop-Loss parameters.

**Logic Flow for Recreation**:
1. Install `protoc` (the protobuf compiler).
2. Create a `.proto` file with `syntax = "proto3"`.
3. Define messages using unique field tags (e.g., `symbol = 1`).
4. **Resiliency Rule**: Never re-use field tags (even for deleted fields) to maintain backward compatibility during live system upgrades.

---
---
### [EXHAUSTIVE] daemons/cloud_publisher.py (Distributed State Orchestrator)

**Technical Architecture**:
Acts as the **GCP Ingress/Egress Gateway**. It implements an asynchronous polling and archival strategy using `google-cloud-firestore`, `google-cloud-storage`, and `asyncpg`.

**Method-by-Method Technical Breakdown**:
1. **`_init_cloud_clients()`**:
   - Implements **Lazy Initialization** and **Credential Sanitization**.
   - Specifically checks if `GOOGLE_APPLICATION_CREDENTIALS` is a valid file to prevent the metadata server from hanging during auth-refresh loops in Docker.
2. **`_heartbeat_loop()`**:
   - **Data Aggregation**: Performs multi-step Redis `GET` and `HGETALL` operations to hydrate a monolithic `state` dictionary.
   - **Latency Tracking**: Calculates `network_lag_ms` by comparing the `exchange_ts` in market ticks against local system time.
   - **Stale Protection**: If lag exceeds 500ms, it flags the dashboard as 'Stale,' preventing the trader from making decisions on old data.
3. **`_command_watcher()`**:
   - Polling-based consumer of the Firestore `commands/latest` document.
   - On detecting a flag (e.g., `PANIC_BUTTON: True`), it resets the flag in Firestore and publishes to the Redis `panic_channel`.
4. **`_eod_snapshot()`**:
   - **TimescaleDB Extraction**: Uses a **Server-Side Cursor** via `conn.cursor()` to stream data in 50,000-row chunks, preventing Out-of-Memory (OOM) errors.
   - **Parquet Serialization**: Uses `pyarrow` to convert row-data into columnar Parquet files with 'snappy' compression.
   - **Workflow**: SQL Query -> PyArrow Table -> Local Temp File -> GCS Upload -> Cleanup.

**Logic Flow for Recreation**:
1. Initialize Redis and Google Cloud clients.
2. Start a 5s loop that:
   - Reads global metrics (Alpha, P&L, Veto states) from Redis.
   - Constructs a JSON payload.
   - Updates Firestore `system/metadata` with `merge=True`.
3. Start a 3s loop that:
   - Reads a specific Firestore document for a `PANIC_BUTTON` boolean.
   - If true, fire a Redis broadcast and reset the cloud flag.
4. Define a scheduled task (15:35 IST) to:
   - Connect to PostgreSQL.
   - Stream today's records into a Parquet writer.
   - Upload the resulting file to a GCS bucket.

---
---
### [EXHAUSTIVE] daemons/data_logger.py (TimescaleDB Ingestion Service)

**Technical Architecture**:
Implements a **Buffered Write Pattern** using `asyncpg` and ZMQ. It serves as the primary sink for synthesized market states.

**Method-by-Method Technical Breakdown**:
1. **`start()` / Schema Management**:
   - Executes `ALTER TABLE IF NOT EXISTS` at startup. This ensures that 'Wave' updates to the feature set (like `asto_regime`) are propagated to the database without manual migration scripts.
2. **`_batch_writer()`**:
   - **Concurrency**: Uses `asyncio.Lock` to maintain thread-safe access to the `self.batch` list during swap-and-flush cycles.
   - **Operation**: Periodically (10s) extracts the batch and uses `conn.executemany()` for high-throughput insertion.
   - **Conflict Handling**: Uses `ON CONFLICT (time, symbol) DO NOTHING` to prevent ID collisions during service restarts or overlapping batches.
3. **`with_db_retry`**: Wraps the batch writer to handle transient DB connection drops, calling `_reconnect_pool` on failure.

**Logic Flow for Recreation**:
1. Subscribe to ZMQ `MARKET_STATE` topic.
2. Append incoming JSON payloads to a local list.
3. Every 10 seconds:
   - Acquire lock.
   - Move current list to local storage; empty global list; release lock.
   - Map JSON fields to SQL parameters (handling missing keys).
   - Execute a bulk `INSERT` into `market_history`.
   - Log the flush count.

---
### [EXHAUSTIVE] daemons/regime_detector.py (Deterministic State Machine)

**Technical Architecture**:
A deterministic **Regime Classifier** based on signal-processing heuristics. It bypasses complex probabilistic training in favor of low-latency, rule-based state transitions (S18).

**Method-by-Method Technical Breakdown**:
1. **`_calculate_adx_approximation()`**:
   - Implements a windowed ADX calculation using `numpy`. 
   - **Fidelity**: Injects 'Intraday Wicks' (Day High/Low) from the live stream into the daily close array to provide a real-time 'Trend Pulse' before the day ends.
2. **`classify_regime()`**:
   - **Hysteresis Gate**: Implements distinct 'Entry' and 'Exit' thresholds. Example: Entering Trend requires ADX > 25, but exiting requires ADX < 20. This prevents 'Oscillation' in choppy markets.
3. **`Persistence (S26)`**: Calculated as a percentage of the last 100 classifications.
4. **`Quality (S27)`**:
   - For Trending: `(ADX / 50) * 100`.
   - For Ranging: `(Historical_RV / Current_RV) * 100`.

**Logic Flow for Recreation**:
1. Subscribe to the `GLOBAL` or Asset-Specific Signal Vector SHM.
2. Every market state update:
   - Calculate 14-day Realized Volatility.
   - Calculate ADX using historical closes + intraday wicks.
   - Apply the Hysteresis state machine to define the S18 integer.
   - Calculate Persistence (S26) and Factor Confidence (S27).
   - Write a `RegimeVector` to the shared memory bus.

---
### [EXHAUSTIVE] daemons/liquidation_daemon.py (Global Risk Guardian)

**Technical Architecture**:
A multi-threaded **Risk Oversight Service** that implements the **Triple-Barrier and Margin-Halt logic**. It operates primarily as a consumer of DB state and SHM signals.

**Method-by-Method Technical Breakdown**:
1. **`_check_stop_day_loss()`**:
   - Fetches global P&L from Redis (`DAILY_REALIZED_PNL_LIVE`).
   - Hard-compares against the `STOP_DAY_LOSS` limit.
   - Triggers `panic_channel` broadcast.
2. **`_evaluate_barriers()`**:
   - **Phase 1: SHM Consumption**: Reads the `SignalVector` for the specific asset (NIFTY/BANKNIFTY).
   - **Phase 2: Hedge Waterfall**: If `asto > 90` or `delta > 0.15`, it initiates a ZMQ call to the `FutureHedge` daemon to neutralize the delta.
   - **Phase 3: Barriers**: Checks price vs `sl_level` and `tp_level`. 
3. **`_monitor_fill_slippage()`**: 
   - Subscribes to `order_confirmations`. 
   - Logic: `abs(fill - intended) / intended > 0.02`. 
   - Action: Sets a **SLIPPAGE_HALT** key in Redis for 60s.

**Logic Flow for Recreation**:
1. Hydrate a list of 'Open Positions' from the SQL `portfolio` table.
2. Start a continuous loop listening to the `TICK.*` ZMQ topic.
3. For every tick on an open instrument:
   - Identify the Strategy and Lifecycle (Kinetic/Positional).
   - Check if current price breaches the Stop-Loss.
   - Check for Global Veto states (S18=3, VPIN > 0.8).
   - If any barrier is breached:
     - Generate a `SQUARE_OFF` command packet.
     - Push to the `ORDER_INTENT` port.
     - Log a 'CRITICAL' alert.

---
---
### [EXHAUSTIVE] daemons/market_sensor.py (Signal Processing & Microstructure)

**Technical Architecture**:
A **Multi-Process Hybrid Sensor**. It uses `asyncio` for I/O and `multiprocessing` for CPU-bound signal engineering. It produces a `SignalVector` every 50-100ms.

**Method-by-Method Technical Breakdown**:
1. **`_compute_worker()`**: 
   - **GIL Bypass**: Operates in an isolated OS process. Receives 'Snapshots' of market depth/trades via an `mp.Queue`.
   - **Black-Scholes Integration**: Calculates ATM Greeks (Delta, Gamma, Charm, Vanna) for the current spot using the `core.greeks` module.
   - **Signal Normalization**: Maps raw indicators (OFI, CVD) into Z-Scores using rolling means and standard deviations to ensure the MetaRouter receives 'Unit-less' inputs.
2. **`_ofi()` (Order Flow Imbalance)**:
   - Implements the **Cont-Stoikov Model**. 
   - Logic: `Delta_Bid = (Size if Price > Prev else Size-Prev if Price==Prev else -Prev)`. 
   - This provides a high-fidelity look at 'Hidden' liquidity pressure.
3. **`_classify_trade()`**: 
   - Implements the **Lee-Ready Algorithm**. 
   - Assigns 'Aggression' to ticks based on mid-quote proximity. Handles 'Tie-Breakers' using the previous tick's sign (Tick Rule).
4. **`AdaptiveSuperTrendOscillator (ASTO)`**:
   - Implements S23 Logic: `HL2 +/- (Adaptive_Multiplier * ATR)`. 
   - The multiplier expands during high-volatility regimes (high Z-Vol) to prevent 'Whipsaws.'

**Logic Flow for Recreation**:
1. Subscribe to `TICK.*` and `QUOTE.*` via ZMQ.
2. Populate rolling deques (`tick_store`) for the last 2000 events.
3. Every 100ms, pack a `Snapshot` (Deques converted to Lists) and push to the `compute_queue`.
4. In the worker process:
   - Calculate ATR and Vol-adjusted multipliers.
   - Compute Greeks for the ATM strike.
   - Run the ASTO state machine.
   - Return a `SignalVector` dictionary.
5. In the main process:
   - Recieve the `SignalVector`.
   - Update Shared Memory (`ShmManager`).
   - Push to Redis for the UI Dashboard.

---
### [EXHAUSTIVE] daemons/meta_router.py (Institutional Risk Gatekeeper)

**Technical Architecture**:
The system's **Arbitration Layer**. It acts as a synchronous validator sitting between the Strategy Engines (Intent) and the Execution Bridges (Order).

**Method-by-Method Technical Breakdown**:
1. **`_evaluate_vetoes()`**:
   - Implements a **Waterfall Boolean Matrix**. 
   - It performs $O(1)$ lookups against the `local_signals` cache (hydrated from SHM) to ensure sub-millisecond validation.
   - **Deterministic Override**: If the Regime Engine has 'Jitter' (fast flipping), the `DeterministicRegimeTracker` posterior probability must be > 0.60 to allow a trend-following trade.
2. **`PortfolioStressTestEngine`**: 
   - Implements a **Taylor Series Approximation** for P&L Shocks: $\Delta P \approx \Delta \cdot dS + \frac{1}{2} \Gamma \cdot dS^2 + \nu \cdot dV + \Theta \cdot dT$.
   - It shocks the `portfolio_greeks` by -20% (Spot) and +50% (VIX) to calculate 'Worst-Case Drawdown.'
3. **`AsyncMarginManager.reserve()`**:
   - Executes a **LUA Script** in Redis to atomically check/decrement margin. 
   - This prevents 'Race Conditions' where two strategies try to spend the same ₹1,00,000 at the exact same millisecond.
4. **`process_single_intent()`**:
   - The entry point for strategy orders. 
   - It 'Shadows' (logs) the intent, applies 'Alpha Decay' (lot reduction for latency), runs the Vetoes, and finally 'Dispatches' the order.

**Logic Flow for Recreation**:
1. Listen for `RAW_INTENT` via ZMQ PULL.
2. Fetch the current 'Institutional Reality' (Margin, Greeks, Regime) from Redis/SHM.
3. Apply the 15-Gate Veto rules.
4. If the intent is LIVE:
   - Calculate the margin requirement (e.g., $Lots \times 150,000 \times (1 + IV)$).
   - Atomically reserve funds via Redis LUA.
   - If reservation fails, veto the trade with `MARGIN_REJECTION`.
5. Enrich the order packet (calculate the exact `tradingsymbol` if it's an option spread).
6. Send the final `ORDER_REQUEST` to the `orders` port.
7. Journal the result (Allowed/Vetoed) to the database.

---
---
### [EXHAUSTIVE] daemons/order_reconciler.py (Distributed State Reconciliation)

**Technical Architecture**:
The **Consistency Agent** for multi-leg atomicity. It uses a **Watchdog Pattern** over Redis `order_status` keys to detect execution drift and trigger compensatory actions.

**Method-by-Method Technical Breakdown**:
1. **`_watchdog_loop()`**: 
   - Polling period: 500ms. 
   - Logic: Scans `self.inflight_baskets` for `now - start_time > 3.0`. 
   - This ensures that transient network hiccups at the broker don't leave the portfolio un-hedged.
2. **`_reconcile_logic()`**:
   - Implements **State Verification**. It retrieves the `order_status` from Redis (populated by the Bridges).
   - If `PARTIAL_FILL` detected: It calculates the `remaining_qty` and calls `force_fill`.
   - If `REJECTED` or `CANCELLED`: It flags the basket for `trigger_rollback`.
3. **`trigger_rollback()`**:
   - **Phase 1: Neutralization**: Identifies legs with `COMPLETE` or `PARTIAL` status and issues immediate `MARKET` offset orders.
   - **Phase 2: Margin Release**: Calls `margin_manager.release()` for the rejected legs to ensure the 'Margin Pool' isn't contaminated by phantom reservations.
4. **`_reboot_audit()`**: 
   - Essential for **Disaster Recovery**. It synchronizes the `pending_orders` Redis hash with actual broker position truth on startup.
   - If an order is $> 300s$ old with no status, it's flagged as a `PHANTOM_ORDER` and triggers a strategy-level kill-switch (`STRATEGY_LOCK`).

**Logic Flow for Recreation**:
1. Subscribe to `Ports.SYSTEM_CMD` for `BASKET_ORIGINATION` events.
2. On event: Store the basket metadata in a local dictionary with a timestamp.
3. In a background task (Watchdog):
   - Every 500ms, identify baskets > 3s old.
   - For each leg in the basket, fetch the current execution status from Redis.
   - If status != 'COMPLETE':
     - Map the drift (Partial/Rejected/Pending).
     - Dispatch `MARKET` orders to close the gap or rollback the risk.
     - Release margin for failed legs.
4. On startup: Iterate through all 'In-Flight' orders from the previous session and reconcile against the broker API.

---
### [EXHAUSTIVE] daemons/shoonya_gateway.py (Real-Time WebSocket Gateway)

**Technical Architecture**:
A **High-Throughput Ingress Controller** designed to map external binary/JSON feeds into internal Shared Memory and ZeroMQ buses. It is optimized for sub-millisecond 'Tick-to-Bus' latency.

**Method-by-Method Technical Breakdown**:
1. **`event_handler_feed_update()`**:
   - Executed in a dedicated Shoonya listener thread.
   - Performs **Latency Benchmarking**: `now() - exchange_ltt`.
   - **Temporal Ordering**: Assigns a monotonic `sequence_id` to prevent out-of-order processing in down-stream strategy engines.
2. **`_broadcast_tick()`**:
   - **Dual-Path Distribution**: 
     - 1. Writes to **Shared Memory** (`shm.py`) for zero-latency compute access.
     - 2. Publishes to **ZeroMQ** (`MARKET_DATA` port) to trigger event-driven strategy logic.
3. **`_dynamic_subscription_listener()`**: 
   - A JIT (Just-In-Time) listener. Allows other daemons (like `LiquidationDaemon`) to request data for specific options on-the-fly via a Redis channel.
4. **`_feed_watchdog_loop()`**: 
   - Implements a **Hardware-Style Watchdog**. If the feed timestamp for NIFTY50 hasn't moved in 2s, it calls `api.close_websocket()`, triggering the library's internal auto-reconnect logic.

**Logic Flow for Recreation**:
1. Perform TOTP-based authentication with the Shoonya REST API.
2. Initialize `TickSharedMemory` and map symbols to fixed memory slots.
3. Open a WebSocket connection and subscribe to core indices (NSE|26000, NSE|26001).
4. For every incoming tick:
   - Extract `lp` (price), `v` (volume), and `oi` (open interest).
   - Write these as a binary struct into the correct Shared Memory slot.
   - Compute the message latency and sequence ID.
   - Publish a JSON-serialized packet to the ZMQ `TICK.{symbol}` topic.
5. Provide a Redis-based listener to add/remove subscriptions dynamically.

---
### [EXHAUSTIVE] daemons/snapshot_manager.py (Structural State Orchestrator)

**Technical Architecture**:
The **Slow-Lane Control Service**. It manages operations that are non-critical for latency but essential for correctness (Margins, Chains, History). It implements a **REST Rate Limiter** to prevent API bans.

**Method-by-Method Technical Breakdown**:
1. **`_boot_hydration()`**: 
   - A blocking **Sequential Dependency Resolver**. 
   - It fetches: 1. Lot Sizes -> 2. Expiries -> 3. 14-Day History -> 4. Account Margins. 
   - This ensures all other daemons have a valid "Source of Truth" in Redis before they start.
2. **`_circuit_breaker_monitor()`**: 
   - Implements the SEBI **Time-Varying Circuit Breaker Logic**. 
   - It monitors the percentage distance of LTP from `prev_day_close`. 
   - On breach, it sets `SYSTEM_HALT` in Redis, which is checked by the `MetaRouter` before every order.
3. **`_option_chain_scanner()`**: 
   - Performs a 60s sweep of the ATM-15 to ATM+15 option chain. 
   - It identifies the `CALL_WALL` and `PUT_WALL` (max OI) for each index.
4. **`_ghost_position_sync()`**: 
   - A **Drift Detection Engine**. 
   - It performs a 3-way join: `Broker API` vs `TimescaleDB Portfolio` vs `Redis Truth`. 
   - If a position exists on the broker but not in our database, it flags a `GHOST_DETECTED` event.

**Logic Flow for Recreation**:
1. On start: Connect to REST API and perform the Hydration sequence.
2. Enter a multi-loop async lifecycle:
   - Loop A (60s): Fetch `get_limits` and `get_positions`. Update `ACCOUNT:MARGIN:AVAILABLE` and `broker_positions` in Redis.
   - Loop B (60s): Query the Option Chain. Calculate PCR and identify OI volatility walls.
   - Loop C (5s): Compare current price to yesterday's close. Trigger Halts if Move > 10%.
   - Loop D (1s): If `OFF_HOUR_SIMULATOR=True`, generate Brownian price updates and publish them to ZMQ to fake market activity.
3. Provide a Semaphore-protected `_call_api` wrapper to enforce 3 REST requests per second limit.

---
---
### [EXHAUSTIVE] daemons/strategy_engine.py (Tactical Logic Engine)

**Technical Architecture**:
The **Signal-to-Intent Translator**. It uses an **Object-Oriented Strategy Registry** that allows for hot-reloading. It performs high-frequency tick evaluation using Shared Memory (SHM) for O(1) attribute lookups.

**Method-by-Method Technical Breakdown**:
1. **`on_tick()` (Base Class Interface)**: 
   - Input: Raw tick/Quote object + `qstate` (SignalVector) + `regime` (RegimeVector).
   - Output: A list of `Intent` dictionaries containing `action`, `strike`, `lifecycle_class`, and `parent_uuid`.
2. **`IronCondorStrategy` (Neutral Yield)**:
   - Uses **Iterative Binary Search** (`find_strike_for_delta`) to identify strikes matching precise Delta targets (e.g., 0.15 Delta wings).
   - **Hybrid Hedge Logic**: If `ASTO > 90` (Extreme Trend), it calculates `net_delta` across all legs and issues a `HEDGE_REQUEST` for Futures to flatten the Delta.
3. **`TastyTrade0DTEStrategy`**: 
   - Implements **Temporal Logic Gates**: Only permits entry between 09:30 and 10:30 on the specific `expiry_day` for that asset.
   - **Edge Filter**: Disqualifies entries unless `IV - RV > 3.0%`.
4. **`config_subscriber()`**: 
   - Uses `asyncio` to poll Redis `hgetall("active_strategies")`. It dynamically instantiates classes from the `strategy_registry` dictionary, enabling the addition of new alpha models without a service reboot.

---
### [EXHAUSTIVE] daemons/system_controller.py (C2 Orchestrator)

**Technical Architecture**:
The **Distributed Lifecycle Manager**. It functions as the 'Brainstem' of the fortress, orchestrating time-based events, external security (GCP), and regulatory accounting.

**Method-by-Method Technical Breakdown**:
1. **`_three_stage_eod_scheduler()`**: 
   - Orchestrates a **Phased Liquidation Sequence**:
     - Stage 1 (15:00): Sets `HALT_KINETIC=True`. Strategy engine uses this to block new trend originations.
     - Stage 2 (15:20): Publishes `FORCE_LIQUIDATE` to the `panic_channel` for all `ZERO_DTE` and `KINETIC` lifecycles.
     - Stage 3 (16:00): Performs EOD database journaling and triggers OS-level shutdown.
2. **`_preemption_poller()`**: 
   - Polls `http://metadata.google.internal/computeMetadata/v1/instance/preempted`. 
   - On `True`, it uses `asyncio.shield()` to protect the `SQUARE_OFF_ALL` sequence from the impending SIGTERM.
3. **`_hard_state_sync()` (Ghost Logic)**: 
   - Performs an **Asynchronous Delta Audit**. 
   - It fetches the `broker_positions` (the external reality) and compares it against the `portfolio` table (the internal reality). 
   - Logic: `If Broker > DB: Action='CLOSE_POSITION' | If Broker < DB: Action='UPDATE_DB'`.
4. **`_periodic_margin_sync()`**: 
   - Synchronizes limits from the `LIVE_CAPITAL_LIMIT` Redis key. 
   - Automatically recalculates the **50:50 Cash/Collateral regulatory split** after subtracting the `HEDGE_RESERVE_PCT`.

---
### [EXHAUSTIVE] daemons/unified_bridge.py (Unified Execution Engine)

**Technical Architecture**:
The **Reality Multiplexer**. It implements a **N-Layer Execution Pipeline** (Shadow -> Paper -> Live) to ensure complete counterfactual parity for quant research.

**Method-by-Method Technical Breakdown**:
1. **`handle_intent_rendering()`**: 
   - The **Transaction Coordinator**. 
   - **Symmetry Protection**: It processes every `Intent` in the same sequence regardless of reality.
   - **Latency Lock**: Before live execution, it verifies that the source tick timestamp is $< 250ms$ old to prevent 'Stale-Quote Execution.'
2. **`calculate_paper_fill()`**: 
   - Implements **Regime-Aware Slippage Modeling**. 
   - Formula: $Price_{exec} = LTP \pm (BaseSlip \cdot RegimeMultiplier)$. 
   - This ensures paper trading results are statistically realistic compared to live execution.
3. **`update_portfolio_universal()`**: 
   - A **Pure Accounting Engine**. 
   - It manages realized P&L using **WAC (Weighted Average Cost)**. 
   - It performs **Atomic Capital Migration**: Moves funds between `CASH_COMPONENT` and `QUARANTINE_PREMIUM` to model T+1 clearing.
4. **`place_order()` (ShoonyaHandler)**: 
   - Implements **Order Nudging**. If a live order is rejected with code 400 (price out of range), it automatically 'Nudges' the price by 0.1-0.3% and retries once.
   - Includes a **TokenBucket Rate Limiter** (8 req/sec) to adhere to vendor API limits.

---
---
### [EXHAUSTIVE] daemons/tick_sensor.py (High-Frequency Ingestor)

**Technical Architecture**:
The **Low-Latency Fast Path**. It utilizes **Shared Memory (POSIX/SystemV)** for inter-process communication (IPC) and is typically pinned to an isolated CPU core (`taskset`) to prevent context-switching overhead.

**Method-by-Method Technical Breakdown**:
1. **`TickSharedMemory.write_tick()`**: 
   - Performs a binary write to a pre-allocated memory-mapped file. 
   - Struct: `[Symbol (32b), Price (64f), Volume (64q), Timestamp (64f), Latency (64f), SequenceID (64q)]`. 
   - This allows consumption by C++/Rust/Numba components with zero serialization overhead.
2. **`_process_raw_tick()`**: 
   - Implements **Temporal Drift Calculation**: `Latency = Now(UTC) - ExchangeTime(converted to UTC)`. 
   - Alerts are triggered if Nifty/BankNifty latency exceeds 2.0s during market hours.
3. **`_subscription_listener()`**: 
   - Listens to the `tick_sensor:subscriptions` Redis channel for **JIT (Just-In-Time)** requests. This allows the system to scale to 100+ symbols dynamically without flooding the WebSocket with unnecessary data.
4. **`_oi_diff_buffers`**: 
   - A `collections.deque` with `maxlen=5`. It calculates a rolling mean of `ΔOI`. If the mean exceeds 500 per tick, it flags the `oi_accel` attribute, indicating 'Liquidity Clustering.'

---
### [EXHAUSTIVE] daemons/fii_dii_fetcher.py (Institutional Macro Scraper)

**Technical Architecture**:
The **Institutional Alpha Sensor**. It is a robust web scraper built with `httpx` and `BeautifulSoup`-style logic to ingest unstructured exchange data.

**Method-by-Method Technical Breakdown**:
1. **`fetch_data()`**: 
   - Implements **Session Priming**: It must first hit the NSE home page to generate valid `Cookies` and `Referrer` headers before the API endpoint will respond with a `200 OK`. 
   - It performs **JSON Normalization** on the raw exchange response.
2. **`fii_bias` Logic**: 
   - Computes a linear normalization of FII Net Value. 
   - $Bias = \text{clip}(\frac{FII_{NetValue}}{2000.0}, -1.0, 1.0)$. 
   - This provides a stationary feature for the MetaRouter models.
3. **`run()`**: 
   - Uses a strict **Market Hour Gate**: `if 9 <= now.hour <= 16`. 
   - Implements a 30-minute `asyncio.sleep()` to ensure the bot doesn't get IP-blocked by the exchange firewall.

---
### [EXHAUSTIVE] daemons/data_gateway.py (Monolithic Legacy Coordinator)

**Technical Architecture**:
The **Monolithic Orchestrator**. This component precedes the microservice split and contains the 'Source of Truth' for many derivative-specific calculations.

**Method-by-Method Technical Breakdown**:
1. **`get_optimal_strike()`**: 
   - Implements an **Iterative Solver** for the Black-Scholes Delta. 
   - It searches the `base_strike ± 500` range to find the strike closest to $0.50$ Delta, which represents the 'ATM Straddle' point.
2. **`_sync_expiries()`**: 
   - Performs a `get_option_chain` API call. 
   - It parses the `exDate` (e.g., "26-MAR-2026") and formats it to the standard "26MAR" string used by the system for symbol construction (`NIFTY26MAR22350CE`).
3. **`_circuit_breaker_monitor()`**: 
   - Implements the **SEBI Rule 11.2** logic. 
   - It monitors the % change of Nifty vs. Day-Open. 
   - On a 10%/15%/20% breach, it updates the `SYSTEM_HALT` Redis key with a `REASON` and `RESUME_TIME` based on the time-of-day matrix.
4. **`_pcr_ingestion_loop()`**: 
   - An **O(N) Summation**. 
   - It iterates through all active option tokens, sums `OI` for `otype=CE` vs `otype=PE`, and publishes the ratio.

---
---
### [EXHAUSTIVE] Strategy Library: Hunters (Engine Internal)

**Technical Architecture**:
Implemented as **Stateful Python Classes** inheriting from `BaseStrategy`. They utilize **Vectorized Lookups** into Shared Memory to evaluate signals in sub-millisecond windows.

**Method-by-Method Technical Breakdown**:
1. **`KineticHunterStrategy.on_tick()`**: 
   - Implements a **Lead-Lag Gate**: It slices the `hw_alphas` array (Shared Memory index 0-4) to verify that at least 3 of the top 5 Nifty heavyweights are moving in the same direction as the index.
   - Formula: $Directionality = \sum (\text{Sign}(Alpha_{HW}) == \text{Sign}(ASTO_{Index})) \ge 3$.
2. **`TastyTrade0DTEStrategy.on_tick()`**: 
   - Implements **IV-RV Smoothing**: It compares the `implied_vol` (from Greeks) vs the `realized_vol` (from history). It only enters if the 'Volatility Risk Premium' (VRP) is $> 3\%$.
3. **`ElasticHunterStrategy.check_exit()`**: 
   - Performs a **Statistical Mean Crossover** check. It monitors the `price_zscore` from the Market Sensor. If the z-score crosses back over $0.0$ (VWAP touch), it triggers an instant `MARKET_CLOSE` order to capture the reversion.

---
### [EXHAUSTIVE] Alpha Engine: TickEngine (Rust)

**Technical Architecture**:
A **High-Concurrency Signal Pipeline** implemented in Rust using `PyO3`. It performs **Branchless Mathematical Extraction** to minimize latency.

**Method-by-Method Technical Breakdown**:
1. **`OfiEngine.calculate_ofi_zscore()`**: 
   - Maintains a `VecDeque` of size 100. 
   - Calculation: $Z = \frac{OFI - \mu}{\sigma}$. 
   - Uses Welford's algorithm or standard rolling sums for $\mu$ (mean) and $\sigma$ (std_dev) to ensure O(1) update complexity.
2. **`VpinEngine.update()`**: 
   - Implements **Volume-Synchronized Probability of Informed Trading**. 
   - Logic: Aggregates volume into a `vpin_bucket_size` (e.g., 5000 shares). 
   - $VPIN = \frac{|\sum V_{buy} - \sum V_{sell}|}{V_{total}}$. 
   - This provides a stationary measure of volume toxicity ranging from 0.0 to 1.0.
3. **`calculate_greeks()` (greeks.rs)**: 
   - A **Vectorized Black-Scholes Solver**. 
   - Implements `delta`, `gamma`, `vega`, `theta`, and second-order greeks `vanna` (dDelta/dSigma) and `charm` (dDelta/dT). 
   - Uses the `statrs` library for high-precision Normal Distribution CDF/PDF lookups.

---
---
### [EXHAUSTIVE] utils/shoonya_master.py (Symbol Mapping Utility)

**Technical Architecture**:
The **Static Metadata Synchronizer**. It bridges the gap between human-readable trading symbols and binary-exchange tokens.

**Method-by-Method Technical Breakdown**:
1. **`parse_and_cache()`**: 
   - Uses `pandas.read_csv` with `dtype=str` to prevent leading-zero truncation on tokens. 
   - It performs **Redis Pipelining** via `r.hset` in chunks of 10,000 to avoid blocking the single-threaded Redis event loop during the massive ingestion of ~100k symbols.
2. **`MASTER_URLS`**: 
   - Hardcoded endpoints for NFO, BFO, NSE, and BSE zip archives provided by the NorenApi vendor.

---
### [EXHAUSTIVE] utils/macro_event_fetcher.py (Economic Pipeline)

**Technical Architecture**:
An **Asynchronous Data Ingestor**. It utilizes `asyncio.gather` for parallel network requests and implements a simple DNS-level cache to minimize latency.

**Method-by-Method Technical Breakdown**:
1. **`fetch_forex_factory()`**: 
   - Implements **User-Agent Masquerading** to bypass basic exchange scrapers blockers. 
   - It parses `datetime.fromisoformat` and converts everything to the local `IST (Asia/Kolkata)` timezone to ensure time-alignment with the SEBI market clock.
2. **`deduplicate()`**: 
   - Uses a **Composite Hash Key**: `(event_name_lowercase[:30] + event_date)`. This covers edge cases where ForexFactory and FMP name the same event slightly differently (e.g., "Non-Farm Payrolls" vs "NFP").

---
### [EXHAUSTIVE] infrastructure/gcp_provision.py (IaC Provisioner)

**Technical Architecture**:
An **Infrastructure-as-Code (IaC) Manager** using the Google Cloud Python SDK. It implements a **Stateless Bootstrap Pattern**.

**Method-by-Method Technical Breakdown**:
1. **`STARTUP_SCRIPT` Execution**: 
   - **Kernel Tuning**: It writes to `/etc/sysctl.conf` to increase `net.core.somaxconn` to 65535 and enables `Transparent HugePages (THP)`. This reduces the CPU overhead of translating virtual memory addresses during high-frequency trading.
   - **Storage Strategy**: It dynamically discovers the SSD device using `/dev/disk/by-id/google-persistent-disk-1` and mounts it with the `discard` option for efficient SSD block management.
2. **`.env` Injection**: 
   - Uses **Base64 Encoding** (`ENV_B64`) to safely pass multi-line secrets through the GCP Metadata service without shell-escaping corruption.
3. **`is_nse_holiday()`**: 
   - Utilizes the `holidays.financial_holidays("XNSE")` subdivision which contains the authoritative NSE trading calendar.

---
---
### [EXHAUSTIVE] Dashboard API: Internal & External Layers

**Technical Architecture**:
A **Distributed Proxy Pattern**. The internal layer is a high-bandwidth `FastAPI` service on the VM; the external layer is a serverless `Cloud Run` instance providing a globally available fallback cache.

**Method-by-Method Technical Breakdown**:
1. **`smart_proxy()` (CloudRun)**: 
   - Implements **Heartbeat-Aware Routing**. 
   - Logic: `If now() - Firestore.last_heartbeat < 90s: Target=VM_IP | Else: Target=Firestore_GCS`. 
   - This ensures the UI is 'Always Up' even during severe server preemptions.
2. **`AuditLogger.log_event()` (Internal API)**: 
   - An **Atomic Redis-List Journal**. 
   - It captures the `timestamp`, `event_type`, and `admin_details`. This is the 'Black Box Recorder' of the system used for forensic analysis after a loss.
3. **`get_state()`**: 
   - A **Multi-Hash Aggregator**. 
   - It performs parallel lookups into `latest_market_state`, `regime_state`, and `power_five_matrix`. 
   - It computes the **Effective Available Margin** using the formula: $Margin_{avail} = (Cash + Collateral) \cdot \text{HedgeReserveMultiplier}$.

---
### [EXHAUSTIVE] Integration Testing Suite

**Technical Architecture**:
A **State-Machine Validation Framework** using `pytest` and `asyncio`.

**Method-by-Method Technical Breakdown**:
1. **`test_order_lifecycle.py`**: 
   - Orchestrates a **Synchronized Triad Test**: 
     - 1. Injects a synthetic tick into ZMQ.
     - 2. Asserts an `ORDER_REQUEST` is published to the `order_ops` channel.
     - 3. Verifies a `portfolio` row is created in the database with `status='OPEN'`.
2. **`test_audit_remediation.py`**: 
   - Implements **Synthetic Drift Injection**. 
   - It manually alters the Redis `AVAILABLE_MARGIN` to be inconsistent with the DB total. 
   - It then triggers the `order_reconciler` and asserts that the `FIX_MARGIN_DRIFT` event is successfully published and the values are re-aligned.
3. **`test_quant_refinements.py`**: 
   - Validates **Numerical Stability**. 
   - It feeds 'Extreme' price data (e.g., 50% gap down) into the `alpha_engine` and asserts that the resulting `Z-score` and `ASTO` values do not result in `NaN` or `Inf` due to zero-division in the variance calculation.

---
---
### [EXHAUSTIVE] C++ Gateway: Architecture & Optimization

**Technical Architecture**:
A **Multi-Threaded Sharded Ingestor** built with C++20. It utilizes **CPU Pinning** and **SIMD-Accelerated Parsing** to minimize jitter and maximize throughput.

**Method-by-Method Technical Breakdown**:
1. **`TickNormalizer::normalize_shoonya_tick()`**: 
   - Utilizes the `simdjson` library for **Branchless JSON Parsing**. 
   - It performs zero-copy field extraction, mapping Shoonya's `tk` (Token), `lp` (Last Price), and `v` (Volume) into a `trading::TickData` POD (Plain Old Data) structure.
2. **`RedisWriter::write_tick()`**: 
   - Implements **Redis Stream Ingestion** via the `redis-plus-plus` client. 
   - It uses the `XADD` command with a `MAXLEN ~ 100` trim policy. This provides a 'Sliding Window' of the last 100 ticks in memory, allowing the Python `market_sensor` to perform O(1) lookbacks for ASTO/RSI calculations.
3. **`signal_handler()` (main.cpp)**: 
   - Implements a **Graceful Degradation Sequence**. Upon `SIGTERM`, it triggers a 25-second flush interval, ensuring that high-speed RAM buffers (for ticks) are committed to the `mnt/persistent_ssd` before the Spot VM is reclaimed by Google Cloud.

---
---
### [ADDENDUM] Sovereign Risk: Technical Logic & Invalidation

**New Method Logic (LiquidationDaemon)**:
1. **Gamma Acceleration Math**: 
   - Formula: $\text{dynamic\_stall} = 300 \cdot \max(0.1, \min(1.0, \frac{\text{minutes\_to\_close}}{90}))$. 
   - This implements a linear decay of the time-gate threshold starting 90 minutes before close.
2. **IV-RV Edge Calculation**: 
   - Utilizes `state.get("iv_rv_spread")` from the high-fidelity SHM vector. 
   - Exit Trigger: $\text{if } IV-RV < 0.01$.
3. **Real-Time Delta Roll (BS-Model)**: 
   - Injects `BlackScholes.delta()` calculation inside the 0DTE loop. 
   - Uses `T_years = dte / 365.0` and `iv_atm` for institutional pricing accuracy. 
   - Logic: $\text{if } |\Delta| > 0.35 \rightarrow \text{Publish Topic.ORDER_INTENT with Action=ROLL}$.
4. **Velocity/Slope Divergence**: 
   - Logic: $\text{if } |\text{slope\_1m}| > (|\text{avg\_slope}| \cdot 2.0) \text{ and } \text{sign}(\text{slope\_1m}) \neq \text{sign}(\text{pnl})$. 
   - This detects 'Anti-Profit Velocity'—when the market is moving too fast against the position to wait for a standard barrier check.

---
