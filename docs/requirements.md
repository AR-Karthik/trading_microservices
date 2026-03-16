
### **Requirements**

Order,Component,Mission Criticality
1,The Heuristic Regime Engine (hmm_engine.py),High: Decides which strategies are allowed to run.
2,The Meta-Router & Sizing Engine (meta_router.py),High: Controls capital allocation and multi-leg basket generation.
3,The Execution Bridge & JIT Data (live_bridge.py / data_gateway.py),"Extreme: Manages broker API, rate limits, and buy-first sequencing."
4,The Liquidation Daemon & Margin Waterfall (liquidation_daemon.py),Extreme: Protects against Delta breaches and handles exits.
5,State Persistence & Transaction Integrity (reconciler.py / DB),"High: Ensures partial fills and system crashes don't leave ""ghost"" positions."
6,Operational Controller & EOD Guards (system_controller.py),Medium: Handles the 15:20 square-off and T-1 expiry sweeps.
7,Observability & Citadel-Style Dashboard (cloud_publisher.py / UI),"Medium: Visualizes Greeks, IV/RV spreads, and strategy performance."


Let's establish the institutional-grade requirements for **Component 1: The Heuristic Regime Engine (`hmm_engine.py`)**. This component is the "Commanding Officer" that dictates which strategies are mathematically authorized to engage.

### **1. Functional Requirements**

* **Multi-Asset Parallelism**: The engine must independently calculate and store unique market regimes for **NIFTY50**, **BANKNIFTY**, and **SENSEX** simultaneously.
* **The Heuristic Quartet**: The engine must ingest four specific metrics for each asset:
* **Realized Volatility (RV)**: 10-day rolling annualized standard deviation.
* **Average Directional Index (ADX)**: 14-period trend strength.
* **Put-Call Ratio (PCR)**: Live index sentiment derived from total Open Interest.
* **Implied Volatility (IV)**: Live At-The-Money (ATM) volatility.


* **Adapter Pattern Output**: The engine must output a JSON string to the Redis key `hmm_regime_state` containing the `regime` name and the full `heuristics` dictionary for audit logging.

### **2. Logical Decision Matrix (The "Citadel" Rules)**

| Heuristic Condition | Mapped Regime String | Strategy Authorization |
| --- | --- | --- |
| **ADX > 25** | `TRENDING` | Authorizes **Gamma Momentum** and **Directional Credit Spreads**. |
| **ADX < 20** AND **RV > 25%** | `High Volatility Chop` | Authorizes **Iron Condors**. |
| **IV - RV > 3.0** AND **ADX < 20** | `RANGING` | Authorizes **0DTE Tastytrade** and **Iron Condors**. |
| **RV > 50%** OR **VPIN > 0.8** | `CRASH / TOXIC` | **VETO MODE**: Blocks all new originations. |

### **3. Performance & Latency Constraints**

* **Update Frequency**: Regime states must be recalculated every **5 seconds**.
* **Execution Latency**: The total time from "Redis Data Fetch" to "Regime Write" must be **< 50ms**.
* **Hardware Determinism**: The process must be **pinned to a specific CPU core** (e.g., Core 4) to eliminate OS context switching.

### **4. Capital & Risk Guards**

* **PCR Extreme Veto**: If PCR < 0.6 (Extreme Fear), the engine must flag the regime as `RED`, blocking the sale of new Puts even in a `RANGING` market.
* **The IV/RV Edge Check**: The engine must only permit premium selling strategies (`Iron Condor`, `Credit Spread`) if the **IV is at least 3 points higher than RV**, ensuring a statistical edge exists.

### **5. Failure Scenarios & Resilience**

* **Stale Data Protection**: If the `LIVE_IV` or `LIVE_ADX` Redis keys have not been updated for > 10 seconds, the engine must default the regime to `RED / WAITING`.
* **Missing Lookback Handling**: If the 14-day daily close array is missing from Redis at 09:15 IST, the engine must trigger an emergency REST API call to fetch the data before allowing any trades.
* **Math Stability**: All division operations (e.g., PCR, RV) must include an epsilon offset ($1e-9$) to prevent `ZeroDivisionError`.

