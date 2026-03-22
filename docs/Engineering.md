# The Engineering Blueprint
## Module 0 - Introduction

### 1. High-Performance Polyglot Architecture
To solve the "Python GIL Bottleneck" while maintaining logical flexibility, the system utilizes a tiered language stack:
* **Data Ingestion (C++):** An ultra-fast gateway utilizing `simdjson` and `uWebSockets` to ingest and parse JSON tick data in the low microsecond range.
* **Math Engine (Rust):** A dedicated Rust extension handles heavy microstructure math (VPIN, Greeks, OFI) using thread-safe, zero-cost abstractions.
* **Orchestration & Logic (Python):** FastAPI and asynchronous Python daemons manage high-level routing, strategy state-machines, and execution bridges.

### 2. Low-Latency IPC & State Management
The system achieves sub-millisecond internal communication through a hybrid IPC strategy:
* **ZeroMQ Binary Bus:** Utilizing Protobuf serialization over ZMQ `PUB/SUB` and `PUSH/PULL` topologies for inter-daemon messaging with a measured latency of ~40µs.
* **Zero-Copy Shared Memory (SHM):** High-frequency "Signal Vectors" (Signals, Greeks, Regimes) are written to SHM segments, allowing any daemon to "read" the entire market state in **<1µs** without network overhead.
* **Redis State Storage:** Manages global flags (`SYSTEM_HALT`), daily P&L persistence, and dynamic configuration synchronization.

### 3. Execution & Reliability Framework
* **Unified Execution Bridge:** A multiplexer that processes orders across three "Realities": **Shadow** (Counterfactual ledger), **Paper** (Regime-aware simulation), and **Live Strike** (Direct Shoonya API integration).
* **Multi-Leg Atomic Reconciler:** A specialized "Janitor" daemon that monitors the health of complex spreads, detecting partial fills or rejections and executing automated **Circuit Breaker Rollbacks** to prevent unhedged exposure.
* **Infrastructure:** Fully containerized via Docker, utilizing **TimescaleDB** for high-velocity time-series persistence and GCP-integrated alerting functions.

# Module 1 - (Data Ingestion & Gateway)

### 1. High-Performance Multi-Gateway Architecture
To bypass the Python Global Interpreter Lock (GIL) for data processing while maintaining retail API compatibility, the ingestion layer is split into a high-speed C++ core and an asynchronous Python bridge.

#### A. The C++ "Firehose" Gateway (`cpp_gateway/`)
* **Networking**: Utilizes `uWebSockets` to manage persistent, high-throughput SSL connections to the exchange's tick server.
* **JSON Parsing**: Employs `simdjson` (Single Instruction, Multiple Data) to parse incoming JSON packets. This allows for parsing speeds in the gigabytes-per-second range, ensuring the gateway is never the bottleneck.
* **Normalization Engine**: A `TickNormalizer` class maps heterogeneous exchange fields (e.g., `lp`, `v`, `ap`) into a standardized internal `Tick` object.
* **Serialization**: Converts normalized ticks into **Google Protocol Buffers (Protobuf)** binary format before broadcasting.

#### B. The Shoonya Python Bridge (`daemons/shoonya_gateway.py`)
* **Session Management**: Uses the `NorenRestApi` for secure handshake, automated 2FA (TOTP) login, and session token renewal.
* **Async Subscription**: Implements an `asyncio`-based websocket listener that handles the initial index subscriptions (NIFTY, BANKNIFTY, FINNIFTY) and manages dynamic JIT subscriptions for option strikes.
* **Task Prioritization**: Separates the "Read" loop from the "Process" loop using an internal `asyncio.Queue` to prevent backpressure on the socket.

### 2. The Inter-Process Communication (IPC) Topology
The system utilizes a tiered communication strategy to distribute data to downstream consumers (Market Sensor, Data Logger) without adding network overhead.

