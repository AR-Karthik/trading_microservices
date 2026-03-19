# 🛡️ Project K.A.R.T.H.I.K. — Complete Debug & Maintenance Suite

This document provides a comprehensive guide for technical operators to maintain, debug, and monitor the high-frequency trading system when connected via SSH to the VM.

---

## 1. Environment & SSH Access

### Global Entry Point
All core trading logic resides under:
```bash
cd /opt/trading
```

### Identity and Authentication
Ensure the following files are present and non-empty:
- `.env`: Holds all API keys (Shoonya, Telegram, GCP).
- `gcp_creds.json`: (Usually mounted from Secret Manager or Service Account).

---

## 2. Process & Lifecycle Management

The system is orchestrated using **Docker Compose**. Use the following shorthand commands via the `Makefile`:

| Command | Action |
| :--- | :--- |
| `make ps` | Check status of all containers (Health, Uptime). |
| `make logs` | Tail all logs simultaneously. |
| `make build` | Rebuild images after a code change (`git pull`). |
| `make up` | Start/Update all containers in detached mode. |
| `make down` | Gracefully stop all services. |
| `make clean` | Purge caches and standard volumes (use with caution). |

### Target Service Debugging
To debug a specific daemon (e.g., Meta-Router):
```bash
docker logs -f meta_router --tail 100
docker restart meta_router
```

---

## 3. Data Integrity & Verification

### Redis (Real-Time State)
Redis is the primary high-speed message bus. Access it directly:
```bash
docker exec -it trading_redis redis-cli
```

**Key Verification Patterns:**
- `GET COMPOSITE_ALPHA`: Returns the global high-frequency alpha score.
- `HGETALL hmm_regime_state`: Shows current market regime for all indices.
- `KEYS latest_market_state:*`: Look for ticks and OHLC state.
- `HGETALL power_five_matrix`: View individual bank/stock Z-scores.
- `GET TRADING_PAUSED`: If this key exists, all new orders are vetoed.

### TimescaleDB (Historical Data & Trades)
The persistent record for backtesting and EOD analytics:
```bash
docker exec -it trading_timescaledb psql -U trading_user -d trading_db
```

**Useful Queries:**
- Verify trade logging: `SELECT * FROM trades ORDER BY time DESC LIMIT 10;`
- Check market depth snapshots: `SELECT * FROM market_history LIMIT 5;`

### Firestore (Cloud Sync)
Used by the remote Cloud Run dashboard. Check these documents in the GCP Console:
- `system/metadata`: Contains the heartbeat of the VM.
- `system/config`: Contains capital limits and risk settings.
- `commands/latest`: Used to send `PANIC` signals to the VM.

---

## 4. Connectivity & IPC

### System Heartbeats
Monitor the `cloud_publisher` daemon to ensure the VM is "visible" to the world:
```bash
docker logs -f cloud_publisher
```

### Shared Memory (RAM Disk)
Used for sub-millisecond IPC between C++ and Python gateways:
```bash
ls -lh /ram_disk
```
Ensure files like `tick_matrix.bin` or `state_vec.bin` are updating their timestamps every second.

---

## 5. Hot-Reload & Update Workflow

When pushing updates from Git:
1. **Pull and Re-deploy:**
   ```bash
   git pull
   make build
   make up
   ```
2. **Post-Update Check:**
   Wait for the Telegram Alerter to send the **"Trading System BOOTED"** message to your dedicated channel.

---

## 6. Critical Log Locations

| Component | Path / Command |
| :--- | :--- |
| **Startup Scripts** | `/var/log/trading-startup.log` |
| **Kernel / Disk** | `dmesg | grep -i nvme` |
| **GCP Cloud Build** | `gcloud builds list` (if using cloud triggers) |
| **Api Errors** | `tail -f api_error.log` |

---

### Emergency: Kill-Switch
If the UI is unresponsive and you need to halt all trading immediately from SSH:
```bash
# Option A: Pause Logic
docker exec -it trading_redis redis-cli SET TRADING_PAUSED "True"

# Option B: Hard Stop
docker-compose down
```
