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
- **Zero-Copy Serialization**: High-frequency data uses specialized JSON encoding to handle complex NumPy types without CPU overhead.

## 3. GIL Mitigation & Multi-Processing
To bypass Python's Global Interpreter Lock (GIL) and achieve true parallel computation:
- **Compute Offloading**: The `Market Sensor` and `Meta Router` spawn isolated OS processes (`ComputeProcess`, `HmmProcess`) for heavy mathematical tasks (HMM inference, Black-Scholes Greeks).
- **Queue-Based IPC**: Communication between the main I/O loop and the compute workers happens via `multiprocessing.Queue`, ensuring the main thread stays responsive to market ticks.

## 4. State Management (Redis & TimescaleDB)
- **Redis (Real-time Blackboard)**: Serves as a high-speed shared memory for system-wide states (Alpha scores, current regime, risk limits). It acts as the "Bridge" between the Python backend and the React frontend.
- **TimescaleDB (Persistence)**: A time-series optimized PostgreSQL extension used to log every trade and equity fluctuation, enabling per-strategy performance analytics and historical audit trails.

## 5. Resilience & Risk Controls
- **Feed Watchdog**: Monitors tick staleness. If a feed gaps by >1000ms, a TCP socket reset is forced to re-establish the connection.
- **Docker-Based Persistence**: DB and Redis data are mapped to host volumes, ensuring trade history and system state survive VM restarts or container wipes.
- **Rate Limiting**: Enforces a strict 10 Orders-Per-Second (OPS) limit to comply with institutional broker requirements.

## 6. Infrastructure & Deployment
- **GCP Spot VM**: Utilizes `c2-standard-4` instances for high compute performance at ~70% lower cost.
- **Tailscale**: Secure, zero-config P2P networking for private access to the dashboard without exposing ports to the public internet.
- **Zero-Build Frontend**: The React UI uses a CDN-based delivery model, ensuring it can be deployed instantly on any server without complex Node.js build pipelines.
