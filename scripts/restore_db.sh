#!/bin/bash
# Project K.A.R.T.H.I.K. - DB Restore from GCS (SRS #118)
# =========================================================

set -e

source .env

PROJECT_ID=${GCP_PROJECT_ID:-"karthiks-trading-assistant"}
BUCKET=${GCS_MODEL_BUCKET:-"karthiks-trading-models"}

if [ -z "$1" ]; then
    echo "Usage: ./scripts/restore_db.sh [FILENAME_ON_GCS]"
    echo "Example: ./scripts/restore_db.sh db_backup_20260314_130000.sql.gz"
    exit 1
fi

FILE=$1
LOCAL_FILE="/tmp/$FILE"

echo "--- Initiating DB Restore ---"

# 1. Download from GCS
echo "Downloading gs://$BUCKET/backups/db/$FILE..."
gcloud storage cp "gs://$BUCKET/backups/db/$FILE" "$LOCAL_FILE" --project="$PROJECT_ID"

# 2. Restore to Container
echo "Restoring to timescaledb container (dropping existing data)..."
gunzip -c "$LOCAL_FILE" | docker exec -i trading_timescaledb psql -U trading_user -d trading_db

echo "--- Restore Complete! ---"
rm "$LOCAL_FILE"
