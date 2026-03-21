@
# Functional Documentation
Comprehensive guide to the system's business and quantitative logic.
---
@
---
### [EXHAUSTIVE] core/__init__.py (Package Entrypoint)

**Functional Purpose**: 
This file is the 'Welcome' sign for the `core` folder. In the Python programming language, a folder isn't considered a 'Package' (a group of related tools) unless it has this file inside it. Even if it's empty, it tells the rest of the bot that it can safely 'Import' (borrow) tools from this folder.

**Quant Finance Context**: 
In a trading system, organization is safety. By marking this folder as a package, we ensure that common tools like 'Alerts' and 'Auth' can be shared across all the different 'Daemons' (the little bots that handle trading) without each one needing its own copy of the code.

**Exhaustive Method Logic**:
- Currently, this file contains only a comment. It does not perform any active tasks, but its mere presence activates the modular features of the system.

**Variable Dictionary**:
- None (Empty file).

---
### [EXHAUSTIVE] core/alerts.py (Centralized Alerting)

**Functional Purpose**: 
The alerting system is the 'voice' of the trading bot. Its primary job is to ensure that critical events (e.g., a massive price drop, a strategy failure, or a margin warning) are immediately transmitted to the human trader or other microservices without slowing down the bot's core execution.

**Quant Finance Context**: 
In high-frequency or algorithmic trading, 'Slippage' isn't just about price—it's also about 'Information Latency.' If a bot hits a stop-loss or runs out of capital, every second of delayed notification can result in financial loss. This component ensures that notifications are sent 'out-of-band' (in a separate lane) so the bot can keep trading.

**Exhaustive Method Logic**:
1. `get_instance()`: Implements a **Singleton Pattern**. This ensures that the entire bot uses only one alerting object, preventing the computer from opening hundreds of redundant connections to the database.
2. `__init__`: Sets up the address for the Redis database. It does **not** connect immediately; it waits until a real alert is sent (Lazy Loading) to save resources during startup.
3. `_ensure_redis()`: A safety gate that checks if the connection to the database is alive. If the database is down, it logs an error but **does not crash the bot**, allowing trading to continue even if notifications are broken.
4. `alert(text, alert_type, **kwargs)`: 
   - Receives the message (a string of text).
   - Adds a precise 'timestamp' (ISO format) so the trader knows exactly when the event happened.
   - Packages the data into a JSON 'Letter' and pushes it into a specific mailbox (the `telegram_alerts` list in Redis).
5. `send_cloud_alert()`: A simple 'shortcut' function that any other part of the bot can call easily without knowing how the internal alerting machinery works.

**Variable Dictionary**:
- `_redis`: The actual connection tube to the database (Redis).
- `text`: The human-readable message (e.g., 'In-Flight Strategy REJECTED').
- `alert_type`: Categorizes the alert (e.g., 'SYSTEM', 'TRADE', 'RISK').
- `payload`: The final combined package of message + time + category.

---
---
### [EXHAUSTIVE] core/auth.py (Authentication & Config)

**Functional Purpose**: 
This is the 'Vault' of the system. It handles all the secret passwords and connection addresses (like for Redis and the Database) so that other parts of the bot don't have to worry about security details.

**Quant Finance Context**: 
Security is paramount in trading. Hardcoding database passwords inside the trading logic is a major risk. This component pulls those secrets from a protected environment (`.env`) and ensures that every part of the system uses the 'Official' correct credentials.

**Exhaustive Method Logic**:
1. `get_redis_url()`: 
   - Checks the `.env` file for `REDIS_HOST` and `REDIS_PASSWORD`.
   - If there is no password (common for local testing), it creates a simple link (e.g., `redis://localhost:6379`).
   - If there IS a password (standard for cloud servers), it creates a secure, authenticated link. This is chosen specifically to support both local development and cloud production without changing code.
2. `get_db_dsn()`: Similar to the Redis function, it creates a long 'Postgres Address' (DSN) that includes the username, password, and database name.
3. `get_db_config()`: Instead of a long string, it returns a 'Dictionary' (a structured list of settings). This is used by the more modern database tools in the bot that need the host and user separated out for better control.

**Variable Dictionary**:
- `HEDGE_RESERVE_PCT`: A hardcoded constant (0.15 or 15%). This is used to 'reserve' extra money for hedges, ensuring the bot always has a buffer for complex trades.
- `REDIS_HOST`: The location of the Redis computer.
- `DB_USER`: The username for the database (e.g., 'trading_user').

---
### [EXHAUSTIVE] core/db_retry.py (Database Resilience)

**Functional Purpose**: 
This is the 'Bodyguard' for the database. In a complex system with many moving parts (like Docker containers), sometimes the database isn't ready when the bot starts, or a quick network glitch disconnects it. This component handles those 'blips' automatically so the bot doesn't crash.

**Quant Finance Context**: 
Data is the lifeblood of quant trading. If the bot can't save a trade to the database because of a 1-second network hiccup, you lose the 'Audit Trail' of your money. This component ensures that every attempt to talk to the database is 'Robust' and persistent.

**Exhaustive Method Logic**:
1. `with_db_retry(max_retries, backoff)`: 
   - This is a 'Decorator' (a wrapper). Think of it as a safety helmet you put on any function that talks to the database.
   - It tries to run the function. If it fails due to a connection error, it waits a bit (`backoff`) and tries again.
   - Importantly, it can call `_reconnect_pool()` on the parent object. This means if the initial 'tube' to the database is broken, it throws it away and builds a new one automatically.
2. `robust_db_connect(dsn, max_retries, timeout)`:
   - Used during the bot's 'Startup Sequence.' 
   - Instead of giving up if the database isn't awake yet, it waits (e.g., 2s, then 4s, then 6s...) for up to 10 attempts. This is crucial for environments where the Bot sometimes starts before the Database is fully 'Healthy.'

**Variable Dictionary**:
- `max_retries`: The number of second chances given to a database call (Default: 3 for operations, 10 for startup).
- `backoff`: The multiplier for waiting. Each failure makes the bot wait slightly longer to give the database time to breathe.
- `dsn`: The 'Address' of the database (Data Source Name).

---
---
### [EXHAUSTIVE] core/execution_wrapper.py (Multi-Leg Execution)

**Functional Purpose**: 
The 'Execution Specialist.' Many professional trading strategies involve multiple 'legs' (e.g., buying one option and selling another at the same time). This component handles the complex dance of placing those orders in the correct order to save money on 'Margin' (collateral).

**Quant Finance Context**: 
Margin efficiency is key to profitability. If you place a 'Short' (Sell) order before your 'Long' (Buy) order, the broker might think you are taking a huge risk and block the trade or overcharge you. This wrapper ensures the 'Buy' leg always goes first to 'Cover' the risk for the 'Sell' leg.

**Exhaustive Method Logic**:
1. `_get_best_price()`: A simple helper that asks the broker, 'What is the cheapest price I can get for this stock right now?'
2. `adaptive_limit_chase()`: 
   - This is a 'Smart' order. Instead of just saying 'Buy at 100,' if the market moves to 100.05, the bot 'Chases' it by moving its bid slightly higher (0.05 at a time).
   - It has a `max_slippage_pct` (10%) limit. If the price moves too far away, it kills the trade and triggers a **SLIPPAGE_HALT** (Panic) to prevent the bot from buying at a terrible price.
