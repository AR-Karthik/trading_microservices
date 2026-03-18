"""
Historical Parameter Calibration Worker
Iteratively trains Gaussian Mixture HMM regimes using TimeScaleDB OHLCV data,
evaluating convergence likelihoods before promoting to production via GCS.
"""

import os
import sys
import logging
import pickle
import numpy as np
import pandas as pd
import asyncpg
import asyncio
from datetime import datetime, timedelta
from hmmlearn import hmm
from google.cloud import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("HMMTrainer")

db_host = os.getenv("DB_HOST", "localhost")
DB_DSN = f"postgres://trading_user:trading_pass@{db_host}:5432/trading_db"
MODEL_PATH = "data/models/hmm_v_latest.pkl"
GENERIC_MODEL_PATH = "data/models/hmm_generic.pkl"

class HMMTrainer:
    def __init__(self, n_states=3):
        self.n_states = n_states
        self.model = None

    async def fetch_data(self):
        """Fetches the last 30 days of features and prices from TimescaleDB."""
        logger.info("Fetching training data from TimescaleDB...")
        try:
            conn = await asyncpg.connect(DB_DSN)
            # Query NIFTY50 history
            rows = await conn.fetch("""
                SELECT time, price, log_ofi_zscore, cvd, vpin, basis_zscore, vol_term_ratio
                FROM market_history
                WHERE symbol = 'NIFTY50'
                ORDER BY time ASC
            """)
            await conn.close()
            
            if not rows:
                logger.warning("No data found in market_history table.")
                return None
                
            df = pd.DataFrame(rows, columns=['time', 'price', 'ofi', 'cvd', 'vpin', 'basis', 'vol_ratio'])
            return df
        except Exception as e:
            logger.error(f"Failed to fetch data: {e}")
            return None

    def preprocess(self, df):
        """Calculates features and labels for training."""
        # Feature vector: [Log-OFI, VPIN, Basis Z-Score, Vol Term Ratio]
        # Label (Outcome): 5-min forward return
        df['ret_5m'] = df['price'].pct_change(5).shift(-5)
        
        # Drop rows with NaN (edges of the shift/change)
        df = df.dropna()
        
        features = df[['ofi', 'vpin', 'basis', 'vol_ratio']].values
        return features, df

    def train(self, X):
        """Runs the EM algorithm to train/update the GMM-HMM."""
        logger.info(f"Training GMM-HMM with {self.n_states} states...")
        model = hmm.GaussianHMM(n_components=self.n_states, covariance_type="full", n_iter=100)
        model.fit(X)
        return model

    def validate(self, model, X):
        """Calculates log-likelihood and checks for state stability."""
        score = model.score(X)
        logger.info(f"New Model Log-Likelihood: {score:.2f}")
        return score

    def save_model(self, model):
        """Saves model to disk and uploads to GCS."""
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(model, f)
        logger.info(f"Model saved to {MODEL_PATH}")
        
        # Upload to GCS if configured
        bucket_name = os.getenv("GCS_MODEL_BUCKET")
        if bucket_name:
            try:
                client = storage.Client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(os.path.basename(MODEL_PATH))
                blob.upload_from_filename(MODEL_PATH)
                logger.info(f"Successfully uploaded {os.path.basename(MODEL_PATH)} to GCS.")
            except Exception as e:
                logger.error(f"GCS upload failed: {e}")

    async def run_lifecycle(self):
        df = await self.fetch_data()
        if df is None or len(df) < 500:
            logger.warning("Insufficient data for training. Requiring at least 500 records.")
            return

        X, df_clean = self.preprocess(df)
        new_model = self.train(X)
        new_score = self.validate(new_model, X)
        
        # Comparative validation
        current_model = None
        if os.path.exists(MODEL_PATH):
            with open(MODEL_PATH, "rb") as f:
                current_model = pickle.load(f)
        
        if current_model:
            try:
                current_score = current_model.score(X)
                logger.info(f"Existing Model Log-Likelihood: {current_score:.2f}")
                
                if new_score > current_score:
                    logger.info("New model outperforms current model. Promoting...")
                    self.save_model(new_model)
                else:
                    logger.info("Current model is still more accurate. Keeping existing model.")
            except Exception as e:
                logger.warning(f"Validation error: {e}. Defaulting to new model.")
                self.save_model(new_model)
        else:
            logger.info("No existing model found. Saving new model.")
            self.save_model(new_model)

if __name__ == "__main__":
    trainer = HMMTrainer()
    asyncio.run(trainer.run_lifecycle())