### **6. Production Validation (The "Certify" Test Case)**

* **Setup**: Manually inject a fake `NIFTY_14D_CLOSE` array with low volatility and set `LIVE_ADX:NIFTY50` to `12`.
* **Action**: Start the engine and verify via Redis CLI (`HGET hmm_regime_state NIFTY50`).
* **Success Criteria**: The output must show `regime: "RANGING"`. Then, change `LIVE_ADX` to `30`. Within 5 seconds, the Redis state must change to `regime: "TRENDING"`.

---



**Component 2: The Meta-Router & Sizing Engine (`meta_router.py`)**. This is the system's "Chief Financial Officer," responsible for capital efficiency and converting strategy signals into multi-leg execution baskets.

### **1. Functional Requirements**

* **Multi-Leg Basket Generation**: The engine must convert high-level strategy intents into specific **JSON baskets** containing up to 4 legs for `POSITIONAL` and `ZERO_DTE` strategies.
* **Unique Relational Tagging**: Every basket must be stamped with a `parent_uuid` at inception to allow the `LiquidationDaemon` and `OrderReconciler` to track the legs as a single atomic unit.
* **Lifecycle Classification**: The router must append a `lifecycle_class` tag (`KINETIC`, `POSITIONAL`, or `ZERO_DTE`) to every order payload to dictate the downstream exit physics.
* **JIT Strike Resolution**: For option strategies, the router must resolve the current **ATM (At-The-Money) strike** and select the appropriate OTM (Out-Of-The-Money) strikes based on Delta targets (e.g., 15 Delta for Iron Condors) before dispatching.

### **2. Capital Allocation & Sizing Guards (₹8,00,000 Portfolio)**

| Constraint | Specification | Institutional "Why" |
| --- | --- | --- |
| **Max Portfolio Heat** | **25% ** | Limits total open risk at any single moment to prevent catastrophic loss. |
| **Hedge Reserve Tranche** | **15% ** | Permanently walls off "dry powder" strictly for the **Margin Waterfall** and Delta hedging. |
| **Sizing Formula** | **ATR + Half-Kelly** | Uses `MAX_RISK_PER_TRADE / (ATR * Multiplier)` to ensure position size is normalized by market volatility. |
| **Unsettled Funds Veto** | **T+1 Logic** | Excludes intraday realized profits from new sizing calculations as they are not settled until the next day. |

### **3. Strategic Veto Logic (The "Safety Valves")**

* **Cross-Index Divergence Check**: The router must block new entries if **NIFTY50** and **BANKNIFTY** exhibit a price divergence of $> 2.0\%$, signaling a "Regime Fracture" where standard correlations break down.
* **VPIN Toxicity Veto**: Any `POSITIONAL` signal must be blocked if the live **VPIN exceeds 0.80**, as this indicates high volume toxicity and an imminent "toxic" breakout that could shred a Delta-neutral spread.
* **Regime-Strategy Lock**: The router must strictly enforce the mapping:
* **Iron Condor** $\rightarrow$ Authorized ONLY in `High Volatility Chop` or `RANGING` regimes.
* **Gamma Momentum** $\rightarrow$ Authorized ONLY in `TRENDING` regimes.

### **4. Performance & Failure Scenarios**

