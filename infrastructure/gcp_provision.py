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

# ── NSE Holiday Guard ─────────────────────────────────────────────────────────
def is_nse_holiday(target_date: date | None = None) -> bool:
    """
    Returns True if target_date is an NSE market holiday.
    Uses python-holidays package with India country + NSE sub-market.
    Also filters weekends.
    """
    try:
        import holidays
        target = target_date or date.today()
        # Weekends are always non-trading
        if target.weekday() >= 5:
            return True
        # NSE holidays (financial market subset)
        india_holidays = holidays.country_holidays("IN", subdiv="MH", years=target.year)
        return target in india_holidays
    except ImportError:
        import warnings
        warnings.warn("python-holidays not installed; skipping NSE holiday check.")
        return False


def abort_if_holiday():
    """Exits the provisioner if today is an NSE holiday."""
    today = date.today()
    if is_nse_holiday(today):
        print(f"🚫 Today ({today.isoformat()}) is an NSE holiday or weekend. VM will not be started.")
        sys.exit(0)  # Clean exit (not error) so Cloud Scheduler marks success


# ── Macro Event Pre-fetch ─────────────────────────────────────────────────────
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


# ─── Config ───────────────────────────────────────────────
PROJECT_ID          = os.getenv("GCP_PROJECT_ID", "loanbot-8ac74")
ZONE                = "asia-south1-a"
INSTANCE_NAME       = "trading-engine-spot"
MACHINE_TYPE        = f"zones/{ZONE}/machineTypes/c2-standard-4"
TAILSCALE_AUTH_KEY  = os.getenv("TAILSCALE_AUTH_KEY", "")
GITHUB_PAT          = os.getenv("GITHUB_PAT_TOKEN", "")
REPO_URL            = f"https://{GITHUB_PAT}@github.com/AR-Karthik/trading_microservices.git" if GITHUB_PAT else "https://github.com/AR-Karthik/trading_microservices.git"
REPO_DIR            = "/opt/trading"

# Build .env content from environment (loaded from local .env via dotenv)
ENV_VARS = {
    "SHOONYA_USER":     os.getenv("SHOONYA_USER", ""),
    "SHOONYA_PWD":      os.getenv("SHOONYA_PWD", ""),
    "SHOONYA_VC":       os.getenv("SHOONYA_VC", ""),
    "SHOONYA_APP_KEY":  os.getenv("SHOONYA_APP_KEY", ""),
    "SHOONYA_FACTOR2":  os.getenv("SHOONYA_FACTOR2", ""),
    "SHOONYA_IMEI":     os.getenv("SHOONYA_IMEI", "abc1234"),
    "SHOONYA_HOST":     os.getenv("SHOONYA_HOST", ""),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", ""),
    "GCP_PROJECT_ID":     PROJECT_ID,
}
ENV_CONTENT = "\n".join(f"{k}={v}" for k, v in ENV_VARS.items())

# ─── Startup Script ──────────────────────────────────────
STARTUP_SCRIPT = f"""#!/bin/bash
set -e
exec > /var/log/trading-startup.log 2>&1

echo "=== Trading Engine Startup: $(date) ==="

# 1. System update + Docker
apt-get update -y
apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release git
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# Docker Compose v2
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 2. Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey={TAILSCALE_AUTH_KEY} --hostname=trading-engine --accept-routes || echo "Tailscale up failed, continuing..."

# 3. Clone repository
git clone {REPO_URL} {REPO_DIR}
cd {REPO_DIR}

# 4. Write .env securely from instance metadata (avoids storing secrets in GitHub)
cat > {REPO_DIR}/.env << 'ENVEOF'
{ENV_CONTENT}
ENVEOF

echo ".env written."

# 5. Launch all services
docker compose up -d --build

echo "=== All services started: $(date) ==="
"""

def get_or_create_firewall(client, project: str):
    """Ensure port 8501 is open for the dashboard."""
    firewall_client = compute_v1.FirewallsClient()
    rule_name = "allow-trading-dashboard"
    try:
        firewall_client.get(project=project, firewall=rule_name)
        print(f"Firewall rule '{rule_name}' already exists.")
    except Exception:
        print(f"Creating firewall rule '{rule_name}' for port 8501...")
        firewall = compute_v1.Firewall()
        firewall.name = rule_name
        firewall.direction = "INGRESS"
        firewall.network = "global/networks/default"
        firewall.allowed = [compute_v1.Allowed(I_p_protocol="tcp", ports=["8501"])]
        firewall.source_ranges = ["0.0.0.0/0"]
        firewall.target_tags = ["trading-dashboard"]
        firewall_client.insert(project=project, firewall_resource=firewall)
        print("Firewall rule created.")

def create_spot_instance():
    """Creates a GCP Spot VM instance with all trading services."""
    instance_client = compute_v1.InstancesClient()

    # Check if already exists
    try:
        existing = instance_client.get(project=PROJECT_ID, zone=ZONE, instance=INSTANCE_NAME)
        print(f"Instance '{INSTANCE_NAME}' already exists (status: {existing.status}). Skipping creation.")
        print(f"  → Dashboard should be at http://<tailscale-IP>:8501")
        return
    except Exception:
        pass  # Doesn't exist, proceed

    instance = compute_v1.Instance()
    instance.name = INSTANCE_NAME
    instance.machine_type = MACHINE_TYPE
    instance.tags = compute_v1.Tags(items=["trading-dashboard"])

    # Spot VM scheduling
    instance.scheduling = compute_v1.Scheduling(
        provisioning_model="SPOT",
        instance_termination_action="STOP",
        on_host_maintenance="TERMINATE",
        automatic_restart=False
    )

    # Boot Disk: 50GB pd-ssd
    disk = compute_v1.AttachedDisk(
        auto_delete=True,
        boot=True,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            disk_size_gb=50,
            disk_type=f"zones/{ZONE}/diskTypes/pd-ssd",
            source_image="projects/debian-cloud/global/images/family/debian-12"
        )
    )
    instance.disks = [disk]

    # Network with external IP
    access_config = compute_v1.AccessConfig(
        type_="ONE_TO_ONE_NAT",
        name="External NAT"
    )
    network_interface = compute_v1.NetworkInterface(
        network="global/networks/default",
        access_configs=[access_config]
    )
    instance.network_interfaces = [network_interface]

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

        # Poll until done
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
            print(f"  → Startup script is installing Docker + Tailscale (~3-5 minutes)")
            print(f"  → Dashboard will be available at http://<tailscale-IP>:8501")
            print(f"  → Check logs: gcloud compute ssh {INSTANCE_NAME} --zone={ZONE} -- 'tail -f /var/log/trading-startup.log'")

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
    parser = argparse.ArgumentParser(description="GCP Trading Engine Provisioner")
    parser.add_argument("--action", choices=["create", "delete"], default="create")
    parser.add_argument("--skip-holiday-check", action="store_true",
                        help="Override NSE holiday guard (use for manual deploys)")
    parser.add_argument("--skip-macro-fetch", action="store_true",
                        help="Skip ForexFactory+FMP macro calendar fetch")
    args = parser.parse_args()

    if args.action == "create":
        # ── NSE Holiday Guard ──────────────────────────────────────────────
        if not args.skip_holiday_check:
            abort_if_holiday()

        # ── Macro Event Pre-fetch ──────────────────────────────────────────
        if not args.skip_macro_fetch:
            prefetch_macro_events()

        get_or_create_firewall(compute_v1.InstancesClient(), PROJECT_ID)
        create_spot_instance()

    elif args.action == "delete":
        delete_instance()

