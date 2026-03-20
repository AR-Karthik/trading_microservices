# Codebase Documentation

This document provides detailed information about each file in the project, including its purpose, components (classes, modules, methods, attributes), and the rationale behind its design and implementation.

---

## Root Configuration Files

### [.dockerignore](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/.dockerignore)

- **Purpose**: Specifies files and directories that should be excluded from the Docker build context.
- **Key Patterns**:
  - `.git`, `.gitignore`: Excludes version control metadata.
  - `.env`, `.env.*`: Prevents sensitive environment variables from being baked into images.
  - `__pycache__`, `*.pyc`, `*.pyo`: Excludes Python compiled bytecode.
  - `data/`, `logs/`, `*.log`: Excludes local data and logs.
  - `.vscode/`, `.idea/`, `.agents/`: Excludes editor-specific and agent-specific metadata.
  - `node_modules/`: Excludes JavaScript dependencies (if any).
- **Rationale**: 
  - **Security**: Ensures secrets (like `.env`) are not accidentally shared in Docker images.
  - **Performance**: Reduces the size of the build context sent to the Docker daemon, speeding up builds.
  - **Cleanliness**: Ensures only source code and necessary artifacts are included, leading to leaner images.

### [docker-compose.local.yml](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/docker-compose.local.yml)

- **Purpose**: Defines and orchestrates the local multi-container Docker application for the trading microservices.
- **Services**:
  - `postgres`: TimescaleDB for persistent time-series and relational data.
  - `redis`: In-memory data store for caching and IPC.
  - `data_gateway`: Bridge between external data sources and internal systems.
  - `hmm_engine_nifty/banknifty/sensex`: Hidden Markov Model engines for different market segments.
  - `market_sensor`: Monitors market conditions and provides signals.
  - `meta_router`: Routes messages and data between services.
  - `strategy_engine`: Executes trading strategies based on signals.
  - `liquidation_daemon`: Manages exiting positions.
  - `system_controller`: High-level management of the entire system.
  - `paper_bridge`: Simulates execution in a paper trading environment.
  - `order_reconciler`: Ensures order states are consistent across systems.
  - `live_bridge`: Interface for live trading execution.
  - `telegram_alerter`: Sends notifications via Telegram.
  - `cloud_publisher`: Publishes data to cloud-based services.
  - `dashboard_api`: Backend for the monitoring dashboard.
  - `dashboard_frontend`: Web interface for monitoring.
- **Rationale**: 
  - **Microservices Architecture**: Allows independent scaling and deployment of components.
  - **Consistency**: Provides a reproducible local environment that mirrors production.
  - **Healthchecks**: Ensures services are up and ready before dependent services start.

### [Dockerfile.local](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/Dockerfile.local)

- **Purpose**: Defines the Docker image for the Python-based microservices.
- **Key Instructions**:
  - `FROM python:3.11-slim`: Uses a lightweight Python base image.
  - `RUN apt-get install...`: Installs critical runtime libraries (ZMQ, Protobuf, PostgreSQL, SSL).
  - `WORKDIR /app`: Sets the working directory.
  - `COPY requirements.txt . && pip install...`: Installs Python dependencies.
  - `RUN mkdir -p /ram_disk`: Prepares the RAM disk mount point for fast IPC.
- **Rationale**: 
  - **Uniformity**: All Python services use the same base image, ensuring environment consistency.
  - **Minimalism**: `slim` image reduces attack surface and image size.
  - **Performance**: Pre-installs libraries needed for high-performance networking (ZMQ) and data handling (Protobuf).

### [Makefile](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/Makefile)

- **Purpose**: Provides automation for common development and operational tasks.
- **Key Targets**:
  - `build`: Build Docker images using Docker Compose.
  - `up`, `down`: Start and stop the entire service stack.
  - `ps`, `logs`: Inspect status and view logs.
  - `test`: Run the test suite via `pytest`.
  - `run-local`, `run-live`: Run the system locally (outside or inside Docker).
  - `lint`: Run static analysis (flake8, mypy).
  - `clean`: Remove temporary files and Docker volumes.
- **Rationale**: 
  - **Productivity**: Shortens complex commands into simple, memorable targets.
  - **Standardization**: Ensures all developers use the same commands for building and testing.

### [startup.sh](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/startup.sh)

- **Purpose**: A comprehensive bootstrapping script for provisioning a fresh cloud instance (e.g., GCE) with the trading system.
- **Key Features**:
  - **System Prep**: Updates OS, installs Docker and dependencies.
  - **Deployment**: Clones the repository and sets up `.env` securely.
  - **Kernel Tuning**: Optimizes network and file limits (THP, sysctl) for high-performance trading.
  - **Storage Management**: Automatically discovers and mounts RAM disks (for low-latency IPC) and NVMe/SSD disks (for high-throughput DB/Redis storage).
  - **Service Launch**: Starts all containers and waits for a "booted" signal from the Telegram alerter.
- **Rationale**: 
  - **Infrastructure as Code (partial)**: Automates the manual steps of setting up a trading server.
  - **Performance Optimization**: Applies critical OS-level tweaks required for low-latency systems that standard Docker setups might miss.
  - **Resilience**: Includes validation steps to ensure hardware (disks) and software (services) are correctly initialized.
---

## C++ Gateway Component

### [cpp_gateway/CMakeLists.txt](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/CMakeLists.txt)
- **Purpose**: Configures the build system for the high-performance C++ market data ingestion engine.
- **Key Features**: 
  - Uses C++20 standard for modern features.
  - Links critical performance libraries: `protobuf`, `libzmq`, `hiredis`, `redis++`, and `simdjson`.
- **Rationale**: CMake ensures a cross-platform (or at least consistent Linux) build environment for the performance-critical part of the stack.

### [cpp_gateway/include/redis_writer.h](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/include/redis_writer.h) / [cpp_gateway/src/redis_writer.cpp](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/src/redis_writer.cpp)
- **Purpose**: Class `RedisWriter` manages the persistence of tick data into Redis Streams.
- **Key Methods**: 
  - `writeTick()`: Formats and pushes normalized tick data into a Redis Stream for downstream consumers.
- **Rationale**: Using Redis Streams allows for a reliable, bufferable time-series feed that multiple Python microservices can consume without slowing down the ingestion engine.

### [cpp_gateway/include/shard.h](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/include/shard.h) / [cpp_gateway/src/shard.cpp](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/src/shard.cpp)
- **Purpose**: Implements the `Shard` class, which is a self-contained ingestion thread pinned to a specific CPU core.
- **Key Features**: 
  - **Core Pinning**: Uses `pthread_setaffinity_np` to reduce context switching.
  - **Isolation**: Each shard manages its own ZMQ and Redis connections to ensure no head-of-line blocking.
- **Rationale**: Sharding allows the system to scale horizontally with CPU cores while maintaining ultra-low latency for high-throughput symbol feeds.

### [cpp_gateway/include/tick_normalizer.h](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/include/tick_normalizer.h) / [cpp_gateway/src/tick_normalizer.cpp](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/src/tick_normalizer.cpp)
- **Purpose**: Utility for converting raw JSON ticks from brokers into internal Protobuf messages.
- **Key Features**: 
  - **simdjson**: Uses the SIMD-optimized JSON parser for near-instantaneous extraction of tick fields.
- **Rationale**: Rapid parsing is critical to ensure the P99 latency of the ingestion pipeline remains sub-millisecond.

### [cpp_gateway/include/zmq_publisher.h](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/include/zmq_publisher.h) / [cpp_gateway/src/zmq_publisher.cpp](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/src/zmq_publisher.cpp)
- **Purpose**: Class `ZmqPublisher` handles the broadcasting of ticks over the internal ZeroMQ network.
- **Key Methods**: 
  - `publish()`: Sends serialized Protobuf messages to a `PUB` socket for real-time strategy triggers.
- **Rationale**: ZMQ provides a lightweight, brokerless messaging layer optimized for high-frequency data distribution.

### [cpp_gateway/proto/messages.proto](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/proto/messages.proto)
- **Purpose**: Defines the language-agnostic data schemas for the entire microservices architecture.
- **Key Messages**: 
  - `TickData`: Standardized market tick.
  - `AlphaSignal`: Quantitative signals generated by sensors.
- **Rationale**: Protobuf ensures that C++ ingestion, Rust sensors, and Python strategies can communicate with zero ambiguity and minimal overhead.

### [cpp_gateway/src/main.cpp](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/cpp_gateway/src/main.cpp)
- **Purpose**: Application entry point that initializes and orchestrates multiple ingestion shards.
- **Logic**: 
  - Reads configuration, sets up signal handlers for graceful shutdown, and spawns the `Shard` threads.
- **Rationale**: Decouples shard orchestration from individual shard logic, allowing for dynamic configuration of the ingestion engine.

---

## Daemons Component

### [daemons/__init__.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/__init__.py)
- **Purpose**: Marks the directory as a Python package.