* **Internal Binary Bus (ZeroMQ)**:
    * **Pattern**: `PUB/SUB` (Publisher/Subscriber).
    * **Transport**: `ipc:///tmp/market_data.ipc` (Unix Domain Sockets) for local communication, bypassing the TCP stack.
    * **Latency**: Measured at ~40µs for binary payloads.
* **Zero-Copy Shared Memory (SHM)**:
    * **Implementation**: Memory-mapped files (`mmap`) containing a circular buffer of the latest 1,000 ticks for every subscribed symbol.
    * **Utility**: Allows the `StrategyEngine` and `MarketSensor` to perform **O(1) lookups** ($<1$µs) to fetch the current price of any asset without waiting for a socket message.

### 3. Data Integrity & Sequence Logic
* **Monotonic Sequencing**: Every tick is assigned a `sequence_id` at the gateway entry point. 
* **Gap Detection**: Downstream daemons compare the `incoming_seq` with the `last_processed_seq`. If a gap $>1$ is detected, the daemon triggers a `DATA_GAP_CRITICAL` log and flushes rolling volume-delta calculations to prevent contaminated math.
* **Binary Protobuf Schema (`core/messages.proto`)**:
    ```protobuf
    message Tick {
        string symbol = 1;
        fixed64 timestamp = 2;
        float ltp = 3;
        int32 volume = 4;
        float bid = 5;
        float ask = 6;
        uint64 sequence_id = 7;
    }
    ```

### 4. Reliability & The "Heartbeat" Protocol
* **Websocket Watchdog**: The `shoonya_gateway` monitors the time since the last packet. If no data is received for $>5$ seconds during market hours, it triggers an automatic socket recycling (`hard_reset()`).
* **Redis Global State**: The gateway updates a `gateway:status` key in Redis every 1 second.
* **Dockerized Resilience**: The gateway runs in a "Restart-Always" Docker container. If the C++ process segments or the Python bridge hangs, the container engine restores the service within 2 seconds.

### 5. JIT (Just-In-Time) Subscription Logic
* **Trigger**: The `StrategyEngine` sends a `SUB_REQUEST` message over the Command Bus (ZMQ REQ/REP).
* **Action**: The `ShoonyaBridge` receives the token, validates it, and sends the `ts` (Touchline Subscription) command to the broker.
* **Cleanup**: A "TTL" (Time-To-Live) logic removes inactive option strikes from the subscription list after 30 minutes of no trade activity to preserve bandwidth.

# Module 2 - (Market Sensor & Feature Engineering)

### 1. Hybrid High-Performance Math Stack
To handle tick-by-tick calculations without falling behind the "Exchange Firehose," the Market Sensor utilizes a **Rust-powered compute core** integrated into a Python orchestration daemon.

#### A. The Rust Math Engine (`core/tick_engine/`)
* **Integration**: Built as a Python extension using **PyO3**, allowing Python to call high-performance Rust functions with near-zero overhead.
* **Concurrency**: Utilizes Rust’s `Rayon` library for data-parallelism, calculating microstructure metrics (VPIN, OFI) across multiple symbols simultaneously without hitting the Python GIL.
* **Zero-Copy Access**: Directly reads from the Shared Memory (SHM) buffers populated by Module 1, ensuring that the math engine always operates on the "freshest" data packet.

#### B. The Sensor Daemon (`daemons/market_sensor.py`)
* **Orchestration**: An `asyncio` loop that manages the lifecycle of signal calculation workers.
* **Frequency**: Operates on a **"Pulse" architecture** rather than a "Time" architecture. A computation pulse is triggered every time 50 new ticks arrive for a primary index.

### 2. Microstructure Signal Implementation
The sensor transforms raw price/volume data into a standardized **Signal Vector** ($V_{sig}$).

* **VPIN (Volume-Synchronized Probability of Informed Trading)**:
    * **Algorithm**: Divides the trading day into "volume buckets." It calculates the absolute difference between buy-initiated and sell-initiated volume within each bucket.
    * **Latency Guard**: Rust implementation uses a rolling window buffer to ensure $O(1)$ updates as new ticks arrive.