* **Double-Spending Prevention**: The engine must use **Redis LUA scripts** (`LUA_RESERVE`) to atomically check and lock margin before dispatching an order.
* **Broker Margin Sync**: Before sizing any trade, the router must pull the **Live Ledger Truth** from the broker API. If the broker's cash balance is lower than the local database, the broker's value must be used for sizing.
* **Partial Payload Recovery**: If the router generates a 4-leg basket but the system crashes before dispatch, the `Pending_Journal` in Redis must allow for a sub-second recovery of the intent upon reboot.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Set the global `AVAILABLE_MARGIN_LIVE` to ₹8,00,000 and trigger a mock `IronCondor` signal.
* **Action**: Observe the generated JSON payload in the ZMQ `ORDERS_LIVE` stream.
* **Success Criteria**:
1. The payload contains exactly **4 legs** with unique `symbol` tokens.
2. A `parent_uuid` and `lifecycle_class: "POSITIONAL"` are present.
3. The Redis `AVAILABLE_MARGIN_LIVE` has been reduced by the exact margin amount required for the hedged spread.
4. Verify that if a second identical signal is fired within 500ms, it is vetoed as a **Double-Tap**.








**Component 3: The Execution Bridge & JIT Data (`live_bridge.py` / `data_gateway.py`)**. This component is the system's "Physical Muscle," managing the sub-millisecond interface with the broker and ensuring technical order integrity.

### **1. Functional Requirements**

* **Atomic Basket Dispatch**: The bridge must accept the multi-leg JSON payloads from the Meta-Router and fire them to the broker as a single logical unit.
* **Strict Buy-First Sequencing**: To prevent "Naked Leg" margin rejections, the bridge must strictly execute `BUY` legs (long wings) first and confirm their fill status before dispatching `SELL` legs (short strikes).
* **Just-In-Time (JIT) Subscriptions**: Upon receiving a `POSITIONAL` intent, the `data_gateway` must dynamically subscribe to the specific OTM option tokens via the `FEED_CMD` channel before the order is fired to ensure the `LiquidationDaemon` has immediate price visibility.
* **Token Bucket Rate Limiting**: The bridge must enforce a hard limit of **8 requests per second** to the broker API to prevent account suspension or 429 errors.

### **2. Technical & Performance Constraints**

* **Internal Latency**: Total time from ZMQ intent receipt to REST API dispatch must be **< 20ms**.
* **Order Persistence (Journaling)**: Every intent must be written to the `Pending_Journal` in Redis *before* being sent to the broker; the record is only cleared upon receiving a terminal `FILLED` or `REJECTED` status.
* **Adaptive Limit Chasing**: For OTM legs, the bridge must use a **5-loop "Chaser" logic**, modifying the limit price by one tick every 500ms to ensure fills in thin liquidity without hitting the `MAX_SLIPPAGE_PCT` cap.

### **3. Capital & Risk Guards**

* **Slippage Circuit Breaker**: If the realized slippage on a fill exceeds **0.10% of the asset price**, the bridge must publish a `SLIPPAGE_HALT` and block all new originations for 60 seconds.
* **Partial Fill Rollback**: If a multi-leg basket is only partially filled after 3 seconds, the bridge must immediately fire market orders to close the partially executed legs and return the account to a flat state.
* **Margin Check Pre-Flight**: The bridge must perform a final `LUA_RESERVE` check against live broker margins immediately before dispatching to prevent "Insufficient Funds" rejections due to intraday MTM fluctuations.

### **4. Failure Scenarios & Resilience**

* **Ghost Fill Recovery**: Every 15 minutes, the bridge must reconcile its local `portfolio` table with the broker's `get_positions()` API. Any "orphan" positions found at the broker must be instantly liquidated.
* **API 400 (Circuit Limit) Handling**: If the broker rejects an order due to exchange price bands, the bridge must "nudge" the limit price by 0.1% toward the current spot and retry twice before aborting.
* **Stale Feed Protection**: If the `data_gateway` detects a >1000ms delay in the index tick stream, it must broadcast a `FEED_STALE` signal, causing the bridge to block all new entry dispatches until the feed is reset.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Initiate a 4-leg **Iron Condor** intent in a simulated "Thin Liquidity" environment (high bid-ask spreads).
* **Action**: Observe the sequencing and chaser behavior in the logs.
* **Success Criteria**:
1. Verify that `BUY` legs were fired and confirmed *before* `SELL` legs appeared in the API logs.
2. Verify the `Pending_Journal` entry was created and correctly cleared after the final fill.
3. Manually fail the 4th leg of the basket; verify that the bridge automatically triggered an **inverse market order** to close the 3 successfully filled legs.
4. Verify that total API calls did not exceed the 8 req/sec bucket limit.