### [daemons/cloud_publisher.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/cloud_publisher.py)
- **Purpose**: A bridge for synchronizing live trading data and system status with Google Cloud Platform (Firestore and GCS). It enables remote monitoring and control while keeping the core trading engine isolated.
- **Class: `CloudPublisher`**:
    - **Attributes**:
        - `redis`: `aioredis` client for fetching real-time metrics from the local Redis cache.
        - `firestore_db`: `google.cloud.firestore.AsyncClient` for pushing live status and reading remote commands.
        - `gcs_client`: `google.cloud.storage.Client` for archiving data at the end of the day.
        - `external_ip`: The public IP of the VM, used for identification and hybrid proxy logic.
        - `_eod_done_today`: A guard flag to ensure EOD snapshots are only run once per day.
    - **Methods**:
        - `__init__(self)`: Sets up Redis connection strings and bucket names from environment variables.
        - `_init_cloud_clients(self)`: 
            - **Logic**: Performs lazy initialization of GCP clients. It includes a critical check to detect if `GOOGLE_APPLICATION_CREDENTIALS` is a directory (a common Docker mounting error) and handles authentication failures gracefully by falling back to local-only mode.
            - **Rationale**: Prevents the entire daemon from hanging if GCP metadata or credentials are unavailable.
        - `_fetch_external_ip(self)`: 
            - **Logic**: Queries the internal GCP metadata server (`http://metadata.google.internal/...`).
            - **Rationale**: Useful for the dashboard to identify which specific VM instance is reporting data.
        - `_heartbeat_loop(self)`: 
            - **Logic**: Every 5 seconds, it aggregates a massive snapshot of the system from Redis, including:
                - **Distributed Indicators**: Composite Alpha, Multi-index regimes (NIFTY, BANKNIFTY, SENSEX).
                - **Market Heatmaps**: Greeks (Log-OFI, VPIN, RSI, PCR, IV) and "Power Five" stock Z-scores.
                - **Portfolio State**: Net Delta exposure per index and margin utilization.
                - **System Health**: VM status and average slippage audit.
            - **Rationale**: Decouples the "noisy" public-facing monitoring traffic from the core ZMQ/SHM data buses.
        - `_sync_config_to_firestore(self)`: 
            - **Logic**: Periodically mirrors critical configuration keys (capital limits, risk per trade, active regime engine) from Redis to Firestore.
            - **Rationale**: Allows the user to view the last known configuration even when the VM is powered down.
        - `_command_watcher(self)`: 
            - **Logic**: Polls Firestore for remote commands every 3 seconds.
            - **Commands**:
                - `PANIC_BUTTON`: Triggers an immediate `SQUARE_OFF_ALL` signal to the `panic_channel`.
                - `PAUSE_TRADING`: Sets a `TRADING_PAUSED` flag in Redis to block new entries.
                - `RESUME_TRADING`: Clears the pause flag.
            - **Rationale**: Provides a reliable remote "kill switch" and lifecycle management for the system.
        - `_eod_snapshot(self)`: 
            - **Logic**: Triggers at 15:35 IST to export daily data.
            - **Rationale**: Automates the transition from live trading to historical archiving.
        - `_upload_tick_parquet(self, snapshot_time)`/`_upload_trades_parquet(...)`:
            - **Logic**: Connects to TimescaleDB, fetches the day's records, converts them to a Pandas DataFrame, exports to a compressed `.parquet` file in `/tmp/`, and uploads to GCS.
            - **Rationale**: Parquet format provides high compression and efficient querying for future backtesting and BigQuery analysis.
        - `_fetch_spread_heatmap(self)`: 
            - **Logic**: Calculates current bid-ask percentage spreads for top symbols.
            - **Rationale**: Real-time visibility into market liquidity and slippage risk.
        - `run(self)`: Entry point that gathers and runs the concurrent heartbeat, command watcher, and EOD snapshot loops.

### [daemons/data_gateway.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/data_gateway.py)
- **Purpose**: The primary ingestion bridge for market data via the Shoonya (Finvasia) API. It handles WebSocket connections, dynamic option subscriptions, and ensures data integrity through watchdogs.
- **Class: `DataGateway`**:
    - **Attributes**:
        - `mq`: `MQManager` for ZeroMQ PUB/SUB orchestration.
        - `redis_client`: `redis.Redis` for shared state, caching, and pub/sub.
        - `pub_socket`: ZMQ PUB socket broadcasting market data on `Ports.MARKET_DATA`.
        - `_prices`/`_oi`: Dicts holding the latest price and Open Interest for tracked symbols.
        - `_last_tick_ts`: Tracking of the last update time per symbol for staleness detection.
        - `api`: `NorenApi` instance for broker interaction.
        - `tick_queue`: `asyncio.Queue` for buffering ticks from the background WebSocket thread.
        - `active_option_tokens`: Map of active Shoonya tokens to option trading symbols.
        - `shm_ticks`: `TickSharedMemory` for ultra-low latency data sharing with the strategy engine.
        - `sim_mode`: Flag indicating if the gateway is in simulation mode (off-hours or debug).
    - **Core Methods**:
        - `__init__(self)`: Initializes ZMQ, Redis, and creates the Tick Shared Memory segment.
        - `_ensure_login(self)`: Authenticates with the broker using credentials and TOTP, with exponential backoff on failure.
        - `start(self)`: Orchestrates the startup of all concurrent tasks (streams, matchdogs, schedulers).
        - `get_optimal_strike(self, spot, ...)`: 
            - **Logic**: Uses the `BlackScholes` core module to calculate Delta for a range of strikes. It returns the strike closest to 0.50 Delta (ATM).
            - **Rationale**: Critical for strategies like Gamma Scalping that require precise ATM option selection.
        - `_mode_controller(self)`: 
            - **Logic**: Automatically switches between LIVE and SIMULATED modes based on IST market hours (09:15 - 15:30) and environment flags.
            - **Rationale**: Allows the system to run 24/7 for testing purposes even when the primary exchange is closed.
        - `_tick_stream(self)`: 
            - **Logic**: Manages the `NorenApi` WebSocket lifecycle. In LIVE mode, it spawns a background thread to feed `tick_queue`. In SIMULATED mode, it generates random prices. It processes raw ticks, calculates OI acceleration, updates Redis (latest + history), and publishes via ZMQ.
            - **Rationale**: Decouples the "noisy" raw broker stream from the cleaned, normalized internal data bus.
        - `_dynamic_subscription_manager(self)`: 
            - **Logic**: Every 5 minutes, it evaluates indices like NIFTY50 and SENSEX. If the spot price has moved, it dynamically subscribes to new ATM CE and PE strikes.
            - **Rationale**: Ensures the system is always watching the most liquid and relevant options as the underlying price fluctuates.
        - `_staleness_watchdog(self)`: 
            - **Logic**: Monitors `_last_tick_ts`. If an index feed stops for >1000ms, it triggers a socket reset and broadcasts a critical alert.
            - **Rationale**: Protects against silent failures where the WebSocket remains "connected" but data stops flowing.
        - `_pcr_ingestion_loop(self)`/`_calculate_pcr(...)`: 
            - **Logic**: Calls `get_option_chain`, sums OI for Puts and Calls for the current expiry, and identifies "Major Walls" (max OI strikes).
            - **Rationale**: Provides institutional-grade sentiment metrics (Put-Call Ratio) for regime detection.
        - `_circuit_breaker_monitor(self)`: 
            - **Logic**: Implements a SEBI-compliant circuit breaker logic. If Nifty or BankNifty move 10/15/20%, it broadcasts a `SYSTEM_HALT` signal and forces a cooldown period.
            - **Rationale**: Essential regulatory compliance and tail-risk protection.
        - `_lot_size_scheduler(self)`/`_fetch_and_store_lot_sizes()`: 
            - **Logic**: At 09:01 IST daily, it fetches the current lot sizes for indices from the broker.
            - **Rationale**: Protects against exchange-driven lot size changes (e.g., Nifty 75 -> 50).

### [daemons/data_logger.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/data_logger.py)
- **Purpose**: Persists real-time market states and engineered signals into a TimescaleDB instance. It acts as the system's "Black Box Recorder," ensuring a high-fidelity historical record is available for backtesting and auditing.
- **Class: `DataLogger`**:
    - **Attributes**:
        - `mq`: `MQManager` for ZMQ orchestration.
        - `batch`: A temporary list used to buffer incoming records to optimize database write performance.
        - `batch_lock`: `asyncio.Lock` used to synchronize access between the ingestion loop and the flusher task.
        - `_redis`: `redis.Redis` client for heartbeats and global state checks.
        - `pool`: `asyncpg.Pool` for managed, asynchronous database connections.
    - **Methods**:
        - `__init__(self)`: Initializes ZMQ and Redis clients.
        - `start(self)`: 
            - **Logic**: Establishes the database pool. Executes idempotent SQL `ALTER TABLE` commands to ensure the `market_history` table has columns for the latest signals (e.g., `asto`, `asto_multiplier`, `exit_path_70_progress`). It then starts the batch flusher and the ZMQ receiver loop.
            - **Rationale**: Centralizes database lifecycle and schema maintenance.
        - `_reconnect_pool(self)`: 
            - **Logic**: Used as a callback for the `@with_db_retry` decorator to recreate the connection pool if the database goes offline.
        - `_batch_writer(self)`: 
            - **Logic**: A background task that runs every 10 seconds. It swaps the active batch, formats the records into a tuple-list, and performs an atomically-batched `executemany` INSERT into `market_history`. It uses `ON CONFLICT DO NOTHING` to prevent primary key errors on restarts.
            - **Rationale**: Buffering writes into 10-second windows drastically reduces disk IOPS and prevents database lock contention during high-frequency bursts.
        - `start() reception loop`: 
            - **Logic**: Continuously listens for `MARKET_STATE` updates via ZMQ. It checks a `LOGGER_STOP` flag in Redis to allow for graceful silencing of the logger during maintenance or session transitions.
            - **Rationale**: Decouples data reception from database IO.
- **Rationale**: By moving database persistence to a dedicated microservice, the system ensures that even if the database experiences latency, the core trading and signaling loops remain unaffected.

### [daemons/fii_dii_fetcher.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/fii_dii_fetcher.py)
- **Purpose**: A periodic scraper that retrieves Institutional (FII/DII) trading activity from the NSE website. This data is used as a macro-sentiment filter for trading strategies.
- **Class: `FIIDIIFetcher`**:
    - **Attributes**:
        - `redis`: Redis client for persisting the fetched institutional data.
        - `url`: The target NSE API endpoint for trade details.
        - `headers`: A dictionary of HTTP headers (User-Agent, Referer) required to successfully traverse NSE's anti-bot protections.
        - `session`: `httpx.AsyncClient` for high-performance, asynchronous HTTP requests.
    - **Methods**:
        - `__init__(self)`: Configures the HTTP session and internal Redis connection.
        - `fetch_data(self)`: 
            - **Logic**: It first performs a "priming" GET request to `nseindia.com` to obtain valid session cookies. After a 1-second delay, it requests the actual API endpoint. On success, it serializes the JSON response into Redis. On failure or exception, it returns defensively structured mock data.
            - **Rationale**: Automates the collection of institutional flow data which is only published once per day but serves as a critical indicator for long-term trend direction.
        - `_get_mock_data(self)`: 
            - **Logic**: Provides hardcoded FII/DII net values.
            - **Rationale**: Prevents downstream regime engines or dashboards from breaking if the NSE website is down or changes its API structure.
        - `run(self)`: 
            - **Logic**: An infinite loop that checks the current hour. If between 09:00 and 16:00 IST, it triggers a fetch. It enforces a 30-minute sleep interval between attempts.
            - **Rationale**: Balances the need for timely data with broad rate-limiting respect for exchange infrastructure.