* **Hurst Exponent ($H$)**:
    * **Method**: Rescaled Range ($R/S$) analysis performed over the last 1,024 ticks. 
    * **Classification**: $H > 0.55 \rightarrow$ Trend; $H < 0.45 \rightarrow$ Mean-Reversion.
* **ASTO S23 (Adaptive SuperTrend Oscillator)**:
    * **Logic**: A normalized oscillator calculated as: $ASTO = \frac{Price - SuperTrend}{ATR} \times 100$.
    * **Smoothing**: Employs an Ehlers MESA Adaptive Moving Average to reduce signal lag while filtering out "micro-noise."

### 3. The Signal Vector Shared Memory (SHM)
Signals are not passed via slow network sockets. They are written into a **Global Signal Vector Table** in Shared Memory.

| Slot (Offset) | Signal Name | Data Type | Description |
| :--- | :--- | :--- | :--- |
| `0x00` | `s18_regime` | `int32` | Determined market state. |
| `0x04` | `s21_vpin` | `float32` | Flow toxicity score ($0.0$ to $1.0$). |
| `0x08` | `s22_whale_pivot` | `float32` | Institutional lead-lag pulse. |
| `0x0C` | `s25_smart_flow` | `float32` | CVD-based buyer/seller bias. |
| `0x10` | `s27_quality` | `float32` | Composite confidence score ($0$ to $100$). |

### 4. IPC & Signal Broadcasting
* **The Pulse Bus (ZeroMQ)**: Once a computation pulse is complete, the Sensor broadcasts a compact Protobuf message over `Ports.SIGNAL_PULSE` (Port 5556).
* **Payload**: Includes the `timestamp`, `symbol`, and a pointer to the SHM offset where the full vector resides. This allows the **Strategy Engine** to wake up and read the results instantly.

### 5. Deterministic Feature Persistence
* **InfluxDB/TimescaleDB Sink**: Every calculated signal is asynchronously pushed to a time-series database.
* **Utility**: This creates an immutable "Signal History" used by the **Module 8 Dashboard** to visualize the system's "internal thoughts" in real-time.
* **Integrity Check**: The sensor calculates a `checksum` for every signal vector. If the `StrategyEngine` reads a vector with a mismatched checksum, it rejects the signal to prevent "dirty data" execution.

### 6. Resilience: The "Safety Mode"
* **Data Starvation Detection**: If the Sensor does not receive new ticks from Module 1 for $>2$ seconds, it writes a `SYSTEM_STALE` flag to Redis.
* **Safe-State Reset**: Upon detection of a major price gap (e.g., after an exchange halt), the sensor automatically resets its rolling Hurst and VPIN buffers to zero to prevent stale math from influencing the restart phase.

# Module 3 - (Regime Detection & Meta-Routing)

### 1. The Deterministic Regime Engine (`daemons/regime_detector.py`)
To prevent "Regime Jitter" (where the system flips states too rapidly on noise), the Meta-Router implements a **Deterministic State Tracker**. This ensures that state transitions are mathematically smoothed before influencing strategy selection.

* **Observation Update**: The engine pulls the `REGIME_STATE` from the Market Sensor's Shared Memory (SHM).
* **Likelihood Function**: It calculates the likelihood of the current observation given the previous state, using a transition matrix $T$.
* **Posterior Calculation**:
  $$P(S_t | O_{1:t}) \propto P(O_t | S_t) \sum_{S_{t-1}} P(S_t | S_{t-1}) P(S_{t-1} | O_{1:t-1})$$
* **Stability Hysteresis**: A regime change is only broadcast to the system if the posterior probability for the new state exceeds a confidence threshold (typically **0.75**) for more than **12** consecutive computation pulses.

