# Functional Specifications: Project K.A.R.T.H.I.K.

This document represents the absolute ground-truth functional specifications for the trading system, derived from a line-by-line analysis of the current active codebase (`v6.5`). It serves as both a blueprint of *what* the system does and a guide on *why* it is configured this way.

## 1. System Objective 
**K.A.R.T.H.I.K.** is an algorithmic trading engine built for speed, modularity, and risk-capped execution. It utilizes a dynamic, pluggable strategy engine, zero-latency shared memory IPC, and a strict three-barrier liquidation system to manage options momentum and mean-reversion workflows.

## 2. Meta Router & Regime Orchestration `[meta_router.py]`
The Meta Router dynamically classifies the market regime and calculates trade allocations.

### 2.1 Decision Providers
The Regime Orchestrator uses three distinct providers:
1. **HMM Provider**: Probabilistic. Reads a serialized Gaussian Mixture Model - HMM state from Redis to classify the market as `TRENDING`, `RANGING`, or `CRASH`.
2. **Deterministic Provider**: Mathematical ruleset.
   - `TRENDING` triggers if: Hurst exponent > 0.55 AND ADX > 25 AND Kaufman Efficiency Ratio (ER) > 0.6.
   - `RANGING` triggers if: Hurst exponent < 0.45.
   - *Probability formula*: `prob = 0.5 + (hurst - 0.5) + (adx - 20) / 100` — linearly scales confidence with both trend persistence (Hurst) and momentum strength (ADX).
3. **Hybrid Provider (Consensus)**: The default active engine. Weights the HMM score at **40%** and the Deterministic score at **60%**. 
   - *Dual-Key Constraint*: Even if the combined score is > 0.70, it will return `RANGING` unless *both* the HMM and Deterministic providers independently agree the market is `TRENDING`.
   - *Run Interval*: 100ms polling loop. Balances CPU load with signal responsiveness.

4. **Cross-Index Divergence (Fractured Market Veto)**: A final structural check across all 3 major indices (NIFTY, BANKNIFTY, SENSEX).
   - *Logic*: If the Tri-Brain detects a `TRENDING` regime in one index while simultaneously detecting a `CRASH` regime in another, it triggers a "Fractured Market Veto," forcing the entire system into a `WAITING` state regardless of individual consensus scores.

> **Rationale & Usage:**
> Relying solely on ML (HMM) is dangerous in unprecedented regime shifts, while pure math (Deterministic) is often too lagging. The 40/60 Hybrid approach ensures the HMM's predictive edge is structurally validated by physical price momentum. The strict `>0.70` consensus threshold prevents the system from taking aggressive momentum trades during choppy, low-probability environments, protecting against theta decay. Finally, the **Cross-Index Divergence Veto** acts as a systemic circuit breaker; if BankNifty is crashing while Nifty is trending, the macro-structure is fundamentally broken, highly manipulative, and unsafe for algorithmic participation.

### 2.2 Fractional Kelly Sizing
Allocations are calculated using `b = 1.5` for the win/loss payout ratio constraint:
- `f = p - ((1.0 - p) / 1.5)`
- Dispatched Weight: Clamped at `max(0.01, 0.5 * f)` (Half-Kelly).

> **Rationale & Usage:**
> The standard Kelly Criterion is mathematically optimal but practically ruinous if win probabilities (`p`) are overestimated. By hardcoding a pessimistic payout ratio (`1.5`) and slicing the final fraction in half (`0.5 * f`), the engine sacrifices theoretical max-scaling to prioritize absolute capital preservation and minimize drawdown volatility.

## 3. Dynamic Strategy Engine `[strategy_engine.py]`
Currently implemented strategies include:

### 3.1 SMACrossoverStrategy
- **Trigger**: Simple moving average cross over a defined `period`.
- **Logic**: `BUY` if `price > SMA * 1.0005`. `SELL` if `price < SMA * 0.9995`.
> **Rationale:** The `1.0005` (~0.05%) buffer eliminates "noise" trades during flat markets where price hugs the SMA, saving significant slippage costs.

### 3.2 MeanReversionStrategy
- **Trigger**: Price deviation from a moving mean.
- **Logic**: `BUY` if `price < mean * (1 - threshold)` (default 0.001). `SELL` if `price > mean * (1 + threshold)`.
> **Usage:** Thrives during the `RANGING` regime. It fades institutional exhaustion, buying into local panics and selling into local euphoric spikes.

