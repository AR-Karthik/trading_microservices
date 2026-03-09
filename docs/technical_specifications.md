# Technical Specifications: Resilient Quant Trading System

This document outlines the architectural and engineering decisions made to build an institutional-grade, low-latency quant trading system.

## 1. Architectural Philosophy
The system follows a **Decoupled Microservices Architecture**, where each core responsibility (Sensing, Routing, Execution) is isolated into its own OS process or container.

### Key Benefits:
- **Fault Isolation**: A failure in the Dashboard API cannot crash the Execution Bridge.
- **Independent Scaling**: The compute-heavy `Market Sensor` can be allocated more CPU cores without affecting the I/O-bound `Data Gateway`.
- **Zero-Latency Monitoring**: Monitoring components are strictly "passive observers," ensuring zero impact on the millisecond execution path.

## 2. High-Performance Messaging (ZeroMQ)
The system utilizes **ZeroMQ** for all inter-service communication instead of traditional HTTP or slower message brokers.

- **Market Data Pipeline (PUB/SUB)**: Ticks are broadcasted to multiple consumers (Sensor, Router, Bridge) simultaneously with sub-millisecond latency.
- **Command Dispatch (PUSH/PULL)**: Orders and system commands follow a point-to-point flow to ensure no command is "double-processed" or dropped.
- **Protocol Buffers (Protobuf)**: Transitioning from Zero-Copy JSON encoding to strict Protobuf schemas for high-frequency Inter-Process Communication (IPC) to maximize throughput and schema safety.

## 3. GIL Mitigation & Multi-Processing
To bypass Python's Global Interpreter Lock (GIL) and achieve true parallel computation:
- **Multi-Process Neural Core**: Switched from a single-threaded asyncio loop to a Multi-Process architecture. Heavy math (HMM, Polars, Correlation) runs on dedicated CPU cores, ensuring the execution engine never lags due to Python's internal locks.
- **Lee-Ready Trade Classification**: To filter "Bid-Ask Bounce," the `Market Sensor` classifies trades relative to the mid-quote ($(Bid + Ask) / 2$). Trades are only scored if they occur above (Buy) or below (Sell) the mid-point, ensuring alpha is based on true market aggression.
- **Queue-Based IPC**: Communication between the main I/O loop and the compute workers happens via `multiprocessing.Queue`, ensuring sub-millisecond responsiveness to market ticks.

## 4. Asynchronous IPC & Decoupled State
- **ZeroMQ Ultra-Low Latency**: Utilizes ZeroMQ for sub-millisecond communication between daemons, moving away from slow, synchronous blocking calls.
- **Decoupled State Reconciliation**: Replaced the fragile "Fire and Forget" order logic with a ROUTER/DEALER pattern. Added an independent **Order Reconciler** daemon that verifies every trade via the broker's REST API to prevent "Phantom" positions.

## 4. State Management (Redis & TimescaleDB)
- **Redis (Real-time Blackboard)**: Serves as a high-speed shared memory for system-wide states (Alpha scores, current regime, risk limits). It acts as the "Bridge" between the Python backend and the React frontend.
- **Pending Journal Persistence**: Implements a Redis-based WAL (Write-Ahead Log) for intents. Before dispatching to the broker, the `Execution Bridge` writes the full intent to a `Pending_Journal`. This ensures that even during a crash, the `System Controller` can recover and audit "Uncertain State" orders upon reboot.
- **TimescaleDB (Persistence)**: A time-series optimized PostgreSQL extension used to log every trade and equity fluctuation, enabling per-strategy performance analytics and historical audit trails.

## 6. Resilience & Risk Controls
- **Atomic UI Budget Lock**: Moved capital limits from hidden `.env` files to the React Dashboard. Implemented a Redis Lua Script to atomically check and reserve margin before an order is even sent.
- **Global Budget Hierarchy**:
    1. **Master Ceiling**: Set via React Dashboard (`GLOBAL_CAPITAL_LIMIT`).
    2. **Strategy Partition**: Meta-Router allocates % of available capital based on confidence (e.g., 40% for Trending, 20% for Ranging).
    3. **Lot Normalizer**: Converts allocated ₹ to whole lots based on current premiums.