### 2. The 15-Gate Veto Matrix (`core/veto_matrix.py`)
The `VetoMatrix` is a bitwise validation engine. Every trade intent must return a `True` value across all 15 gates before it is allowed to pass to the Execution Bridge.

| Gate ID | Technical Logic | Strategic Purpose |
| :--- | :--- | :--- |
| **G01: VPIN_TOXIC** | `vpin_val < 0.82` | Blocks entry during toxic informed-flow spikes. |
| **G02: HEARTBEAT** | `time.now() - sensor_hb < 1.0` | Ensures the data feed is alive and fresh. |
| **G03: REGIME_FIT** | `current_regime in strat_allowed` | Prevents strategy-regime mismatch. |
| **G04: MARGIN_AVAIL** | `redis.get("avail_margin") > req` | Atomic check of account liquidity. |
| **G05: ADX_MOMENTUM** | `adx > 25` (for Kinetic) | Validates structural strength for momentum. |
| **G06: FRACTURE** | `abs(nifty_v - bnf_v) < 0.5%` | Blocks trades if major indices diverge sharply. |
| **G07: CIRCUIT_LMT** | `price < (ul * 0.99)` | Prevents orders near exchange circuit limits. |

### 3. Intent Queue & Alpha Decay (`daemons/meta_router.py`)
The Meta-Router manages a high-speed `asyncio.Queue` of trade intents from multiple strategies.

* **The Decay Function**: Before routing, the router calculates the **Phasic Decay** of the alpha signal based on processing latency.
  $$LotSize_{adj} = LotSize_{orig} \cdot e^{-\lambda(t_{now} - t_{signal})}$$
  If the latency ($t_{now} - t_{signal}$) exceeds **200ms**, the intent is automatically discarded as "Stale."
* **Concurrency Handling**: Uses a `PriorityQueue` where "Liquidation Intents" (Exits) are always placed at the head, followed by "Hedge Intents" (Futures), and finally "New Entries."

### 4. Atomic Margin Quarantine (Redis LUA)
To prevent "Race Conditions" where two concurrent strategies attempt to use the same margin, the Meta-Router utilizes **Redis LUA scripts** for atomic reservation.

* **The Workflow**:
    1.  Strategy sends intent.
    2.  Meta-Router executes LUA script: `reserve_margin.lua`.
    3.  LUA script checks `TOTAL_MARGIN`, subtracts the `INTENT_MARGIN`, and moves it to a `QUARANTINED_POOL`.
    4.  If the result is negative, the script returns `0` (Vetoed).
* **Benefit**: This ensures that order rejections at the broker level due to "Insufficient Funds" are reduced to near-zero.

### 5. High-Speed IPC State Broadcast
* **Regime Vector (SHM)**: The Router writes the final, smoothed regime state into `REGIME_SHM` for O(1) read access by all strategy daemons.
* **Global Panic Bus (ZMQ)**: If the Veto Matrix triggers a critical violation (e.g., G01 Toxic or G02 Dead Feed), the Router publishes a `SYSTEM_VETO` message over the ZeroMQ PUB bus (Port 5557). All active strategy daemons are programmed to immediately "Sleep" or "Hold" upon receiving this message.

# Module 4 - (Strategy Engine)

### 1. The Strategy Registry & Base Class (`core/base_strategy.py`)
To ensure modularity, every strategy (0DTE, Kinetic, etc.) inherits from a unified `BaseStrategy` class. This enforces a consistent interface for the `Orchestrator`.

* **Initialization**: Strategies subscribe to specific Shared Memory (SHM) offsets and ZeroMQ signal pulses.
* **The `on_pulse()` Hook**: A high-frequency event handler triggered by the Market Sensor’s signal pulse.
* **State Machine**: Maintains internal states: `IDLE`, `WAKING_UP` (JIT sub phase), `MONITORING`, `IN_TRADE`, and `COOLDOWN`.