**Component 4: The Liquidation Daemon & Margin Waterfall (`liquidation_daemon.py`)**. This component is the system's "Security Guard," responsible for defending the portfolio against structural breaches and managing the Delta-neutrality of your positional spreads.

### **1. Functional Requirements**

* **Bifurcated Risk Logic**: The daemon must apply different exit physics based on the `lifecycle_class` tag: `KINETIC` trades follow ATR-based trailing stops, while `POSITIONAL` trades follow Greek and structural barriers.
* **Continuous Portfolio Hydration**: On startup and every 60 seconds, the daemon must hydrate its internal state from the `portfolio` table to ensure it is monitoring all live positions across system restarts.
* **Net Delta Monitoring**: For every index (Nifty, BankNifty, Sensex), the daemon must calculate the **Net Delta** of the combined `POSITIONAL` spreads by summing the individual leg deltas fetched from the `market_sensor`.
* **Vertical Time Barrier**: The daemon must monitor the **Days to Expiry (DTE)**; any `POSITIONAL` spread must be forcefully liquidated if **DTE < 3** to avoid the high-gamma "Gamma Trap" of expiry week.

### **2. The Margin Waterfall Protocol (±0.15 Delta Band)**

If an index's **Net Delta** breaches the **±0.15** threshold, the daemon must execute the following prioritized waterfall to restore neutrality:

| Step | Action | Logic |
| --- | --- | --- |
| **1. Future Hedge** | **Short/Long Micro-Future** | Check `HEDGE_RESERVE_LIVE`. If margin $\ge$ ₹1,00,000, fire a future hedge to neutralize Delta instantly. |
| **2. Untested Roll** | **Roll Untested Side** | If future margin is unavailable, roll the "untested" (safe) side of the Iron Condor closer to the spot to collect credit and offset Delta. |
| **3. Pro-Rata Slash** | **Protocol C (25% Cut)** | If Delta remains $\ge$ 0.25 or margin utilization exceeds 90%, liquidate 25% of the total position size to collapse the SPAN requirement. |

### **3. Performance & Exit Constraints**

* **Evaluation Frequency**: The daemon must evaluate the entire portfolio state every **1 second**.
* **Strict Exit Sequencing**: When liquidating a spread, the daemon must command the bridge to **close Short legs first**, wait for fill confirmation, and then close the Long wings to avoid temporary margin spikes or naked short penalties.
* **Stagnation Exit (Kinetic Only)**: For `KINETIC` scalps, the daemon must trigger a `THETA_STALL` exit if the price remains within a 0.1 ATR band for more than 300 seconds.

### **4. Capital & Risk Guards**

* **Dynamic SPAN Watchdog**: If the broker's live `margin_utilized` exceeds **90%** of total capital, the daemon must block all new `KINETIC` entries and initiate Step 3 of the Waterfall (Pro-Rata Slash) on `POSITIONAL` trades.
* **Take-Profit Cap**: For `POSITIONAL` spreads, the daemon must trigger a full exit when the total net premium decay reaches **60%** of the initial credit received.
* **Hard Day-Loss Limit**: If the total realized + unrealized MTM loss for the day exceeds **2% of capital (₹16,000)**, the daemon must broadcast a `SQUARE_OFF_ALL` command and lock the system.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Deploy a mock Nifty Iron Condor. Manually manipulate the Nifty Spot price in Redis to simulate a move toward the Short Call strike.
* **Action**: Observe the `liquidation_daemon` as the Net Delta hits +0.16.
* **Success Criteria**:
1. Verify the daemon first attempts to fire a **Nifty Future Short** order using the `HEDGE_RESERVE`.
2. Simulate "Low Margin" in the reserve; verify the daemon instead triggers a **Roll Up** of the Put wing.
3. Confirm that throughout the adjustment, the Long legs of the spread were never closed before the Shorts.
4. Verify the `parent_uuid` remained consistent across all adjustment trades in the audit log.









