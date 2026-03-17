# GCP Deployment Review - Project K.A.R.T.H.I.K.

This document outlines potential points of failure and recommendations for a stable deployment on Google Cloud Platform (GCP).


## 3. Storage & Persistence

### [IMPORTANT] TimescaleDB Volume Permissions
- **Problem**: `docker-compose.yml` mounts `./volumes/timescale_data`. On Linux (GCP), Docker volume permissions can be tricky if the host user doesn't match the container user.
- **Recommendation**: Ensure the deployment script runs `chown -R 777 ./volumes` or specifically sets permissions for the Postgres user (UID 999).


### [INFO] RAM Disk Usage
- **Problem**: The system uses `/ram_disk` for shared memory signals. 
- **Recommendation**: Ensure the VM has sufficient RAM. The `Dockerfile.local` creates this directory, but it should be mounted as a `tmpfs` volume in `docker-compose.yml` for actual speed benefits and to avoid wearing out physical SSDs.

---

## 4. GCP Specific Integrations


### External IP Discovery
- **Problem**: `cloud_publisher.py` attempts to fetch the external IP from `http://metadata.google.internal`.
- **Recommendation**: Ensure the VM has the "Compute Engine Read" scope enabled so the Metadata Flavor header works correctly.

---

## 5. Error Handling & Resilience

### [WARNING] ZMQ Message Loss
- **Problem**: ZMQ `PUB/SUB` can drop messages if the subscriber is slow or reconnecting.
- **Recommendation**: Critical commands like `PANIC_BUTTON` should use Redis Pub/Sub (which is already done in `cloud_publisher.py`) or a `PUSH/PULL` pattern with acknowledgements if message delivery must be guaranteed.

### [INFO] Fallback Mechanisms
- **Problem**: `shm.py` falls back to Redis if shared memory fails.
- **Recommendation**: Monitor the `SHM_FALLBACK` status. If many daemons fall back, performance (latency) will degrade significantly.

---

## 6. Backend & Frontend Integration

### [IMPORTANT] CORS Tightening
- **Problem**: `dashboard/api/main.py` uses `allow_origins=["*"]`.
- **Recommendation**: Restrict this to the internal Tailscale IP or the specific Cloud Run URL of your frontend if deploying separately.

### [INFO] Dashboard Discovery
- **Problem**: `cloud_publisher.py` publishes the VM's external IP to Firestore.
- **Recommendation**: Ensure the frontend is configured to use this IP dynamically to connect to the FastAPI backend.

---

## 7. Data Reference Uniformity

### [WARNING] Regime State Inconsistency
- **Problem**: Different modules use different keys to access regime data:
    - `meta_router.py`: Reads from `hmm_regime_state` (Redis Hash).
    - `cloud_publisher.py`: Reads from `HMM_REGIME:{asset}` and `HMM_REGIME` (Global).
    - `dashboard/api/main.py`: Handles all three, but this duplication increases the risk of stale data if one isn't updated.
- **Recommendation**: Standardize on a single source of truth (e.g., the `HMM_REGIME` hash) and update all consumers.

### [IMPORTANT] Margin & Capital Key Mismatch
- **Problem**: 
    - `dashboard/api/main.py` looks for `AVAILABLE_MARGIN_LIVE`.
    - `meta_router.py` and `system_controller.py` use `CASH_COMPONENT_LIVE`.
- **Recommendation**: Ensure the `system_controller` or a dedicated risk worker synchronizes these keys, or update the dashboard to use the same component keys as the trading engine.

### [WARNING] Power Five Matrix Structure
- **Problem**: 
    - `dashboard/api/main.py` expects `power_five_matrix` to contain JSON strings with a `z_score` field.
    - `cloud_publisher.py` attempts to parse it as a raw float.
- **Recommendation**: Standardize the data format in Redis for the `power_five_matrix` hash.

---