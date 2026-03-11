# Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

**An Institutional-Grade, Sub-Millisecond Algorithmic Trading Microservices Architecture.**

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)

---

## 📐 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                       DATA INGESTION LAYER                       │
│    [C++ Gateway (uWebSockets + simdjson + Protobuf)]             │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Protobuf binary over ZeroMQ (40µs)
┌──────────────────────▼──────────────────────────────────────────┐
│                  MARKET SENSOR (Python Orchestration)           │
│         [Rust Tick Engine (PyO3)] ← VPIN, OFI, Greeks           │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Shared Memory IPC (<1µs)
┌──────────────────────▼──────────────────────────────────────────┐
│                   META-ROUTER (Python Orchestration)             │
│         Macro Time Windows · ORPHAN Commands · Regime           │
└──────┬──────────────────────────────────────┬───────────────────┘
       │  ACTIVATE/ORPHAN commands            │
       ▼                                      ▼
┌─────────────────────┐        ┌─────────────────────────────────┐
│  STRATEGY ENGINE    │        │   LIQUIDATION DAEMON             │
│  strategy_engine.py │        │   liquidation_daemon.py          │
│  · DTE-based sizing │        │   · Triple Barrier Exit          │
│  · Gamma Scalping   │        │   · ATR Stop-Loss/Take-Profit    │
│  · Mean Reversion   │        │   · Optimal Stopping (5 min)     │
│  · VWAP · OI Pulse  │        │   · CVD Signal Reversal          │
└──────┬──────────────┘        └────────────────┬────────────────┘
       │  ORDER_INTENT                           │
       ▼                                         ▼
┌───────────────────────────────────────────────────────────────┐
│             EXECUTION BRIDGES (ZeroMQ PUSH/PULL)              │
│   paper_bridge.py (Simulated) ·  live_bridge.py (Shoonya API) │
└───────────────────────────┬───────────────────────────────────┘
                            │
                  ┌─────────┴──────────┐
                  │   TimescaleDB      │
                  │   (trades, PnL)    │
                  └─────────┬──────────┘
                            │
              ┌─────────────▼─────────────┐
              │   DASHBOARD API (FastAPI)  │
              │   Redis/DB Bridge · Panic  │
              └─────────────┬─────────────┘
                            │
              ┌─────────────▼─────────────┐
              │   DASHBOARD UI (React)     │
              │   Premium · Mobile-First   │
              └────────────────────────────┘