**Component 5: State Persistence & Transaction Integrity (`order_reconciler.py` / DB)**. This component is the system's "Internal Auditor," ensuring that the distributed state of the broker, the database, and the strategy engines remains in perfect synchronization.

### **1. Functional Requirements**

* **Atomic Intent Journaling**: Every trade intent generated by the Meta-Router must be written to a `Pending_Journal` in Redis before the `live_bridge` initiates any API calls.
* **Basket State Machine**: The reconciler must track the lifecycle of every `parent_uuid` through four mandatory states: `PENDING` $\rightarrow$ `PARTIAL` $\rightarrow$ `COMPLETE` (or `REJECTED`).
* **Stale Order Watchdog**: The reconciler must scan the `Pending_Journal` every 500ms for any order intent that hasn't reached a terminal state within **3 seconds**.
* **Double-Entry Bookkeeping**: Every filled order must result in two simultaneous writes: one to the `trades` table (historical audit) and one to the `portfolio` table (live risk) using an atomic database transaction.

### **2. Transaction Integrity & Rollback Protocols**

In the event of an execution anomaly, the reconciler must enforce the following hard rules:

| Scenario | Action | Technical Logic |
| --- | --- | --- |
| **Partial Fill (Long)** | **Force Fill** | If a protective wing is only 50% filled after 3 seconds, fire an aggressive **Market Order** to secure the remaining quantity immediately. |
| **Partial Fill (Short)** | **Rollback** | If the short legs fail but long wings are filled, the reconciler must command the bridge to **liquidate the orphans** to prevent theta-bleed. |
| **API Timeout** | **Verify & Sync** | If the bridge loses connection mid-request, the reconciler must poll the broker's `get_order_book()` to determine if the order reached the exchange before retrying. |
| **Ghost Position** | **Orphan Kill** | If a position exists at the broker but not in the `portfolio` table, it must be terminated within 60 seconds of detection. |

### **3. Database Schema & Ledger Truth**

* **Relational Integrity**: The `portfolio` table must use the `parent_uuid` as a foreign key to ensure that no leg can exist in the database without an associated parent strategy intent.
* **Audit Metadata**: Every trade record in TimescaleDB must include `audit_tags` containing the specific `heuristics` (ADX, RV, PCR) that were active at the millisecond of entry.
* **Row-Level Locking**: All updates to the `portfolio` table must utilize `SELECT FOR UPDATE` to prevent race conditions during simultaneous `KINETIC` and `POSITIONAL` liquidations.

### **4. Failure Scenarios & Resilience**

* **Cold Boot Hydration**: Upon system restart, the reconciler must perform a **Triple-Sync**: (Broker Positions $\leftrightarrow$ Database Portfolio $\leftrightarrow$ Redis Active State). Any discrepancy must trigger a system halt and manual intervention alert.
* **Dead-Letter Queue**: If a rollback command fails three times, the `parent_uuid` must be moved to a `CRITICAL_INTERVENTION` queue in Redis, and a persistent "System Failure" alarm must be sent via Telegram.
* **Storage Exhaustion**: If the database write latency exceeds 200ms (due to disk I/O), the reconciler must switch to **Memory-Only Mode**, caching trades in Redis until the DB recovers.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Use the `paper_bridge` to fire a 4-leg Iron Condor.
* **Action**: Manually kill the `data_gateway` process after the first 2 legs are filled.
* **Success Criteria**:
1. Verify the `order_reconciler` detected the stale `parent_uuid` within **3 seconds**.
2. Verify the reconciler issued a **Rollback Command** to liquidate the 2 filled legs.
3. Confirm the `portfolio` table is empty and the `trades` table correctly marked the event as `FAILED_BASKET_ROLLBACK`.
4. Verify a Telegram alert was dispatched with the `correlation_id` of the failed basket.