### 2. 0DTE Iron Butterfly Strike-Selection Algorithm
The 0DTE engine utilizes a high-fidelity **Binary Search Strike Finder** to identify optimal legs under 100ms.

* **ATM Discovery**: Pulls the current Spot price from `MarketDataShm` to identify the At-The-Money (ATM) strike.
* **Leg Construction**:
    * **Shorts**: Sells the ATM Call and Put.
    * **Wings**: Uses a `PremiumTarget` search (e.g., finding the first OTM strike with a premium $\approx 10\%$ of the straddle credit) to define the protective wings.
* **Validation**: Performs a "Pre-Flight Greek Check" to ensure the initial Net Delta of the butterfly is within $\pm 0.05$.

### 3. Gamma Scalper: Delta-Neutral Rebalancing Logic
The Gamma Scalper maintains a long-option core and dynamically hedges using index futures.

* **Threshold Trigger**: Rebalancing is triggered only when the "Delta Error" ($\Delta_{\epsilon}$) breaches the tolerance:
    $$\Delta_{\epsilon} = | \Delta_{options} + \Delta_{futures} | > \text{Threshold (e.g., 0.10)}$$
* **Execution Logic**: If $\Delta_{\epsilon}$ is breached, the engine generates an intent for the exact number of future lots required to return the net Delta to zero. 
* **Slippage Guard**: Uses a "Passive-Aggressive" order logic—placing limit orders at the mid-price for 5 seconds before "crossing the spread" to ensure the hedge is completed.

### 4. Kinetic Hunter: The Three-Key Alignment Gate
The Kinetic Hunter (Momentum) strategy uses a strictly gated entry logic to prevent "Chop Mortality."

* **Gate 1: ASTO Magnitude**: Logic: `abs(asto_val) > 70`.
* **Gate 2: Smart Flow Alignment**: Checks the Cumulative Volume Delta (CVD) slope over the last 60 seconds. Must be aligned with ASTO direction.
* **Gate 3: Lead-Lag Confirmation**: Queries the `Whale_Pivot` SHM offset ($S_{22}$). Logic: `whale_pivot_direction == signal_direction`.
* **Intent Generation**: If all keys align, the engine calculates the entry size based on the **Composite Quality Score ($S_{27}$)** and dispatches a `MARKET_LIMIT` intent to the Meta-Router.

### 5. JIT (Just-In-Time) Warmup & Subscription
To maintain low-latency response, the engine uses a "Warmup" phase.

* **Pre-Flight Sub**: When the Market Sensor detects a "Volatility Expansion" ($S_{26}$ rising), the Strategy Engine proactively sends a `SUB_REQUEST` for the nearest 5 strikes to the Ingestion Gateway.
* **Cache Priming**: The engine "primes" its internal Greeks calculator (Black-Scholes Rust engine) with the latest IV values for these strikes 2 minutes before a potential entry.

### 6. Dynamic Configuration & Persistence
* **Live Polling**: Strategies poll a Redis-based configuration hash (`config:strat_id`) every 60 seconds to update parameters (SL, TP, Lot Size) without requiring a daemon restart.
* **Intent Journaling**: Every intent generated is logged to the `INTENT_LOG` in TimescaleDB with a microsecond-precision timestamp, enabling the **Module 5 Bridge** to perform "Shadow vs. Actual" slippage analysis.

# Module 6 - (Risk Guardianship & Liquidation)

### 1. The Liquidation Daemon Architecture (`daemons/liquidation_daemon.py`)
The `LiquidationDaemon` operates as a high-priority, "Zero-Trust" observer. It does not generate alpha; it only monitors for **Alpha-Death** or **Risk-Breach**.

* **Surveillance Loop**: An `asyncio` event loop that polls the `PositionMap` in Redis and the `SignalVector` in Shared Memory (SHM) every 100ms.
* **The "Sovereign" Override**: If a liquidation trigger is met, the daemon bypasses the `StrategyEngine` and sends a `FORCE_EXIT` intent directly to the `UnifiedBridge`.
* **Heartbeat Guard**: If the daemon itself stops pulsing (detected by the `SystemWatcher`), the bridge is programmed to enter an "Emergency Neutral" mode.

