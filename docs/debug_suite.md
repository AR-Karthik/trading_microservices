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

The system is orchestrated using **Docker Compose**. Since `make` is not available on the VM, use these direct commands:

| Command | Action |
| :--- | :--- |
| `sudo docker compose ps` | Check status of all containers (Health, Uptime). |
| `sudo docker compose logs --tail=50 -f` | Tail all logs simultaneously. |
| `sudo docker compose build` | Rebuild images after a code change (`git pull`). |
| `sudo docker compose up -d` | Start/Update all containers in detached mode. |
| `sudo docker compose down` | Gracefully stop all services. |
| `sudo docker ps -a` | See all containers, including those that crashed/stopped. |

### Target Service Debugging
To debug a specific daemon (e.g., Data Gateway):
```bash
sudo docker compose logs -f data_gateway --tail 100
sudo docker compose restart data_gateway
```

---

## 3. Data Integrity & Exhaustive Diagnostics

### Redis (Real-Time State & Health)
Redis is the primary high-speed message bus. Use these for deep diagnostics:

**1. Basic Connectivity:**
```bash
sudo docker exec trading_redis redis-cli ping
```

**2. Exhaustive Metrics:**
```bash
# Check if Redis is unable to persist (the "MISCONF" error)
sudo docker exec trading_redis redis-cli info persistence | grep rdb_last_bgsave_status

# Check Memory Usage
sudo docker exec trading_redis redis-cli info memory | grep used_memory_human

# List Connected Clients (check for stale/too many connections)
sudo docker exec trading_redis redis-cli client list

# Real-time Stream (WARNING: High Volume)
sudo docker exec trading_redis redis-cli monitor
```

**3. Key Verification Patterns:**
- `GET COMPOSITE_ALPHA`: Returns the global high-frequency alpha score.
- `HGETALL hmm_regime_state`: Shows current market regime for all indices.
- `KEYS *`: List all keys (use `DBSIZE` first to check count).

### TimescaleDB (Historical Data)
```bash
sudo docker exec -it trading_timescaledb psql -U trading_user -d trading_db
```

### Firestore (Cloud Sync)
Used by the remote Cloud Run dashboard. Check these documents in the GCP Console:
- `system/metadata`: Contains the heartbeat of the VM.
- `system/config`: Contains capital limits and risk settings.
- `commands/latest`: Used to send `PANIC` signals to the VM.

---

## 4. Connectivity, Ports & IPC

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

### Port / Socket Conflicts
If a service fails to start because "address already in use":
```bash
# Check what is listening on a specific port (e.g., 5555 for ZMQ)
sudo ss -lptn | grep 5555
sudo netstat -tulpn | grep 5555
```

### ZeroMQ Diagnostics
Since ZeroMQ uses internal Docker networking, check if the ports are reachable from another container:
```bash
sudo docker compose exec data_gateway nc -zv meta_router 5555
```

---

## 5. Infrastructure & Mounts (CRITICAL)

### NVMe Persistence
Ensure the high-performance disk is correctly mounted and data isn't filling up the boot disk:
```bash
# Check mountpoint
mountpoint /mnt/hot_nvme

# Check disk space on the mount
df -h /mnt/hot_nvme

# Verify Symlinks are NOT broken
ls -la /opt/trading/data/redis
ls -la /opt/trading/data/db
```

### Permissions
If Redis or DBs fail to write:
```bash
# Ensure 777 permissions for the data folders
sudo chmod -R 777 /mnt/hot_nvme/redis_data
sudo chmod -R 777 /mnt/hot_nvme/timescale_data
```

---

## 6. Hot-Reload & Update Workflow

When pushing updates from Git:
1. **Pull and Re-deploy:**
   ```bash
   cd /opt/trading
   git pull
   sudo docker compose up -d --build
   ```
2. **Post-Update Check:**
   Check container logs for any "ModuleNotFoundError" or "TypeError" during startup.

---

## 7. Emergency: Kill-Switch
```bash
# Option A: Pause Logic
sudo docker exec trading_redis redis-cli SET TRADING_PAUSED "True"

# Option B: Hard Stop
sudo docker compose down
```
