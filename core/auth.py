"""
Centralized Authentication and Configuration Manager
Handles credential resolution and connection string generation for system resources.
"""
import os

from dotenv import load_dotenv

_dotenv_loaded = False

def _ensure_dotenv():
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv()
        _dotenv_loaded = True

def get_redis_url():
    """Returns a fully authenticated Redis URL based on .env variables."""
    _ensure_dotenv()
    host = os.getenv("REDIS_HOST", "redis")
    password = os.getenv("REDIS_PASSWORD", "").strip()
    
    # If password is empty, don't include the auth part
    if not password:
        return f"redis://{host}:6379"
    
    # URL format: redis://:password@host:port
    return f"redis://:{password}@{host}:6379"

def get_db_dsn():
    """Returns a fully authenticated Postgres DSN based on .env variables."""
    _ensure_dotenv()
    user = os.getenv("DB_USER", "trading_user")
    password = os.getenv("DB_PASS", "trading_pass")
    host = os.getenv("DB_HOST", "timescaledb")
    name = os.getenv("DB_NAME", "trading_db")
    
    # URL format: postgres://user:password@host:5432/dbname
    return f"postgres://{user}:{password}@{host}:5432/{name}"

def get_db_config():
    """Returns a dictionary for asyncpg.connect() or pool.acquire()."""
    _ensure_dotenv()
    return {
        "user": os.getenv("DB_USER", "trading_user"),
        "password": os.getenv("DB_PASS", "trading_pass"),
        "host": os.getenv("DB_HOST", "timescaledb"),
        "database": os.getenv("DB_NAME", "trading_db"),
        "port": 5432
    }

HEDGE_RESERVE_PCT = 0.15 # [Parity] 15% margin reserve for hedge legs