---



**Component 6: Operational Controller & EOD Guards (`system_controller.py`)**. This component acts as the "General Manager," overseeing the daily schedule, regulatory compliance, and terminal safety sequences to ensure the system starts and stops with absolute discipline.

### **1. Functional Requirements**

* **Three-Stage EOD Shutdown**: The controller must execute a hard-coded time-based liquidation sequence:
* **15:00 IST**: **Origination Halt**. Block all new entries across all strategy engines.
* **15:20 IST**: **Kinetic Square-Off**. Identify all positions with `lifecycle_class: "KINETIC"` and fire market-exit commands.
* **16:00 IST**: **Final Cleanup**. Close all remaining connections and trigger the GCP VM shutdown.


* **T-1 Expiry Calendar Sweep**: Every day at **15:15 IST**, the controller must scan the `portfolio` table for `POSITIONAL` trades whose `expiry_date` is the next business day. These must be liquidated to prevent overnight margin expansion or physical settlement risk.
* **Preemption Protection**: The controller must poll the GCP metadata server every 5 seconds; if a "Spot Preemption" signal is detected, it must trigger an emergency `SQUARE_OFF_ALL` and flush the Redis journal to NVMe within 1 second.

### **2. Regulatory & Capital Compliance (SEBI/NSE Rules)**

* **SEBI Quarterly Settlement Check**: At the 09:10 IST boot-up, the controller must verify `live_cash_balance > MINIMUM_OPERATING_CAPITAL`. If the balance is low (indicating a regulatory funds sweep), it must block all trading and alert the user.
* **Unsettled Premium Filter**: The controller must ensure the `MarginManager` ignores any intraday realized profits when calculating available margin for new `POSITIONAL` entries, as these funds are not settled (T+1) for margin purposes.
* **50:50 Cash-to-Collateral Guard**: For any multi-day position, the controller must verify that the `cash_margin` component is at least 50% of the total margin required; if not, it must cap the position size to avoid penal interest rates.

### **3. Performance & Safety Constraints**

* **Heartbeat Watchdog**: The controller must monitor the `daemon_heartbeats` Redis hash. If the `DataGateway` or `MarketSensor` stays `DEAD` for >15 seconds during market hours, the controller must trigger a global `SYSTEM_HALT`.
* **Exchange Health Monitor**: The controller must track the incoming tick timestamps. If no new ticks are received for >500ms across all indices, it must assume an exchange halt and suspend all outgoing order routing.
* **Ghost State Reconciliation**: Every 15 minutes, the controller must invoke the `_hard_state_sync()`, comparing the broker's position ledger with the local TimescaleDB to kill any "orphan" trades that bypass the standard reconciler.

### **4. Failure Scenarios & Resilience**

* **Database IO Wait**: If the TimescaleDB write latency exceeds a critical threshold, the controller must pause the `DataLogger` and prioritize the `LiveBridge` execution logs to ensure trade safety over history.
* **Network Brownout**: If the controller loses internet connectivity, it must utilize a local TTL-based "Dead Man's Switch" that triggers a `SUSPEND` state if no "Heartbeat ACK" is received from the cloud for 2 minutes.
* **API Rate-Limit Overflow**: If the `live_bridge` reports a `429 Too Many Requests` error, the controller must initiate a 30-second "Cool Down" period where all non-liquidation orders are blocked.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Run the system with active `KINETIC` and `POSITIONAL` trades in a paper environment. Set the system clock to **15:19:50 IST**.
* **Action**: Observe the system behavior as the clock hits 15:20:00.
* **Success Criteria**:
1. Verify that all `KINETIC` trades were closed automatically at 15:20.
2. Verify that `POSITIONAL` trades (expiring in >1 day) were **not** closed and remain in the `portfolio` table.
3. Confirm that the `MarginManager` correctly deducted the 15% `Hedge Reserve` from the total available capital at the start of the session.
4. Simulate a `DataGateway` crash; verify the `SystemController` detected the dead heartbeat and triggered a `SYSTEM_HALT` within 15 seconds.



