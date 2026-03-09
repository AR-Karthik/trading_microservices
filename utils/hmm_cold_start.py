"""
utils/hmm_cold_start.py
=======================
The HMM Bootstrapper (SRS Phase 11)

Responsibilities:
- Downloads historical Nifty 50 minute data from Kaggle (debashis74017/nifty-50-minute-data).
- Downloads recent 1-minute data from Yahoo Finance (^NSEI) for the last 30 days.
- Preprocesses and merges data to ensure the HMM starts with a valid "Generic" model.
- Saves the initial model to 'data/models/hmm_generic.pkl'.
"""

import os
import sys
import logging
import pickle
import pandas as pd
import numpy as np
import kagglehub
import yfinance as yf
from hmmlearn import hmm
from google.cloud import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("HMMColdStart")

MODEL_PATH = "data/models/hmm_generic.pkl"

class HMMColdStarter:
    def __init__(self, n_states=3):
        self.n_states = n_states
        os.makedirs("data/models", exist_ok=True)

    def download_kaggle_data(self):
        """Downloads historical Nifty 50 minute data from Kaggle."""
        logger.info("Downloading historical data from Kaggle...")
        try:
            path = kagglehub.dataset_download("debashis74017/nifty-50-minute-data")
            logger.info(f"Kaggle data downloaded to: {path}")
            # Identify the CSV file in the downloaded path
            for root, dirs, files in os.walk(path):
                for file in files:
                    if file.endswith(".csv"):
                        return os.path.join(root, file)
            return None
        except Exception as e:
            logger.error(f"Kaggle download failed: {e}")
            return None

    def download_yfinance_data(self):
        """Downloads recent 1-minute data for the last 7 days (limit for 1m interval)."""
        logger.info("Downloading recent data from Yahoo Finance...")
        try:
            ticker = "^NSEI"
            # Important: 1m interval is limited to 7 days in yfinance
            data = yf.download(ticker, period="7d", interval="1m")
            if data.empty:
                logger.warning("Yahoo Finance returned empty DataFrame.")
            return data
        except Exception as e:
            logger.error(f"Yahoo Finance download failed: {e}")
            return None

    def preprocess_generic(self, df, source_name):
        """Robustly extracts datetime and price from a DataFrame."""
        df = df.copy()
        
        # 1. Handle MultiIndex (YF)
        if isinstance(df.columns, pd.MultiIndex):
            if 'Close' in df.columns.get_level_values(0):
                df = df['Close']
            else:
                logger.warning(f"No 'Close' in MultiIndex for {source_name}. Flattening...")
                df.columns = [f"{c[0]}_{c[1]}" for c in df.columns]

        # 2. Flatten/Rename to lowercase
        df.columns = [str(c).lower() for c in df.columns]
        
        # 3. Pull time from index if it's a DatetimeIndex
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]

        # 4. Identify Datetime Column
        time_candidates = ['datetime', 'timestamp', 'date', 'index']
        time_col = next((c for c in df.columns if c in time_candidates), None)
        
        if time_col:
            df = df.rename(columns={time_col: 'datetime'})
        else:
            # Fallback: find first column with 'date' or 'time' in name
            time_col = next((c for c in df.columns if 'date' in c or 'time' in c), df.columns[0])
            df = df.rename(columns={time_col: 'datetime'})

        # 5. Identify Price Column
        price_candidates = ['close', 'price', 'last', 'adj close']
        price_col = next((c for c in df.columns if c in price_candidates), None)
        
        if not price_col:
            # Fallback: first column that isn't 'datetime'
            price_col = next((c for c in df.columns if c != 'datetime'), None)
            
        if price_col:
            df = df.rename(columns={price_col: 'price'})
        else:
            logger.error(f"Could not find price column in {source_name}")
            return pd.DataFrame()

        # Convert datetime and normalize to naive UTC
        df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
        if df['datetime'].dt.tz is not None:
            df['datetime'] = df['datetime'].dt.tz_convert(None)
        else:
            df['datetime'] = df['datetime'].dt.tz_localize(None)

        df = df.dropna(subset=['datetime', 'price'])
        
        logger.info(f"{source_name} preprocessed. Shape: {df.shape}")
        return df[['datetime', 'price']]

    def preprocess_kaggle(self, csv_path):
        logger.info(f"Preprocessing Kaggle data: {csv_path}")
        df = pd.read_csv(csv_path)
        return self.preprocess_generic(df, "Kaggle")

    def preprocess_yf(self, df_yf):
        logger.info("Preprocessing YF data")
        if df_yf is None or df_yf.empty:
            return pd.DataFrame()
        return self.preprocess_generic(df_yf, "YahooFinance")

    def engineering_features(self, df):
        """Calculates features for HMM (Log-Return Std/RV as fallback for missing microstructure)."""
        # Since we don't have OFI/VPIN in historical OHLC, we use RV and Price Velocity
        df['log_ret'] = np.log(df['price'] / df['price'].shift(1))
        df['rv'] = df['log_ret'].rolling(window=30).std()
        df['velocity'] = df['log_ret'].rolling(window=5).mean()
        
        # We need to map these to the 4 features expected by MetaRouter:
        # [log_ofi_z, rv, basis_z, vol_term_ratio]
        # In Cold Start, we'll use: [Velocity_Z, RV, 0 (Flat), 1 (Neutral)]
        
        df['velocity_z'] = (df['velocity'] - df['velocity'].rolling(200).mean()) / df['velocity'].rolling(200).std()
        df = df.dropna()
        
        # Match the MetaRouter feature shape (4 features)
        X = df[['velocity_z', 'rv']].values
        # Pad with 0s for missing microstructure features to maintain shape
        X_padded = np.column_stack([X, np.zeros(len(X)), np.ones(len(X))])
        
        return X_padded

    def train_and_save(self, X):
        """Trains the GMM-HMM and saves as generic model."""
        logger.info(f"Training Generic HMM with {len(X)} samples...")
        model = hmm.GaussianHMM(
            n_components=self.n_states,
            covariance_type="full",
            n_iter=100,
            random_state=42
        )
        model.fit(X)
        
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(model, f)
        logger.info(f"Generic model saved to {MODEL_PATH}")
        
        # Upload to GCS if configured
        bucket_name = os.getenv("GCS_MODEL_BUCKET")
        if bucket_name:
            self.upload_to_gcs(MODEL_PATH, bucket_name)

    def upload_to_gcs(self, local_path, bucket_name):
        """Uploads the model file to Google Cloud Storage."""
        logger.info(f"Uploading {local_path} to GCS bucket: {bucket_name}")
        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            if not bucket.exists():
                logger.warning(f"Bucket {bucket_name} does not exist. Skipping GCS upload.")
                return
            
            blob = bucket.blob(os.path.basename(local_path))
            blob.upload_from_filename(local_path)
            logger.info(f"Successfully uploaded to GCS: gs://{bucket_name}/{os.path.basename(local_path)}")
        except Exception as e:
            logger.error(f"GCS upload failed: {e}")

    def run(self):
        # 1. Get Kaggle Data
        kaggle_csv = self.download_kaggle_data()
        if not kaggle_csv:
            logger.error("Failed to get Kaggle data. Aborting.")
            return

        df_kaggle = self.preprocess_kaggle(kaggle_csv)
        
        # 2. Get YF Data
        df_yf_raw = self.download_yfinance_data()
        df_yf = self.preprocess_yf(df_yf_raw) if df_yf_raw is not None else pd.DataFrame()
        
        # 3. Merge (Optional but recommended for continuity)
        df_total = pd.concat([df_kaggle, df_yf]).sort_values('datetime').drop_duplicates('datetime')
        
        # 4. Feature Engineering
        X = self.engineering_features(df_total)
        
        # 5. Train
        if len(X) > 1000:
            self.train_and_save(X)
        else:
            logger.error("Insufficient data points for robust cold-start.")

if __name__ == "__main__":
    starter = HMMColdStarter()
    starter.run()
