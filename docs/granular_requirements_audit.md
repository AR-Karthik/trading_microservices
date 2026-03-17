# Granular Requirements Compliance Audit

This document tracks compliance for every single line item specified in `docs/requirements.md`.

## Component 1: Heuristic Regime Engine (`hmm_engine.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 1.1 | Multi-Asset Parallelism (NIFTY50, BANKNIFTY, SENSEX) | | | |
| 1.2 | Realized Volatility (RV): 10-day rolling annualized | | | |
| 1.3 | Average Directional Index (ADX): 14-period trend | | | |
| 1.4 | Put-Call Ratio (PCR): Live index sentiment from OI | | | |
| 1.5 | Implied Volatility (IV): Live ATM volatility | | | |
| 1.6 | Adapter Pattern Output: JSON string to `hmm_regime_state` | | | |
| 1.7 | Logic: ADX > 25 -> `TRENDING` | | | |
| 1.8 | Logic: ADX < 20 AND RV > 25% -> `High Volatility Chop` | | | |
| 1.9 | Logic: IV - RV > 3.0 AND ADX < 20 -> `RANGING` | | | |
| 1.10 | Logic: RV > 50% OR VPIN > 0.8 -> `CRASH / TOXIC` | | | |
| 1.11 | Update Frequency: 5 seconds | | | |
| 1.12 | Execution Latency: < 50ms | | | |
| 1.13 | Hardware Determinism: CPU Core Pinning | | | |
| 1.14 | PCR Extreme Veto: PCR < 0.6 -> `RED` | | | |
| 1.15 | IV/RV Edge Check: Premium selling ONLY if IV - RV > 3.0 | | | |
| 1.16 | Stale Data Protection: > 10s -> `RED / WAITING` | | | |
| 1.17 | Missing Lookback Handling: 09:15 IST emergency REST | | | |
| 1.18 | Math Stability: Epsilon offset ($1e-9$) | | | |

## Component 2: Meta-Router & Sizing Engine (`meta_router.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 2.1 | Multi-Leg Basket Generation (up to 4 legs) | | | |
| 2.2 | Unique Relational Tagging (`parent_uuid`) | | | |
| 2.3 | Lifecycle Classification (`KINETIC`, `POSITIONAL`, `ZERO_DTE`) | | | |
| 2.4 | JIT Strike Resolution (ATM/Delta targets) | | | |
| 2.5 | Max Portfolio Heat: 25% | | | |
| 2.6 | Hedge Reserve Tranche: 15% | | | |
| 2.7 | Sizing Formula: ATR + Half-Kelly | | | |
| 2.8 | Unsettled Funds Veto: T+1 Logic | | | |
| 2.9 | Cross-Index Divergence Check (> 2.0%) | | | |
| 2.10 | VPIN Toxicity Veto (> 0.80) | | | |
| 2.11 | Regime-Strategy Lock (IC -> Chop/Ranging, Gamma -> Trend) | | | |
| 2.12 | Double-Spending Prevention: Redis LUA scripts | | | |
| 2.13 | Broker Margin Sync: Live Ledger Truth | | | |
| 2.14 | Partial Payload Recovery: `Pending_Journal` | | | |

## Component 3: Execution Bridge (`live_bridge.py` / `data_gateway.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 3.1 | Atomic Basket Dispatch | | | |
| 3.2 | Strict Buy-First Sequencing | | | |
| 3.3 | Just-In-Time (JIT) Subscriptions (`FEED_CMD`) | | | |
| 3.4 | Token Bucket Rate Limiting (8 req/s) | | | |
| 3.5 | Internal Latency: < 20ms | | | |
| 3.6 | Order Persistence (Journaling) | | | |
| 3.7 | Adaptive Limit Chasing (5-loop Chaser) | | | |
| 3.8 | Slippage Circuit Breaker (0.10% -> `SLIPPAGE_HALT`) | | | |
| 3.9 | Partial Fill Rollback (3s) | | | |
| 3.10 | Margin Check Pre-Flight: Final `LUA_RESERVE` | | | |
| 3.11 | Ghost Fill Recovery: Reconcile every 15m | | | |
| 3.12 | API 400 (Circuit Limit) Handling: 0.1% nudge | | | |
| 3.13 | Stale Feed Protection (> 1000ms delay -> `FEED_STALE`) | | | |

## Component 4: Liquidation Daemon (`liquidation_daemon.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 4.1 | Bifurcated Risk Logic (Kinetic vs Positional) | | | |
| 4.2 | Continuous Portfolio Hydration (every 60s) | | | |
| 4.3 | Net Delta Monitoring by Index | | | |
| 4.4 | Vertical Time Barrier (DTE < 3) | | | |
| 4.5 | Waterfall Step 1: Future Hedge (±0.15 Delta) | | | |
| 4.6 | Waterfall Step 2: Untested Roll | | | |
| 4.7 | Waterfall Step 3: Pro-Rata Slash (25% Cut) | | | |
| 4.8 | Evaluation Frequency: 1 second | | | |
| 4.9 | Strict Exit Sequencing (Shorts first) | | | |
| 4.10 | Stagnation Exit (Kinetic: 300s stall) | | | |
| 4.11 | Dynamic SPAN Watchdog (90% utilization) | | | |
| 4.12 | Take-Profit Cap (60% Decay) | | | |
| 4.13 | Hard Day-Loss Limit (2% / ₹16,000) | | | |