```

---

## 🧩 Module Reference

### 1. `daemons/market_sensor.py` — The Eyes of the System

**Purpose:** Processes raw tick data into a structured, real-time market state including the **Composite Alpha Score ($S_{total}$)**.

**How it works:**

Every 500ms, it consumes tick data from the ZeroMQ bus and computes:

| Metric | Method |
|--------|--------|
| Hurst Exponent | R/S analysis — detects trending (H>0.55) vs mean-reverting (H<0.45) regimes |
| Realized Volatility | 15-min rolling log-return standard deviation |
| Order Flow Imbalance (OFI) | Volume-weighted sign of price changes |

It then runs the **`CompositeAlphaScorer`**:

$$S_{total} = [(W_{env} \times S_{env}) + (W_{str} \times S_{str}) + (W_{div} \times S_{div})] \times TimeMultiplier$$

| Module | Weight | Signals |
|--------|--------|---------|
| **Environmental** | 20% | FII Net Long/Short bias · VIX 15-min slope · IV Percentile (IVP) |
| **Structural** | 30% | Futures Basis · Max Pain gravity · OI Wall proximity · PCR thresholds |
| **Divergence** | 50% | Price vs PCR slope · Price vs VIX correlation · Price vs CVD divergence |

> **IVP > 80** → Nullifies all long option signals (theta is too expensive).
> **TimeMultiplier** is reduced to 0.5 during lunch (11:30–13:30) and post-market.

The final `s_total` and all metrics are published to Redis (`latest_market_state`) and the ZeroMQ `MARKET_STATE` topic.

---

### 2. `daemons/meta_router.py` — The Gatekeeper

**Purpose:** Implements the **Macro Time Windows** and translates Alpha Scores into strategy commands.

**Macro Time Windows:**
```
09:30 ─────────── 11:30   [ENTRY AUTHORIZED — Session 1]
11:30 ─────────── 13:30   [LUNCH — ORPHAN issued, no new entries]
13:30 ─────────── 15:00   [ENTRY AUTHORIZED — Session 2]
15:00 ─────────── EOD     [ORPHAN issued, Liquidation Daemon takes over]
```

At exactly **11:30** and **15:00**, the Meta-Router broadcasts a global `ORPHAN` command to all active strategies. Positions are **not** market-dumped — they are handed off to the Liquidation Daemon for graceful exit.

**Execution Matrix Mapping:**

| Alpha Score ($S_{total}$) | Action |
|---|---|
| `+75 to +100` | `ACTIVATE` Long Gamma Call Scalper |
| `-39 to +39` | `SLEEP` — no new entries |
| `-75 to -100` | `ACTIVATE` Long Gamma Put Scalper |

---

### 3. `daemons/strategy_engine.py` — The Signal Factory

**Purpose:** Houses all trading strategies. Subscribes to market data, generates `BUY`/`SELL` signals, applies DTE sizing, and emits orders.

**Strategies:**

| Strategy | Class | Logic |
|---|---|---|
| **Gamma Scalping** | `GammaScalpingStrategy` | Delta-hedges a long gamma options position using Black-Scholes. Rebalances when delta error exceeds threshold. |
| **Mean Reversion** | `MeanReversionStrategy` | Fades price away from rolling mean when deviation exceeds a configurable z-score threshold. |
| **SMA Crossover** | `SMACrossoverStrategy` | Classic price vs. rolling mean signal with configurable period. |
| **OI Pulse Scalping** | `OIPulseScalpingStrategy` | Detects unusual Open Interest acceleration as a leading indicator. |
| **Anchored VWAP** | `AnchoredVWAPStrategy` | Trades momentum relative to session VWAP anchored at 09:15. |
| **Custom Code** | `CustomCodeStrategy` | Executes arbitrary user-submitted Python code as a strategy. |

**Dynamic DTE Sizing:**
```python
# Wed/Thu = Expiry days → 100% Gamma exposure
# Fri–Tue = Non-expiry → 50% position size to reduce Theta risk
is_expiry = now.strftime("%A") in ["Wednesday", "Thursday"]
qty = base_qty if is_expiry else int(base_qty * 0.5)
```

Strategies are **hot-loaded** from Redis (`active_strategies` hash), allowing zero-downtime deployment of new strategies.

---

### 4. `daemons/liquidation_daemon.py` — The Exit Manager

**Purpose:** Accepts orphaned positions from strategies and executes graceful exits using a **Triple Barrier** approach, avoiding slippage from panic-selling.

**Triple Barrier Exit Logic:**

```
Position Accepted via ORPHAN/HANDOFF command
        │
        ├── 🚧 Price Barrier (checked every tick)
        │       Take-Profit: entry_price + 2×ATR
        │       Stop-Loss:   entry_price - 1×ATR
        │
        ├── ⏱️ Time Barrier (5-minute timer)
        │       If neither TP nor SL triggered after 5 min,
        │       enter "Liquidation Mode" — trail tight ATR stop,
        │       wait for spread compression to minimize slippage.
        │
        └── 📡 Signal Barrier (checked every tick)
                If live CVD/Alpha Score flips strictly against
                position direction → instant exit override.