### 3.3 OIPulseScalpingStrategy
- **Logic**: `BUY` if OI change % > `oi_threshold` (default 2.0%). `SELL` if OI change % < `-oi_threshold`.
> **Rationale:** Rapid OI increases coupled with price spikes indicate aggressive new institutional positioning (not just short covering). This strategy rides the very tip of the spear during `TRENDING` breakouts.

### 3.4 AnchoredVWAPStrategy
- **Trigger**: VWAP calculation anchored to a specific time (default `09:15:00`).
- **Logic**: `BUY` if price > VWAP. `SELL` if price < VWAP.
> **Rationale & Usage:** Institutional execution algorithms (TWAP/VWAP) frequently base their intraday buys against the anchored Volume-Weighted Average Price. When price convincingly breaks VWAP on high volume, it signals that large passive buyers have finished loading, allowing the system to ride the ensuing liquidity vacuum.

### 3.5 GammaScalpingStrategy
- **Trigger**: Delta hedging based on Black-Scholes.
- **Logic**: Hedge triggered if `abs(target_pos - current_pos) >= hedge_threshold` (default 0.10).
> **Rationale & Usage:** This operates entirely differently than directional momentum. It buys options and dynamically sells the underlying (or vice versa) to remain "Delta Neutral." When the market chops wildly, the position continually accrues small gains from volatility without needing a clean directional trend, effectively "harvesting" the Gamma while isolating Theta decay risk.

### 3.6 CustomCodeStrategy
- **Trigger**: Reads `strategy_code` string from the Redis `active_strategies` hash. Compiled and executed at runtime via Python's `exec()`.
- **Logic**: Arbitrary Python code received from the UI/Firestore API. The function signature is `run(price, signals, position) -> action`.
- **Safety**: Sandbox isolation using a `timeout` wrapper. No file-system access.

> **Rationale:** Enables quants to hot-deploy a new strategy hypothesis directly from the UI without restarting any daemon. The live system acts as an execution engine; the quant can write the alpha logic on a laptop and push it to production in under one second.

### 3.7 Shadow Trading Mode (`--shadow` CLI Flag)
- **Activation**: Pass `--shadow` to `strategy_engine.py` at launch.
- **Behavior**: All strategy logic runs normally (regime checks, signal evaluation, lot sizing) but the final order dispatch call is suppressed. No ZeroMQ message is sent to the execution bridges.

> **Rationale:** Allows the system to be run live alongside a real session to perform "paper" monitoring and signal validation without any risk of accidentally firing an order. Essential during model calibration phases or when observing a new strategy in a live environment without capital exposure.

### 3.8 Pre-Flight JIT Warmup Engine
- **Trigger**: Runs automatically before entering the live order loop at startup.
- **Logic**: Fires 1,000 synthetic tick payloads through the complete signal pipeline (Hurst → Kelly → strategy eval).
- **Purpose**: Forces Python's JIT optimization to compile the hot paths. Without this, the first 100–200 real ticks incur CPU spikes as Python bytecode is compiled on demand.

> **Rationale:** In high-frequency options trading, a 50ms CPU spike during the first real tick could cause a missed ATM fill. Pre-warming the JIT path ensures the signal pipeline is CPU-hot and fully compiled before market open.

## 4. Sub-Millisecond Alpha Vetoes `[market_sensor.py]`

### 4.1 VPIN & Composite Alpha Vetoes
- **Toxic Veto**: If the market is marked `toxic_option=True` (VPIN > 0.8) and composite Alpha is negative, all BUY signals are ignored. Alpha > 20 vetoes SELLs, Alpha < -20 vetoes BUYs.
- **Zero Gamma Level (ZGL)**: Constantly estimated using volume-node mean pricing across the last 100 high-transaction ticks.

> **Rationale & Usage:** 
> VPIN detects "toxic" order flow—when smart money is aggressively leaning on one side of the order book right before a structural break. The Veto overrides all local strategy logic. Even if the `MeanReversionStrategy` signals a highly profitable local dip to BUY, the Veto blocks it because the microstructure indicates a high probability the floor is about to collapse.
> The **Zero Gamma Level** predicts where large institutional option dealers flip their hedge profiles (from buying dips to selling rips). Tracking ZGL prevents the algorithms from buying calls directly into a wall of passive dealer selling.

