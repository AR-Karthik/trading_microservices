#!/usr/bin/env python3
"""
GCP Spot VM Provisioner for Trading Microservices.
Provisions a c2-standard-4 Spot VM in asia-south1-a, installs Docker + Tailscale,
clones the repo, injects .env securely via instance metadata, and starts all services.
"""
import os
import time
import json
import argparse
import sys
from datetime import date
from google.cloud import compute_v1
from dotenv import load_dotenv

load_dotenv()

# Add project root to path so we can import our own utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ------ NSE Holiday Guard -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def is_nse_holiday(target_date: date | None = None) -> bool:
    """
    Returns True if target_date is an NSE market holiday.
    Uses python-holidays package with XNSE (financial market) subdivision.
    Also filters weekends.
    """
    try:
        import holidays
        target = target_date or date.today()
        # Weekends are always non-trading
        if target.weekday() >= 5:
            return True
        # NSE financial holidays
        market_holidays = holidays.financial_holidays("XNSE", years=target.year)
        return target in market_holidays
    except (ImportError, AttributeError):
        import warnings
        warnings.warn("python-holidays (XNSE) not supported; skipping NSE holiday check.")
        return False


def abort_if_holiday():
    """Exits the provisioner if today is an NSE holiday."""
    today = date.today()
    if is_nse_holiday(today):
        print(f"🛑 Today ({today.isoformat()}) is an NSE holiday or weekend. VM will not be started.")
        sys.exit(0)  # Clean exit (not error) so Cloud Scheduler marks success


# ------ Macro Event Pre-fetch -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def prefetch_macro_events():
    """
    Fetches latest macro events from ForexFactory + FMP and writes to
    data/macro_calendar.json before the VM is provisioned.
    On failure, logs a warning but does NOT abort VM creation.
    """
    print("Fetching macro calendar (ForexFactory + FMP)...")
    try:
        from utils.macro_event_fetcher import fetch_and_write
        events = fetch_and_write(write_to_disk=True, write_to_redis=False)
        print(f"[SUCCESS] Macro calendar: {len(events)} events written.")
    except Exception as e:
        print(f"[WARNING] Macro event fetch failed ({e}). System controller will use cached calendar.")


# --------- Config ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
PROJECT_ID          = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
ZONE                = "asia-south1-a"
INSTANCE_NAME       = "trading-engine-spot"
MACHINE_TYPE        = f"zones/{ZONE}/machineTypes/c2-standard-4"
GITHUB_PAT          = os.getenv("GITHUB_PAT_TOKEN", "")
REPO_URL            = f"https://{GITHUB_PAT}@github.com/AR-Karthik/trading_microservices.git" if GITHUB_PAT else "https://github.com/AR-Karthik/trading_microservices.git"
REPO_DIR            = "/opt/trading"

# Build .env content from local environment
ENV_VARS = {
    "SHOONYA_USER":       os.getenv("SHOONYA_USER", ""),
    "SHOONYA_PWD":        os.getenv("SHOONYA_PWD", ""),
    "SHOONYA_VC":         os.getenv("SHOONYA_VC", ""),
    "SHOONYA_APP_KEY":    os.getenv("SHOONYA_APP_KEY", ""),
    "SHOONYA_FACTOR2":    os.getenv("SHOONYA_FACTOR2", ""),
    "SHOONYA_IMEI":       os.getenv("SHOONYA_IMEI", "abc1234"),
    "SHOONYA_HOST":       os.getenv("SHOONYA_HOST", ""),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", ""),
    "GCP_PROJECT_ID":     PROJECT_ID,
    "GCS_MODEL_BUCKET":   os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models"),
    "DASHBOARD_ACCESS_KEY": os.getenv("DASHBOARD_ACCESS_KEY", "K_A_R_T_H_I_K_2026_PRO"),
    "SIMULATION_MODE":    os.getenv("SIMULATION_MODE", "false"),
    "REDIS_HOST":         "redis",
    "DB_HOST":            "timescaledb",
}
ENV_CONTENT = "\n".join(f"{k}={v}" for k, v in ENV_VARS.items())