3. `execute_legs(orders, sequential)`:
   - **Step 1: Sorting**: It automatically moves 'BUY' orders to the front of the list.
   - **Step 2: Reconciler Ping**: It tells the 'Auditor' (OrderReconciler) that a new group of trades (a Basket) is starting.
   - **Step 3: Execution**: It fires the orders. If it's a multi-leg trade, it waits up to 5 seconds for the first leg to be 'Confirmed' before firing the next one. This 'Confirmation Wait' is the gold standard for margin safety.

**Variable Dictionary**:
- `MultiLegExecutor`: The brain that manages the order sequence.
- `max_slippage_pct`: The 'Tolerance.' How much extra are we willing to pay to get the trade done?
- `parent_uuid`: A unique 'Family Name' shared by all legs of the same strategy.
- `is_otm`: A flag for 'Out-of-the-Money' options, which are harder to trade and require the 'Chasing' logic.

---
### [EXHAUSTIVE] core/greeks.py (Black-Scholes Engine)

**Functional Purpose**: 
The 'Quant Engine.' This file contains the mathematical formulas used to price options and calculate 'Greeks'—the numbers that tell us how the price of an option will change when things like time, volatility, or stock price move.

**Quant Finance Context**: 
Options aren't just stocks; they have 'expiration dates' and 'volatility.' This file uses the industry-standard Black-Scholes model to predict exactly how much an option is worth. For example, 'Delta' tells us if an option is as risky as owning the stock itself.

**Exhaustive Method Logic**:
1. `d1()` and `d2()`: These are the core math 'Steps' inside the Black-Scholes formula. They calculate the probability that an option will finish 'in-the-money.'
2. `call_price()` and `put_price()`: Calculates the fair market value of an option right now.
3. `delta()`: Measures sensitivity to Price. (e.g., if Delta is 0.5, the option moves $0.50 for every $1 the stock moves).
4. `gamma()`: Measures sensitivity to Delta. It tells us how fast the Delta itself is changing (important for 'Gamma Scalping' strategies).
5. `vega()`: Measures sensitivity to Volatility (fear). If Vega is 5.0, the option price rises $5 if market fear goes up by 1%.
6. `theta()`: Measures sensitivity to Time. It tells us exactly how much money the option loses every night just by sitting in our account (Time Decay).

**Variable Dictionary**:
- `S`: Spot Price (The current price of the stock).
- `K`: Strike Price (The price we agreed to buy/sell at).
- `T`: Time (How many days/years are left until the option expires).
- `r`: Risk-free interest rate.
- `sigma`: Implied Volatility (The 'Fear' factor).

---
### [EXHAUSTIVE] core/health.py (Heartbeat Monitoring)

**Functional Purpose**: 
The 'Life Support' system. In a microservices system, you have many small programs running at once. If the 'Brain' (MetaRouter) dies, the bot could lose control. This component makes sure every program is 'Ticking' correctly.

**Quant Finance Context**: 
System failure is the greatest risk in algo-trading. If your 'Liquidation Daemon' (the one that stops losses) crashes, you might stay in a losing trade for hours. This file ensures that if any part of the bot stops 'Ticking,' the whole system can go into 'Safe Mode.'

**Exhaustive Method Logic**:
1. `HeartbeatProvider.run_heartbeat()`: 
   - Every 5 seconds, it writes the current time to a shared 'Bulletin Board' (Redis).
   - It's like a person saying 'I am here' every few seconds to prove they are alive.
2. `HealthAggregator.get_system_health()`:
   - This is the 'Doctor.' It looks at the Bulletin Board and checks if any daemon hasn't posted a 'Ticking' time for more than 15 seconds.
   - It calculates a 'Health Score' (e.g., 1.0 means everything is perfect, 0.5 means half the bot is broken).

**Variable Dictionary**:
- `Daemons`: A list of all the official parts of the bot (e.g., MarketSensor, MetaRouter).
- `LH: (Live Heartbeat)`: A special sticker put on every living daemon for the trader's dashboard.
- `alive_count`: A tally of all healthy programs.

---
### [EXHAUSTIVE] core/logger.py (Structured Logging)

**Functional Purpose**: 
The 'Black Box Recorder.' Every single thing the bot does—every decision, every mistake, and every trade—is recorded here in a format that both humans and computers can read easily.

**Quant Finance Context**: 
Trading requires a perfect 'Audit Trail.' If you lose money, you need to be able to go back into the records and find the exact millisecond the bot made a decision. This component uses 'JSON' (a structured format) so we can search these logs using computers later.

**Exhaustive Method Logic**:
1. `JsonFormatter.format()`:
   - Takes a normal log message (like 'Error!') and attaches a precise timestamp, the function name, the line number, and a 'Correlation ID' (a unique ID shared by all parts of a single trade).
2. `setup_logger()`:
   - Sets up two 'Paths': one to the computer screen (so the trader can watch live) and one to a file in the `logs/` folder (for the permanent record).
   - It uses a 'Rotating File,' which means it won't fill up the computer's hard drive—it keeps the last 5 days of history and deletes the old ones automatically.

**Variable Dictionary**:
- `RotatingFileHandler`: The mechanic that swaps out old log files for new ones.
- `log_entry`: The 'Packet' of information about a single event.

---
---
### [EXHAUSTIVE] core/margin.py (Atomic Margin Vault)

**Functional Purpose**: 
The 'Banker' of the system. In professional trading, you don't just 'spend money'; you 'reserve margin.' This component ensures that if the bot wants to place a trade, it has enough real cash or collateral to cover the risk. It uses heavy-duty security (LUA scripts) to make sure two strategies don't try to spend the same dollar at the same millisecond.

