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
- **Atomic UI Budget Lock**: Moved capital limits from hidden `.env` files to the Streamlit HUD. Implemented a Redis Lua Script to atomically check and reserve margin before an order is even sent.
- **Global Budget Hierarchy**:
    1. **Master Ceiling**: Set via Streamlit HUD (`GLOBAL_CAPITAL_LIMIT`).
    2. **Strategy Partition**: Meta-Router allocates % of available capital based on confidence (e.g., 40% for Trending, 20% for Ranging).
    3. **Lot Normalizer**: Converts allocated ₹ to whole lots based on current premiums.
- **Dynamic NSE Protection**: Integrated real-time fetching of NSE Circuit Limits and Execution Price Bands. The system "clamps" orders to exchange rules to prevent instant rejections by the broker's RMS.
- **Preemption Concurrency**: Optimized for GCP Spot VM shutdown notices. During a 30-second warning, the system fires exits in batches of 10 (with 1.01s delays) to flatten the portfolio while obeying SEBI's 10-orders-per-second limit.
- **Double-Tap Execution Guard**: Prevents "over-exposure" due to race conditions. The `Execution Bridge` uses an Atomic Redis Token Lock per symbol; if a duplicate intent arrives before the first is reconciled, it is automatically rejected.
- **Dynamic Slippage Thresholds**: The `Liquidation Daemon` adapts to market panic. During periods of extreme Realized Volatility (RV > 3σ), it abandons progressive spread-crossing and switches to marketable limit orders to guarantee immediate exit.
- **Rate Limiting**: Enforces a strict 10 Orders-Per-Second (OPS) limit to comply with institutional broker requirements.

## 7. Distributed Integration Blueprint (V5.2)
The Capital Allocation logic is physically distributed across the system:
- **Streamlit UI**: User entry for `GLOBAL_CAPITAL_LIMIT`.
- **Meta-Router**: Performs real-time Lot Sizing calculations using Polars.
- **Strategy Engine**: Executes Atomic Redis Transactions (Lua) to "Lock" the calculated capital.
- **Execution Bridge**: Transforms "Theoretical Lots" into final Order Quantity (`Lots * Lot_Size`).
- **Liquidation Daemon**: Triggers Redis "Unlock" on exit, updating `AVAILABLE_MARGIN` with realized P&L.

## 8. Polyglot Infrastructure Migration (Phase 3 & Beyond)
To achieve ultimate low-latency performance and memory safety, the system is architected to transition compute-heavy layers out of Python:
1.  **C++ Data Gateway**: WebSocket normalization and high-frequency ZeroMQ broadcasting will move to C++ to bypass the Python interpreter overhead.
2.  **Rust Risk & Greeks**: Black-Scholes calculations (Delta, Gamma, Vega) and the execution of Atomic Margin Locks will transition to Rust for memory-safe thread-level parallelism.
3.  **Python Native Glue**: The Meta-Router, Market Sensor (via Polars/NumPy), and Dashboard API remain in Python where developer flexibility and ecosystem maturity outstrip microsecond execution requirements. Shared memory and Redis serve as the communication bridge.

## 9. Quantitative & Strategy Refinements
- **STT Tax Optimization**: Redefined strategy targets to 20–30 point structural moves to ensure viability after the 2026 hike in Options STT (0.15%).
- **Index Dispersion Filter**: Added a Top 5 Stock Correlation sensor (RELIANCE, HDFC, etc.). Momentum trades are VETOED if heavyweights aren't moving in a unified direction.
- **Vanna/Charm Analysis**: Integrated second-order Greeks to detect "Delta Bleed" on 0DTE/1DTE days, preventing entries into options decaying faster than index movement.
- **Kelly Criterion & VPIN Filter**: Strategy bet sizing is dynamically scaled via the Fractional Kelly Criterion based on HMM state probabilities. VPIN (Volume-Synchronized Probability of Informed Trading) > 0.8 triggers a global veto on Long entries to avoid toxic liquidity vacuums.
- **Dual-Stage Liquidation Protocol**: A 70-30 profit-taking rule. At +1.2x ATR, 70% of the position is market-sold to lock in risk-off profits. The STT-covered 30% "runner" holds until HMM invalidation or a 2.5x ATR hard ceiling is met.

## 10. Infrastructure & Automation
- **VM Proximity (Mumbai)**: Instances are deployed in **GCP `asia-south1` (Mumbai)** to minimize physical distance to NSE servers, achieving microsecond-scale execution latency.
- **Ultra-Low Latency Engineering**: Combines `uvloop` (high-performance asyncio event loop substitution) with fire-and-forget execution patterns and Protobuf serialization to ensure the order path is never blocked by I/O.
- **Serverless Pre-Flight Oracle**: Holiday and news scanning moved to a GCP Cloud Function (using `holidays` and FMP APIs) to decide if the VM should boot.
- **Data Integrity**: All databases mapped to a **Detached Persistent SSD**, ensuring trade logs and ML states are safe if the Spot VM is reclaimed.
- **Tailscale**: Secure, zero-config P2P networking for private access to the dashboard.