### 4.2 Lee-Ready Trade Classification
All tick data is classified using the Lee-Ready algorithm before contributing to CVD or VPIN:
- **Logic**: Compare Last Traded Price (LTP) to mid-quote `(bid + ask) / 2`. If LTP > mid → aggressive **Buy** (+1). If LTP < mid → aggressive **Sell** (-1). If LTP == mid → Neutral (0).

> **Rationale:** Raw trade volume is meaningless without direction. The mid-quote filter removes bid-ask bounce noise — a trade that hits exactly at mid-quote is a passive, non-directional fill and does not contribute to order flow imbalance. This is industry-standard practice in microstructure research (Lee & Ready, 1991).

### 4.3 Composite Alpha Scorer — Sub-Components & Weights

The `CompositeAlphaScorer` aggregates three sub-scores:

| Sub-Score | Weight | Source Signals |
|-----------|--------|---------------|
| **Environmental (env)** | 20% | `fii_bias`, `vix_slope`, `ivp` |
| **Structural (str)** | 30% | `basis_slope`, `dist_max_pain`, Put-Call Ratio (PCR) |
| **Divergence (div)** | 50% | Price/PCR divergence, CVD slope divergence |

**Key scoring rules:**
- If `ivp > 80` (Implied Vol Percentile extreme): env sub-score is immediately set to **-100**, collapsing the total alpha regardless of other signals. This is a hard protective floor.
- If `ivp < 20`: env sub-score gets a +20 boost (cheap vol is a structural buy).
- If `pcr > 1.3`: structural sub-score penalized -20 (extreme put loading = bearish).
- If `pcr < 0.7`: structural sub-score rewarded +20 (call-dominated = local euphoria, fade carefully).

**Time-of-Day Multiplier:**
- Between **11:30 – 13:30 IST** (Midday lull) and **after 15:00 IST** (EOD noise): the total alpha score is automatically multiplied by **0.5**.
- All other hours: multiplier = **1.0**.

> **Rationale:** Indian options markets exhibit a well-documented midday liquidity vacuum (11:30–13:30 IST) where institutional participants pull bids and market makers widen spreads. Alpha signals generated in this window have historically lower predictive power. Halving the alpha prevents the system from over-sizing into low-conviction setups. The post-15:00 reduction similarly protects against MOC (Market-on-Close) order book manipulation by institutions squaring off positions.

### 4.4 VPIN Configuration
- **Algorithm**: Volume-Synchronized Probability of Informed Trading.
- **Bucket Size**: `5,000` volume units. VPIN is recalculated every time cumulative volume crosses this threshold.
- **Series Length**: Maintains the last 50 VPIN readings (rolling window).
- **Toxicity Flag Threshold**: `vpin > 0.8`.

> **Rationale:** A 5,000-unit bucket on NSE options is calibrated to approximately 2-3 minutes of average flow during normal sessions. Smaller buckets generate too much noise; larger buckets react too slowly to intraday regime changes. 0.8 as the toxicity threshold is the 80th percentile of informed-flow probability — at this level, the evidence of one-sided smart-money activity is statistically strong enough to override all local strategy signals.

### 4.5 Rust Tick Engine (Optional High-Performance Mode)
- **Activation**: Set environment variable `USE_RUST_ENGINE=1`.
- **Function**: A Rust-compiled extension (`tick_engine` crate) replaces the Python-based VPIN bucket accumulator and CVD sign classifier.
- **Performance**: Eliminates Python GIL overhead for tick classification. CVD and VPIN run at C-level speed directly on the `TickData` struct.

> **Rationale:** At high-frequency tick rates (>500 ticks/sec during NSE opening), the Python Lee-Ready classifier and VPIN bucket loop can begin to queue up ticks. The Rust extension processes the same logic in nanoseconds per tick, ensuring the I/O event loop never falls behind the live feed.

## 5. Order Sizing & Margin Validation
- **Fallback Lot Sizing**: If the Meta Router is bypassed, resizing defaults to Expiry Logic:
    - **Expiry Days** (Wed/Thu): 100 lots base.
    - **Non-Expiry Days**: 50 lots base (50%).
- **Margin Locking**: Before any `BUY` is dispatched, the system performs an atomic check against `AVAILABLE_MARGIN_{MODE}`. 

