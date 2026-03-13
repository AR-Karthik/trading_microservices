import os
import subprocess
import shutil
import sys

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
REGION = "asia-south1"
ZONE = "asia-south1-a"
FUNCTION_NAME = "trading-scheduler-func"
TOPIC_NAME = "trading-auto-schedule"

def run_cmd(cmd):
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        # Ignore "Already exists" or "not found" errors for idempotency
        if "already exists" in result.stderr.lower() or "not found" in result.stderr.lower():
            print(f"Info: {result.stderr.strip()}")
        else:
            print(f"Error: {result.stderr}")
    else:
        print(result.stdout)
    return result

def deploy():
    # 1. Create Pub/Sub Topic
    run_cmd(f"gcloud pubsub topics create {TOPIC_NAME}")

    # 2. Deploy Cloud Function
    # We need a requirements.txt for the function
    req_path = os.path.join("infrastructure", "requirements.txt")
    with open(req_path, "w") as f:
        f.write("google-cloud-compute\nholidays\nfunctions-framework\n")

    # Copy func to main.py in infrastructure
    src_func = os.path.join("infrastructure", "gcp_scheduler_func.py")
    dst_func = os.path.join("infrastructure", "main.py")
    shutil.copy2(src_func, dst_func)
    
    cmd = (
        f"gcloud functions deploy {FUNCTION_NAME} "
        f"--runtime python311 "
        f"--trigger-topic {TOPIC_NAME} "
        f"--entry-point schedule_trading_vm "
        f"--region {REGION} "
        f"--set-env-vars GCP_PROJECT_ID={PROJECT_ID} "
        f"--source ./infrastructure "
        f"--no-gen2 " 
        f"--allow-unauthenticated"
    )
    
    run_cmd(cmd)

    # 3. Create Cloud Scheduler Jobs
    # Morning Start: 09:00 IST (03:30 UTC)
    run_cmd(
        f"gcloud scheduler jobs create pubsub start-trading-vm "
        f"--schedule='30 3 * * 1-5' "
        f"--topic={TOPIC_NAME} "
        f"--message-body='START' "
        f"--time-zone='UTC' "
        f"--description='Start Trading VM at 09:00 IST'"
    )

    # Evening Stop: 15:45 IST (10:15 UTC)
    run_cmd(
        f"gcloud scheduler jobs create pubsub stop-trading-vm "
        f"--schedule='15 10 * * 1-5' "
        f"--topic={TOPIC_NAME} "
        f"--message-body='STOP' "
        f"--time-zone='UTC' "
        f"--description='Stop Trading VM at 15:45 IST'"
    )

    # Cleanup
    if os.path.exists(dst_func):
        os.remove(dst_func)
    # Don't delete requirements.txt yet, it might be needed for the actual deployment build if it's GCF gen 1?
    # Actually gcloud functions deploy uploads the source.

if __name__ == "__main__":
    deploy()