### 2. Triple-Barrier Implementation Logic
The daemon evaluates every active position against three concurrent barriers. The first barrier touched triggers an immediate exit.

* **Barrier 1: Horizontal (ATR-Based TP/SL)**:
    * **Calculation**: Pulls the ATR from `MarketSensorShm`.
    * **Logic**: `Exit = Current_Price > Entry + (ATR * TP_Mult)` OR `Current_Price < Entry - (ATR * SL_Mult)`.
* **Barrier 2: Vertical (Time-Value Decay)**:
    * **Logic**: Monitors the `entry_timestamp`. If `time.now() - entry_timestamp > max_hold_seconds` (e.g., 300s for 0DTE), a "Stagnation Exit" is fired.
* **Barrier 3: Microstructure (CVD/ASTO Flip)**:
    * **Logic**: If the `s25_smart_flow` (CVD) or `asto_val` flips significantly against the position ($|ASTO| < 50$), the momentum is considered "Dead" and an exit is triggered to preserve remaining premium.

### 3. ATR-Adaptive Trailing Stops
To lock in profits during trending moves, the daemon implements a non-linear trailing stop.

* **Ratchet Mechanism**: The stop-loss level only moves in the direction of the trade (up for longs, down for shorts).
* **Trend Sensitivity**: In "Extreme Trend" regimes ($S_{18}=1$ and $|ASTO| > 80$), the trailing stop tightens from $1.5\times$ ATR to $0.75\times$ ATR to protect against sharp institutional reversals.

### 4. Exit Sequencing & "Shorts-First" Protocol
To prevent margin spikes or "Naked Short" exposure during the liquidation of complex spreads (Iron Condors), the daemon follows a strict execution sequence.

* **The Slasher Logic**:
    1.  **Phase 1**: Fires `MARKET` orders for all **Short Legs** (income legs) to release margin and kill primary risk.
    2.  **Phase 2**: Monitors the `OrderReconciler` for fill confirmation.
    3.  **Phase 3**: Fires `MARKET` orders for **Long Legs** (protective wings).
* **Strategic Outcome**: Ensures the account is never left with an unhedged short position if the broker rejects the second half of the liquidation.

### 5. Global Safety Gates (The Circuit Breaker)
Beyond individual trades, the daemon monitors the "Global Portfolio State."

* **SDL (Stop Day Loss)**: A hard-coded equity floor (e.g., ₹16,000 drawdown). If breached, the daemon sets the `GLOBAL_PANIC` flag in Redis.
* **Slippage Monitor**: If the average slippage across the last 3 liquidations exceeds 2%, the daemon triggers a `LIQUIDITY_HALT`, preventing new entries for 5 minutes until the order book stabilizes.
* **API Resilience**: If the Shoonya API returns a `429` (Rate Limit) during a liquidation, the daemon immediately switches to a "Backup Execution Node" to ensure the exit is completed.

### 6. Persistence & Journaling
* **Liquidation Logs**: Every forced exit is logged to TimescaleDB with the specific `trigger_reason` (e.g., `TP_HIT`, `TIME_LIMIT`, `MANUAL_OVERRIDE`).
* **Post-Mortem Hook**: After a liquidation, the daemon updates the `StrategyStats` in Redis to recalculate the "Hit Ratio" and "Average Drawdown" for the triggering strategy.

# Module 7 - (Order Reconciliation & Basket Integrity)

### 1. The Atomic Basket Schema (`core/basket_manager.py`)
To manage multi-leg spreads as a single unit, the system utilizes a **Basket Context Object** stored in Redis under the `active_baskets:` namespace.

