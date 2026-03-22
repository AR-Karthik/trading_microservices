import os
import requests
import zipfile
import pandas as pd
import redis
import logging
from core.auth import get_redis_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('shoonya_master')

MASTER_URLS = {
    "NFO": "https://api.shoonya.com/NFO_symbols.txt.zip",
    "BFO": "https://api.shoonya.com/BFO_symbols.txt.zip",
    "NSE": "https://api.shoonya.com/NSE_symbols.txt.zip",
    "BSE": "https://api.shoonya.com/BSE_symbols.txt.zip"
}

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def download_and_extract(exchange: str):
    url = MASTER_URLS.get(exchange)
    if not url: return
    
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    zip_path = os.path.join(DOWNLOAD_DIR, f"{exchange}_symbols.txt.zip")
    txt_path = os.path.join(DOWNLOAD_DIR, f"{exchange}_symbols.txt")
    
    logger.info(f"Downloading {exchange} master file from {url}...")
    response = requests.get(url, stream=True)
    response.raise_for_status()
    with open(zip_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            
    logger.info(f"Extracting {exchange}...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(DOWNLOAD_DIR)
        # Rename the generic 'NFO_symbols.txt' etc to exchange-specific if needed
        # Actually Shoonya zips usually contain a file like NFO_symbols.txt inside
        # Let's ensure we know the filename. 
        # Usually it matches the zip name minus .zip
        inner_file = zip_ref.namelist()[0]
        os.replace(os.path.join(DOWNLOAD_DIR, inner_file), txt_path)
    logger.info(f"Extracted to {txt_path}")

def parse_and_cache(exchange: str):
    txt_path = os.path.join(DOWNLOAD_DIR, f"{exchange}_symbols.txt")
    if not os.path.exists(txt_path):
        logger.error(f"Master file {txt_path} not found.")
        return

    logger.info(f"Parsing {exchange} master file...")
    try:
        df = pd.read_csv(txt_path, index_col=False, dtype=str)
    except Exception as e:
        logger.error(f"Failed to read CSV for {exchange}: {e}")
        return
    
    if 'TradingSymbol' not in df.columns or 'Token' not in df.columns:
        logger.error(f"Required columns not found in {exchange} master file.")
        return
        
    logger.info(f"Loaded {len(df)} {exchange} symbols. Syncing to Redis...")
    
    try:
        r = redis.from_url(get_redis_url(), decode_responses=True)
        r.ping()
        
        symbol_to_token = dict(zip(df['TradingSymbol'], df['Token']))
        token_to_symbol = dict(zip(df['Token'], df['TradingSymbol']))
        
        token_hash = f'shoonya_{exchange.lower()}_tokens'
        symbol_hash = f'shoonya_{exchange.lower()}_symbols'
        
        r.delete(token_hash)
        r.delete(symbol_hash)
        
        chunk_size = 10000
        items_sym_tok = list(symbol_to_token.items())
        items_tok_sym = list(token_to_symbol.items())
        
        for i in range(0, len(items_sym_tok), chunk_size):
            r.hset(token_hash, mapping=dict(items_sym_tok[i:i+chunk_size]))
            
        for i in range(0, len(items_tok_sym), chunk_size):
            r.hset(symbol_hash, mapping=dict(items_tok_sym[i:i+chunk_size]))
            
        logger.info(f"Successfully synced {exchange} symbols to Redis.")
    except Exception as e:
        logger.error(f"Redis sync failed for {exchange}: {e}")

def get_token(symbol: str, exchange: str = "NFO") -> str:
    """Helper function to fetch Token from Redis for a given exchange"""
    try:
        r = redis.from_url(get_redis_url(), decode_responses=True)
        return r.hget(f'shoonya_{exchange.lower()}_tokens', symbol)
    except Exception:
        return None

def get_symbol(token: str, exchange: str = "NFO") -> str:
    """Helper function to fetch TradingSymbol from Redis for a given exchange"""
    try:
        r = redis.from_url(get_redis_url(), decode_responses=True)
        return r.hget(f'shoonya_{exchange.lower()}_symbols', token)
    except Exception:
        return None

if __name__ == "__main__":
    for exch in MASTER_URLS.keys():
        try:
            download_and_extract(exch)
            parse_and_cache(exch)
        except Exception as e:
            logger.error(f"Failed to process {exch}: {e}")