> **Rationale:** Options gamma explodes on expiry days (Wed/Thu in India). The capital requirement per lot drops, and Delta moves violently. Sizing up on expiry captures asymmetric Gamma momentum, while cutting size in half on non-expiry days protects against painful Theta burn during slow grinds.

## 6. The Three-Barrier Liquidation Protocol `[liquidation_daemon.py]`
Once an entry is logged, it is handed off to the `LiquidationDaemon`. All threshold values are defined as module-level constants for single-source-of-truth management.

**Constants:**
```python
ATR_TP1_MULTIPLIER = 1.2   # TP1 partial exit (70%)
ATR_TP_MULTIPLIER  = 2.5   # TP2 Runner Hard Ceiling
ATR_SL_MULTIPLIER  = 1.0   # Base Stop Loss
STALL_TIMEOUT_SEC  = 300   # 5-minute normal stall timer
CVD_FLIP_EXIT_THRESHOLD   = 5    # Barrier 3 ticks threshold
BARRIER2_SPREAD_CROSS_PCT = 0.10 # 10% bid-ask cross per retry
```

### 6.1 Barrier 1: The 70-30 Rule
- **Risk-Off (Target 1)**: Set at `Entry Price + ATR_TP1_MULTIPLIER * ATR` (1.2×ATR). If hit, a partial exit function markets out **70%** of the position to cover costs.
- **Runner Hunt (Target 2)**: The remaining 30% enters "active trailing". The Hard Ceiling is `Entry Price + ATR_TP_MULTIPLIER * ATR` (2.5×ATR). SL moves to Entry. 
- **Regime-Adaptive Stop Loss**:
    - Low/Normal Volatility: Stop Loss is `Entry - ATR_SL_MULTIPLIER * ATR` (1.0×ATR).
    - High Volatility (`RV > 0.001` or `VIX > 18.0`): Stop loss expands to `Entry - 1.5 * ATR` to survive whipsaws.
- **HMM Regime Invalidation**: If the market regime shifts from `TRENDING` to `RANGING` or `CRASH` after entry, the runner position is immediately closed regardless of price level.

> **Rationale:** The 1.2×ATR TP1 is mathematically derived to cover round-trip brokerage (Shoonya flat ₹20) plus slippage (0.3–0.5pt) on the smallest viable lot size. This guarantees the strategy is profitable by TP1 even after friction costs. The 2.5×ATR TP2 represents the statistical 2σ move expected in a genuine trending session on NSE mid-cap options.

### 6.2 Barrier 2: Time-Decay (Stall Mode)
- **Normal Protocol**: If a position stalls for `STALL_TIMEOUT_SEC` (300 seconds / 5 mins), the system crosses the Bid-Ask spread by `BARRIER2_SPREAD_CROSS_PCT` (10%) per retry.
- **Low Volatility Shrink**: If Volatility is extremely low (`RV < 0.0005` or `VIX < 12.0`), the stall timer shrinks to `180 seconds` (3 mins).

> **Rationale:** Time is the enemy of options buyers (Theta). If a momentum entry fails to move into profit within 5 minutes, the original momentum thesis is structurally dead. The system ruthlessly aborts stalled trades rather than hoping for a reversal, preventing slow bleeding deaths. In very low-vol environments where the full 5 minutes could add an extra ₹10–15 in Theta decay per lot, the timer shrinks proactively.

### 6.3 Barrier 3: Volatility Panic & Structural Flip
- **Dynamic Slippage (Panic)**: If Realized Vol (`RV`) spikes above `0.002` (approx 3σ), the engine fires aggressive marketable limit orders to exit.
- **CVD Structural Flip**: If the Cumulative Volume Delta flips against the position for `CVD_FLIP_EXIT_THRESHOLD` (**5 consecutive ticks**), it exits. (Forgiveness extends to **10 ticks** if it is a "Runner" position)

> **Rationale:** A 5-tick CVD flip against a position indicates the immediate micro-trend has fatally reversed. Exiting *before* the price actually hits the physical Stop Loss saves a mathematically massive amount of capital over thousands of trades. Runners get 10 ticks of forgiveness to survive minor pullbacks.

### 6.4 Barrier 2b: HTTP 400 Granular Error Handler
When the broker's API returns an HTTP 400 error, the daemon parses the error message (`emsg`) and decides whether to re-fire or abort:

