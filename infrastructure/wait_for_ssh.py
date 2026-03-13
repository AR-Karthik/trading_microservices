import time
import subprocess
import sys
import argparse

def wait_for_ssh(instance_name, zone, timeout=300):
    """Waits for a GCP instance to be ready for SSH."""
    print(f"⌛ Waiting for SSH readiness on '{instance_name}' in '{zone}'...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        cmd = [
            "gcloud", "compute", "ssh", instance_name,
            f"--zone={zone}",
            "--command=echo ready",
            "--quiet",
            "--tunnel-through-iap"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ SSH is ready on '{instance_name}'!")
            return True
        print(".", end="", flush=True)
        time.sleep(5)
    
    print(f"\n❌ Timeout: SSH not ready on '{instance_name}' after {timeout}s")
    return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("instance")
    parser.add_argument("zone")
    args = parser.parse_args()
    if not wait_for_ssh(args.instance, args.zone):
        sys.exit(1)
