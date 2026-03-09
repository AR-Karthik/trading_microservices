# Functional Specifications: Project K.A.R.T.H.I.K.

This document serves as the high-level business logic blueprint for **Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)**, a fully autonomous quant trading engine.

## 1. Signal Intelligence & Alpha Scoring
The system identifies opportunities through a **Composite Alpha Scoring Model**, which aggregates disparate market signals into a single normalized value (`s_total`).

### Alpha Components & Financial Logic:
- **Environmental (20%)**: FII bias, VIX term-structure, and IVP. 
    - *Logic*: FII bias tracks institutional flows (the "Big Money"). High IVP (>80) indicates expensive premium, vetoing long entries.
- **Structural (30%)**: Futures Basis slope, Max Pain, and Put-Call Ratio (PCR).
    - *Logic*: Basis slope identifies futures premium/discount trends. PCR extremes indicate overbought/oversold exhaustion.
- **Divergence (50%)**: Order Flow Imbalance (OFI), CVD absorption, price-volume dislocation, and VPIN.
    - *Logic*: **OFI-Z > +2.0** signals aggressive market orders overwhelming the book.
    - *Lee-Ready Filter*: To ensure OFI isn't biased by bid-ask bounce, every tick is classified as "Aggressive Buy" if $LTP > MidQuote$ and "Aggressive Sell" if $LTP < MidQuote$. **CVD Absorption** identifies when large players are absorbing pressure.
    - *Flow Toxicity (VPIN)*: **VPIN > 0.8** indicates informed institutional selling that overwhelms liquidity, triggering a Veto on all Long entries.

## 2. Regime Identification (HMM)
The system uses a **Gaussian Mixture Model - Hidden Markov Model (GMM-HMM)** to identify the latent state of the market. This runs in an isolated OS process to ensure high-frequency tick updates are never delayed by matrix calculations.

### Regime States:
- **TRENDING**: High log-OFI Z-score and realized volatility. Optimized for **Gamma Momentum** strategies.
- **RANGING**: Low volatility and mean-reverting basis. Optimized for **Institutional Fade** strategies.
- **CRASH**: Extreme volatility and one-sided order flow. Triggers defensive "Neutral" posture or aggressive liquidation.

## 3. The Meta Router & Strategy Matrix
The Meta Router determines strategy authorization based on Alpha, Regime, and Vetoes.

### Strategy Parameter Matrix:
| Strategy | Entry Trigger | Range/Guard | Exit/Invalidation | Financial Logic |
| :--- | :--- | :--- | :--- | :--- |
| **Gamma Momentum** | OFI-Z > +2.0 & Spot Breakout | DTE ≤ 2, Delta [0.45, 0.55] | Regime flip to RANGING | ATM options have max Gamma; Delta 0.50 captures the steepest part of the S-curve for rapid scalps. |
| **Institutional Fade** | Spot Z > |2.5| (15-min) | CVD Absorption = 1 | Spread > 300% or OFI Spike | Mean reversion targets statistical extremes; spread expansion identifies dangerous "liquidity vacuums." |
| **OI Pulse** | OI Accel > 300% (10-min) | Spot trending to strike | Wall breach > 30s | OI acceleration indicates institutional "wall building" or aggressive hedging intent. |
| **Lead-Lag** | Index Corr < 0.40 | Ratio Z > |2.5| | Corr > 0.80 | Dislocation between Nifty/BNF is usually temporary; buying the laggard captures the eventual "catch up" correlation. |
| **Anchored VWAP** | |Alpha| > 40 & VWAP touch | SMA Bullish Crossover | 1-min Candle close wrong side | Anchored VWAP represents the volume-weighted average price of all participants since the open. |

- **Selective Activation**: Momentum strategies are only activated when Net GEX is negative and the regime is TRENDING.
- **Veto Logic**:
    - **Correlation Veto**: If heavyweight correlation is too low (Correlation < 0.70), momentum trades are blocked.
    - **OI Wall Veto**: If the spot price is within 15 points of the top-3 OI walls, fresh buy entries are vetoed.
- **Macro Windows**: High-liquidity windows are restricted to **09:30–11:30** and **13:30–15:00**.

## 4. Capital Allocation & Lot Sizing Logic
The system utilizes a dynamic, weight-based allocation model to manage risk and maximize compounding.