---



We are now establishing the institutional-grade requirements for **Component 7: Observability & Citadel-Style Dashboard (`cloud_publisher.py` / UI)**. This component is the system's "Vision," providing real-time transparency into the mathematical health, risk exposure, and execution performance of the ₹8,00,000 portfolio.

### **1. Functional Requirements**

* **Unified Risk Visualization**: The dashboard must provide a real-time, consolidated view of the **Portfolio Greeks** (Delta, Gamma, Theta, Vega) aggregated by index (Nifty, BankNifty, Sensex).
* **Regime State Broadcast**: The UI must display the current Heuristic Engine output for each index, highlighting the active regime (`TRENDING`, `RANGING`, `CHOP`, `CRASH`) and the underlying heuristics (ADX, RV, PCR).
* **Bid-Ask Spread Heatmap**: To monitor "Freak Trade" risk, the system must visualize the live bid-ask spreads of the ATM and OTM options currently held in the portfolio.
* **Asynchronous Alert Pipeline**: Critical system events (Order Fills, Margin Waterfall Triggers, System Halts) must be pushed to the **Telegram Alerter** within 1 second of occurrence.

### **2. Performance & Data Constraints**

* **Cloud Discovery Pulse**: Every 60 seconds, the `cloud_publisher` must update the Firestore `system/metadata` with the current VM external IP and P&L status to allow the dashboard to find the server dynamically.
* **Heartbeat Frequency**: The dashboard must refresh its "System Health" status every 5 seconds, pulling from the `daemon_heartbeats` Redis hash to identify dead processes.
* **EOD Data Lake Sync**: At 15:35 IST, the publisher must convert the day's `market_history` and `trades` into compressed `.parquet` files and upload them to Google Cloud Storage for historical analysis.

### **3. Strategic "Edge" Indicators (The Citadel Stack)**

| Indicator | Logic | Institutional "Why" |
| --- | --- | --- |
| **IV/RV Spread** | $IV_{ATM} - RV_{10D}$ | Visualizes the "Volatility Risk Premium" to confirm if selling an Iron Condor has a statistical edge. |
| **Delta Drift** | Current Net Delta vs. $\pm 0.15$ | A gauge showing how close the current positional spread is to triggering a **Margin Waterfall** hedge. |
| **Slippage Audit** | Fill Price vs. Mid-Price | Displays the "Execution Friction" in real-time to alert the user if the broker's liquidity is degrading. |
| **Portfolio Heat** | Margin Used / Capital | A radial gauge representing the percentage of total capital currently at risk. |

### **4. Failure Scenarios & Resilience**

* **Network Jitter Resilience**: The cloud publisher must use an **Async Sink** for logging; if the connection to Google Cloud is slow, it must drop UI updates to ensure it never blocks the tick-processing or order-execution threads.
* **Silent Publisher Recovery**: If Firestore becomes unreachable, the publisher must cache critical alerts in the Redis `telegram_alerts` queue and retry the cloud write every 30 seconds.
* **Stale Dashboard Veto**: The UI must display a "DISCONNECTED" overlay if the system timestamp in the metadata is $> 60$ seconds old, preventing the user from making manual interventions based on stale data.

### **5. Production Validation (The "Certify" Test Case)**

* **Setup**: Start the full K.A.R.T.H.I.K. stack and fire a mock **Nifty Iron Condor**.
* **Action**: Observe the dashboard and the Telegram channel simultaneously.
* **Success Criteria**:
1. Verify the **Portfolio Delta** correctly sums to a near-zero value (Delta Neutral).
2. Check that the **IV/RV Spread** matches the math generated in the `hmm_engine.py` audit log.
3. Confirm the Telegram bot pushed a "Position Opened" notification within 1 second of the order fill.
4. Manually kill the `data_gateway`; verify the dashboard "Health" widget turns **RED** within 15 seconds.



