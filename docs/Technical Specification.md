# Module 0 - Part 2: The Engineering Blueprint

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