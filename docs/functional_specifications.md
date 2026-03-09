# Functional Specifications: Quant Strategy Intelligence

This document details the logic, signal processing, and risk management strategies that drive the efficiency of the quant trading system.

## 1. Signal Intelligence & Alpha Scoring
The system identifies opportunities through a **Composite Alpha Scoring Model**, which aggregates disparate market signals into a single normalized value (`s_total`).

### Alpha Components:
- **Environmental (20%)**: FII bias, VIX term-structure, and Implied Volatility Percentile (IVP).
- **Structural (30%)**: Futures Basis slope, distance to Max Pain, and Put-Call Ratio (PCR).
- **Divergence (50%)**: Order Flow Imbalance (OFI), Cumulative Volume Delta (CVD) absorption, and price-volume dislocation.

## 2. Regime Identification (HMM)
The system uses a **Gaussian Mixture Model - Hidden Markov Model (GMM-HMM)** to identify the latent state of the market. This runs in an isolated OS process to ensure high-frequency tick updates are never delayed by matrix calculations.

### Regime States:
- **TRENDING**: High log-OFI Z-score and realized volatility. Optimized for **Gamma Momentum** strategies.
- **RANGING**: Low volatility and mean-reverting basis. Optimized for **Institutional Fade** strategies.
- **CRASH**: Extreme volatility and one-sided order flow. Triggers defensive "Neutral" posture or aggressive liquidation.

## 3. The Meta Router (Strategy Selector)
The Meta Router acts as the "指揮官" (Commander), determining which sub-strategies are authorized to enter based on the current Alpha Score and Regime.

- **Selective Activation**: Momentum strategies are only activated when Net GEX is negative and the regime is TRENDING.
- **Veto Logic**:
    - **Dispersion Veto**: If heavyweight correlation is too high (Dispersion < 0.30), momentum trades are blocked.
    - **OI Wall Veto**: If the spot price is within 15 points of the top-3 OI walls, fresh buy entries are vetoed.
- **Macro Windows**: Entry is restricted to high-liquidity windows (09:30–11:30 and 13:30–15:00) to avoid lunch-time churn and EOD volatility.

## 4. Execution & Risk Management
The system is designed for **low capital utilization** and **Buy-only (Long Premium)** strategies, optimized for retail/pro-sumer margin constraints.

### The Three-Barrier Exit System:
- **Barrier 1 (ATR-Mapped)**: Dynamic Take-Profit (2.5×ATR) and Stop-Loss (1.0×ATR) that breathe with market volatility.
- **Barrier 2 (Time-Decay Stall)**: If a position stalls for >5 minutes, the system progressively crosses the bid-ask spread to force an exit.
- **Barrier 3 (Structural Flip)**: If Order Flow (CVD) flips consistently against the position, a market exit is triggered regardless of P&L.

### Global Safeguards:
- **Stop Day Loss**: A global hard limit on daily realized loss. Once breached, all fresh opening trades are blocked.
- **Panic Button**: A mobile-friendly dashboard switch that instantly liquidates all active positions through the `Liquidation Daemon`.
- **Granular Exception Handling**: The system distinguishes between "Safe to Retry" errors (e.g., price range freeze) and "Abort" errors (e.g., margin shortfall).