### Mathematical Lot Sizing & Fractional Kelly
When a strategy triggers, the Meta-Router calculates the number of lots using Polars:
$$Lots = \lfloor \frac{Available\_Margin \times f_{Kelly}}{Ask\_Price \times Lot\_Size} \rfloor$$
- **Available_Margin**: `GLOBAL_CAPITAL_LIMIT` - `CURRENT_MARGIN_UTILIZED` (from Redis).
- **Fractional Kelly ($f_{Kelly}$)**: $f^* = 0.5 \times (p - \frac{1-p}{b})$, where $p$ is the HMM Strategy probability and $b$ is the historical Strategy Profit Factor. Weight is clamped between 10% and 50%.
- **Ask_Price**: Real-time option premium.
- **Lot_Size**: Fetched at market open (e.g., 65 for Nifty).
- *The Floor Function* ($\lfloor \rfloor$) ensures zero budget breaches.

### Anti-Overdrive Safeguards
- **Max Single-Trade Cap**: No single trade can consume >50% of the total `GLOBAL_CAPITAL_LIMIT`, regardless of confidence.
- **2026 STT Friction Buffer**: Trades on premiums < 50 points with only 1 lot are VETOED. This ensures the mathematical expectancy covers the 0.15% STT and broker fees.

## 5. Execution & Risk Management
The system is designed for **low capital utilization** and **Buy-only (Long Premium)** strategies, optimized for retail/pro-sumer margin constraints.

### The Dual-Stage Liquidation Protocol (70-30 Rule):
- **Barrier 1a (Target 1 - Risk Off)**: Dynamic **Take-Profit (1.2×ATR)**. When hit, immediately market-sell **70%** of lots to lock in realized profit and cover STT/brokerage.
- **Barrier 1b (Target 2 - Runner Hunt)**: The remaining **30%** enters Active Trailing Mode. Break-even Lock moves SL to Entry. The runner is invalidated and closed if:
    - **HMM Regime Shifts** (e.g., Trending to Ranging).
    - Checks hit the **Hard Ceiling (2.5×ATR)**.
    - SL hits (Entry price).
- **Regime-Adaptive Mutlipliers**: Stop Loss expands from 1.0x ATR to 1.5x ATR during High Volatility regimes (RV > 0.001) to prevent stop-hunting whipsaws.
- **Barrier 2 (Dynamic Slippage)**: 
    - *Stall Mode*: If a position stalls for >5 minutes, the system progressively crosses the spread in 0.05 increments.
    - *Panic Mode*: If Realized Volatility (**RV > 3σ**), the system abandons the timer and fires a marketable limit order at the Best Bid - 1% (Puts) or Best Ask + 1% (Calls) for instant execution.
- **Barrier 3 (Structural Flip)**: If Order Flow (**CVD flips**) consistently against the position for **>10 ticks**, a market exit is triggered regardless of P&L.

### Global Safeguards:
- **Stop Day Loss**: A user-configurable hard limit on cumulative daily realized loss. The full enforcement pipeline is:
    1. **UI Configuration**: The user sets the limit (e.g., ₹5000 default) via the dashboard sidebar. Saved to Redis key `STOP_DAY_LOSS`.
    2. **Daily P&L Tracking**: After every trade fill, `paper_bridge.py` recalculates the day's total realized P&L from TimescaleDB and writes it to `DAILY_REALIZED_PNL_PAPER` (or `_LIVE`) in Redis.
    3. **Breach Detection**: `liquidation_daemon.py` checks `DAILY_REALIZED_PNL_{MODE}` vs `STOP_DAY_LOSS` on every tick. On first breach, it:
        - Sets `STOP_DAY_LOSS_BREACHED_{MODE} = True` in Redis.
        - Publishes `SQUARE_OFF_ALL` to `panic_channel` → triggers immediate full liquidation of open positions.
    4. **Entry Gate**: Before accepting any new order, `paper_bridge.py` checks `STOP_DAY_LOSS_BREACHED_{MODE}`. If `True`, only exit orders (closing existing positions) are allowed through. New opening trades are blocked with a log warning.
    5. **Dashboard Indicator**: The sidebar displays a live colour-coded buffer bar showing remaining daily loss allowance (green → amber → red as limit approaches).
- **Double-Tap Guard**: An atomic Redis token lock prevents the system from triggering multiple identical trades on the same strike during high-volatility "Regime: TRENDING" moments.
- **Journaled Persistence**: Every order is logged to a `Pending_Journal` in Redis before execution, ensuring data integrity for P&L tracking even if a daemon crashes during dispatch.
- **Panic Button**: A mobile-friendly dashboard switch that instantly liquidates all active positions through the `Liquidation Daemon`.
- **Macro Lockdown**: The system automatically "locks down" 30 minutes before high-impact news (defined in `data/macro_calendar.json` and managed by `SystemController`) to avoid "fake out" volatility by suppressing "CRASH" regime detection in the Meta-Router.