* **Data Structure**:
    ```json
    {
      "basket_id": "BKT-20231027-001",
      "strategy_id": "0DTE_BUTTERFLY",
      "status": "IN_FLIGHT",
      "legs": [
        {"symbol": "NIFTY23OCT19500CE", "type": "LONG", "qty": 50, "status": "PENDING"},
        {"symbol": "NIFTY23OCT19600CE", "type": "SHORT", "qty": 50, "status": "PENDING"}
      ],
      "expiry_gate": 1698412500000 
    }
    ```
* **Monotonic State Management**: A basket only transitions to `COMMITTED` once every leg in the array moves to `FILLED` status in the `OrderMap`.

### 2. The "Wing-First" Execution Sequence
To eliminate the possibility of an unhedged naked short, the reconciler enforces a strict execution hierarchy for all new entries.

* **Phase 1: Protective Leg Dispatch**: The bridge fires `BUY` orders for the long wings.
* **Phase 2: Feedback Wait**: The reconciler polls the `OrderUpdateBus`.
* **Phase 3: Income Leg Dispatch**: Only after the wings are confirmed `FILLED` does the reconciler release the `SELL` (short) orders for the inner legs.
* **Strategic Outcome**: If the broker rejects the short legs (due to margin or limits), the account is left holding a "Protective Long" position rather than a high-risk "Naked Short."

### 3. The Reconciliation Heartbeat (`daemons/order_reconciler.py`)
The `OrderReconciler` is an `asyncio`-driven watcher that bridges the gap between internal "Intent" and external "Broker Reality."

* **100ms Polling Loop**: Continually compares the `active_baskets` list in Redis against the `shoonya_order_book` fetched via the bridge.
* **Mismatch Resolution**:
    * **Orphan Detection**: If an order exists in the broker book but not in the internal `OrderMap`, it is flagged as an `ORPHAN` and immediately moved to a `MANUAL_AUDIT` queue.
    * **Zombie Detection**: If an order is `PENDING` internally but `REJECTED` by the broker, the reconciler triggers the **Circuit Breaker**.

### 4. Circuit Breaker & Atomic Rollback
The system operates on a **"Commit or Revert"** philosophy. If a basket fails to complete within the 3-second window, the reconciler initiates a "Full System Reversal."

* **The Timeout Trigger**: If `time.now() > basket["expiry_gate"]` and `status != COMMITTED`.
* **Rollback Logic**:
    1.  **Cancel**: Immediately sends `CANCEL` requests for all `PENDING` legs in the basket.
    2.  **Market-Exit**: Fires `MARKET` orders to close any legs that were already `FILLED`.
    3.  **Alpha-Veto**: Signals the `StrategyEngine` to enter a 5-minute cooldown for that specific strategy to prevent "Looping Failures."

### 5. Shielded Execution & Persistence (`asyncio.shield`)
To ensure that critical reconciliation logic is never interrupted by a system restart or a SIGTERM, the reconciler utilizes **Asyncio Shields**.

* **Non-Interruptible Tasks**: Rollback and Closing logic are wrapped in `asyncio.shield()`. This ensures the process completes the database write and broker-exit command even if the main daemon is shutting down.
* **State Recovery**: Upon reboot, the `SystemInitializer` queries the `RECONCILE_REQUIRED` flag in Redis. If active, it restarts the reconciler in "Forensic Mode" to clean up any lingering positions before allowing new trade intents.

### 6. Dead-Letter Escalation & Cloud Alerting
* **Terminal Failure Handle**: If a rollback fails (e.g., due to a broker `API_DOWN` error), the basket is moved to the `DEAD_LETTER_HEX`.
* **Alerting**: Triggers a high-priority **Telegram Cloud Alert** and a Red Visual Warning on the **Module 8 Dashboard**, requiring manual operator sign-off before the knight can return to battle.

# Module 8  - (Dashboard & Observability)