```

---

### 5. `daemons/paper_bridge.py` & `live_bridge.py` — The Execution Layer

**Purpose:** Receive order intents and execute them — either in a simulated paper environment or via the live **Shoonya API** for real trades.

- **`paper_bridge.py`**: Simulates fill at the latest tick price, records to TimescaleDB with `execution_type = "Paper"`.
- **`live_bridge.py`**: Routes orders to Shoonya brokerage API with retry logic. Reads `execution_type` from order metadata.
- Both implement a **10 Orders-Per-Second rate limiter** to comply with exchange regulations.

---

### 6. `daemons/data_gateway.py` — The Data Firehose

**Purpose:** Connects to market data feed(s), normalizes tick data, and publishes to the ZeroMQ `MARKET_DATA` bus. Also writes latest ticks to Redis for low-latency dashboard access.

---

### 7. `core/` — Shared Utilities

| File | Purpose |
|---|---|
| `greeks.py` | Black-Scholes Delta, Gamma, Theta, Vega with fast numpy math |
| `mq.py` | ZeroMQ Manager — publisher/subscriber/push/pull factory |
| `shared_memory.py` | Shared memory ring-buffer for zero-copy tick access across processes |
| `execution_wrapper.py` | Unified order placement interface (paper vs live) |
| `health.py` | Health-check heartbeat emitter for monitoring |

---

### 8. `dashboard/` — The Premium Control Room

**Purpose:** A modern, mobile-first React dashboard providing real-time visibility and control.

**Features:**
- **High-Performance API (FastAPI)** — Bridges Redis/TimescaleDB with sub-50ms latency.
- **Glassmorphism UI** — Premium, responsive dark-mode design optimized for mobile thumbs.
- **Alpha Score Master** — Live $S_{total}$ with regime-colored glow and HMM/GEX status.
- **Dynamic Portfolio** — Swipeable position cards with real-time unrealized P&L updates.
- **Panic Control** — Large, accessible "SQUARE OFF ALL" switch for crisis management.
- **Paper/Live Switch** — Instantly toggle view modes across the entire interface.

---

## ⚡ High-Performance Polyglot Hot-Path
To meet institutional latency standards, the critical execution path is implemented using a polyglot approach:

| Component | Language | Technology | Latency |
|---|---|---|---|
| **Data Gateway** | C++ | uWebSockets · simdjson · Protobuf | ~40µs |
| **Math Engine** | Rust | PyO3 · statrs · pure-math | ~15µs |
| **IPC Pipeline** | SHM | mmap · struct binary packing | <1µs |

### 1. C++ Sharded Gateway
Operates concurrent high-speed shards for different instrument classes. Uses `simdjson` to bypass standard JSON parsing bottlenecks.

### 2. Rust Tick Engine
Encapsulates high-frequency microstructure math (VPIN, log-OFI) and Black-Scholes Greeks. Compiled to a native Python module via PyO3, bypassing the GIL entirely.

### 3. Shared Memory (SHM) IPC
Eliminates ZeroMQ/Redis overhead for Alpha score propagation. The Meta-Router reads signals directly from a 64-byte structured buffer in `mmap` space.

---

## 🏗️ Infrastructure

### Cloud (GCP C2 Spot VM)
Optimized for **asia-south1** (Mumbai) with institutional-grade hardening:
- **Core Pinning**: Strict `cpuset` allocation for zero context-switch jitter.
- **Networking**: Enabling **Tier_1 Performance** + **GVNIC** for sub-millisecond API responsiveness.
- **RAM Disk IPC**: Moving all IPC sockets and Shared Memory to a `tmpfs` RAM disk (`/ram_disk`).
- **Regional SSDs**: Replication across Mumbai zones for zero-loss trade logging.

### Local (Docker Compose)
Services include `redis`, `timescaledb`, and the full Polyglot stack.

```bash
# Provision on GCP (with Tier_1 + Regional SSDs)
python infrastructure/gcp_provision.py --action create

# Local Build & Launch
docker compose up -d --build
```

Dashboard: `http://localhost:8501` (React UI) | `http://localhost:8000/docs` (API Docs)

---

## ⚙️ Configuration & Environment

Copy `.env.example` (rename `.env`) and fill in:

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | Your GCP project |
| `TAILSCALE_AUTH_KEY` | Tailscale reusable auth key |
| `SHOONYA_USER` / `SHOONYA_PWD` | Shoonya brokerage credentials |
| `SHOONYA_APP_KEY` / `SHOONYA_VC` | API key and vendor code |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alert notifications |

---

## 🚀 Quick Start (Local)

```bash
git clone https://github.com/AR-Karthik/trading_microservices.git
cd trading_microservices
cp .env.example .env   # fill in your credentials

# Start all services
docker compose up -d

# Start infra + dashboard only (React UI + API)
docker compose up -d redis timescaledb dashboard_api dashboard_frontend
```

Dashboard UI: `http://localhost:8501` | API Docs: `http://localhost:8000/docs`

---

## 🛡️ Risk Management

| Layer | Mechanism |
|---|---|
| **Entry Gating** | Macro Time Windows — no new signals outside 09:30–11:30, 13:30–15:00 |
| **Alpha Threshold** | SLEEP mode when \|S_total\| < 39 — no trades in ambiguous regimes |
| **DTE Sizing** | 50% size reduction on non-expiry days to control Gamma/Theta risk |
| **Kill Switch** | Global PANIC button → instant `SQUARE_OFF_ALL` via Redis pub |
| **Graceful Exit** | Liquidation Daemon uses optimal stopping, not market dumps |
| **Signal Barrier** | Immediate exit if CVD/OFI flips against an active position |
| **Dual Budgets** | Strict live and paper trading budgets configurable via Dashboard UI |

---

## 📦 Dependencies

```
fastapi · uvicorn · tailwind · react · pandas · polars · psycopg2-binary · redis
pyzmq · numpy · google-cloud-compute · python-dotenv · httpx
```

---

*Built by Karthik — combining institutional quant finance with modern MLOps/data engineering principles.*