**Quant Finance Context**: 
Regulatory compliance (like SEBI's 50:50 cash-to-collateral rule) is hardcoded here. If your bot has 1 Crore in stocks (collateral) but 0 Cash, it cannot trade. This component enforces the 'Institutional Ratio'—if you don't have enough liquid cash to cover at least 50% of the risk, the trade is blocked. 

**Exhaustive Method Logic**:
1. `reserve(required_margin)`: 
   - Checks if Total Capital (Cash + Collateral) is enough.
   - Enforces the 50:50 rule: If cash is less than half the required amount, it fails (Error -2).
   - Smart Drain: It uses up 'Collateral' first (up to 50%) to save your 'Liquid Cash' for other needs.
2. `release(amount, original)`: 
   - When a trade ends, it puts the money back.
   - **Profit Rule**: Any money returned *above* the original amount is considered profit and is automatically converted to 'Cash' to increase liquidity.
   - **Ceiling Guard**: Prevents the bot from accidentally 'finding' money that doesn't exist (prevents account balance corruption).
3. `get_state()`: An instant 'X-Ray' of the vault showing exactly how much Cash and Collateral are available for both Live and Test (Paper) trading.
4. `sync_capital()`: Used to update the bot's bank balance from the broker's real screen once an hour to ensure the bot is in sync with reality.

**Variable Dictionary**:
- `CASH_COMPONENT`: Liquid money used for trading.
- `COLLATERAL_COMPONENT`: The value of stocks held in the account, used as a guarantee.
- `50:50 Breach`: A state where a trade is blocked because there's too little cash relative to stocks.

---
### [EXHAUSTIVE] core/mq.py (Messaging & Communication)

**Functional Purpose**: 
The 'Radio Tower.' The bot is made of many separate programs (microservices). This file allows them to talk to each other at high speed without getting their wires crossed. It handles 'Publishing' (broadcasting info) and 'Subscribing' (listening for info).

**Quant Finance Context**: 
Market data must move fast. When the 'Gateway' sees a price change, it 'Publishes' it. Any strategy that needs that price 'Subscribes' to it. This design ensures that adding a new strategy doesn't slow down the existing ones—they all just listen to the same broadcast.

**Exhaustive Method Logic**:
1. `create_publisher()`: Opens a 'TV Station' at a specific port (e.g., 5555 for Price Ticks).
2. `create_subscriber()`: Tunes a radio into specific 'Topics' (e.g., only listen for 'NIFTY' prices).
3. `send_json()`: Packages a message with a **Correlation ID**. This is like a 'Tracking Number' on a package—if a trade fails, you can use this ID to trace its journey through every part of the bot.
4. `NumpyEncoder`: A special translator that allows the bot to send complex math data (numbers from the 'Greeks' or 'HMM' engines) through the radio without breaking.

**Variable Dictionary**:
- `Ports`: A list of 'Channels' for different info (5555=Price, 5556=Orders, 5559=Commands).
- `Topics`: The 'Labels' on messages (e.g., 'TICK' for prices, 'FILL' for completed trades).

---
### [EXHAUSTIVE] core/network_utils.py (Connectivity Resilience)

**Functional Purpose**: 
The 'Safety Fuse.' When the bot talks to external things (like the Broker or a Cloud Server), the connection might be flaky. This component provides 'Circuit Breakers' and 'Backoffs' to stop the bot from panicking if the internet blips.

**Quant Finance Context**: 
'Thundering Herd' prevention. If the Broker's API goes down and 100 bots all try to reconnect every millisecond, they will crash the server. This component implements 'Exponential Backoff'—the bot waits 1s, then 2s, then 4s... giving the server time to recover.

**Exhaustive Method Logic**:
1. `exponential_backoff()`: 
   - A wrapper for functions that might fail. 
   - it retries up to 5 times, waiting longer each time.
2. `CircuitBreaker.call()`: 
   - Watches a connection. If it fails 5 times in a row, it 'Trips' (State: **OPEN**).
   - Once the circuit is open, the bot **stops trying** for 60 seconds. 
   - This prevents 'Wasteful Retries' and protects the system from burning out on a dead connection.

**Variable Dictionary**:
- `State: CLOSED`: Everything is normal.
- `State: OPEN`: Connection is dead; bot is waiting.
- `State: HALF_OPEN`: Bot is cautiously trying one single request to see if things are fixed.

---
### [EXHAUSTIVE] core/shared_memory.py (High-Speed Price Lane)

**Functional Purpose**: 
The 'Teleportation System.' Standard messaging (like ZMQ or Redis) can be 'slow' (taking 1-2 milliseconds). For ultra-fast trading, we use **Shared Memory**—a part of the computer's brain that two programs can look at simultaneously. No cables, no radios—just raw memory.

**Quant Finance Context**: 
Eliminating Latency. In the time it takes to send a message over a network, the price of a stock might have already changed. Shared memory allows the 'Tick Sensor' to see a price update the microsecond the 'Gateway' writes it.

**Exhaustive Method Logic**:
1. `write_tick(slot_index)`: 
   - Takes a price tick and 'Smashes' it into a specific 'Slot' in the computer's memory.
   - It uses a **Binary Format** (`struct`) so it's as small and fast as possible.
2. `read_tick(slot_index)`: 
   - Allows a strategy to 'Peek' at a specific slot to see the latest price of NIFTY or BANKNIFTY without asking anyone else.
3. `CORE_SYMBOLS`: A predefined map. NIFTY50 is always Slot 0, BANKNIFTY is always Slot 1. This 'Memory Map' is why the bot is so fast—it never has to search; it knows exactly where the data is.

**Variable Dictionary**:
- `SHM_NAME`: The 'Secret Name' of the memory chunk (`trading_ticks_shm`).
- `TICK_STRUCT_FORMAT`: The code for the binary layout (Price, Volume, Time, etc.).
- `hires_ts`: A microsecond-level clock used to measure exactly how many 'millionths of a second' it takes for data to travel.

---
---
### [EXHAUSTIVE] core/shm.py (Signal Vector Transport)

**Functional Purpose**: 
The 'Alpha Highway.' While the previous component handled raw prices, this one handles the 'Intelligence' (Signals). It transports a massive 'Vector' (a list of 30+ mathematical indicators like VPIN, OFI, and Greeks) from the Sensor to the Strategy Engine. It uses a 'Checksum' (a digital seal) to make sure the data wasn't corrupted during the high-speed transfer.

**Quant Finance Context**: 
A strategy needs more than just price; it needs 'Context.' This component delivers a complete 'Snapshot' of the market's mood, including volatility, volume pressure, and hidden 'Whale' activity. By using memory-mapping, it ensures the strategy is always looking at the absolute latest mathematical truth.

**Exhaustive Method Logic**:
1. `SignalVector` (Data Table): A huge list of every important number the bot calculates, such as:
   - `vpin`: Toxicity (is the market about to crash?).
   - `ofi_z`: Order Flow Imbalance (which way are the big players leaning?).
   - `iv_atm`: The current 'Fear' levels in the option market.
2. `ShmManager.write(signals)`: 
   - Takes all these numbers and 'Smashes' them into a single 1KB block of memory.
   - It calculates a **CRC Checksum** (it adds up all the numbers). This is a 'Safety Seal.'
3. `ShmManager.read()`: 
   - The Strategy Engine 'Peeks' at the memory.
   - **Staleness Check**: If the data is more than 1 second old, it ignores it.
   - **Integrity Check**: It recalculates the Checksum. If the 'Seal' is broken (the numbers don't add up), it throws the data away to avoid making a trade based on garbage data.

**Variable Dictionary**:
- `s_total`: The final 'Aggregated Alpha' score.
- `RegimeVector`: A smaller table focused on the 'Market Mood' (Trending, Ranging, etc.).
- `veto`: A master 'Kill Switch' baked into the signal vector.

---
---
### [EXHAUSTIVE] core/proto/messages.proto (Universal Language)

**Functional Purpose**: 
The 'Dictionary' of the system. Imagine if the Gateway called a price 'ticker' but the Strategy called it 'symbol.' The bot would get confused. This file prevents that by creating a 'Universal Standard' for how every single piece of data looks. It's like a blueprint for a message.

**Quant Finance Context**: 
Data integrity is critical. If a strategy expects a 'Price' but receives a 'Volume' because the messages got mixed up, it could place a catastrophic trade. This component uses 'Protocol Buffers' (Protobuf) to lock down the exact format of every message, ensuring that 'Price' is always a number and 'Symbol' is always text.

**Exhaustive Message Types**:
1. `TickData`: The standard packet for a live price update. It contains Symbol, Price, Bid/Ask (spread), and Volume. 
2. `AlphaSignal`: The packet for 'Brain Updates.' It contains the HMM market mood (Trending vs Ranging) and the 'Flow Toxicity' (is the market dangerous?).
3. `StrategyConfig`: The packet used to 'Remote Control' the bot. It tells a strategy to Wake up, Sleep, or use a specific amount of Capital.

**Variable Dictionary**:
- `Field IDs` (the numbers like =1, =2): These are like 'House Numbers.' They tell the computer exactly where to find each piece of data inside a message for maximum speed.
- `int64`: A very large whole number (used for Volume).
- `double`: A precise decimal number (used for Prices).

---
---
### [EXHAUSTIVE] daemons/cloud_publisher.py (Dashboard & Cloud Bridge)

**Functional Purpose**: 
The 'PR Agent' and 'Remote Control.' This daemon bridges the gap between the internal trading engine and the external Cloud Dashboard. It sends live updates so the trader can watch P&L from their phone and allows for emergency commands (like 'PANIC') to be sent back to the bot from anywhere.

**Quant Finance Context**: 
Operational oversight. A black-box system is a dangerous system. This component provides 'Total Visibility' into the bot's health, alpha scores, and margin usage. It also performs 'EOD Housekeeping'—saving all the day's trades and prices into a permanent vault (Google Cloud Storage) so they can be used for deep research later.

**Exhaustive Method Logic**:
1. `_init_cloud_clients()`: Securely connects to Google Cloud. If the internet or credentials fail, it gracefully switches to 'Local-Only' mode so the bot keeps trading safely without the cloud.
2. `_heartbeat_loop()`: 
   - Every 5 seconds, it gathers a 'Pulse' of the entire system.
   - It collects: **Live P&L** (real money), **Market Regime** (mood), **Portfolio Delta** (risk exposure), and **System Vetos** (why the bot is NOT trading).
   - It sends this 'Pulse' to the Google Firestore database for the dashboard.
3. `_command_watcher()`: 
   - Watches for 'Remote Orders' from the trader. 
   - **PANIC_BUTTON**: If pressed on the dashboard, this daemon immediately tells every other bot to 'SQUARE_OFF_ALL' (sell everything and exit).
   - **PAUSE/RESUME**: Allows the trader to stop new trades without killing the bot.
4. `_eod_snapshot()`: 
   - Precisely at 15:35 IST (after market close), it performs a 'Data Export.'
   - It converts millions of database rows into a small, fast format (Parquet) and teleports them to the cloud.

**Variable Dictionary**:
- `Firestore`: The 'Live Database' for the phone dashboard.
- `GCS (Google Cloud Storage)`: The 'Archive Vault' for long-term data.
- `PANIC_BUTTON`: A global emergency trigger.
- `realized_pnl_live`: The amount of actual money made or lost today.

---
---
### [EXHAUSTIVE] daemons/data_logger.py (Historical Archivist)

**Functional Purpose**: 
The 'Black Box Recorder.' This daemon spends its entire life listening to the market data "radio" and writing every single price and signal into the permanent database (TimescaleDB). This allows the quants to look back at any day in history to see why the bot made a move.

**Quant Finance Context**: 
High-fidelity backtesting. A strategy is only as good as the data it was trained on. This logger captures not just the 'Price,' but the 'Context' (CVD, VPIN, Z-Scores) at every millisecond, creating a rich dataset for PhD-level research.

**Exhaustive Method Logic**:
1. `start()`: Sets up the database 'Pool' and automatically updates the database table structure if new features (like 'ASTO') are added.
2. `MQ Subscriber`: Listens to the `MARKET_STATE` port. Every time a new set of signals is calculated, this daemon grabs a copy.
3. `_batch_writer()`: 
   - Efficiency Engine: Instead of writing one line at a time (which is slow), it collects updates in a 'Batch' (a bucket) for 10 seconds.
   - It then 'Flushes' the entire bucket into the database at once.
   - If the database is busy, it uses the 'Bodyguard' (Retry logic) to wait and try again.

**Variable Dictionary**:
- `batch`: The temporary holding area for records.
- `TimescaleDB`: A specialized version of Postgres optimized for millions of rows of time-data.
- `LOGGER_STOP`: A safety toggle in Redis used to stop recording during the 'Midnight Maintenance' window.

---
### [EXHAUSTIVE] daemons/hmm_engine.py (Regime Detection)

**Functional Purpose**: 
The 'Economic Meteorologist.' This daemon looks at the 'Weather' of the market. It doesn't care about the price of one stock; it cares about the *Mode* of the whole market. Is it a calm day (Ranging), a rainy day (Trending), or a hurricane (Volatile)?

**Quant Finance Context**: 
Regime-Awareness. A 'Mean Reversion' strategy works great when the market is RANGING, but it's a disaster when the market is TRENDING. This engine tells the 'MetaRouter' exactly what the current 'Vibe' is, so the bot can pick the right tools for the job.

**Exhaustive Method Logic**:
1. `_calculate_adx_approximation()`: Measures **Trend Strength**. If the number is high (>25), the market is moving fast in one direction.
2. `_calculate_realized_vol()`: Measures **Raw Fear**. It looks at the last 14 days and calculates the 'Annualized Standard Deviation.'
3. `classify_regime()`: 
   - Uses **Hysteresis** (Stability logic). It doesn't flip-flop back and forth. To enter a 'Trend,' you need a strong signal (>25). To leave a 'Trend,' the signal must drop heavily (<20).
4. `S26 (Persistence)`: Measures how long the market has been in this mood.
5. `S27 (Quality)`: A 'Confidence Score.' A 100% Quality score means the trend is undeniable.

**Variable Dictionary**:
- `s18_int`: The numerical code for market mood (0=Neutral, 1=Trend, 2=Range, 3=Volatility).
- `ADX`: The Average Directional Index (Trend power).
- `RV`: Realized Volatility (Historical Speed).
- `IV-RV Spread`: The gap between 'Expected Fear' and 'Actual Fear.'

---
### [EXHAUSTIVE] daemons/liquidation_daemon.py (Portfolio Guardian)

**Functional Purpose**: 
The 'In-Flight Safety System.' While the strategies try to buy low and sell high, this daemon does only one thing: **SELL.** Its only job is to protect your money by closing trades that have hit their 'Triple-Barrier' limits (Time, Price, or Loss).

**Quant Finance Context**: 
Automated Risk Management. Human traders often get 'Hopeful' and hold losing trades too long. This daemon has no 'Hope.' If a trade hits a Stop-Loss or a 'Regime Shift' (the market turns toxic), this daemon executes a cold, hard exit within milliseconds.

**Exhaustive Method Logic**:
1. `_check_stop_day_loss()`: The 'Master Fuse.' If the whole bot loses more than 2% of the total bank account in one day, this function kills everything and locks the doors.
2. `_check_hybrid_drawdown()`: Monitors 'Pairs.' If you are buying a future to hedge an option, it looks at both relative to each other. If the 'Net Loss' of the pair is too high, it closes both.
3. `_evaluate_barriers()` (The 5-Gate Exit Hierarchy):
   - **Gate 1: Hard Stop**: LTP hits the calculated Stop-Loss.
   - **Gate 2: Regime Shift**: The HMM engine (above) reports 'Volatile/Toxic.' The daemon exits immediately to avoid a crash.
   - **Gate 3: Hedge Waterfall**: If the risk (Delta) gets too high, it tries to 'Smooth' it out by buying Micro-Futures.
   - **Gate 4: Quality Decay**: If the signal 'Quality' (S27) drops but we are in profit, it takes the money and runs.
   - **Gate 5: Time Gate**: At 15:15 IST, it closes everything to avoid 'Overnight Risk.'
4. `_monitor_fill_slippage()`: Watches the 'Execution Quality.' If the broker is giving us bad prices (high slippage), it pauses all trading.

**Variable Dictionary**:
- `Triple-Barrier`: The standard Exit rule (Profit Target, Stop Loss, Time Limit).
- `SQUARE_OFF_ALL`: The 'Nuclear Option' command that clears the portfolio.
- `ATR (Average True Range)`: Used to calculate 'Volatile' stops—stops are wider on fast days and tighter on slow days.
- `parent_uuid`: The 'Tracking ID' for a complex multi-leg trade.

---
---
### [EXHAUSTIVE] daemons/market_sensor.py (Signal Engineering Engine)

**Functional Purpose**: 
The 'Eyes and Ears' of the system. This daemon takes raw, chaotic price ticks from the exchange and transforms them into clear, actionable 'Signals.' It answers questions like: "Is the big money buying?" (CVD), "Is the market atmosphere toxic?" (VPIN), and "Is this a real trend or a trap?" (ASTO).

**Quant Finance Context**: 
High-frequency alpha generation. In modern trading, just knowing the price isn't enough. You need to know 'Market Microstructure'—the hidden flow of orders. This sensor uses advanced math (like the Hurst Exponent) to determine if the market is likely to 'Mean Revert' (bounce back) or 'Breakout' (keep running).

**Exhaustive Method Logic**:
1. `_compute_worker()` (The Math Factory): 
   - Runs in a separate 'Process' so it never slows down the main data stream.
   - **ASTO (Adaptive SuperTrend Oscillator)**: Calculates a 'Comfort Zone' for the price. If the price breaks this zone with high volume, it signals a strong trade.
   - **VPIN (Flow Toxicity)**: Measures how 'One-Sided' the trading is. If 90% of trades are 'Aggressive Buys,' the market is toxic/unstable.
   - **Greeks (Delta/Gamma/Vega)**: Uses the Black-Scholes model to predict how option prices will change.
2. `_ofi()` (Order Flow Imbalance): Tracks if limit orders are being 'Stacked' on the Buy side or 'Pulled' from the Sell side.
3. `_classify_trade()` (Lee-Ready Rule): Decides if a trade happened because a Buyer was aggressive (Market Order) or a Seller was desperate.

**Variable Dictionary**:
- `ASTO`: A normalized score (-100 to +100) showing price momentum.
- `CVD`: Cumulative Volume Delta—the running total of aggressive buying minus aggressive selling.
- `Hurst Exponent`: A number from 0 to 1. >0.5 means Trending, <0.5 means Bouncing (Mean Reversion).
- `Z-Scores`: Standardized numbers that show how 'Extreme' a signal is compared to its recent history.

---
### [EXHAUSTIVE] daemons/meta_router.py (Executive Decision Router)

**Functional Purpose**: 
The 'Supreme Court' of the trading bot. Even if a strategy wants to trade, the MetaRouter has the final say. It checks every proposed trade against a '15-Gate Veto Matrix' to ensure the trade is safe, profitable, and compliant with institutional risk rules.

**Quant Finance Context**: 
Multi-Strategy Risk Aggregation. When you run 5 different bots, they might all try to buy the same stock at once, creating too much risk. The MetaRouter 'Vets' these intents to ensure the total portfolio remains balanced and doesn't explode during a market crash.

**Exhaustive Method Logic**:
1. `_evaluate_vetoes()` (The 15 Gates):
   - **Gate 3 (VPIN)**: Stops trading if the Market Sensor (above) says the flow is too toxic.
   - **Gate 5 (Regime Lock)**: Only allows 'Trend' strategies to run if the market is actually 'Trending.'
   - **Gate 10 (VIX Spike)**: If fear (VIX) jumps suddenly, it cancels all new trades.
   - **Gate 12 (Asset Heat)**: Limits how much money can be bet on a single index (e.g., no more than 50 lots of NIFTY).
2. `BayesianRegimeTracker`: Uses 'Probability' to smooth out market noise. It asks: "What is the 90% chance of the market mood being 'Ranging' right now?"
3. `PortfolioStressTestEngine`: Performs 'Crash Simulations.' It asks: "If the market drops 5% in 1 minute (Flash Crash), how much money will we lose?" If the answer is "Too much," it vetos the trade.
4. `RegulatoryCircuitBreaker`: Ensures we don't send orders too fast (SEBI rule: max 10 per second).

**Variable Dictionary**:
- `Veto Ledger`: A history of every reason a trade was REJECTED.
- `Shadow Ledger`: A record of what the bot *would* have done if it wasn't filtered (used for research).
- `Institutional Truth Matrix`: The final verdict on whether a trade goes to the 'Live' market or just the 'Paper' (Simulation) market.

---
---
### [EXHAUSTIVE] daemons/order_reconciler.py (Post-Trade Auditor)

**Functional Purpose**: 
The 'Safety Inspector.' When the bot executes a complex trade (like an 'Iron Condor' with 4 different legs), there is always a risk that only part of the trade fills while the rest fails. This daemon watches every basket like a hawk. If it sees a 'Gap' (a partial fill), it immediately steps in to either 'Force' the rest of the trade or 'Rollback' the whole thing to keep the account safe.

**Quant Finance Context**: 
Execution Risk Management. In high-volatility markets, 'Slippage' or 'Rejections' are common. A half-filled trade is a 'Naked Risk' that could lead to unlimited losses. This reconciler ensures 'Atomic Consistency'—either the whole strategy is in, or the whole strategy is out.

**Exhaustive Method Logic**:
1. `_watchdog_loop()`: Keeps a 3-second 'Timer' for every trade. If the broker hasn't confirmed the whole trade by then, the watchdog barks and triggers an audit.
2. `reconcile_basket()`: 
   - Compares what we *wanted* to buy vs what we *actually* got.
   - **Force Fill**: if 90% is done, it buys the remaining 10% regardless of price to 'Close the Gap.'
   - **Rollback**: If the broker rejected the core part of the trade, this function sells everything else we just bought to get back to a 'Flat' state.
3. `_reboot_audit()`: When the bot starts up, it scans the 'Pending' list for 'Ghost Orders'—trades that were left hanging when the bot was last turned off.

**Variable Dictionary**:
- `inflight_baskets`: The list of trades currently being processed by the broker.
- `parent_uuid`: The unique ID that links all legs of a single strategy together.
- `rollback_retries`: A counter to ensure we don't get stuck in an infinite loop trying to fix a broken trade.

---
### [EXHAUSTIVE] daemons/shoonya_gateway.py (Live Market Feed)

**Functional Purpose**: 
The 'Main Data Pipeline.' This is the only way the bot communicates with the outside world (the Stock Exchange). It maintains a persistent, high-speed connection (WebSocket) that streams millions of price updates every day.

**Quant Finance Context**: 
Low-Latency Connectivity. Every millisecond counts. This gateway is designed to be as 'Thin' as possible, taking the raw data from the broker and blasting it into the bot's internal 'Shared Memory' so every other component can see the price change instantly without any delay.

**Exhaustive Method Logic**:
1. `event_handler_feed_update()`: The 'Traffic Controller.' Every time the broker sends a price tick, this function catches it, calculates the 'Latency' (how old the data is), and attaches a 'Sequence ID' (a serial number) before sending it to the rest of the bot.
2. `_dynamic_subscription_listener()` (JIT Mode): Allows the bot to say "Hey, I'm interested in the 22500 Call Option now," and the gateway will instantly start streaming that data.
3. `_feed_watchdog_loop()`: A 'Liveliness' check. If the data stops flowing for more than 2 seconds during market hours, this function 'Kicks' the connection to restart it.

**Variable Dictionary**:
- `active_tokens`: The list of instruments we are currently watching.
- `sequence_id`: A monotonic counter used to ensure we never process an old price after a new one.
- `latency_ms`: The travel time of the data from the exchange to our server.

---
### [EXHAUSTIVE] daemons/snapshot_manager.py (Structural Data & Safety)

**Functional Purpose**: 
The 'Operational Librarian.' While other daemons focus on speed, this one focuses on 'Facts.' it manages all the structural data: "How much money is in the account?", "When do the options expire?", and "What are the legal rules for trading today?". It also runs the 'Simulator' for weekend testing.

**Quant Finance Context**: 
Operational Integrity & Compliance. It implements the SEBI 'Circuit Breaker' rules—if the market crashes too fast, this daemon detects the legal limit and 'Halts' the entire bot before the exchange does. It also performs 'Ghost Syncing' to ensure the bot's internal records match the broker's real records perfectly.

**Exhaustive Method Logic**:
1. `_boot_hydration()`: The 'Morning Routine.' Before trading starts, it downloads the lot sizes, expiry dates, and the last 14 days of history for every index.
2. `_circuit_breaker_monitor()`: Tracks the total market move. If Nifty moves 10% or more, it triggers a 'System Halt' to prevent the bot from trading in a broken market.
3. `_option_chain_scanner()`: Scans the entire market to find 'OI Walls'—huge clusters of open trades that act as 'Magnets' or 'Barriers' for the price.
4. `_run_simulator()`: When the market is closed, this function creates 'Fake' prices using Brownian Motion (random walks) so developers can test strategies 24/7.
5. `_ghost_position_sync()`: Every 60 seconds, it asks the broker: "What do we own?" and compares it to its database. If there's a mismatch (a 'Ghost'), it fires a CRITICAL alert.

**Variable Dictionary**:
- `CIRCUIT_BREAKER_MATRIX`: The official formula for market halts (10%, 15%, 20%).
- `prev_closes`: The official closing price from yesterday (the baseline for all today's moves).
- `OFF_HOUR_SIMULATOR`: A toggle that turns on the 'Fake Market' mode.

---
---
### [EXHAUSTIVE] daemons/strategy_engine.py (The Tactician)

**Functional Purpose**: 
The 'Decision Maker.' This daemon contains the actual logic for when to buy and sell. It doesn't just look at price; it looks at 'Regimes' (Market Mood). It has different specialized 'Hunters' for different market conditions: one for trending markets (Kinetic), one for sideways markets (Elastic), and one for high-income option spreads (Positional).

**Quant Finance Context**: 
Dynamic Multi-Strategy Allocation. Most bots fail because they try to use one set of rules for every market. The Strategy Engine is smarter: it only 'Activates' the Kinetic Hunter when the HMM Engine confirms a strong trend. It also implements the 'TastyTrade' 0-DTE philosophy—selling insurance to the market when the implied volatility is high.

**Exhaustive Method Logic**:
1. `KineticHunterStrategy`: The 'Trend Follower.' It waits for the ASTO indicator to exceed 70. It confirms the move by checking if the 'Power 5' (top 5 stocks like Reliance and HDFC Bank) are moving in the same direction.
2. `ElasticHunterStrategy`: The 'Rubber Band' logic. If the price stretched too far from its average (VWAP) by more than 2 Standard Deviations (Z-score > 2), it bets that the price will snap back to the mean.
3. `TastyTrade0DTEStrategy`: The 'Insurance Salesman.' On expiry days (e.g., Thursday for Nifty), it sells options right at the strike price to collect 'Theta' (time decay).
4. `config_subscriber()`: This allows a human trader to change strategy settings (like lot sizes) in real-time through the dashboard without restarting the bot.

**Variable Dictionary**:
- `S18 (Regime)`: 0=Neutral, 1=Trending, 2=Ranging, 3=Volatile.
- `Z-Score`: How 'Weird' or 'Extreme' the current price is compared to history.
- `Lot Size`: The number of shares/contracts to trade (e.g., 50 for Nifty).

---
### [EXHAUSTIVE] daemons/system_controller.py (Global Commander)

**Functional Purpose**: 
The 'Overseer' and 'Guardian.' This daemon manages the entire lifecycle of the trading day. It handle the 'Morning Hydration' (getting ready), the 'Afternoon Slowdown' (stopping new trades), and the 'Emergency Evacuation' (if the server crashes or the market breaks). It is the final authority on system-wide safety.

**Quant Finance Context**: 
Institutional Risk & Regulatory Compliance. It ensures the bot obeys SEBI (regulator) rules, like the 'T+1 Settlement' (you can't spend money you just earned until tomorrow) and 'Quarterly Settlements.' It also guards against 'Black Swan' events by monitoring a Global Macro Calendar for major news (like RBI rate hikes).

**Exhaustive Method Logic**:
1. `_three_stage_eod_scheduler()`: 
   - **15:00**: Stops all 'Trend' bots (no new trades near market close).
   - **15:20**: Liquidates all 'Intraday' trades (forces everyone out to avoid overnight risk).
   - **16:00**: Safely shuts down the VM to save cost.
2. `_preemption_poller()`: A 'Panic Button.' If Google Cloud says "We are taking your server back in 30 seconds," this function instantly squares off every single trade in the account to prevent a disaster.
3. `_hard_state_sync()`: Every 15 minutes, it audits the system. If it finds a 'Ghost' trade (a trade on the broker that the bot forgot about), it kills it immediately.
4. `_unsettled_premium_quarantine()`: Locks up profits earned today so they can't be 'Double Spent' before they are settled.

**Variable Dictionary**:
- `SYSTEM_HALT`: A global 'Emergency Stop' flag.
- `HEDGE_RESERVE_PCT`: The % of cash kept as emergency backup (default 15%).
- `SQUARE_OFF_KINETIC`: The 15:20 deadline for day-traders.

---
### [EXHAUSTIVE] daemons/unified_bridge.py (Execution Hub)

**Functional Purpose**: 
The 'Execution Bridge.' This is where 'Theory' meets 'Reality.' It takes an 'Intent' (I want to buy) and turns it into a 'Fill' (I have bought). It manages three different worlds simultaneously: a 'Shadow' world for research, a 'Paper' world for testing, and an 'Actual' world for real money.

**Quant Finance Context**: 
Execution Parity and Alpha Decay. This component ensures that the way we test the bot is *identical* to how it trades live. It adds realistic 'Slippage' (price friction) to paper trades based on how crazy the market is, so our test results aren't 'Too Good to be True.'

**Exhaustive Method Logic**:
1. `handle_intent_rendering()`: The core pipeline. 
   - **Shadow**: Records what *would* have happened if prices were perfect.
   - **Paper**: Simulates a trade with 'Slippage' (e.g., buying at 100.05 instead of 100).
   - **Live**: Sends the real command to the Shoonya API.
2. `update_portfolio_universal()`: The 'Accountant.' It calculates P&L (Profit/Loss) for every trade. It uses the exact same math for Paper and Live so the research is 'High Fidelity.'
3. `check_kill_switch_breach()`: If the bot loses too much money (e.g., ₹16,000 loss in one day), this function 'Suicides'—it kills all trades and locks the system down.
4. `liquidate_all_realities()`: A 'Master Clear' command that wipes the portfolio clean across all accounts.

**Variable Dictionary**:
- `Daily Loss Limit`: The maximum amount of money we are willing to lose in one day.
- `Slippage`: The 'Tax' of trading (the difference between the price you want and the price you get).
- `Execution Reality`: Whether a trade is 'Paper', 'Actual', or 'Shadow'.

---
---
### [EXHAUSTIVE] daemons/tick_sensor.py (The Fast Lane)

**Functional Purpose**: 
The 'Speed Demon.' This is a high-performance, lightweight version of the market data receiver. It is designed for one thing: getting price updates from the exchange to the strategy engine as fast as humanly possible. It bypasses heavy processing to ensure the bot can react to price changes in milliseconds.

**Quant Finance Context**: 
Ultra-Low Latency (ULL) Ingestion. In trading, 'Latency' (delay) is the enemy. The Tick Sensor uses 'Shared Memory,' which is like a communal whiteboard where it writes the price once, and every other part of the bot can read it instantly without waiting for a network message. It also tracks 'OI Acceleration'—if thousands of new contracts are being opened in seconds, it signals a massive 'Institutional Move' is happening.

**Exhaustive Method Logic**:
1. `_tick_iterator()`: The core loop. It connects to the Shoonya WebSocket and waits for data. When a price comes in, it immediately puts it in a 'Shared Memory' slot based on the stock symbol.
2. `_process_raw_tick()`: Cleans the data. It calculates the 'Latency' (how old the data is) and the 'OI Accel' (how fast open interest is growing).
3. `_reconnect_watchdog()`: A 'Liveliness Guardian.' If the Nifty 50 price hasn't updated in 2 seconds, it assumes the connection is 'Stalled' and kills the socket to trigger an instant restart.
4. `_run_simulator()`: A high-frequency (10Hz) fake price generator for testing the bot's speed during weekends.

**Variable Dictionary**:
- `OI Accel`: The 'Pressure' of the market (positive = buying pressure, negative = liquidating).
- `Sequence ID`: A 'Serial Number' for every tick to ensure we process them in the exact order they arrived.
- `SHM Slot`: The specific 'Parking Space' in computer memory for a stock's price.

---
### [EXHAUSTIVE] daemons/fii_dii_fetcher.py (Institutional Pulse)

**Functional Purpose**: 
The 'Big Money Tracker.' It watches what the 'Whales' (Foreign and Domestic Institutions) are doing. Since individual traders are small, we want to trade in the same direction as the massive funds. This fetcher grabs the 'Net Value' of their trades once every 30 minutes.

**Quant Finance Context**: 
Macro Sentiment Bias. If Foreign Institutional Investors (FII) are buying ₹2000 Crore worth of stocks, it's a 'Bullish Bias.' This bias is used as a 'Filter'—if the bias is -1.0 (heavy selling), the bot might be blocked from taking any 'Long' (buy) trades, even if the charts look good.

**Exhaustive Method Logic**:
1. `fetch_data()`: Scrapes the NSE India website. It mimics a real browser to avoid being blocked.
2. `fii_bias calculation`: Converts the 'Crores' (Millions of Dollars) into a simple number between -1 (Bearish) and +1 (Bullish). 
   - Rule: Selling > ₹2000 Cr = -1.0. Buying > ₹2000 Cr = +1.0.
3. `run()`: The scheduler. It wakes up every 30 minutes during market hours, fetches the data, and updates Redis so the rest of the system knows the 'Institutional Mood.'

**Variable Dictionary**:
- `fii_net`: The total money (in Crores) FIIs spent today (Buy - Sell).
- `fii_bias`: The normalized 'Sentiment' score used by the MetaRouter.

---
### [EXHAUSTIVE] daemons/data_gateway.py (Legacy Monolithic Gateway)

**Functional Purpose**: 
The 'Legacy Hub.' This is the original, all-in-one data receiver. While newer services like `shoonya_gateway` and `tick_sensor` are more specialized, the Data Gateway is kept as a robust fallback or for research. it handles everything from searching for option strikes to calculating the 'Put-Call Ratio' (PCR).

**Quant Finance Context**: 
Monolithic vs. Microservice Architecture. This file represents how the system used to work—one big service doing 10 different jobs. It's exhaustive because it contains the 'Black-Scholes' math for calculating option 'Greeks' (Delta, Theta) and the 'PCR' logic which tells us if more people are betting on a crash (High PCR) or a rally (Low PCR).

**Exhaustive Method Logic**:
1. `_dynamic_subscription_manager()`: Every 5 minutes, it looks at the current price and 'Subscribes' to the nearest option contracts. This ensures the bot is always watching the most 'Liquid' (active) strikes.
2. `get_optimal_strike()`: Uses math to find the 'Perfect' option to trade—one that isn't too expensive but moves fast enough to make a profit (Delta ~0.50).
3. `_calculate_pcr()`: Sums the 'Open Interest' of all Put options and divides it by Call options. This is a classic 'Contrarian' indicator.
4. `_circuit_breaker_monitor()`: Watches for 'Market Freezes.' If the market jumps 10%, it knows exactly how many minutes the exchange will halt trading and pauses the bot accordingly.

**Variable Dictionary**:
- `Optimal Strike`: The strike price that has the best 'Risk/Reward' balance according to Black-Scholes.
- `PCR (Put-Call Ratio)`: >1.0 means bearish sentiment, <0.7 means bullish sentiment.
- `Staleness Threshold`: 1000ms. If a price is older than 1 second, it's considered 'Stale' (garbage).

---
---
### [EXHAUSTIVE] Strategy Library: The Hunters

**Functional Purpose**: 
The 'Alpha Generators.' These are specialized logic blocks that identify 'Edge' in the market. Each 'Hunter' is tuned for a specific Market Regime (Trending, Ranging, or Volatile). They don't just look at price; they look at the 'Speed' and 'Toxicity' of the order flow.

**Quant Finance Context**: 
Regime-Specific Alpha. 
- **Kinetic Hunter**: Captures Momentum. It uses the ASTO (Adjusted Stochastic) to find strong trends confirmed by heavyweights.
- **Elastic Hunter**: Captures Mean Reversion. It bets that 'Overstretched' prices (High Z-score) will return to the average (VWAP).
- **0-DTE Hunter**: Captures Time Decay. It sells options on the day of expiry (0 days to expiry) when volatility is overpriced.

**Exhaustive Class Logic**:
1. `KineticHunterStrategy`: 
   - **Trigger**: |ASTO| > 70 and 'Smart Flow' (whale buying) is positive.
   - **Confirmation**: It checks the 'Power Five' (Nifty heavyweights like Reliance). If Nifty is going up but Reliance is flat, it stays out.
2. `TastyTrade0DTEStrategy`: 
   - **Trigger**: On the specific expiry day (e.g., Thursday for Nifty), between 09:30 and 10:30 IST.
   - **Logic**: Sells an 'Iron Butterfly' (selling ATM options and buying OTM wings) to collect premium while defined-risk.
3. `DirectionalCreditSpreadStrategy`: 
   - **Trigger**: Alpha > 25 (Bullish) or < -25 (Bearish).
   - **Logic**: Sells an option and buys a cheaper one further away to create a 'Safe Bettor's' position with high probability of success.

---
### [EXHAUSTIVE] Alpha Engine: Rust Core (TickEngine)

**Functional Purpose**: 
The 'Signal Factory.' Built in Rust for maximum speed, this engine processes every single tick (price change) to calculate advanced math indicators like OFI (Order Flow Imbalance) and VPIN (Toxicity). It tells the bot if the 'Whales' are trying to hide their orders or if the market is about to crash due to 'Toxic' volume.

**Quant Finance Context**: 
Microstructure Alpha. These are 'PhD-Level' signals that look deeper than just a chart. 
- **OFI (Order Flow Imbalance)**: Tracks who is 'more aggressive'—the buyers or the sellers.
- **VPIN (VPIN)**: Detects 'Informed Trading.' If high-volume trades are happening in one direction exclusively, the VPIN jumps, signaling a massive move is imminent.

**Exhaustive Method Logic**:
1. `update_ofi_zscore()`: Calculates how 'Abnormal' the current buying/selling pressure is compared to the last 100 ticks.
2. `update_vpin()`: Groups volume into 'Buckets.' If a bucket is 90% buyers, the VPIN score increases, indicating high 'Toxicity' for sellers.
3. `get_greeks()`: Calculates exactly how much an option's price will change if the stock moves ₹1 (Delta) or if time passes by 1 day (Theta).

---
---
### [EXHAUSTIVE] utils/shoonya_master.py (The Cartographer)

**Functional Purpose**: 
The 'Master Data Registry.' Every stock and option in India has a unique 'Token' ID. This script downloads the massive list of all symbols (thousands of them) from the Shoonya Exchange servers, cleans them, and stores them in Redis. It is the 'GPS' of the system—without it, the bot wouldn't know which ID belongs to 'NIFTY.'

**Quant Finance Context**: 
Dynamic Universe Management. Trading symbols change every week (especially options). This utility ensures our internal database is perfectly synced with the exchange's latest reality every morning at 08:30 IST. 

**Exhaustive Method Logic**:
1. `download_and_extract()`: Goes to the Shoonya server, downloads a zipped text file, and extracts it to the `data/` folder.
2. `parse_and_cache()`: Uses 'Pandas' to read the giant list and converts it into a 'Dictionary' (Key-Value pairs). It then pushes this to Redis so other services can find any token in under 1 millisecond.
3. `get_token()`: A simple helper used by other bots. If you say "Give me NIFTY," it returns "26000."

---
### [EXHAUSTIVE] utils/macro_event_fetcher.py (The Oracle)

**Functional Purpose**: 
The 'Global News Monitor.' Professional traders never trade during major news (like inflation reports or interest rate hikes) because the market becomes random and violent. This script fetches the calendar of 'High Impact' events worldwide and warns the bot.

**Quant Finance Context**: 
Event-Based Risk Avoidance. By knowing exactly when the US Federal Reserve or the RBI is speaking, the bot enters a 'Macro Lockdown' state—closing positions or pausing entries 15 minutes before the news hits. This prevents 'Slippage' and 'Black Swan' losses.

**Exhaustive Method Logic**:
1. `fetch_forex_factory()`: Connects to a global news feed. It only picks 'Red Folder' (High Impact) events for USD and INR.
2. `deduplicate()`: Sometimes two news sources report the same event. This logic ensures we don't have 'Double Alerts.'
3. `fetch_and_write()`: The main pipeline. It saves the final list to `data/macro_calendar.json`. The 'System Controller' reads this file at 09:15 AM to plan the day's safety windows.

---
### [EXHAUSTIVE] infrastructure/gcp_provision.py (The Architect)

**Functional Purpose**: 
The 'Auto-Launcher.' This script is the reason you don't have to manually start a server every morning. It talks to Google Cloud, rents a powerful computer for the day (a 'Spot VM'), and sets up the entire software environment from scratch in 3 minutes.

**Quant Finance Context**: 
Operational Reliability & Cost Optimization. Using 'Spot VMs' saves 60-90% on server costs. This script also 'Tunes' the Linux kernel for trading—it optimizes the network settings (TCP stack) so data packets travel faster from the broker to our bot.

**Exhaustive Method Logic**:
1. `is_nse_holiday()`: The 'Sleep' logic. If it's a Saturday, Sunday, or a Diwali holiday, it stops immediately to save money.
2. `create_spot_instance()`: The main builder. It creates a 'c2-standard-4' server (which is a high-performance compute-optimized machine).
3. `STARTUP_SCRIPT`: A massive set of commands injected into the new server. It installs Docker, mounts a 'RAM Disk' (memory-speed storage) for our data, and pulls the latest code from GitHub.
4. `ensure_firewall_rule()`: Opens the 'Gates.' It ensures the server can talk to our dashboard but remains locked to everyone else.

---
---
### [EXHAUSTIVE] Dashboard & Control Center

**Functional Purpose**: 
The 'Cockpit.' This is the user interface where the trader monitors and controls the bot. It consists of an 'Internal Hub' (on the server) and a 'Public Proxy' (in the cloud). It allows you to see what the bot is thinking, how much it is winning, and—most importantly—when to hit the 'Panic' button.

**Quant Finance Context**: 
Real-Time Transparency & Intervention. A 'Black Box' bot is dangerous. The Dashboard provides 'Glass Box' visibility into the math (Alpha scores, RSI, Greeks). It also implements 'Human-in-the-Loop' safety—you can manually override the bot's risk limits or toggle specific strategies ON/OFF based on your intuition.

**Exhaustive Component Logic**:
1. **Internal API (dashboard/api/)**: 
   - **Real-Time State**: Pulls data from Redis every second to show the current 'Alpha' of the market.
   - **Portfolio View**: Shows every open trade, its current profit/loss, and its 'Delta' (risk exposure).
   - **Hot Configuration**: Allows changing the bot's 'Confidence' threshold or 'Capital Limits' while it's running.
2. **Cloud Proxy (dashboard/cloudrun/)**: 
   - **Availability**: Even if the trading VM crashes, this proxy stays alive. It shows the 'Last Known State' from Firestore.
   - **Security**: Only the person with the 'Master Access Key' can enter the cockpit.
   - **Command Queuing**: If you hit 'Panic' while the VM is offline, it queues the command in the cloud so the bot sees it the moment it reboots.

**Variable Dictionary**:
- `Alpha Score`: 1.0 = Bullish, -1.0 = Bearish.
- `HMM Regime`: The identified market 'Mood' (e.g., TRENDING_UP).
- `Vanna/Charm`: Advanced 'Greeks' showing how risk changes as time passes or volatility jumps.

---
### [EXHAUSTIVE] QA & Integration Testing

**Functional Purpose**: 
The 'Safety Net.' Before any code is allowed to trade real money, it must pass a 'Gauntlet' of tests. These scripts simulate market crashes, network failures, and bad data to make sure the bot handles them gracefully without losing money.

**Quant Finance Context**: 
Failure Mode Mitigation. We test for 'Broken Data' (what if Nifty price becomes zero?) and 'Execution Racing' (what if we try to buy the same thing twice?).

**Exhaustive Test Logic**:
1. `test_order_lifecycle.py`: This is the most critical test. It starts a 'Paper' trade, waits for a fill, moves the price around, and then triggers a 'Stop Loss' to ensure the bot exits exactly where it promised.
2. `test_system_controller.py`: Simulates 15:20 IST to ensure the bot 'Force Squares Off' all positions correctly.
3. `test_audit_remediation.py`: Intentionally creates a 'Ghost Position' (a trade the bot didn't record) and verifies that the `order_reconciler` finds and kills it within 60 seconds.

---
---
### [EXHAUSTIVE] C++ High-Performance Gateway (Next-Gen)

**Functional Purpose**: 
The 'Turbo Ingestor.' While the main bot is in Python, this C++ layer is used for 'Ultra-Low Latency' tasks. It handles the raw firehose of market data (thousands of ticks per second) and cleans it before the Python bots even see it. It is the 'High-Speed Bridge' between the Exchange and the Brain.

**Quant Finance Context**: 
Latency Arb & Tick Normalization. In HFT (High-Frequency Trading), every microsecond matters. This C++ gateway uses 'SIMD' (Parallel CPU instructions) to process data faster than a human can blink. It ensures that when a price moves on the exchange, our bot knows about it in under 500 microseconds.

**Exhaustive Component Logic**:
1. **The Tri-Brain Shards**: The gateway splits the market into three 'Shards' (Alpha, Beta, Gamma). Each shard runs on its own dedicated CPU core to prevent 'Traffic Jams' in data processing.
   - **Shard Alpha**: Focuses only on NIFTY 50.
   - **Shard Beta**: Focuses only on BANK NIFTY.
   - **Shard Gamma**: Focuses on the BSE SENSEX.
2. **SIMD Normalization**: It 'Normalizes' the data. Different exchanges send data in different formats; this script converts them all into one 'Universal Language' for the bot.

---