- **Rationale**: Institutional buying (FII) and selling (DII) are "smart money" indicators. Tracking their daily net value allows the system to align with major market participants.

### [daemons/hmm_engine.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/hmm_engine.py)
- **Purpose**: A deterministic market regime detection engine. It classifies the market into states (RANGING, TRENDING, etc.) to allow strategy parameters (TP/SL, sizing) to adapt dynamically.
- **Class: `HeuristicEngine`**:
    - **Attributes**:
        - `asset_id`: The identifier for the asset (e.g., NIFTY50).
        - `core_pin`: CPU core to pin the process to for performance.
        - `mq`: `MQManager` instance for ZMQ communication.
        - `shm`: `ShmManager` for low-latency shared memory writes.
        - `r`: Redis client for state and parameter storage.
        - `history_14d`: List of the last 14 daily closing prices.
        - `last_pcr`: Put-Call Ratio for sentiment.
        - `adx_val`: Trend strength indicator.
        - `rv_val`: Annualized realized volatility.
        - `iv_val`: Implied Volatility (ATM).
        - `vpin_val`: Order flow toxicity (VPIN).
        - `stale_override`: Boolean flag to signal if data is too old.
        - `last_regime_ts`: Timestamp of the last regime classification.
        - `tick_buffer`: Deque of recent ticks for intraday calculations.
        - `api`: Shoonya API for fallback data fetching.
    - **Methods**:
        - `__init__(self, asset_id, core_pin)`: Initializes connections, state, and API fallback.
        - `_pin_core(self)`: Pins the process to a specific CPU core to minimize context switching latency.
        - `_fetch_parameters(self)`:
            - **Logic**: Fetches 14-day history, PCR, IV, and VPIN from Redis. Includes a fallback to `_load_lookback_shoonya` if history is missing. Calculates RV and ADX approx from the history.
            - **Rationale**: Uses Redis as a central parameter store for cross-daemon synchronization.
        - `_load_lookback_shoonya(self)`:
            - **Logic**: Logs into Shoonya API using environment credentials and fetches the last 20 days of daily candles.
            - **Rationale**: Critical for system robustness on first boot or if Redis persistence fails. Ensures the engine has enough data to compute 14-day indicators immediately.
        - `_calculate_realized_vol(self, closes)`:
            - **Logic**: Computes log returns of closing prices, then `std(returns) * sqrt(252)`.
            - **Rationale**: Standard method to annualize volatility from daily data.
        - `_calculate_adx_approximation(self, closes)`:
            - **Logic**: Approximates trend strength using absolute momentum `|ups - downs| / (ups + downs)`.
            - **Rationale**: Full ADX requires High/Low/Close candles; this proxy is used when only closing prices are available in the daily history.
        - `classify_regime(self)`:
            - **Logic**: A priority-based decision matrix:
                1. **CRASH/TOXIC**: RV > 50% OR VPIN > 0.8.
                2. **OVERBOUGHT/OVERSOLD**: PCR > 1.5 or PCR < 0.6.
                3. **TRENDING**: ADX > 25.
                4. **HIGH_VOL_CHOP**: ADX < 20 AND RV > 25%.
                5. **RANGING**: Default state or if (IV - RV) > 3.0pp and ADX < 20.
            - **Rationale**: Deterministic mapping ensures predictable behavior. High vol/toxicity blocks entries. PCR extremes signal reversal risk. ADX distinguishes trend from range.
        - `run(self)`:
            - **Logic**: Main loop. Pins core, fetches params every 300 ticks, and classifies regime every 5 seconds. Updates Shared Memory (`SignalVector`) and Redis (`hmm_regime_state`) for consumption by other daemons.

### [daemons/liquidation_daemon.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/liquidation_daemon.py)
- **Purpose**: The sovereign risk manager responsible for monitoring open positions and executing exits based on a Triple-Barrier system (Take Profit, Stop Loss, and Time Decay). It acts as an independent "safety process" ensure positions are closed even if the strategy engine fails.
- **Class: `LiquidationDaemon`**:
    - **Attributes**:
        - `order_pub`: ZMQ PUSH socket for dispatching liquidation orders to the execution bridges.
        - `orphaned_positions`: An in-memory dict of active positions being monitored.
        - `_redis`: Redis client for state tracking, alerts, and inter-process communication.
        - `pool`: `asyncpg.Pool` for maintaining state consistency with the TimescaleDB portfolio.
        - `shm_alpha_managers`: Multiple `ShmManager` instances for low-latency reading of multi-index alpha signals.
    - **Core Methods**:
        - `run(self)`: 
            - **Logic**: Performs startup hydration from the DB and spawns concurrent tasks for handoff listening, market monitoring, slippage audits, and heartbeats.
        - `_position_alert_listener(self)`: 
            - **Logic**: Instantly arms barriers for new positions by listening to the `NEW_POSITION_ALERTS` pub/sub channel.
            - **Rationale**: Eliminates the monitoring gap between order fill and daemon hydration.
        - `_monitor_fill_slippage(self)`: 
            - **Logic**: Monitors execution quality. If slippage breaches an RV-adaptive budget, it sets a global `SLIPPAGE_HALT` to block new entries.
            - **Rationale**: Implements a "quality-of-service" circuit breaker for trade execution.
        - `_check_stop_day_loss(self)`: 
            - **Logic**: Compares `Realized + Unrealized P&L` against a 2% capital limit. If breached, it sets a global block and triggers `SQUARE_OFF_ALL`.
            - **Rationale**: The ultimate account-level safety valve for catastrophic drawdowns.
        - `_check_hybrid_drawdown(self)`: 
            - **Logic**: Aggregates P&L across all legs of a hedged basket (`parent_uuid`). Liquidates the entire basket if the combined loss breaches a dynamic, lot-scaled limit.
            - **Rationale**: Protects multi-leg structures (Iron Condors, Hedged Gamma) from losing more than their theoretical risk.
        - `_evaluate_kinetic_barriers/positional/zero_dte`: 
            - **Logic**: Lifecycle-specific risk evaluation.
                - **Kinetic**: ATR-based SL/TP, "Signal Flip" (CVD reversal) exits, and regime-aware "Optimal Stopping" (Theta stall).
                - **Positional**: Structural breach checks (underlying vs short strikes), "Twitchy Mode" (tight 0.5x ATR trail during extreme trends), and 60% decay profit taking.
                - **Zero-DTE**: Aggressive 0.5% underlying move stop and 50% credit profit targets.
            - **Rationale**: Tailors risk management logic to the specific time-horizon and Greeks profile of each trade.
        - `_execute_hedge_waterfall(self, pos, delta, rv)`: 
            - **Logic**: A 3-step defensive protocol for delta-neutral positions: 1) Neutralize with Micro-Futures, 2) Roll untested wings if margin is low, 3) Pro-rata 25% size reduction as a last resort.
            - **Rationale**: Algorithmic defense for complex option desks.
        - `_attempt_exit(self, ...)`: 
            - **Logic**: Fires MARKET orders with "Sequenced Liquidation" (closing Short legs before Long legs in a basket).
            - **Rationale**: Optimizes margin release and reduces the risk of being held long on naked wings during liquidation.
        - `_fire_order_with_retries(self, ...)`: 
            - **Logic**: Intelligent handling of broker HTTP 400 errors. It recalibrates price bounds for "Circuit Limit" errors and raises critical alerts for "Abort" (margin) errors.
            - **Rationale**: Hardens the execution layer against common exchange/broker API failures.
- **Rationale**: By decoupling liquidation from signal generation, the system ensures that risk management is always active, lower-latency (via SHM), and resilient to component crashes.

### [daemons/live_bridge.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/live_bridge.py)
- **Purpose**: The authenticated execution gateway for live trading on the Shoonya (Finvasia) platform. It manages the lifecycle of real orders, rate limiting, and transaction accounting.
- **Class: `LiveExecutionEngine`**:
    - **Attributes**:
        - `api`: `NorenApi` client for the Shoonya broker.
        - `rate_limiter`: `TokenBucketRateLimiter` configured to 8 requests/sec to stay within broker API limits.
        - `total_realized_pnl`: Atomic tracker for the day's total realized P&L.
        - `pool`: `asyncpg.Pool` for persistent trade and portfolio logging.
        - `multileg_executor`: Helper for executing complex structures (like multi-leg Iron Condors) as atomic units.
    - **Core Methods**:
        - `authenticate(self)`: 
            - **Logic**: Performs secure SHA256-hashed login. On success, it recovers the last known P&L from Redis to maintain daily continuity across restarts.
        - `update_portfolio(self, ...)`: 
            - **Logic**: Uses a SQL `SELECT ... FOR UPDATE` lock to atomically update position size and average price in the database. It calculates realized P&L incrementally as positions are closed.
            - **Rationale**: Ensures the database remains a "Single Source of Truth" that matches the broker's ledger exactly.
        - `execute_live_order(self, order)`: 
            - **Logic**: Maps symbols to correct exchanges (NFO/BFO). Dispatches the order via a thread-safe executor. If the broker returns an HTTP 400 or generic rejection, it attempts a secondary "Limit Nudge" (0.1% price offset) to overcome exchange price-band rejections.
            - **Rationale**: Hardens the execution against common API "flicker" and brittle exchange rules.
        - `handle_order(self, order, ...)`: 
            - **Logic**: A multi-stage safety pipeline:
                1. **Double-Tap Guard**: Uses a 10s Redis lock per symbol to prevent double-execution from duplicate signals.
                2. **Stale Feed Guard**: Aborts if the market data timestamp is > 1s old.
                3. **Pre-flight Margin Check**: Uses an atomic LUA script to reserve capital before the order is fired.
                4. **Pending Journaling**: Persists the intent to Redis before network dispatch (Crash Consistency).
                5. **Post-Fill Handoff**: Signals the `LiquidationDaemon` to arm its barriers.
                6. **Rollback Monitor**: If an order remains partially filled for >3s, it triggers an automatic basket rollback.
            - **Rationale**: Implements "Defensive Execution"—minimizing the probability of errant, duplicate, or un-hedged trades.
        - `panic_listener(self)`: 
            - **Logic**: Listens for a global `SQUARE_OFF` signal. It implements a SEBI-batching protocol (10 orders per 1.1s) to liquidate all live positions while avoiding broker rate-limit bans.
            - **Rationale**: Ensures a smooth, non-blocked exit during market emergencies.
        - `_handle_basket_rollback(self, parent_uuid)`: 
            - **Logic**: Rapidly market-closes every leg associated with a specific parent ID.
            - **Rationale**: Prevents being stuck with naked option wings if a multi-leg hedge fails to execute.