## Component 5: State Persistence & Transaction Integrity (`order_reconciler.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 5.1 | Atomic Intent Journaling | | | |
| 5.2 | Basket State Machine (`PENDING` -> `PARTIAL` -> `COMPLETE`) | | | |
| 5.3 | Stale Order Watchdog (500ms check, 3s timeout) | | | |
| 5.4 | Double-Entry Bookkeeping (`trades` and `portfolio`) | | | |
| 5.5 | Rollback: Partial Fill (Long) -> Force Fill | | | |
| 5.6 | Rollback: Partial Fill (Short) -> Rollback | | | |
| 5.7 | API Timeout Handling (Verify & Sync) | | | |
| 5.8 | Ghost Position (Orphan Kill) | | | |
| 5.9 | Relational Integrity (`parent_uuid` as FK) | | | |
| 5.10 | Audit Metadata (`heuristics` in DB) | | | |
| 5.11 | Row-Level Locking (`SELECT FOR UPDATE`) | | | |
| 5.12 | Cold Boot Hydration (Triple-Sync) | | | |
| 5.13 | Dead-Letter Queue (`CRITICAL_INTERVENTION`) | | | |
| 5.14 | Storage Exhaustion (Memory-Only Mode) | | | |

## Component 6: Operational Controller & EOD Guards (`system_controller.py`)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 6.1 | EOD Shutdown stage 1: 15:00 (Origination Halt) | | | |
| 6.2 | EOD Shutdown stage 2: 15:20 (Kinetic Square-Off) | | | |
| 6.3 | EOD Shutdown stage 3: 16:00 (GCP VM Shutdown) | | | |
| 6.4 | T-1 Expiry Calendar Sweep (15:15 IST) | | | |
| 6.5 | Preemption Protection (GCP Metadata 5s poll) | | | |
| 6.6 | SEBI Quarterly Settlement Check (09:10 IST) | | | |
| 6.7 | Unsettled Premium Filter (T+1 Logic) | | | |
| 6.8 | 50:50 Cash-to-Collateral Guard | | | |
| 6.9 | Heartbeat Watchdog (15s DEAD -> `SYSTEM_HALT`) | | | |
| 6.10 | Exchange Health Monitor (500ms STALE -> `SUSPEND`) | | | |
| 6.11 | Ghost State Reconciliation (15m sync) | | | |
| 6.12 | Database IO Wait (IO threshold prioritization) | | | |
| 6.13 | Network Brownout (Dead Man's Switch 2m) | | | |
| 6.14 | API Rate-Limit Overflow (30s Cool Down on 429) | | | |

## Component 7: Observability & Dashboard (`cloud_publisher.py` / UI)

| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 7.1 | Unified Risk Visualization (Portfolio Greeks) | | | |
| 7.2 | Regime State Broadcast (UI display) | | | |
| 7.3 | Bid-Ask Spread Heatmap | | | |
| 7.4 | Asynchronous Alert Pipeline (Telegram 1s) | | | |
| 7.5 | Cloud Discovery Pulse (60s metadata update) | | | |
| 7.6 | Heartbeat Frequency (5s dashboard refresh) | | | |
| 7.7 | EOD Data Lake Sync (15:35 Parquet upload) | | | |
| 7.8 | Indicator: IV/RV Spread | | | |
| 7.9 | Indicator: Delta Drift | | | |
| 7.10 | Indicator: Slippage Audit | | | |
| 7.11 | Indicator: Portfolio Heat | | | |
| 7.12 | Network Jitter Resilience (Async Sink) | | | |
| 7.13 | Silent Publisher Recovery (Redis caching) | | | |
| 7.14 | Stale Dashboard Veto (> 60s -> DISCONNECTED) | | | |

## Component 8: Strategies & Global Guards

### Strategy S1: Gamma Momentum Hunter
| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 8.1.1 | Lifecycle: `KINETIC` | | | |
| 8.1.2 | Regime: `TRENDING` | | | |
| 8.1.3 | Log-OFI Z-Score > 2.0 | | | |
| 8.1.4 | Target: 1.2 * ATR / Stop: 1.0 * ATR | | | |
| 8.1.5 | Time-Veto: Stall exit (300s) | | | |

### Strategy S6: Institutional Iron Condor
| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 8.2.1 | Lifecycle: `POSITIONAL` | | | |
| 8.2.2 | Regime: `High Volatility Chop` or `RANGING` | | | |
| 8.2.3 | Volatility Edge: IV - RV > 3.0 | | | |
| 8.2.4 | Target: 60% Decay / Stop: DTE < 3 | | | |

### Directional Credit Spreads
| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 8.3.1 | Regime: `TRENDING` + PCR Alignment | | | |
| 8.3.2 | Structure: 2-leg basket (25 Delta / 15 Delta) | | | |
| 8.3.3 | Target: 70% Profit / Stop: 2x Credit | | | |

### 0DTE Tastytrade
| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 8.4.1 | Lifecycle: `ZERO_DTE` | | | |
| 8.4.2 | Timing: Wed/Thu 09:30-10:30 IST | | | |
| 8.4.3 | Target: 50% Credit / Stop: 0.5% move | | | |

### Global Guards
| # | Requirement | Status | Evidence | Fix Required |
| --- | --- | --- | --- | --- |
| 8.5.1 | Market Storm: 15 mins before Macro News | | | |
| 8.5.2 | Index Fracture: Divergence > 2% | | | |
| 8.5.3 | VPIN Panic: VPIN > 0.80 | | | |
| 8.5.4 | Low Vol Trap: IV < 12% | | | |
