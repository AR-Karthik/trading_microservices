#!/bin/bash
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
rm -rf /opt/trading
git clone https://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@github.com/AR-Karthik/trading_microservices.git /opt/trading
cd /opt/trading

# 3. Write .env securely from instance metadata
cat > /opt/trading/.env << 'ENVEOF'
SHOONYA_USER=FN196017
SHOONYA_PWD=V@rsh@02
SHOONYA_VC=FN196017_U
SHOONYA_APP_KEY=1879c8b154b381c468528f448f40fc62
SHOONYA_FACTOR2=AD3TQ23543XP74N62672YZ3N7D666V2K
SHOONYA_IMEI=abc1234
SHOONYA_HOST=https://api.shoonya.com/NorenWClientTP/
TELEGRAM_BOT_TOKEN=8500172196:AAEGDbqPXTLksEdvpuUVJOBRfBPJvRTNxRM
TELEGRAM_CHAT_ID=1173952032
GCP_PROJECT_ID=karthiks-trading-assistant
GCS_MODEL_BUCKET=karthiks-trading-models
DASHBOARD_ACCESS_KEY=K_A_R_T_H_I_K_2026_PRO
SIMULATION_MODE=true
REDIS_HOST=redis
DB_HOST=timescaledb
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
mount_disk() {
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
}

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
rm -f /opt/trading/data/redis /opt/trading/data/db
ln -s /mnt/hot_nvme/redis_data /opt/trading/data/redis
ln -s /mnt/hot_nvme/db_data /opt/trading/data/db

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