### 1. High-Performance Monitoring Stack
To achieve real-time visibility without impacting the latency of the trading daemons, the dashboard utilizes a decoupled, asynchronous architecture.

* **Backend (`api/main.py`)**: Built with **FastAPI** and **Uvicorn**. It acts as a high-speed read-layer for Redis (current state) and TimescaleDB (historical performance).
* **Frontend (`dashboard/`)**: A **React-based, Mobile-First** application utilizing **Tailwind CSS** and **Shadcn/UI**. It is optimized for "Glanceable Intelligence" on both desktop and mobile devices.
* **Data Transport**: Uses **WebSockets (FastAPI-SocketIO)** for real-time streaming of LTP, P&L, and Greek updates, reducing the overhead of HTTP polling.

### 2. The Heartbeat Watchdog (`core/heartbeat.py`)
Every daemon in the Project K.A.R.T.H.I.K. ecosystem (Sensor, Router, Strategy, Bridge) must implement a `HeartbeatProvider`.

* **Mechanism**: Every 1,000ms, the daemon writes a JSON object to a Redis hash: `system:heartbeats:{daemon_name}`.
* **Payload**: Includes `timestamp`, `pid`, `memory_usage`, and `last_processed_seq`.
* **Dashboard Logic**: The frontend calculates `time.now() - heartbeat_ts`. 
    * **Green**: $< 2s$ (System Healthy).
    * **Amber**: $2s - 5s$ (Jitter Detected).
    * **Red**: $> 5s$ (Daemon Frozen/Crashed).

### 3. Real-Time Greek & P&L Visualization
The dashboard provides a "War Room" view of the portfolio's mathematical health.

* **Greek Surface**: Real-time aggregation of Net Delta, Gamma, Theta, and Vega across all active baskets. 
* **The Veto Matrix Display**: A live grid showing the 15-Gate Veto states. If a trade is rejected, the specific gate (e.g., `G01: TOXIC`) flashes red, providing immediate "Why" transparency to the operator.
* **Equity Curve**: Uses **Recharts** to plot the "Shadow P&L" vs. "Live P&L" in real-time, highlighting the "Slippage Gap."

### 4. The "Manual Override" Command Bus
The dashboard is not just for viewing; it is an active control interface using a **Command Bus (ZeroMQ REQ/REP)**.

* **The Panic Button**: Triggers a `GLOBAL_SQUARE_OFF` signal. 
* **Execution Path**: 
    1.  Dashboard UI sends `POST /api/v1/panic`.
    2.  FastAPI pushes a high-priority message to the `CommandBus` (ZMQ Port 5559).
    3.  The **Module 5 Bridge** and **Module 6 Liquidation Daemon** receive the intent and initiate an immediate market exit of all positions.
* **Strategic Halt**: Allows the operator to toggle the `STRATEGY_ALLOW` flag in Redis to "Pause" specific knights without killing the entire system.

### 5. Post-Trade Journaling & TimescaleDB Integration
For institutional-grade "Post-Game" analysis, the dashboard pulls from the persistence layer.

* **Trade Reconstruction**: Users can click any filled order to see the exact **Signal Vector ($S_{total}$)** and **Regime** that existed at the microsecond of the trade intent.
* **Slippage Audit**: Calculates the "Execution Alpha" by comparing the `shadow_price` (at signal) vs. the `fill_price` (at broker), visualized as a "Friction Heatmap" over the trading day.

### 6. Cloud Alerting & Telegram Bridge (`daemons/cloud_notifier.py`)
To ensure the operator is never "offline," the dashboard is backed by a proactive notification daemon.

* **Telegram Bot**: Forwards critical events (e.g., `BASKET_REJECTED`, `SDL_BREACHED`, or `API_DISCONNECT`) to a private Telegram channel.
* **Cloud Mirror**: Periodically pushes a "Snap-State" of the account P&L to a GCP-hosted Cloud Run instance, allowing for emergency monitoring if the local infrastructure loses internet connectivity.