- **Dynamic NSE Protection**: Integrated real-time fetching of NSE Circuit Limits and Execution Price Bands. The system "clamps" orders to exchange rules to prevent instant rejections by the broker's RMS.
- **Preemption Concurrency**: Optimized for GCP Spot VM shutdown notices. During a 30-second warning, the system fires exits in batches of 10 (with 1.01s delays) to flatten the portfolio while obeying SEBI's 10-orders-per-second limit.
- **Double-Tap Execution Guard**: Prevents "over-exposure" due to race conditions. The `Execution Bridge` uses an Atomic Redis Token Lock per symbol; if a duplicate intent arrives before the first is reconciled, it is automatically rejected.
- **Dynamic Slippage Thresholds**: The `Liquidation Daemon` adapts to market panic. During periods of extreme Realized Volatility (RV > 3σ), it abandons progressive spread-crossing and switches to marketable limit orders to guarantee immediate exit.
- **Rate Limiting**: Enforces a strict 10 Orders-Per-Second (OPS) limit to comply with institutional broker requirements.

## 7. Distributed Integration Blueprint (V5.2)
The Capital Allocation logic is physically distributed across the system:
- **Streamlit UI**: Legacy reference; current user entry is via React `GLOBAL_CAPITAL_LIMIT`.
- **Meta-Router**: Performs real-time Lot Sizing calculations using Polars.
- **Strategy Engine**: Executes Atomic Redis Transactions (Lua) to "Lock" the calculated capital.
- **Execution Bridge**: Transforms "Theoretical Lots" into final Order Quantity (`Lots * Lot_Size`).
- **Liquidation Daemon**: Triggers Redis "Unlock" on exit, updating `AVAILABLE_MARGIN` with realized P&L.

## 8. Polyglot Infrastructure Migration (Institutional Grade)
To achieve ultimate low-latency performance and memory safety, the system implements a polyglot pipeline:
1.  **C++ Data Gateway**: Handles high-frequency WebSocket sharding, `simdjson` normalization, and Protobuf broadcasting.
2.  **Rust Tick Engine (PyO3)**: Performance-critical math including VPIN, OFI, and Black-Scholes Greeks (Delta, Gamma, Vega, Charm, Vanna).
3.  **Python Orchestration**: The Meta-Router and Market Sensor remain in Python for agility, consuming high-speed signals via **Shared Memory (SHM)**.
4.  **Macro Event Oracle**: `macro_event_fetcher.py` polls ForexFactory and FMP APIs to populate the system's event horizon.
5.  **FII Bias Extractor**: `fii_data_extractor.py` performs EOD sentiment analysis on institutional participation.

## 9. News Integration & Macro Lockdown Flow
The system implements a defensive "Veto" logic for high-impact news:
1.  **Ingestion**: `macro_event_fetcher.py` merges global economic calendars into `macro_calendar.json`.
2.  **Monitoring**: `SystemController` watches the calendar and sets `MACRO_EVENT_LOCKDOWN=True` in Redis 30m before/after events.
3.  **Strategy Veto**: `MetaRouter` suppresses aggressive trade entries (especially "CRASH" regime detection) while the lockdown is active.

