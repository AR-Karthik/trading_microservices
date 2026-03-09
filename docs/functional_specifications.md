# Functional Specifications: Quant Strategy Intelligence

This document details the logic, signal processing, and risk management strategies that drive the efficiency of the quant trading system.

## 1. Signal Intelligence & Alpha Scoring
The system identifies opportunities through a **Composite Alpha Scoring Model**, which aggregates disparate market signals into a single normalized value (`s_total`).

### Alpha Components & Financial Logic:
- **Environmental (20%)**: FII bias, VIX term-structure, and IVP. 
    - *Logic*: FII bias tracks institutional flows (the "Big Money"). High IVP (>80) indicates expensive premium, vetoing long entries.
- **Structural (30%)**: Futures Basis slope, Max Pain, and Put-Call Ratio (PCR).
    - *Logic*: Basis slope identifies futures premium/discount trends. PCR extremes indicate overbought/oversold exhaustion.
- **Divergence (50%)**: Order Flow Imbalance (OFI), CVD absorption, and price-volume dislocation.
    - *Logic*: **OFI-Z > +2.0** signals aggressive market orders overwhelming the book. **CVD Absorption** identifies when large players are absorbing sell pressure (bullish).

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
    - **Dispersion Veto**: If heavyweight correlation is too high (Dispersion < 0.30), momentum trades are blocked.
    - **OI Wall Veto**: If the spot price is within 15 points of the top-3 OI walls, fresh buy entries are vetoed.
- **Macro Windows**: High-liquidity windows are restricted to **09:30–11:30** and **13:30–15:00**.

## 4. Execution & Risk Management
The system is designed for **low capital utilization** and **Buy-only (Long Premium)** strategies, optimized for retail/pro-sumer margin constraints.

### The Three-Barrier Exit System:
- **Barrier 1 (ATR-Mapped)**: Dynamic **Take-Profit (2.5×ATR)** and **Stop-Loss (1.0×ATR)** that breathe with market volatility to prevent "stop hunting."
- **Barrier 2 (Time-Decay Stall)**: If a position stalls for **>5 minutes**, the system progressively crosses the bid-ask spread to force an exit (prevents Theta bleed).
- **Barrier 3 (Structural Flip)**: If Order Flow (**CVD flips**) consistently against the position for **>10 ticks**, a market exit is triggered regardless of P&L.

### Global Safeguards:
- **Stop Day Loss**: A global hard limit (e.g., ₹5000) on daily realized loss. Once breached, all fresh opening trades are blocked.
- **Panic Button**: A mobile-friendly dashboard switch that instantly liquidates all active positions through the `Liquidation Daemon`.
- **Macro Lockdown**: The system automatically "locks down" 30 minutes before high-impact news to avoid "fake out" volatility.
