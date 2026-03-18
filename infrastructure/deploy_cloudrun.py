#!/usr/bin/env python3
"""
Cloud Run Provisioning Tool
Automates the containerization and managed serverless deployment of the 
Command Center Dashboard, establishing direct ties to Firestore and BigQuery infrastructure.
"""
import os
import subprocess
import argparse
import sys

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "karthiks-trading-assistant")
REGION = "asia-south1"
SERVICE_NAME = "karthik-dashboard"
IMAGE_NAME = f"gcr.io/{PROJECT_ID}/{SERVICE_NAME}"
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "cloudrun")


def run_cmd(cmd: str, check=True):
    """Run a shell command and print output."""
    print(f"  → {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())
    if check and result.returncode != 0:
        print(f"  ✗ Command failed with exit code {result.returncode}")
        return False
    return True


def enable_apis():
    """Enable required GCP APIs."""
    apis = [
        "run.googleapis.com",
        "cloudbuild.googleapis.com",
        "firestore.googleapis.com",
        "storage.googleapis.com",
        "iap.googleapis.com",
        "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com",
    ]
    for api in apis:
        run_cmd(f"gcloud services enable {api} --project={PROJECT_ID} --quiet", check=False)


def create_gcs_bucket():
    """Create the GCS model bucket if it doesn't exist."""
    bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
    result = subprocess.run(
        f"gcloud storage buckets describe gs://{bucket} --project={PROJECT_ID} --quiet",
        shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Creating GCS bucket gs://{bucket} in {REGION}...")
        run_cmd(f"gcloud storage buckets create gs://{bucket} --location={REGION} --project={PROJECT_ID} --quiet")
    else:
        print(f"GCS bucket gs://{bucket} already exists.")


def setup_firestore():
    """Create Firestore database in Native mode."""
    # Check if Firestore is already initialized
    result = subprocess.run(
        f"gcloud firestore databases list --project={PROJECT_ID} --format=json --quiet",
        shell=True, capture_output=True, text=True
    )
    if '"name"' in result.stdout:
        print("Firestore database already exists.")
        return
    
    print("Creating Firestore database (Native mode)...")
    run_cmd(
        f"gcloud firestore databases create --project={PROJECT_ID} --location={REGION} --type=firestore-native --quiet",
        check=False
    )


def setup_bigquery():
    """Setup BigQuery dataset and External Tables pointing to GCS Parquet files."""
    dataset = "trading_analytics"
    bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
    
    print(f"\n=== Setting up BigQuery Dataset: {dataset} ===")
    run_cmd(f"bq mk --if_exists --dataset {PROJECT_ID}:{dataset}")

    # Mount high-frequency Parquet market state archives into BigQuery for ad-hoc querying
    market_table = f"{dataset}.market_history_external"
    print(f"Creating External Table: {market_table}")
    run_cmd(
        f"bq mk --if_exists --external_table_definition=PARQUET=gs://{bucket}/market_history/*.parquet "
        f"{market_table}"
    )

    # Maintain raw tick telemetry external tables mapping to GCS
    tick_table = f"{dataset}.tick_history_external"
    print(f"Creating External Table: {tick_table}")
    run_cmd(
        f"bq mk --if_exists --external_table_definition=PARQUET=gs://{bucket}/tick_history/*.parquet "
        f"{tick_table}"
    )

    # 2. Trade History External Table
    trade_table = f"{dataset}.trade_history_external"
    print(f"Creating External Table: {trade_table}")
    run_cmd(
        f"bq mk --if_exists --external_table_definition=PARQUET=gs://{bucket}/trade_history/*.parquet "
        f"{trade_table}"
    )
    
    # Get Project Number for Service Account
    result = subprocess.run(
        f"gcloud projects describe {PROJECT_ID} --format='value(projectNumber)' --quiet",
        shell=True, capture_output=True, text=True
    )
    project_number = result.stdout.strip()
    if not project_number:
        print("Failed to get project number. IAM may fail.")
        return

    sa_email = f"{project_number}-compute@developer.gserviceaccount.com"
    
    print(f"Granting BigQuery, Firestore, and Storage permissions to {sa_email}...")
    roles = [
        'roles/bigquery.dataViewer',
        'roles/bigquery.jobUser',
        'roles/datastore.user',
        'roles/storage.objectViewer'
    ]
    for role in roles:
        run_cmd(
            f"gcloud projects add-iam-policy-binding {PROJECT_ID} "
            f"--member='serviceAccount:{sa_email}' "
            f"--role='{role}' --quiet"
        )
    
    # 3. Secret Manager access
    run_cmd(
        f"gcloud secrets add-iam-policy-binding DASHBOARD_ACCESS_KEY "
        f"--member='serviceAccount:{sa_email}' "
        f"--role='roles/secretmanager.secretAccessor' --project={PROJECT_ID} --quiet",
        check=False
    )


def setup_secrets():
    """Create the dashboard access key secret if it doesn't exist."""
    print("\n=== Initializing Dashboard Secret Key ===")
    
    # Check if secret exists
    result = subprocess.run(
        f"gcloud secrets describe DASHBOARD_ACCESS_KEY --project={PROJECT_ID} --quiet",
        shell=True, capture_output=True, text=True
    )
    
    if result.returncode != 0:
        import uuid
        new_key = str(uuid.uuid4()).replace("-", "").upper()[:16]
        print(f"Creating new DASHBOARD_ACCESS_KEY secret...")
        run_cmd(f"echo {new_key} | gcloud secrets create DASHBOARD_ACCESS_KEY --data-file=- --project={PROJECT_ID} --quiet")
    else:
        print("DASHBOARD_ACCESS_KEY secret already exists.")


def deploy_cloudrun():
    """Build and deploy the Cloud Run dashboard."""
    print("\n=== Deploying Cloud Run Dashboard ===")
    
    # Build and deploy using Cloud Build
    run_cmd(
        f"gcloud run deploy {SERVICE_NAME} "
        f"--source {DASHBOARD_DIR} "
        f"--region {REGION} "
        f"--project {PROJECT_ID} "
        f"--platform managed "
        f"--allow-unauthenticated "
        f"--set-env-vars GCP_PROJECT_ID={PROJECT_ID},GCS_MODEL_BUCKET={os.getenv('GCS_MODEL_BUCKET', 'karthiks-trading-models')} "
        f"--memory 256Mi "
        f"--cpu 1 "
        f"--min-instances 0 "
        f"--max-instances 2 "
        f"--timeout 60 "
        f"--quiet"
    )
    
    # Get the URL
    result = subprocess.run(
        f"gcloud run services describe {SERVICE_NAME} --region={REGION} --project={PROJECT_ID} --format='value(status.url)' --quiet",
        shell=True, capture_output=True, text=True
    )
    url = result.stdout.strip()
    if url:
        print(f"\n🎉 Dashboard deployed at: {url}")
        print(f"   Open in your browser to access the trading dashboard.")
    return url


def teardown():
    """Delete all Cloud Run resources."""
    print("\n=== Tearing Down Cloud Run Resources ===")
    
    # Delete Cloud Run service
    run_cmd(
        f"gcloud run services delete {SERVICE_NAME} --region={REGION} --project={PROJECT_ID} --quiet",
        check=False
    )
    
    # Delete GCS bucket contents and bucket
    bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
    run_cmd(f"gcloud storage rm -r gs://{bucket} --project={PROJECT_ID}", check=False)
    
    # Delete Firestore data (optional — costs nothing)
    print("Note: Firestore data remains (free tier). Delete manually if needed.")
    
    print("\n✅ All billable Cloud Run resources deleted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cloud Run Dashboard Deployer")
    parser.add_argument("--action", choices=["deploy", "teardown", "setup-only"], default="deploy")
    args = parser.parse_args()

    if args.action == "deploy":
        enable_apis()
        create_gcs_bucket()
        setup_firestore()
        setup_bigquery()
        setup_secrets()
        deploy_cloudrun()
    elif args.action == "setup-only":
        enable_apis()
        create_gcs_bucket()
        setup_firestore()
        setup_bigquery()
        print("\n✅ Cloud infrastructure ready. Run with --action deploy to push the dashboard.")
    elif args.action == "teardown":
        teardown()