## 9. Quantitative & Strategy Refinements
- **STT Tax Optimization**: Redefined strategy targets to 20–30 point structural moves to ensure viability after the 2026 hike in Options STT (0.15%).
- **Index Dispersion Filter**: Added a Top 5 Stock Correlation sensor (RELIANCE, HDFC, etc.). Momentum trades are VETOED if heavyweights aren't moving in a unified direction.
- **Vanna/Charm Analysis**: Integrated second-order Greeks to detect "Delta Bleed" on 0DTE/1DTE days, preventing entries into options decaying faster than index movement.
- **Kelly Criterion & VPIN Filter**: Strategy bet sizing is dynamically scaled via the Fractional Kelly Criterion based on HMM state probabilities. VPIN (Volume-Synchronized Probability of Informed Trading) > 0.8 triggers a global veto on Long entries to avoid toxic liquidity vacuums.
- **Dual-Stage Liquidation Protocol**: A 70-30 profit-taking rule. At +1.2x ATR, 70% of the position is market-sold to lock in risk-off profits. The STT-covered 30% "runner" holds until HMM invalidation or a 2.5x ATR hard ceiling is met.

## 11. C++ Gateway Sharding & SIMD Ingestion
- **Instrument Sharding**: The Gateway operates three concurrent C++ shards (Index Spot/Futures, Nifty Options, BankNifty Options) to prevent head-of-line blocking.
- **SIMD-Accelerated Parsing**: Utilizes `simdjson` for sub-microsecond JSON-to-Protobuf normalization, significantly reducing the "Normalization Bottleneck."
- **Institutional Connectivity**: Designed for uWebSockets (epoll/kqueue) for high-concurrency socket handling with minimal memory footprint.

## 12. Sub-Microsecond Inter-Process Communication (SHM)
- **Shared Memory Pipeline**: Market signals (Alpha, VPIN, OFI) are written to `/dev/shm` (Linux) or named mmap (Windows) using a 64-byte structured buffer.
- **Latency reduction**: Bypasses ZeroMQ's internal queueing and context switching, allowing the Meta-Router to read signals in **<1µs** vs ~200µs for ZMQ and ~5ms for Redis.
- **RAM Disk (tmpfs)**: IPC sockets and SHM files are host-mapped to a virtual RAM disk (`/ram_disk`) to minimize physical I/O latency.
- **Stale Data Protection**: The SHM reader implementation includes timestamp-based staleness checks (>500ms) and simple CRC integrity verification.

## 13. Institutional Infrastructure (High-Performance Cloud)
- **CPU Affinity (Core Pinning)**: Leverages `cpuset` to isolate high-frequency processes on dedicated vCPUs.
    - **Core 0**: C++ Data Gateway (Throughput-heavy I/O).
    - **Core 1**: Market Sensor (Compute-heavy microstructure math).
    - **Core 2**: Meta-Router/Strategy Engine (Inference & Routing).
    - **Core 3**: Shared Services (Redis, DB, OS, Liquidation).
- **GCP Tier_1 Networking**: Premium Tier networking with `GVNIC` and `TIER_1` egress performance enabled for jitter-free communication with Shoonya.
- **Regional Data Strategy**: TimescaleDB is backed by Regional Persistent Disks to ensure zero data loss during zonal outages in Mumbai (`asia-south1`).
- **Kernel Tuning**: OS-level optimizations including expanded `ulimit` (1M open files) and optimized TCP window sizes for high-connection concurrency.

## 14. Infrastructure & Automation
- **VM Proximity (Mumbai)**: Instances are deployed in **GCP `asia-south1` (Mumbai)** to minimize physical distance to NSE servers, achieving microsecond-scale execution latency.
- **Ultra-Low Latency Engineering**: Combines `uvloop` (high-performance asyncio event loop substitution) with fire-and-forget execution patterns and Protobuf serialization to ensure the order path is never blocked by I/O.
- **Serverless Pre-Flight Oracle**: Holiday and news scanning moved to a GCP Cloud Function (using `holidays` and FMP APIs) to decide if the VM should boot.
- **Data Integrity**: All databases mapped to a **Detached Persistent SSD**, ensuring trade logs and ML states are safe if the Spot VM is reclaimed.
- **Tailscale**: Secure, zero-config P2P networking for private access to the dashboard.
