#!/bin/bash
# Project K.A.R.T.H.I.K. - DB Backup & WAL Shipping (SRS #110, #112)
# =================================================================

set -e

# Load environment variables
source .env

PROJECT_ID=${GCP_PROJECT_ID:-"karthiks-trading-assistant"}
BUCKET=${GCS_MODEL_BUCKET:-"karthiks-trading-models"}
BACKUP_DIR="/mnt/hot_nvme/backups"
WAL_BUFFER="/mnt/hot_nvme/wal_buffer"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"
mkdir -p "$WAL_BUFFER"

echo "--- Starting PostgreSQL Backup ($TIMESTAMP) ---"

# 1. Full SQL Dump
FILENAME="db_backup_$TIMESTAMP.sql.gz"
echo "Extracting dump from timescaledb container..."
docker exec trading_timescaledb pg_dump -U trading_user trading_db | gzip > "$BACKUP_DIR/$FILENAME"

# 2. Upload to GCS (Backgrounded to avoid blocking)
echo "Shipping full backup to GCS: gs://$BUCKET/backups/db/$FILENAME"
ionice -c 3 gcloud storage cp "$BACKUP_DIR/$FILENAME" "gs://$BUCKET/backups/db/$FILENAME" --project="$PROJECT_ID" &

# 3. WAL Shipping (Non-blocking background loop)
# This handles segments moved to the buffer by PostgreSQL's archive_command
echo "Initiating background WAL sync..."
(
    while true; do
        if [ "$(ls -A $WAL_BUFFER)" ]; then
            echo "Syncing WAL segments to GCS..."
            # Using ionice -c 3 (Idle priority) to ensure zero impact on market scanner
            ionice -c 3 gcloud storage cp "$WAL_BUFFER/*" "gs://$BUCKET/backups/wal/" --project="$PROJECT_ID" && rm -f "$WAL_BUFFER"/*
        fi
        sleep 60
    done
) &

echo "Backup script initiated. GCS uploads are running in background with Idle priority."