---

### Strategies 
---

### **1. Gamma Momentum Hunter (`S1`)**

* **Mission**: Capitalize on explosive directional moves amplified by dealer delta-hedging.
* **Lifecycle**: `KINETIC` (Intraday only; squared off at 15:20 IST).
* **Regime Authorization**: `TRENDING` (ADX > 25).
* **Entry Logic**:
* **Leading Signal**: Log-OFI Z-Score > 2.0 (aggressive buying/selling imbalance).
* **Contextual Trigger**: Negative GEX (Slippery market) + ADX > 25.
* **Instrument**: ATM (At-The-Money) Naked Calls or Puts.


* **Exit Logic**:
* **Target**: 1.2 * ATR.
* **Stop-Loss**: 1.0 * ATR.
* **Time-Veto**: Stall exit if price doesn't move for 300 seconds.



### **2. The Institutional Iron Condor (`S6`)**

* **Mission**: Harvest "Volatility Risk Premium" (Theta) in stable or high-volatility sideways markets.
* **Lifecycle**: `POSITIONAL` (Multi-day; carries overnight).
* **Regime Authorization**: `High Volatility Chop` or `RANGING`.
* **Entry Logic**:
* **Volatility Edge**: IV - RV > 3.0 (Ensures you are selling "expensive" premium).
* **Structure**: 4-leg basket (Short OTM Call/Put at 15 Delta; Long OTM wings at 5 Delta).


* **Management (The Waterfall)**:
* **Primary Guard**: Net Delta ±0.15 Band.
* **Adjustment**: Neutralize via Futures or rolling the untested side.


* **Exit Logic**:
* **Profit Target**: 60% of initial net credit received.
* **Vertical Barrier**: Exit at 15:15 IST if DTE < 3.



### **3. Directional Credit Spreads**

* **Mission**: Profit from a directional bias with a built-in safety net (Vertical Spreads).
* **Lifecycle**: `POSITIONAL` (Multi-day).
* **Regime Authorization**: `TRENDING`.
* **Entry Logic**:
* **Bias Confirmation**: ADX > 25 + PCR alignment (PCR > 1.0 for Bullish; < 0.8 for Bearish).
* **Structure**: 2-leg basket (Sell 25 Delta OTM; Buy 15 Delta OTM).


* **Exit Logic**:
* **Target**: 70% of max possible profit.
* **Stop-Loss**: 2x the initial credit received (Risk/Reward 1:1).



### **4. 0DTE Tastytrade (The Expiry Harvester)**

* **Mission**: Capture rapid decay on Expiry Day while staying Delta-neutral.
* **Lifecycle**: `ZERO_DTE` (Expiry day only; 09:30 to 15:15 IST).
* **Regime Authorization**: `RANGING` or `High Volatility Chop`.
* **Entry Logic**:
* **Timing**: Fires only on Wednesdays/Thursdays between 09:30 and 10:30 IST.
* **Structure**: High-theta Iron Butterfly or Iron Condor centered at ATM.


* **Exit Logic**:
* **Profit Target**: 50% of credit received.
* **Aggressive Stop**: Exit if underlying moves > 0.5% from inception spot price.



---

### **Strategic Veto Matrix (Global Guards)**

| Veto Type | Condition | Action |
| --- | --- | --- |
| **Market Storm** | 15 mins before Tier-1 Macro News | Block ALL new entries. |
| **Index Fracture** | Nifty/BankNifty Divergence > 2% | Pause all `POSITIONAL` entries. |
| **VPIN Panic** | VPIN > 0.80 | Immediate VETO on all premium selling. |
| **Low Vol Trap** | IV < 12% | Block all `POSITIONAL` spreads (Not enough premium). |

---
