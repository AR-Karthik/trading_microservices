import os
import requests
import zipfile
import pandas as pd
import redis
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('shoonya_master')

MASTER_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ZIP_PATH = os.path.join(DOWNLOAD_DIR, "NFO_symbols.txt.zip")
TXT_PATH = os.path.join(DOWNLOAD_DIR, "NFO_symbols.txt")

def download_and_extract():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    logger.info(f"Downloading master file from {MASTER_URL}...")
    response = requests.get(MASTER_URL, stream=True)
    response.raise_for_status()
    with open(ZIP_PATH, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            
    logger.info("Extracting...")
    with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
        zip_ref.extractall(DOWNLOAD_DIR)
    logger.info(f"Extracted to {TXT_PATH}")

def parse_and_cache():
    logger.info("Parsing master file...")
    try:
        # File has trailing commas
        df = pd.read_csv(TXT_PATH, index_col=False, dtype=str)
    except Exception as e:
        logger.error(f"Failed to read CSV: {e}")
        return
    
    if 'TradingSymbol' not in df.columns or 'Token' not in df.columns:
        logger.error("Required columns ('TradingSymbol', 'Token') not found in master file.")
        return
        
    logger.info(f"Loaded {len(df)} symbols. Syncing to Redis...")
    
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        r.ping()
        
        symbol_to_token = dict(zip(df['TradingSymbol'], df['Token']))
        token_to_symbol = dict(zip(df['Token'], df['TradingSymbol']))
        
        r.delete('shoonya_nfo_tokens')
        r.delete('shoonya_nfo_symbols')
        
        chunk_size = 10000
        items_sym_tok = list(symbol_to_token.items())
        items_tok_sym = list(token_to_symbol.items())
        
        for i in range(0, len(items_sym_tok), chunk_size):
            r.hset('shoonya_nfo_tokens', mapping=dict(items_sym_tok[i:i+chunk_size]))
            
        for i in range(0, len(items_tok_sym), chunk_size):
            r.hset('shoonya_nfo_symbols', mapping=dict(items_tok_sym[i:i+chunk_size]))
            
        logger.info("Successfully synced NFO symbols to Redis (shoonya_nfo_tokens, shoonya_nfo_symbols).")
    except Exception as e:
        logger.error(f"Redis sync failed: {e}")

def get_token(symbol: str) -> str:
    """Helper function to fetch Token for a given TradingSymbol from Redis"""
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        return r.hget('shoonya_nfo_tokens', symbol)
    except Exception:
        return None

def get_symbol(token: str) -> str:
    """Helper function to fetch TradingSymbol for a given Token from Redis"""
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        return r.hget('shoonya_nfo_symbols', token)
    except Exception:
        return None

if __name__ == "__main__":
    download_and_extract()
    parse_and_cache()