| Error Message Contains | Classification | Action |
|----------------------|----------------|--------|
| `"price range"`, `"circuit"`, `"freeze"`, `"price band"`, `"pcl"`, `"ucl"` | **Circuit Limit** | Recalculate ±2% price bounds and re-fire (max 3 retries). |
| `"margin"`, `"insufficient"`, `"invalid"`, `"malformed"`, `"not found"` | **Abort** | Halt retries. Push Telegram alert. Require manual intervention. |
| All other errors | **Retry** | Retry with 1-second back-off (max 3 retries). |

> **Rationale:** NSE options are subject to daily price bands (typically ±20% of previous close for options). During fast-moving markets, the "stale" TP2 price may fall outside the current band after a circuit halt and resume. Rather than abandoning the position entirely, the daemon adjusts the price to within band limits and re-fires. For genuine broker errors (margin calls, invalid order structure), re-firing would simply fail again and waste time — immediate escalation is the correct response.

## 7. System Controller & Safeguards `[system_controller.py]`
- **Macro Event Lockdown**: Identifies Tier 1 events. Activates 30 mins before, clears 30 mins after.
  > **Rationale:** CPI/Fed announcements cause algorithmic liquidity vacuums. Staying in the market is gambling, not trading.
- **GCP Preemption**: Spot Node polls metadata every 5 seconds. Preemption triggers batched `SQUARE_OFF_ALL`.
  > **Rationale:** Spot VMs are 80% cheaper but can be killed anytime. A 5-second polling loop guarantees the system has 25 seconds to liquidate elegantly before Google pulls the physical plug.
- **SEBI 10 OPS Law**: Execution engine fires max **10 orders per batch**, forcing a `1.01-second` wait time between batches.
  > **Rationale:** Indian brokerages strictly penalize API abuse. Firing 11 orders in 1 second results in a hard RMS block.
- **Hard Stop Day Loss**: Checked on every tick. Default limit `₹5,000`. 
  > **Rationale:** Prevents compounding revenge-trading algorithms during unmodeled "Black Swan" technical glitches.
- **EOD Hard Shutdown**: Hard shutdown trigger at exactly **16:00 IST**. Publishes `SQUARE_OFF_ALL`, sets `SYSTEM_HALTED=True` in Redis, and terminates all async tasks.
  > **Rationale:** NSE market hours end at 15:30 IST. Any positions still open at 16:00 are either errors or dangerously illiquid post-market options. The hard stop prevents overnight risk.

### 7.1 HMM Synchronization Watcher
- **Warm-Up Window (09:00 – 09:15 IST)**: Sets `HMM_WARM_UP=True` in Redis. During this window, the system runs in `WAITING` mode — regime signals are computed but no orders are fired.
- **Data Logger Hard Stop (15:25 IST)**: Sets `LOGGER_STOP=True`. The `market_sensor` stops writing to the `market_history` TimescaleDB table.

> **Rationale:** The HMM model's hidden state must stabilize from the opening auction noise. NSE opening (09:15 IST) is notoriously "manipulative" — large institutions run opening algo orders that create fake momentum. Waiting until 09:15 for the first live signal prevents chasing opening auction noise that immediately reverses.
> The 15:25 data logger stop prevents MOC (Market-on-Close) order book manipulation from contaminating the HMM's training data. If the 15:25–15:30 closing auction panic is recorded as a "CRASH" regime, it would inappropriately bias the HMM's initial state at tomorrow's open.

### 7.2 Orphaned Intent Audit (Boot Reconciliation)
- **Trigger**: Runs automatically on every `SystemController.start()`.
- **Logic**: Scans Redis for all keys matching `Pending_Journal:*`. For each found, pushes a Telegram alert with the full order payload.

> **Rationale:** If the system crashed mid-execution (e.g., VM OOM kill), an order may have been dispatched to the broker but never confirmed or cleared from the journal. On reboot, this audit surfaces any such "ghost" orders for human review. Without this check, a trader could believe no position is open while the broker actually holds an unfilled order.

### 7.3 Telegram Alert System
All daemons push critical alerts to Redis list `telegram_alerts`. A separate `telemetry_alerter` service (or the `cloud_publisher`) drains this list and dispatches messages to a configured Telegram bot.

| Daemon | Alert Types |
|--------|------------|
| `system_controller` | Boot status, preemption, macro lockdown, EOD shutdown |
| `liquidation_daemon` | Barrier exits, HTTP 400 aborts, exit failures |
| `live_bridge` | Authentication failures, kill switch trips |
| `cloud_publisher` | Heartbeat failures |