- **Rationale**: The Bridge is the most critical "Last Mile" component. Its architecture prioritizes crash consistency (via journaling), regulatory compliance (via batching), and insolvency protection (via pre-flight margin locks.

### [daemons/market_sensor.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/market_sensor.py)
- **Purpose**: The high-throughput analytical engine of the system. it transforms raw market ticks into quantitative alpha signals (Greeks, GEX, VPIN, OFI) at sub-millisecond speeds.
- **Key Architectures**:
    - **Multiprocessing Pipeline**: Utilizes a dedicated `mp.Process` (`_compute_worker`) to offload heavy mathematical modeling (Vectorized Greeks, Pearson correlation matrices). This ensures the primary ingestion loop never stalls due to the Python GIL.
    - **Shared Memory (SHM) Broadcast**: Writes computed `SignalVector` objects directly to Shared Memory segments, allowing downstream strategy engines to consume alpha signals with zero-copy latency.
- **Class: `MarketSensor`**:
    - **Attributes**:
        - `tick_store`: A rolling buffer of the last 2,000 ticks per instrument.
        - `vpin_series`, `ofi_series`, `cvd_series`: Real-time microstructure buffers used to detect flow toxicity and absorption.
        - `shm_managers`: Mapping of hardware-accelerated memory segments for indexed assets (NIFTY/BANKNIFTY).
        - `rust_engine`: (Optional) Integration with a native Rust extension for ultra-low latency trade classification.
    - **Core Methods**:
        - `_classify_trade(self, tick)`: 
            - **Logic**: Implements the **Lee-Ready algorithm**. It classifies volume based on price proximity to the Bid-Ask midpoint (Aggressive Buy vs. Aggressive Sell).
            - **Rationale**: Filters out "noise" from bid-ask bounces, providing a true measure of directional aggression.
        - `_update_vpin(self, tick)`: 
            - **Logic**: Implements **Volume-bucketing** (Standard: 5,000 units). It calculates the information asymmetry (toxicity) once a volume bucket is filled.
            - **Rationale**: High VPIN acts as a lead indicator for "Toxic Flow," triggering trade vetos to avoid being adverse-selected.
        - `_publish_market_state(self, ...)`: 
            - **Logic**: 
                1. **Signal Fusion**: Aggregates FII Bias, VIX slopes, and basis dislocation into Environmental/Structural scores.
                2. **Hysteresis Regime Map**: Tracks `EXTREME_TREND` vs `TRENDING` using the **ASTO** (Adaptive SuperTrend) indicator.
                3. **VIX Spike Guard**: Monitors 5-minute VIX expansion; if volatility expands >15% in 5m, it sets a global `VIX_SPIKE_DETECTED` veto.
                4. **Whale Pivot**: Computes alignment between price/VWAP and 15-minute slopes to detect institutional "big money" direction.
            - **Rationale**: Provides a canonical, multi-dimensional view of market health that is synchronized across the entire system via Redis and ZMQ.
- **Sub-Process: `_compute_worker`**:
    - **Logic**: An isolated loop that runs standard quantitative libraries (`scipy`, `numpy`) to calculate Black-Scholes Greeks, GEX (Gamma Exposure), and Index Dispersion (Pearson Correlation of the Top 10 heavyweights).
    - **Rationale**: Isolates computationally expensive "Floating Point" math from the latency-sensitive I/O loops.
- **Rationale**: The Sensor is the "Alpha Factory." Its design prioritizes parallelization and memory-mapped IPC to handle the massive data volume of the Indian derivative markets without dropping packets.

### [daemons/meta_router.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/meta_router.py)
- **Purpose**: The decision orchestrator and risk controller of the system. It validates alpha signals, calculates optimal trade sizing, and enforces a multi-layered battery of safety vetoes before any order reaches the exchange.
- **Class: `BaseStrategyLogic`**:
    - **Logic: "Hybrid Sizing" (Conviction × Volatility)**:
        - **Step 1: ATR Unitization**: Calculates position size such that the rupee loss at a 1.0x ATR stop-loss is exactly equal to the configured `MAX_RISK_PER_TRADE` (e.g., ₹2,500). This ensures a constant risk profile regardless of market volatility.
        - **Step 2: Half-Kelly Scaling**: Scales the ATR-unit total by a **Half-Kelly** fraction based on the win-probability provided by the regime engines.
    - **Rationale**: Decouples the "Conviction" (p) from the "Volatility" (ATR), preventing over-leveraging during high-volatility regimes even if conviction is high.
- **Class: `MetaRouter`**:
    - **Attributes**:
        - `brains`: Multi-asset logic handlers for NIFTY, BANKNIFTY, and the Top 10 heavyweights.
        - `heat_limit`: Global cap on aggregate portfolio exposure (Fractional Kelly).
        - `vpin_toxicity`: Boundary for blocking entries during high informed-trading toxicity.
    - **Core Methods**:
        - `broadcast_decisions(self, ...)`: 
            - **Logic: The "Veto Battery"**: Each trade intent must survive 8+ independent guards:
                1. **Regime-Strategy Lock**: Ensures `IronCondor` is only active in `RANGING` markets.
                2. **VPIN Veto**: Blocks `POSITIONAL` entries if toxicity > 0.82.
                3. **Low Vol Trap**: Blocks entries if IV < 12% (unfavorable premium).
                4. **ASTO Hunter/Shield**: Enforces trend-alignment and prevents entries during extreme over-extensions.
                5. **Regulatory Guard**: Enforces the 50:50 Cash-to-Collateral margin rule via atomic LUA checks.
                6. **VIX Spike Veto**: Freezes entries if VIX expands >15% in 5 minutes.
                7. **Premium Quarantine**: Scales down sizing if intraday credits aren't yet settled by the clearing house.
            - **Logic: Atomic Margin Reservation**: Calls a Redis LUA script to reserve capital from `CASH_COMPONENT_LIVE` *before* dispatching.
            - **Rationale**: Implements "Fail-Fast" risk management at the gateway level.
        - `_calculate_portfolio_greeks(self)`: 
            - **Logic**: Periodically (10s) scans the live database, calculates Black-Scholes Greeks for every position, and exports net Delta/Gamma to Redis.
            - **Rationale**: Provides real-time visibility into the system's "Hedge Status."
        - `_build_basket_intent(self, ...)`: 
            - **Logic**: Dynamically resolves optimal strikes for 4-leg Iron Condors (e.g., Short at 0.15 Delta, Long at 0.05 Delta).
            - **Rationale**: Automates the construction of complex, hedged option structures based on institutional Greek targets.
        - `_hedge_request_listener(self)`: 
            - **Logic**: Listens for emergency hedge requests from the `StrategyEngine` and dispatches Micro-Futures orders using a 15% dedicated "Hedge Margin" reserve.
- **Rationale**: The MetaRouter acts as the "Chief Risk Officer." Its architecture is designed to prevent rogue trades, over-leverage, and non-compliant margin usage while ensuring that every trade is mathematically justified by the current market regime.

### [daemons/order_reconciler.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/order_reconciler.py)
- **Purpose**: The "Janitor" of the execution layer. It monitors the health of multi-leg "basket" orders and resolves discrepancies (partial fills, broker rejections, system crashes) to ensure the system never holds unhedged tail risk.
- **Class: `OrderReconciler`**:
    - **Attributes**:
        - `inflight_baskets`: Tracking ledger for active multi-leg intents (e.g., Iron Condors).
        - `api`: Direct `NorenApi` instance used for "Last Resort" status polling if the primary Redis status feed is stale or missing.
        - `_memory_pending_orders`: In-memory fallback ledger for when Redis persistence fails (Spec 11.2).
    - **Core Methods**:
        - `_watchdog_loop(self)`: 
            - **Logic**: Scans the `inflight_baskets` every 500ms. If a basket remains "Open" for more than 3 seconds without a completion signal, it triggers a mandatory reconciliation.
            - **Rationale**: Enforces a strict time-to-fill limit for complex option spreads.
        - `reconcile_basket(self, p_uuid)`: 
            - **Logic**: 
                1. Queries Redis `order_status:{id}`.
                2. If status is missing, it polls the broker directly via `get_order_history`.
                3. **Partial Fill Resolution**: If some legs are incomplete, it identifies the remaining quantity and calls `force_fill`.
                4. **Rejection Resolution**: If any leg is rejected by the exchange, it calls `trigger_rollback`.
            - **Rationale**: Prevents "Broken Condors"—the highest source of portfolio insolvency where one "wing" is missing, leading to naked tail exposure.
        - `force_fill(self, ...)`: 
            - **Logic**: Dispatches urgent MARKET orders to fulfill the remaining contracts of a partially filled leg.
            - **Rationale**: Prioritizes hedge integrity over price optimization during execution slippage.
        - `trigger_rollback(self, ...)`: 
            - **Logic**: 
                1. Implements a **3-Strike Retry** protocol.
                2. On failure, it fires a global `BASKET_ROLLBACK` and proactively liquidates any legs that *did* fill.
                3. On "Strike 3", it moves the basket to the **`CRITICAL_INTERVENTION`** dead-letter queue and sends a critical push alert.
            - **Rationale**: Ensures the system "Fails-Safe" by closing whatever legs it can and escalating to the operator for manual rescue.
        - `_reboot_audit(self)`: 
            - **Logic: Triple-Sync (Broker ↔ DB ↔ Redis)**: On startup, it scans for orphaned intents. If an order is > 5 minutes old with no status, it marks it as a **"PHANTOM ORDER"** for immediate operator audit.
            - **Rationale**: Guarantees ledger consistency after system reboots or ungraceful outages.
- **Rationale**: By decoupling reconciliation from the primary execution loop, the system ensures that "Leg-In Risk" is managed by a dedicated, resilient actor with its own broker connection and state-tracking logic.

### [daemons/paper_bridge.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/paper_bridge.py)
- **Purpose**: The simulated execution engine and shadow ledger manager. It provides a high-fidelity environment for paper trading by replicating broker matching logic, slippage, and transaction costs in a local TimescaleDB instance.
- **Class: `PaperBridge`**:
    - **Attributes**:
        - `pool`: `asyncpg.Pool` used to manage the `trades` and `portfolio` tables.
        - `order_timestamps`: A rolling deque used to enforce realistic API rate limits in simulation.
        - `total_realized_pnl`: Asset-specific daily P&L tracker synchronized with Redis.
    - **Core Methods**:
        - `update_portfolio(self, ...)`: 
            - **Logic**: Performs an atomic `SELECT ... FOR UPDATE` on the shadow portfolio ledger. It calculates realized P&L incrementally based on its own internal accounting rules and updates average price/quantity.
            - **Rationale**: Replicates the exact accounting behavior of a real broker, allowing the system to track "Shadow P&L" with institutional accuracy.
        - `execute_orders(self, ...)`: 
            - **Logic**: 
                1. **Strict Filtering**: Only processes intents explicitly marked for the `Paper` environment.
                2. **Rate Limiting**: Enforces an 8 orders/sec "Simulated Bucket" to match the Shoonya API's real-world constraints.
                3. **Slippage Modeling**: Adds a random penalty of 2 to 5 ticks (0.10 to 0.25) to the execution price.
                4. **Persistence**: Writes a record back to the `trades` table, embedding the current market regime in `audit_tags` for later performance attribution.
                5. **Handoff**: Publishes to the `NEW_POSITION_ALERTS` Redis channel to signal the `LiquidationDaemon` to begin monitoring the simulated position.
            - **Rationale**: Prevents "Sim-Trading Bias" by forcing the strategy to deal with the same latency and friction it would face in live markets.
        - `panic_listener(self)`: 
            - **Logic**: Listens for the global `SQUARE_OFF` signal and rapidly liquidates all entries in the paper portfolio using simulated market orders.
            - **Rationale**: Allows for full end-to-end testing of the "Emergency Square Off" pipeline during off-market simulations.
        - `_handle_basket_rollback(self, ...)`: 
            - **Logic**: Simulated implementation of the circuit-breaker rollback, closing all associated legs of a multi-leg basket.
- **Rationale**: The Paper Bridge is the primary validation tool. Its architecture ensures that backtests and dry runs are realistic by modeling the "Physical" constraints of the exchange (slippage, rate limits) rather than assuming instant, perfect fills.

### [daemons/shoonya_gateway.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/shoonya_gateway.py)
- **Purpose**: The primary live market data ingestor for the Shoonya (Finvasia) broker. It creates a high-performance bridge between the broker's WebSocket feeds and the system's internal data transports (Shared Memory, ZeroMQ, and Redis).
- **Class: `ShoonyaDataStreamer`**:
    - **Attributes**:
        - `api`: `NorenApi` client for the Shoonya platform.
        - `shm`: `TickSharedMemory` manager for zero-copy IPC.
        - `active_tokens`: Local cache mapping broker-specific tokens to human-readable trading symbols.
        - `symbol_slots`: Ledger of pre-allocated memory slots for each tracked instrument.
    - **Core Methods**:
        - `event_handler_feed_update(self, tick_data)`: 
            - **Logic**: A high-priority callback triggered by the Shoonya WebSocket thread. It extracts Last Traded Price (LTP) and Volume, then hands off the data to the main asyncio event loop via `run_coroutine_threadsafe`.
            - **Rationale**: Isolates the synchronous WebSocket thread from the asynchronous trading logic to prevent ingest blocking.
        - `_broadcast_tick(self, ...)`: 
            - **Logic: The Triple-Transport Broadcast**:
                1. **Shared Memory (SHM)**: Writes the tick directly to a memory-mapped slot at sub-microsecond speeds.
                2. **Redis Time-Travel Buffer**: Maintains a 100-tick rolling stack in Redis for each symbol.
                3. **ZeroMQ (ZMQ)**: Publishes a notification to the `TICK.{symbol}` topic, acting as an event trigger for strategy engines.
            - **Rationale**: SHM provides the speed needed for HFT alpha, while Redis provides the persistence needed for engine recovery after a crash.
        - `_dynamic_subscription_listener(self)`: 
            - **Logic**: Continuously monitors the Redis `dynamic_subscriptions` channel. When a strategy or liquidation engine needs a new strike (e.g., during a roll), it resolves the symbol to a token using `search_scrip` and JIT-subscribes to the live feed.
            - **Rationale**: Avoids the "Token Limit" of the broker by only subscribing to instruments currently in use or being evaluated.
        - `start(self)`: 
            - **Logic**: Orchestrates TOTP-enabled login, starts the heartbeat, and activates the persistent WebSocket connection.
- **Rationale**: The Gateway is designed for "Zero-Loss Ingestion." By utilizing raw Shared Memory as the primary data store, it allows multiple downstream sensors and strategies to consume the same tick simultaneously with zero serialization overhead.

### [daemons/strategy_engine.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/strategy_engine.py)
- **Purpose**: The primary execution host for the system's trading logic. It consumes market data and analytical signals to generate order intents, while enforcing institutional-grade risk vetoes and capital constraints.
- **Class Breakdown**:
    - **`BaseStrategy`**: The fundamental interface for all strategies, providing lifecycle hooks like `on_tick` and schedule-based activation (`is_active_now`).
    - **`IronCondorStrategy`**: 
        - **Logic: [Hedge Hybrid]**: Implements a unique "Hedge or Trap" logic. During extreme trends (|ASTO| > 90), it checks institutional alignment with the "Whale Pivot." If aligned, it dispatches an emergency Micro-Futures hedge; if misaligned, it triggers a "Trap Alert" for the Liquidation Daemon.
        - **Rationale**: Mitigates the "Gamma risk" of short options by converting them into a delta-neutral hybrid structure during explosive moves.
    - **`TastyTrade0DTEStrategy`**: 
        - **Logic**: Specializes in 0-DTE theta harvesting using Iron Butterflies, restricted to the 09:30–10:30 "Volatility Crush" window.
    - **`GammaScalpingStrategy`**: 
        - **Logic**: Performs high-frequency delta hedging based on continuous Black-Scholes calculations.
- **Core Engine Features**:
    - **Asynchronous Alpha Ingest**: Uses `ShmManager` to read both market ticks and analytical alpha (VPIN, ASTO, Greeks) from Shared Memory with zero-copy overhead.
    - **Bytecode Warmup**: Includes a `warmup_engine` method that injects synthetic ticks on boot to trigger the Python JIT compiler, ensuring the engine is "hot" before the 09:15 open.
    - **Dynamic Strategy Registry**: Supports hot-swapping strategies (loading/unloading) via Redis `active_strategies` without restarting the process.
    - **Pre-Flight Risk Battery**: 
        1. **HALT_KINETIC**: Automatically blocks new entries after 15:00 IST to prevent overnight drift.
        2. **Slippage Halt**: A global circuit breaker that freezes entries if the `OrderReconciler` detects excessive market slippage.
        3. **Low Vol Trap**: Blocks `POSITIONAL` entries if Implied Volatility (IV) < 12%, ensuring entries are only made when premiums are favorable.
- **Rationale**: The Strategy Engine is designed for "Modularity without Latency." By hosting diverse strategies in a single, high-performance loop with zero-copy data access, it allows the system to run complex portfolios like "Gamma Scalping" alongside "Iron Condors" with consistent execution timing.

### [daemons/system_controller.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/system_controller.py)
- **Purpose**: The central administrative "Captain" of the system. It orchestrates the trading lifecycle, enforces regulatory margin constraints, and serves as the emergency circuit breaker for the entire microservices fleet.
- **Class: `SystemController`**:
    - **Core Methods & Watchdogs**:
        - `start(self)`: 
            - **Logic: Regulatory Capital Split**: Automatically partitions the global budget into a 50:50 Cash-to-Collateral mix before any daemon starts. 
            - **Rationale**: Enforces mandatory SEBI "Cash Component" rules, preventing the system from over-leveraging on collateralized margins.
        - `_three_stage_eod_scheduler(self)`: 
            - **Logic: The EOD Sequence**:
                1. **15:00 IST**: Triggers `HALT_KINETIC`, blocking all new trade originations.
                2. **15:20 IST**: Triggers `SQUARE_OFF_INTRADAY` for `KINETIC` and `ZERO_DTE` lifecycles.
                3. **16:00 IST**: Initiates hard VM shutdown after pushing the EOD Summary Report.
            - **Rationale**: Automates the complex transition from live trading to data archival, ensuring no intraday positions are inadvertently carried overnight.
        - `_execute_selective_square_off(self, ...)`: 
            - **Logic: SEBI-Compliant Batching**: When liquidating en-masse, it strictly limits execution to 10 orders per second with a 1.01s mandatory wait between batches.
            - **Rationale**: Protects the broker's API and the exchange's risk management systems from "Order Flooding" which could lead to terminal blocking.
        - `_hard_state_sync(self)`: 
            - **Logic: Ghost Fill Sync**: Every 15 minutes, it cross-references the Broker's live position report against the local TimescaleDB. If an "Orphan" position exists on the broker but not in the DB, it triggers an immediate liquidation.
            - **Rationale**: Eliminates "Shadow Risk" where a trade might be filled at the exchange but missed by the local middleware due to a network blip.
        - `_preemption_poller(self)`: 
            - **Logic**: Polls the GCP Metadata service to detect 2-minute preemption notices on Spot VMs.
            - **Rationale**: Triggers an emergency "Panic Square Off" if the server is about to be terminated by Google Cloud, protecting the portfolio from becoming unmanaged.
        - `_exchange_health_monitor(self)`: 
            - **Logic**: Sets `SYSTEM_HALT` if the NIFTY50 tick latency exceeds 500ms or 3 consecutive heartbeats are missed.
- **Rationale**: The Controller provides "Operational Air-Gap." By centralizing lifecycle events (Boot, Mid-Day Macro Lockdowns, and EOD Shutdown), it ensures that all 14+ daemons act as a single, cohesive, and compliant unit.

### [daemons/tiny_recorder.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/daemons/tiny_recorder.py)
- **Purpose**: A specialized telemetry engine designed to capture high-fidelity market data on resource-constrained "Micro" cloud instances. it provides the raw "Market DNA" needed for offline signal reconstruction and strategy backtesting.
- **Class: `TinyRecorder`**:
    - **Attributes**:
        - `symbols`: A curated list of 16+ high-priority tokens, including global indices (NIFTY, BANKNIFTY, SENSEX) and heavyweight stocks (RELIANCE, HDFCBANK) that drive index movement.
        - `binary_file_path`: Path to the daily `.bin.lzma` archive.
    - **Core Methods**:
        - `compress_tick(self, tick_data)`: 
            - **Logic: Binary Packing**: Condenses a standard dictionary-based tick into a 17-byte "Extreme Packet" using the `struct` module. 
            - **Byte Map**: `>B d I f i` (1B Exchange ID, 8B Float Timestamp, 4B Unsigned Int Token, 4B Float Price, 4B Signed Int Volume).
            - **Rationale**: Reduces disk write frequency and volume by over 90% compared to JSON, allowing the recorder to run on low-IOPS storage (like GCP Standard HDD) without dropping packets.
        - `on_message(self, message)`: 
            - **Logic**: Directly streams the packed binary payload into a persistent `lzma` (XZ) compressed file handle. 
        - `connect_and_stream(self)`: 
            - **Logic**: Manages the persistent WebSocket lifecycle and implements a "Time-Aware" EOD sequence. At 15:45 IST, it automatically closes the telemetry stream and initiates a cloud handoff.
        - `push_to_gcs(self)`: 
            - **Logic**: Orchestrates the transfer of the compressed binary log to the central Google Cloud Storage (GCS) data lake using `gsutil`.
- **Rationale**: Tiny Recorder enables "Zero-Cost Observation." By offloading telemetry recording to free-tier cloud instances and utilizing extreme compression, the system can maintain a massive historical tick database for years without significant infrastructure costs.
---

## Core Utilities Component

### [core/alerts.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/alerts.py)
- **Purpose**: A centralized, asynchronous alerting gateway designed to offload notification delivery from the primary trading loops.
- **Classes**:
  - `CloudAlerts`: A singleton manager for Redis-based alert queueing.
    - **Attributes**:
      - `_instance`: Static reference to the singleton instance.
      - `_redis`: Persistent asynchronous Redis connection.
      - `redis_url`: Connection string retrieved from `core.auth`.
    - **Methods**:
      - `get_instance()`: Class method providing thread-safe access to the singleton.
      - `__init__()`: Private constructor initializing the Redis URL.
      - `_ensure_redis()`: Internal coroutine for lazy-loading the Redis connection.
      - `alert(text, alert_type, **kwargs)`: Primary entry point. Asynchronously pushes a JSON-encoded payload to the `telegram_alerts` Redis list.
- **Global Functions**:
  - `send_cloud_alert(text, alert_type, **kwargs)`: Convenience wrapper for `CloudAlerts.get_instance().alert()`.
- **Rationale**: Direct integration with alerting APIs (like Telegram) in a trading loop is non-deterministic due to network latency. This implementation pushes alerts to a local Redis buffer, allowing a separate daemon (like `TelegramAlerter`) to handle the "slow" delivery.

### [core/auth.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/auth.py)
- **Purpose**: Provides a unified interface for credential management and connection string generation.
- **Global Functions**:
  - `get_redis_url()`: Resolves `REDIS_HOST` and `REDIS_PASSWORD` from `.env` to produce a `redis://` or `redis://:password@` URL.
  - `get_db_dsn()`: Constructs a PostgreSQL DSN (`postgres://...`) for `asyncpg`.
  - `get_db_config()`: Returns a dictionary of database credentials (`user`, `password`, `host`, `database`, `port`) for standard DB pool initialization.
- **Rationale**: Centralizing authentication logic prevents "credential drift" where different services might use different environment variables or formats for the same resource.

### [core/db_retry.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/db_retry.py)
- **Purpose**: Implements industrial-grade resilience patterns for TimescaleDB interactions.
- **Global Functions/Decorators**:
  - `with_db_retry(max_retries, backoff)`: Asynchronous decorator that catches `asyncpg` and `OSError` exceptions. If the caller has a `_reconnect_pool` method, it attempts to heal the connection before retrying.
  - `robust_db_connect(dsn, max_retries, timeout)`: A specialized connection factory that loops with increasing wait times until the database is reachable.
- **Rationale**: In a containerized microservices environment, the database might start slower than the daemons. This module ensures that temporary connectivity gaps do not crash the trading engine.

### [core/execution_wrapper.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/execution_wrapper.py)
- **Purpose**: A sophisticated execution orchestrator that manages atomic multi-leg orders and adaptive price chasing.
- **Classes**:
  - `MultiLegExecutor`: The primary interface for complex order lifecycle management.
    - **Attributes**:
      - `engine`: Reference to the underlying execution engine (Live or Paper).
    - **Methods**:
      - `_get_best_price(symbol, side)`: Internal helper to resolve the current mark price.
      - `adaptive_limit_chase(order, max_slippage_pct)`: Implements a 5-step iterative limit-chase algorithm. It gradually adjusts the limit price toward the market while strictly enforcing a slippage cap (default 0.10%). If the cap is reached, it triggers a `SLIPPAGE_HALT` veto.
      - `execute_legs(orders, sequential)`: Orchestrates the placement of multiple orders. If `sequential=True`, it sorts orders (BUY before SELL) to ensure margin eligibility. It also pings the `OrderReconciler` to track basket origination and publishes JIT dynamic subscriptions to the market data feed.
- **Rationale**: Standard order placement is insufficient for options trading where "leg-in" risk is high. This wrapper ensures that margin-releasing legs (Longs) are confirmed by the broker before margin-intensive legs (Shorts) are fired.

### [core/greeks.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/greeks.py)
- **Purpose**: Provides a vectorized, institutional-grade Black-Scholes implementation for option sensitivity analysis.
- **Classes**:
  - `BlackScholes`: Static utility class for Greek calculations.
    - **Static Methods**:
      - `d1(S, K, T, r, sigma)`: Calculates the standard d1 probability component with safety guards for zero-volatility.
      - `d2(S, K, T, r, sigma)`: Calculates the d2 component.
      - `call_price(S, K, T, r, sigma)` / `put_price(...)`: Returns the theoretical fair value.
      - `delta(S, K, T, r, sigma, option_type)`: First-order sensitivity to underlying price.
      - `gamma(S, K, T, r, sigma)`: Second-order sensitivity (rate of change of Delta).
      - `vega(S, K, T, r, sigma)`: Sensitivity to implied volatility (normalized per 1% change).
      - `theta(S, K, T, r, sigma, option_type)`: Sensitivity to time decay (normalized per day).
- **Rationale**: Accurate Greeks are the foundation of delta-neutral strategies. This module provides a Python-side reference that mirrors the high-performance Rust implementation used in the live path.

### [core/health.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/health.py)
- **Purpose**: Defines the system's "Self-Awareness" layer, tracking daemon heartbeats and computing aggregate health scores.
- **Classes**:
  - `Daemons`: Static name registry for all 15+ microservices.
  - `HeartbeatProvider`: A mixin for daemons to report status.
    - **Methods**:
      - `run_heartbeat(interval)`: Async loop that updates a daemon-specific Redis hash (`daemon_heartbeats`) and an individual TTL key.
      - `stop_heartbeat()`: Signals the loop to terminate.
  - `HealthAggregator`: Used by the `SystemController` to determine if the system is "safe to trade."
    - **Attributes**:
      - `required_daemons`: List of critical services that must be alive for a 1.0 health score.
    - **Methods**:
      - `get_system_health()`: Reads the heartbeat hash, compares timestamps against a 15s timeout, and returns a dict containing an aggregate score (0.0 to 1.0) and individual daemon statuses.
- **Rationale**: In a distributed system, "Silent Failures" (dead daemons) are catastrophic. This module provides the telemetry needed for the automated "System Halted" kill-switch.

### [core/logger.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/logger.py)
- **Purpose**: Implements a unified structured logging framework to facilitate easier debugging and observability across the microservices fleet.
- **Classes**:
  - `JsonFormatter`: Extends the standard `logging.Formatter` to produce JSON payloads.
    - **Methods**:
      - `format(record)`: Overrides the default format to extract timestamp, level, logger name, message, module, and line number into a JSON object. It also handles custom `correlation_id` fields for cross-service request tracing.
- **Global Functions**:
  - `setup_logger(name, log_file, level)`: Factory function that configures a logger with a JSON `StreamHandler` for console output and an optional `RotatingFileHandler` for persistent storage (capped at 10MB per file, keeping 5 backups).
- **Rationale**: Standard text logs are difficult to parse at scale. JSON logs allow for direct ingestion into analytical tools, while the `correlation_id` support is critical for debugging complex order flows that pass through multiple daemons.

### [core/margin.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/margin.py)
- **Purpose**: A safety-critical engine that manages capital allocation using atomic Redis LUA scripts. It ensures that the system never "double-spends" capital across multiple concurrent strategy instances.
- **LUA Scripts (Atomicity Layer)**:
    - **`LUA_RESERVE`**: 
        - **Logic**: Implements the **Regulatory 50:50 Cash-to-Collateral Rule**. It calculates the minimum cash required (50% of trade value). If sufficient cash and total margin exist, it consumes collateral first (capped at 50%) and then the remainder from the cash pool.
        - **Rationale**: Prevents violating exchange margin rules where excess collateral cannot be used to cover the 50% "Cash Component" requirement.
    - **`LUA_RELEASE`**: 
        - **Logic**: Proportionally returns margin to both pools upon trade closure, ensuring the 50:50 buffer is restored.
- **Class: `MarginManager`**:
    - **Methods**:
        - `reserve(self, required_margin, execution_type)`: 
            - **Logic**: Executes the `LUA_RESERVE` script atomically. Returns `True` only if the entire block of capital is successfully locked.
            - **Rationale**: Acts as the system's "Financial Gatekeeper," blocking trade entry at the last millisecond if capital is insufficient.
        - `release(self, amount, execution_type)`: Returns capital to the pool.
        - `get_available(self, execution_type)`: Queries the current real-time liquidity of the sub-pools.
- **Rationale**: Using Redis LUA scripts transforms capital management into an "Atomic Operation." This is vital because ZMQ strategies run in parallel; without an atomic lock, two strategies could each see ₹10,000 available and both try to place a ₹7,500 trade simultaneously, leading to a margin call.

### [core/messages.proto](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/messages.proto)
- **Purpose**: Defines the universal wire format for inter-service communication.
- **Protobuf Messages**:
  - `TickData`: The fundamental unit of market data, containing symbol, price, volume, open interest (OI), and bid/ask snapshots.
  - `OrderIntent`: Encapsulates a trading decision, including order ID, action (BUY/SELL), quantity, order type (LMT/MKT), price, and the originating strategy ID.
- **Rationale**: Protobuf provides significantly higher serialization performance and smaller payload sizes compared to JSON, which is vital for maintaining low latency in the system's primary data path.

### [core/mq.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/mq.py)
- **Purpose**: A high-level messaging abstraction layer over ZeroMQ (ZMQ). it provides standardized, asynchronous communication patterns (Pub/Sub, Push/Pull, Dealer/Router) with implicit request tracing.
- **Class: `MQManager`**:
    - **Logic: The Traceable Multipart Pattern**:
        - `send_json(self, socket, topic, data)`: 
            - **Logic**: Encapsulates every message into a 3-part ZMQ frame: `[Topic, Header(metadata), Payload(JSON)]`. It automatically generates or propagates a `correlation_id` in the header.
            - **Rationale**: Enables "End-to-End Tracing." A signal generated in `MarketSensor` carries the same ID through the `StrategyEngine` and `Bridge`, allowing a developer to debug exactly why a specific fill occurred.
        - `recv_json(self, socket)`: 
            - **Logic**: Receives the 3-part frame and injects the `correlation_id` into a Python `contextvars`.
            - **Rationale**: Ensures that any log message emitted by the daemon automatically includes the ID of the triggering event without manual plumbing.
    - **Logic: Resilient Socket Factory**:
        - All sockets created (via `create_publisher`, `create_subscriber`, etc.) are automatically configured with a 1000ms `LINGER` and a 5000ms `RCVTIMEO`.
        - **Rationale**: Prevents "ZMQ Zombies" (processes that hang on exit because of undelivered messages) and "Dead Feeds" (daemons that block forever if a publisher crashes).
- **Core Enums**:
    - **`Ports`**: Standardized TCP port registry (5555–5563) defining the logical system topology.
    - **`Topics`**: Canonical string identifiers (TICK, STATE, CMD, INTENT, FILL) used for subscriber-side filtering.
- **Rationale**: ZMQ is raw and powerful but error-prone. This manager enforces a "System-Wide Protocol" that guarantees every message is identifiable, every socket is non-blocking, and every data type (including NumPy) is serializable.

### [core/network_utils.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/network_utils.py)
- **Purpose**: Provides resilience patterns to isolate and recover from failures in external network dependencies.
- **Global Functions**:
  - `exponential_backoff(max_retries, base_delay, max_delay)`: A decorator that wraps async functions with a retry loop that doubles its wait time after each failure, capping at 30 seconds.
- **Classes**:
  - `CircuitBreaker`: Implements the "Circuit Breaker" pattern to prevent cascading failures.
    - **Attributes**:
      - `state`: `CLOSED` (passing requests), `OPEN` (blocking requests), or `HALF_OPEN` (testing recovery).
      - `failure_count`: Counter for consecutive exceptions.
    - **Methods**:
      - `call(func, *args, **kwargs)`: Proxies a function call. If in `OPEN` state and the `recovery_timeout` has passed, it transitions to `HALF_OPEN`. If a call fails in `CLOSED` or `HALF_OPEN`, it increments the failure count and potentially trips the circuit.
- **Rationale**: When using external APIs (CloudRun, Telegram, Shoonya), transient network blips can cause strategy stalls. This module ensures the system either retries intelligently or "fails fast" to protect the core trading loop.

### [core/shared_memory.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/shared_memory.py)
- **Purpose**: A ultra-low-latency, zero-copy transport layer for market ticks. It allows the data ingestor to share live market snapshots with multiple strategy engines simultaneously without serialization overhead.
- **Class: `TickSharedMemory`**:
    - **Logic: Fixed-Width Struct Packing**: 
        - Every tick is packed into a 56-byte binary structure (`32s d q d`).
        - **Format**: 32-byte Symbol, 8-byte Price, 8-byte Volume, 8-byte Timestamp.
        - **Rationale**: Fixed-width records allow for $O(1)$ memory access via simple offset calculation (`slot_index * 56`), eliminating the need for complex lookups during high-frequency bursts.
    - **Logic: Zero-Copy Propagation**: 
        - The `mmap` buffer is shared across process boundaries. `write_tick` performs a direct memory copy, and `read_tick` performs a direct memory read.
- **Rationale**: Standard IPC (like ZMQ or Redis) requires the data to be serialized (usually to JSON) and copied multiple times across the kernel boundary. This module provides a "Direct Memory Access" (DMA) pattern that reduces tick-to-signal latency from milliseconds to microseconds.

### [core/shm.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/core/shm.py)
- **Purpose**: A specialized memory-mapped data structure for transmitting the "Alpha Signal Vector"—a collection of 25+ real-time quantitative features (VPIN, Greeks, HMM State) produced by the `MarketSensor`.
- **Class: `ShmManager`**:
    - **Logic: Integrity-First Protocol**:
        - **Checksum Validation**: Every `write` operation calculates a primitive CRC32-like checksum of all 25 features. The `read` operation recalculates this sum and rejects the payload if it doesn't match.
        - **Staleness Guard**: Every vector includes a high-resolution timestamp. The reader automatically discards any signal older than 1.0 second.
        - **Rationale**: Prevents "Phantom Signals." In a distributed system, acting on a partially written or stale alpha vector could lead to incorrect sizing or false entries. This protocol guarantees that the engine only acts on "Fresh and Intact" intelligence.
- **Rationale**: The Alpha Vector is the most complex data object in the system. While `shared_memory.py` handles raw market data, `shm.py` handles the "Processed Intelligence," providing a high-speed, safe pathway for the system's brain to communicate with its limbs.

---

## Technical Utils Component

### [utils/fii_data_extractor.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/fii_data_extractor.py)
- **Purpose**: A specialized scraping engine that extracts institutional Participant-Wise Open Interest (OI) from NSE to compute a macro sentiment bias.
- **Classes**:
  - `FIIDataExtractor`: Manages the lifecycle of FII data ingestion and scoring.
    - **Attributes**:
      - `redis_url`: Connection string for the central data store.
      - `_redis`: Persistent asynchronous Redis client.
    - **Methods**:
      - `start()`: The primary execution loop. It schedules the fetch for 18:30 IST (post-market) daily.
      - `_wait_for_fetch_time()`: Internal timer that calculates the sleep duration until the next 18:30 IST window.
      - `_run_extraction()`: Orchestrates the fetch-compute-store pipeline.
      - `_fetch_nse_data()`: Implements a session-aware scraping routine using `httpx`. It first hits the NSE homepage to obtain valid cookies before requesting the `equity-derivative-position-limit` API.
      - `_compute_bias(raw_data)`: Processes the raw JSON. It extracts `futurLong`, `optCallOI`, `futurShort`, and `optPutOI` for "FII" client types. It calculates a Bullish/Bearish bias score normalized to a [-15, +15] scale based on `BIAS_MAX_NET_OI` (500k contracts).
      - `_store_redis(bias, raw_data)`: Persists the `fii_bias` score and a `fii_summary` JSON object (including a text signal like "BULLISH") to Redis.
- **Global Functions**:
  - `run_once()`: CLI entry point for manually triggering a fetch (e.g., for debugging).
- **Rationale**: Retail traders often trade against institutional flows. By quantifying FII "Net Long" positions, the system can dynamically adjust its conviction levels, avoiding "counter-trend" setups during heavy institutional accumulation.

### [utils/hmm_cold_start.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/hmm_cold_start.py)
- **Purpose**: Bootstraps the Gaussian Mixture Hidden Markov Model (GMM-HMM) using public historical datasets, ensuring the system is "ready to trade" on day one without live tick history.
- **Classes**:
  - `HMMColdStarter`: Orchestrates the download, preprocessing, and training of the initial model.
    - **Methods**:
      - `download_kaggle_data()`: Uses `kagglehub` to retrieve minute-level historical Nifty 50 CSVs.
      - `download_yfinance_data()`: Fetches the most recent 7 days of 1-minute candles from Yahoo Finance (`^NSEI`) to ensure the model captures current volatility.
      - `preprocess_generic(df, source_name)`: A robust normalization routine that handles varied datetime/column formats from different providers.
      - `engineering_features(df)`: Calculates Log-Returns, Rolling RV (30-min), and Price Velocity (5-min). It maps these to the 4-feature vector expected by the live engine, padding with neutral values for microstructure features (VPIN/OFI) not available in historical OHLC.
      - `train_and_save(X)`: Trains a 3-state GaussianHMM using the EM algorithm and exports it as `hmm_generic.pkl`.
      - `upload_to_gcs(...)`: Automatically promotes the baked model to Google Cloud Storage for distribution to the trading fleet.
- **Rationale**: Regime-based trading requires a pre-trained "World Model." This script solves the "Cold Start" problem by synthesising a baseline model from heterogenous data sources.

### [utils/hmm_trainer.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/hmm_trainer.py)
- **Purpose**: A continuous learning worker that re-calibrates the HMM regimes using the system's own high-fidelity tick history stored in TimescaleDB.
- **Classes**:
  - `HMMTrainer`: Managed re-training and promotion logic.
    - **Methods**:
      - `fetch_data()`: Queries the `market_history` table for the last 30 days of high-resolution features (VPIN, Log-OFI, Basis, Vol-Ratio).
      - `preprocess(df)`: Segments features and calculates 5-minute forward returns as the optimization target.
      - `train(X)`: Fits a new 3-state HMM to the recent data.
      - `validate(model, X)`: Computes the Log-Likelihood of the new model.
      - `run_lifecycle()`: Implements "Champion-Challenger" logic. It compares the Log-Likelihood of the "new" model against the "current" model on the same dataset. It only promotes (saves to disk and GCS) the new model if it demonstrates a statistically significant improvement in state-stability.
- **Rationale**: Market regimes are non-stationary. A model trained on 2023 data may be invalid in 2025. This trainer ensures the system's "Regime Map" evolves alongside actual market microstructure.

### [utils/macro_event_fetcher.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/macro_event_fetcher.py)
- **Purpose**: A multi-source aggregator that identifies high-impact economic events to trigger automated system lockdowns.
- **Global Functions**:
  - `fetch_forex_factory()`: Scrapes the weekly JSON calendar from ForexFactory, filtering for "High" impact events in USD/INR/EUR/GBP.
  - `fetch_fmp_economic_calendar(days_ahead)`: Queries the Financial Modeling Prep API for localized US/India events.
  - `deduplicate(events)`: Merges feeds and removes overlaps (common for major US data) using a name-date composite key.
  - `fetch_and_write(...)`: The main entry point. Fetches from both sources using `asyncio.gather`, sorts chronologically, and writes the result to `data/macro_calendar.json` and a Redis "macro_events" key.
- **Rationale**: Macro events (like RBI rate decisions or US CPI) cause non-random volatility clusters. By pre-fetching these, the `SystemController` can preemptively halt trading 5 minutes before the event to avoid "spread-widening" and "slippage" traps.

### [utils/post_game_reporter.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/post_game_reporter.py)
- **Purpose**: Generates a consolidated end-of-day (EOD) performance summary for the trading session.
- **Global Functions**:
  - `generate_daily_report()`: Connects to TimescaleDB and executes an aggregate query on the `trades` table to calculate Total Realized P&L, Trade Count, and Win Rate per execution type. It then formats a Markdown report and dispatches it via the Telegram Bot API.
- **Rationale**: Essential for "Passive Monitoring." The operator receives a push notification after market close, providing immediate closure on the day's performance without needing to log into the terminal or dashboard.

### [utils/shoonya_master.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/shoonya_master.py)
- **Purpose**: Synchronizes the broker's (Shoonya/Finvasia) derivative instrument master list with the local Redis cache.
- **Global Functions**:
  - `download_and_extract()`: Downloads the `NFO_symbols.txt.zip` from the Shoonya API and extracts the raw CSV.
  - `parse_and_cache()`: Reads the master file into a Pandas DataFrame and performs a chunked upload to two Redis hashes (`shoonya_nfo_tokens` and `shoonya_nfo_symbols`).
  - `get_token(symbol)` / `get_symbol(token)`: Helper functions for O(1) lookups against the Redis cache.
- **Rationale**: Broker tokens change frequently. Without this synchronization, the `DataGateway` and `ExecutionEngine` would fail to resolve instrument IDs, leading to immediate system failure.

### [utils/telegram_bot.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/utils/telegram_bot.py)
- **Purpose**: The background daemon (`TelegramAlerter`) responsible for the final delivery of all system notifications.
- **Classes**:
  - `TelegramAlerter`: The core dispatcher.
    - **Attributes**:
      - `_enabled`: Boolean flag based on the presence of `TELEGRAM_BOT_TOKEN`.
      - `_redis`: Async Redis client for queue monitoring.
    - **Methods**:
      - `start()`: Drains the `telegram_alerts` Redis list using `BRPOP` (blocking pop).
      - `_dispatch(alert)`: Formats the raw alert JSON into a beautiful HTML message, attaches live account liquidity context, and dispatches via `httpx`.
      - `_send_boot_alert()`: Routine for system startup notifications.
- **Rationale**: Centralizing message delivery in a dedicated daemon ensures that even if the `Telegram API` is slow, it never blocks the primary trading or risk management flows.

---

## Dashboard and Monitoring Component

### [dashboard/api/main.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/dashboard/api/main.py)
- **Purpose**: The primary operational backend (FastAPI) that serves as the "Command Center" for the human operator.
- **Key Modules & Classes**:
  - `AuditLogger`: A static utility that logs system-level actions (config changes, panics) into a rolling Redis list (`audit_trail`), ensuring a tamper-evident record of operator intervention.
  - `SystemState`, `RegimeConfigRequest`, `TelemetryMetrics`: Pydantic models that enforce strict schema validation for all API interactions.
- **Critical Endpoints**:
  - `GET /state`: Aggregates the real-time market state from Redis for NIFTY50, BANKNIFTY, and SENSEX. It provides a "Deep Signal" vector (OFI, Hurst, Vanna, Charm) and identifies the current HMM regime.
  - `POST /regime/config`: Allows hot-swapping the regime engine (HMM vs. Deterministic) and adjusting risk thresholds. It publishes a command to Redis to trigger immediate engine updates across the fleet.
  - `GET /attribution/strategy`: Queries the TimescaleDB to provide a multi-strategy P&L breakdown and efficiency metrics (Win Rate, Profit Factor).
  - `POST /panic`: Triggers a high-priority "Square Off All" signal to the `ExecutionEngine`, bypassing normal order flow logic.
  - `GET /health/telemetry`: Monitors the pulse of all 14+ daemons, reporting heartbeats, sub-millisecond network latency, and OS-level resource usage (CPU/Memory/Disk).
- **Rationale**: Trading at high intensity requires a low-latency, centralized view of the entire system. This API bridges the gap between raw Redis/DB data and the visual GUI, providing safety-critical manual overrides.

### [dashboard/cloudrun/main.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/dashboard/cloudrun/main.py)
- **Purpose**: A resilient API gateway and edge proxy that ensures the dashboard remains operational even if the primary Trading VM is offline.
- **Smart Routing Logic**:
  - `get_vm_ip()`: Resolves the live VM's IP address from Firestore. It treats the VM as "offline" if the heartbeat age exceeds 90 seconds.
  - `smart_proxy()`: Attempts to proxy REST requests directly to the VM with a strict 2-3 second timeout.
- **Offline Fallback Mechanisms**:
  - **State Fallback**: If the VM is unreachable, it pulls the "last known good" state from Firestore (synced by the `CloudSync` daemon).
  - **Portfolio Fallback**: Reads historical trade data directly from GCS Parquet files using `gcsfs` and `pyarrow`.
  - **Command Queueing**: If an operator sends a `panic` or `halt` command while the VM is offline, the gateway "queues" the command in Firestore for the VM to pick up immediately upon reconnection.
- **Security**: Implements an `ACCESS_KEY` middleware that checks for a secret token in headers or cookies before allowing any traffic.
- **Rationale**: The Trading VM lives in a private network (Tailscale). This Cloud Run service provides a public, authenticated, and highly available entry point that maintains system visibility during VM maintenance or outages.

---

## Rust Performance Extensions

### [rust_extensions/tick_engine/src/lib.rs](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/rust_extensions/tick_engine/src/lib.rs)
- **Purpose**: The core high-performance bridge (using `PyO3`) that exposes native Rust market-microstructure logic to the Python trading daemons.
- **Classes**:
  - `TickData`: A lightweight data container for market snapshots (Price, Bid, Ask, Volume).
  - `TickEngine`: The primary stateful processor for a single instrument.
    - **Methods**:
      - `new(vpin_bucket_size)`: Initialises the VPIN and OFI engines with specific periodicity.
      - `classify_trade(tick)`: Determines trade aggression (Buy/Sell/Neutral) based on price proximity to the Bid-Ask midpoint.
      - `update_vpin(tick)`: Incrementally updates Volume-Probability of Informed Trading (VPIN).
      - `update_ofi_zscore(tick)`: Calculates the Order Flow Imbalance (OFI) and normalises it to a rolling Z-Score.
      - `update_cvd(tick)`: Maintains the Cumulative Volume Delta (CVD) for the session.
      - `get_greeks(...)`: A wrapper that proxies Greeks calculation to the native `greeks.rs` module and returns a Python dictionary.
- **Rationale**: Python's Global Interpreter Lock (GIL) is a bottleneck during high-frequency volatility bursts. By offloading trade classification and signal math to Rust, the system can process thousands of ticks per second with predictable sub-microsecond latency.

### [rust_extensions/tick_engine/src/modules/greeks.rs](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/rust_extensions/tick_engine/src/modules/greeks.rs)
- **Purpose**: A vectorized Black-Scholes implementation for real-time option sensitivity analysis.
- **Features**:
  - `calculate_greeks(s, k, t, r, sigma, is_call)`: Computes essential first-order Greeks (Delta, Gamma, Vega, Theta) and critical second-order Greeks:
    - **Vanna** ($d\Delta / d\sigma$): Sensitivity of Delta to changes in volatility.
    - **Charm** ($d\Delta / dt$): Sensitivity of Delta to the passage of time (Delta bleed).
- **Rationale**: Standard Python libraries (like `scipy`) are too slow for per-tick Greeks updates across hundreds of option strikes. This Rust implementation uses the `statrs` library for high-speed normal distribution functions, enabling the system to maintain a real-time "Delta-Neutral" hedge status.

---

## Administrative Scripts

### [scripts/backup_db.sh](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/scripts/backup_db.sh)
- **Purpose**: A safety-critical maintenance utility that ensures disaster recovery and point-in-time recovery (PITR) for the trading database.
- **Logic**:
  - **Full Dump**: Executes `pg_dump` within the Docker container and compresses the output.
  - **WAL Shipping**: Implements a perpetual background loop that monitors the `/mnt/hot_nvme/wal_buffer` directory (where PostgreSQL archives its transaction logs). Every 60 seconds, it ships new segments to GCS and clears the buffer.
  - **Resource Isolation**: Every `gcloud storage` command is wrapped in `ionice -c 3`. This sets the I/O priority to "Idle," ensuring that heavy file uploads never cause disk-latency spikes that could slow down the `MarketScanner` or `ExecutionEngine`.
- **Rationale**: High-frequency trading generates massive amounts of data. This script provides an automated, low-impact way to ship that data to the cloud, allowing for full state reconstruction in the event of local NVMe failure.

### [scripts/seed_firestore.py](file:///c:/Users/karth/.gemini/antigravity/scratch/trading_microservices/scripts/seed_firestore.py)
- **Purpose**: A one-time setup utility to bootstrap the cloud-side of the hybrid infrastructure.
- **Global Functions**:
  - `seed()`: Initializes the two primary collections used by the Cloud Run Dashboard:
    - `system/metadata`: Seeds default fields for heartbeat tracking, live alpha scores, and VM IP.
    - `system/config`: Sets initial "conservative" risk limits (e.g., 55% Hurst threshold, 15k INR max risk) to prevent the system from taking excessive risk upon first boot.
- **Rationale**: The Cloud Dashboard relies on Firestore for "Offline Playback." By seeding these documents with known defaults, the developer ensures the web interface displays meaningful initial states rather than "Undefined" errors during the first deployment.