## 6. Macro News Protection Oracle
To mitigate "Flash Crash" risks during major economic releases, the system maintains an automated Macro Oracle:
- **Event Sourcing**: `utils/macro_event_fetcher.py` aggregates data from Forex Factory (Global) and FMP (US/India Economic Calendars).
- **Veto Mechanism**: During a 60-minute window centered on the event time (T-30 to T+30), the `SystemController` sets a global `MACRO_EVENT_LOCKDOWN` flag.
- **Financial Rationale**: News spikes often cause temporary "Regime: CRASH" prints that are purely noise-driven. By vetoing aggressive entries during these windows, the system preserves capital for structural moves once the "news dust" settles.
- **Sentiment Layer**: EOD FII/DII data provides a secondary filter on long-term institutional bias, preventing the system from fighting against major flow trends.
- **Correlation Guard**: Strict requirement for `HMM == TRENDING` AND `Correlation > 0.7` for momentum strategy activation to ensure high-probability leader-led moves.

---

## 11. HMM Auto-Train & Data Lifecycle

The system employs a self-correcting Hidden Markov Model (HMM) that adapts to shifting market microstructures via a persistent data lifecycle.

### 11.1 Data Ingestion (The "Collector")
- **Mechanism**: The `DataLogger` daemon forks engineered features (Log-OFI, CVD, VPIN) into a TimescaleDB `market_history` table.
- **Hard Stop (15:25 IST)**: Data collection ceases 5 minutes before market close to prevent "Market on Close" (MOC) volatility and erratic price discovery from polluting the clean intraday dataset.

### 11.2 Nightly Training (The "Nightly Brain")
- **Schedule**: A training worker (`hmm_trainer.py`) executes at **18:00 IST** daily.
- **Process**:
    1.  **Feature-Label Pairing**: Market history is queried to align intraday features with ground-truth forward returns (T+5, T+10, T+30).
    2.  **EM Optimization**: The Expectation-Maximization algorithm updates the HMM transition matrix and emission parameters.
    3.  **Validation Gate**: The new model is validated against the previous version using Log-Likelihood scores. Only superior models are promoted to `hmm_v_latest.pkl`.

### 11.3 Market Synchronization
- **Morning Warm-up (09:00 - 09:15)**: The system "primes" the HMM by feeding pre-open and high-volatility open ticks into the buffer to determine the current hidden state without triggering trade activations (`Regime == WAITING`).
- **Gap-Factor**: Each market open is treated as a "New Sequence" start, preventing overnight gaps from causing spurious volatility spikes in the regime detection logic.

## 12. Cloud-Native Dashboard & Remote Command
The dashboard is fully decoupled from the trading VM. The VM **publishes** state to cloud services; the dashboard **reads** from them. This architecture provides:

### 12.1 Real-Time Monitoring (Cloud Run)
- **Heartbeat Publishing**: The `CloudPublisher` daemon on the VM pushes P&L, Regime, Active Lots, and Greeks to **Google Firestore** every 10 seconds.
- **Cloud Run UI**: A lightweight web app on GCP Cloud Run reads Firestore for live metrics and GCS for historical analytics.
- **24/7 Availability**: The dashboard is accessible on any device (phone/desktop) even when the VM is shut down.
- **IAP Security**: Protected by Google Identity-Aware Proxy — only your Google account can access it.

### 12.2 EOD Data Lifecycle
- **Tick History Export**: At 15:35 IST, the full day's tick data is exported as `.parquet` to GCS (`tick_history/` folder).
- **Model Persistence**: The latest HMM model is uploaded to GCS at EOD to survive VM reclamation.
- **BigQuery Integration** (Optional): Point BigQuery at the GCS `tick_history/` folder for SQL-based analysis of months of trading data without running a database.

### 12.3 Remote Command & Control (Panic Button)
- **Mechanism**: The Cloud Run dashboard writes a command (e.g., `PANIC_BUTTON: True`) to a Firestore `trading_commands` document.
- **Execution**: The VM's `CloudPublisher` daemon polls this document every 3 seconds. On detecting the flag, it publishes `SQUARE_OFF_ALL` to Redis, triggering immediate full position liquidation.
- **Additional Commands**: `PAUSE_TRADING` (block new entries) and `RESUME_TRADING` (unblock).
- **Safety Net**: Provides total control from a mobile device during unusual market conditions without SSH access.