> **Rationale:** For a 24/7 automated system running on a remote GCP VM, email or log-only alerts are insufficient. Telegram delivers push notifications to a phone in under 500ms, enabling the operator to respond to critical events (position stuck in circuit limit, authentication failure) within seconds even when away from a workstation.

## 8. Application UI Dashboard `[cloud_publisher.py / cloudrun/main.py]`
The GUI provides a single-pane-of-glass Command Center utilizing a zero-interference observer pattern.

### 8.1 Remote Command & Control (Inputs)
- **Live vs Paper Mode Toggle**: Visually swaps the entire UI theme between Blue (Paper) and Green (Live) to prevent catastrophic execution mistakes.
- **Capital & Budget Injection**: Users type numerical limits for `PAPER_CAPITAL_LIMIT`, `LIVE_CAPITAL_LIMIT`, and `STOP_DAY_LOSS` into the sidebar. Clicking "Save" instantly writes these to Redis, which the backend Margin Manager reads atomically.
  - *Default Paper Capital*: `₹50,000`. *Default Live Capital*: `₹0.0` (null-locked until 120-day audit clears).
- **The Panic Button**: Publishes `{"action": "SQUARE_OFF_ALL", "reason": "MANUAL_DASHBOARD"}` to the Redis `panic_channel`. All execution bridges and the Liquidation Daemon react immediately.

### 8.2 Observation Panes (The 6 Tabs)
1. **Terminal**: 
    - Real-time ticker tape (LTP, Change, OI) pulled from `latest_tick:{symbol}`. 
    - Shows an L2 Orderbook visualization using `bid`/`ask`/`bid_qty`/`ask_qty` fields from `latest_tick`.
    - Monitors "Active Positions," calculating live Unrealized P&L by querying the `portfolio` table in TimescaleDB and computing `(current_ltp - avg_price) * quantity`.
2. **Market Signals**:
    - Acts as the microstructure observatory. Exposes raw values like `Log-OFI Z-Score`, `Dispersion Coeff`, and `Basis Z-score`.
    - Automatically color-codes values (e.g., Log-OFI > 2 displays as `🟢 STRONG BUY FLOW`). Displays `Toxic Option Flag` based on Charm/Vanna. 
3. **Meta-Router Engine**:
    - Real-time X-ray into the HMM's state matrix vs Deterministic Logic overrides.
    - Displays exactly *why* a trade is presently blocked (e.g., `🔴 ACTIVE — MR capped 1 lot` if Dispersion Veto is tripped or `🔴 ACTIVE — Fractured Market Veto` if Cross-Index Divergence is detected).
    - Lists all active strategy modules globally, color-coded by status (Active, Orphaned, Sleep).
4. **Risk & Phantom Orders**: Flags `⚠️ PHANTOM RISK` if an order lacks an ACK for > 3 seconds.
  > **Rationale:** The most dangerous scenario in algorithmic trading is a dispatched order getting "lost" in the broker's API layer. The Reconciler ensures no capital is flying blind.
5. **Strategy Analytics**: Executes multi-dimensional analysis rendering Equity curves and calculating Sharpe/Sortino ratios.
6. **Performance History**:
    - Tracks Macro factors like FII Bias and confirms the operational status of pre-flight checks (NSE Holiday Guards and Macro Data pre-fetching).

### 8.3 External Cloud Publisher (`cloud_publisher.py`)
To ensure the dashboard can operate completely off-premise via Google Cloud Run without pinging the execution node, the `cloud_publisher` daemon acts as a one-way mirror.
- **Firestore Heartbeats**: Every 10 seconds, it pushes a highly compressed vector dictionary to Google Firestore containing: `regime`, `daily_pnl_paper`, `daily_pnl_live`, `margin_utilized`, `capital_limit`, `composite_alpha`, `active_lots`, `stop_day_loss_breached_paper`, `stop_day_loss_breached_live`, `is_vm_running`, `last_heartbeat`, and `timestamp_utc`.

> **Rationale & Usage**: 
> This provides absolute real-time Observability for a mobile device or a remote Risk Manager. By pushing precise margin limits and stop-loss breach states out to a cloud database, an off-site human overseer can see exactly how close the system is to self-terminating its capital allocation without ever needing SSH access to the core trading machine.