# --------- Startup Script ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
STARTUP_SCRIPT = f"""#!/bin/bash
set -e
exec > /var/log/trading-startup.log 2>&1

echo "=== Trading Engine Startup: $(date) ==="

# 1. System update + Docker
apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release git
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --batch --yes -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# 2. Clone repository
rm -rf {REPO_DIR}
git clone {REPO_URL} {REPO_DIR}
cd {REPO_DIR}

# 3. Write .env securely from instance metadata
cat > {REPO_DIR}/.env << 'ENVEOF'
{ENV_CONTENT}
ENVEOF

echo ".env written."

# 4. OS Hardening, Storage Mounts, & Kernel Tuning
echo "Optimizing Kernel (THP, ulimit, TCP)..."
# Transparent HugePages
echo always > /sys/kernel/mm/transparent_hugepage/enabled
# Kernel flags
cat >> /etc/sysctl.conf << 'EOF'
fs.file-max = 1000000
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 8000
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
EOF
sysctl -p || true

# RAM Disk for IPC
mkdir -p /ram_disk
mountpoint -q /ram_disk || mount -t tmpfs -o size=512M tmpfs /ram_disk
grep -q "/ram_disk" /etc/fstab || echo "tmpfs /ram_disk tmpfs rw,size=512M 0 0" >> /etc/fstab

# Robust disk discovery and mounting
mount_disk() {{
    local disk_num=$1
    local mount_point=$2
    local dev=""
    
    echo "Attempting to find device for disk $disk_num..."
    
    # Priority 1: Persistent Disk ID (The most reliable on GCP)
    local disk_id="google-persistent-disk-$(( (disk_num == 'b' ? 1 : 2) ))"
    if [ -e "/dev/disk/by-id/$disk_id" ]; then
        dev="/dev/disk/by-id/$disk_id"
    fi
    
    # Priority 2: Standard naming /dev/sdX
    if [ -z "$dev" ] && [ -e "/dev/sd$disk_num" ]; then
        dev="/dev/sd$disk_num"
    fi
    
    # Priority 3: NVMe naming /dev/nvme0nX
    if [ -z "$dev" ]; then
        local nvme_idx=$(( (disk_num == 'b' ? 1 : 2) ))
        if [ -e "/dev/nvme0n$nvme_idx" ]; then
            dev="/dev/nvme0n$nvme_idx"
        fi
    fi

    if [ -n "$dev" ]; then
        echo "Found device $dev. Setting up $mount_point..."
        mkdir -p "$mount_point"
        # Only format if needed
        if ! blkid "$dev" >/dev/null 2>&1; then
            echo "Formatting $dev..."
            mkfs.ext4 -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "$dev"
        fi
        
        # Mount
        if ! mountpoint -q "$mount_point"; then
            mount -o discard,defaults "$dev" "$mount_point"
        fi
        
        # Persist across reboots
        grep -q "$mount_point" /etc/fstab || echo "$dev $mount_point ext4 discard,defaults,nofail 0 2" >> /etc/fstab
        
        # Critical: Set permissions for Docker volumes
        chmod 777 "$mount_point"
        echo "Successfully mounted $dev to $mount_point"
    else
        echo "ERROR: Could not locate device for disk $disk_num"
        # List all block devices for debugging in startup logs
        lsblk
    fi
}}

# Mount NVMe/SSD (Disk 2) for Hot Storage (Redis/Timescale WAL)
mount_disk "b" "/mnt/hot_nvme"

# Mount Regional SSD/SSD (Disk 3) for Cold Storage (HMM Data)
mount_disk "c" "/mnt/cold_ssd"

# 5. Launch all services
# Symlink Docker volumes to the high-performance disks
mkdir -p /mnt/hot_nvme/redis_data /mnt/hot_nvme/db_data
# Fix permissions explicitly for current mount path
chmod -R 777 /mnt/hot_nvme

# Remove existing symlinks if they exist
rm -f {REPO_DIR}/data/redis {REPO_DIR}/data/db
ln -s /mnt/hot_nvme/redis_data {REPO_DIR}/data/redis
ln -s /mnt/hot_nvme/db_data {REPO_DIR}/data/db

# Use --progress=plain for build logs
docker compose up -d --build --progress=plain

# 6. Wait for Telegram Alerter to be ready
echo "Waiting for Telegram Alerter to send BOOT signal..."
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if docker logs telegram_alerter 2>&1 | grep -q "Trading System BOOTED"; then
        echo "Telegram Alerter is ready and BOOT signal sent."
        break
    fi
    echo "Waiting for telegram_alerter... ($RETRY_COUNT/$MAX_RETRIES)"
    RETRY_COUNT=$((RETRY_COUNT+1))
    sleep 5
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "WARNING: Telegram Alerter did not signal BOOT within timeout."
fi

echo "=== All services started: $(date) ==="
"""
GCP Spot VM Provisioner for Trading Microservices.
"""

def ensure_firewall_rule():
    """Ensures GCP firewall rule for Port 8000 (Hub-and-Spoke Proxy) exists."""
    from google.cloud import compute_v1
    fw_client = compute_v1.FirewallsClient()
    rule_name = "allow-trading-api-v8"
    
    try:
        fw_client.get(project=PROJECT_ID, firewall=rule_name)
        print(f"Firewall rule '{rule_name}' already exists.")
        return
    except Exception:
        print(f"Creating firewall rule '{rule_name}' for Port 8000...")
        
    rule = compute_v1.Firewall()
    rule.name = rule_name
    rule.direction = "INGRESS"
    rule.network = "global/networks/default"
    rule.allowed = [compute_v1.Allowed(I_p_protocol="tcp", ports=["8000"])]
    rule.source_ranges = ["0.0.0.0/0"]
    rule.target_tags = ["trading-api"]
    rule.description = "Allow Hybrid Proxy traffic (v8.0) to Trading VM"

    op = fw_client.insert(project=PROJECT_ID, firewall_resource=rule)
    # Busy wait for completion
    while True:
        status = fw_client.get(project=PROJECT_ID, firewall=rule_name)
        if status: break
        time.sleep(2)
    print(f"✅ Firewall rule '{rule_name}' created.")


def create_spot_instance():
    """Creates a GCP Spot VM instance with all trading services."""
    ensure_firewall_rule()
    instance_client = compute_v1.InstancesClient()

    # Check if already exists
    try:
        existing = instance_client.get(project=PROJECT_ID, zone=ZONE, instance=INSTANCE_NAME)
        print(f"Instance '{INSTANCE_NAME}' already exists (status: {existing.status}). Skipping creation.")
        print(f"  👉 Cloud Run Dashboard should be used for monitoring.")
        return
    except Exception:
        pass  # Doesn't exist, proceed

    instance = compute_v1.Instance()
    instance.name = INSTANCE_NAME
    instance.machine_type = MACHINE_TYPE

    # Spot VM scheduling
    instance.scheduling = compute_v1.Scheduling(
        provisioning_model="SPOT",
        instance_termination_action="STOP",
        on_host_maintenance="TERMINATE",
        automatic_restart=False
    )

    # Disk 1: Boot Disk (Standard)
    disk1 = compute_v1.AttachedDisk(
        auto_delete=False,
        boot=True,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            disk_size_gb=50,
            disk_type=f"zones/{ZONE}/diskTypes/pd-ssd",
            source_image="projects/debian-cloud/global/images/family/debian-12"
        )
    )
    
    # Disk 2: SSD for Hot Path
    disk2 = compute_v1.AttachedDisk(
        auto_delete=False,
        boot=False,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            disk_size_gb=100,
            disk_type=f"zones/{ZONE}/diskTypes/pd-ssd"
        )
    )

    # Disk 3: SSD for Cold Storage
    disk3 = compute_v1.AttachedDisk(
        auto_delete=False,
        boot=False,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            disk_size_gb=200,
            disk_type=f"zones/{ZONE}/diskTypes/pd-ssd"
        )
    )

    instance.disks = [disk1, disk2, disk3]

    # Network with Premium Tier & GVNIC
    access_config = compute_v1.AccessConfig(
        type_="ONE_TO_ONE_NAT",
        name="External NAT",
        network_tier="PREMIUM"
    )
    network_interface = compute_v1.NetworkInterface(
        network="global/networks/default",
        access_configs=[access_config],
        nic_type="GVNIC" 
    )
    instance.network_interfaces = [network_interface]

    # Networking tags for firewall matching
    instance.tags = compute_v1.Tags(items=["trading-api"])

    # Startup script via metadata
    metadata = compute_v1.Metadata(
        items=[compute_v1.Items(key="startup-script", value=STARTUP_SCRIPT)]
    )
    instance.metadata = metadata

    print(f"Provisioning Spot VM '{INSTANCE_NAME}' in {ZONE}...")
    try:
        operation = instance_client.insert(
            project=PROJECT_ID,
            zone=ZONE,
            instance_resource=instance
        )

        zone_ops = compute_v1.ZoneOperationsClient()
        print("Waiting for VM creation to complete...", end="", flush=True)
        while True:
            op = zone_ops.get(project=PROJECT_ID, zone=ZONE, operation=operation.name)
            if op.status == compute_v1.Operation.Status.DONE:
                break
            print(".", end="", flush=True)
            time.sleep(5)
        print(" Done!")

        if op.error:
            print(f"ERROR: {op.error}")
        else:
            print(f"\nInstance '{INSTANCE_NAME}' created successfully!")
            print(f"  👉 Cloud Run Dashboard will be available shortly.")
            print(f"  👉 Check logs: gcloud compute ssh {INSTANCE_NAME} --zone={ZONE} -- 'tail -f /var/log/trading-startup.log'")

    except Exception as e:
        print(f"Failed to provision instance: {e}")

def delete_instance():
    """Tear down the Spot VM."""
    instance_client = compute_v1.InstancesClient()
    try:
        op = instance_client.delete(project=PROJECT_ID, zone=ZONE, instance=INSTANCE_NAME)
        print(f"Deleting '{INSTANCE_NAME}'... (operation: {op.name})")
    except Exception as e:
        print(f"Failed to delete: {e}")

if __name__ == "__main__":
    print(f"DEBUG: Active Project ID: {PROJECT_ID}")
    print(f"DEBUG: Active Zone: {ZONE}")
    parser = argparse.ArgumentParser(description="GCP Trading Engine Provisioner")
    parser.add_argument("--action", choices=["create", "delete"], default="create")
    parser.add_argument("--skip-holiday-check", action="store_true",
                        help="Override NSE holiday guard")
    parser.add_argument("--skip-macro-fetch", action="store_true",
                        help="Skip macro calendar fetch")
    args = parser.parse_args()

    if args.action == "create":
        if not args.skip_holiday_check:
            abort_if_holiday()
        if not args.skip_macro_fetch:
            prefetch_macro_events()
        create_spot_instance()

    elif args.action == "delete":
        delete_instance()
