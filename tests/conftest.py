import sys
import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.mq import MQManager

@pytest.fixture
def mock_mq():
    mq = MagicMock(spec=MQManager)
    mq.publish = AsyncMock()
    mq.subscribe = AsyncMock()
    return mq

@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock()
    redis.lpush = AsyncMock()
    redis.ltrim = AsyncMock()
    redis.lrange = AsyncMock(return_value=[])
    return redis

@pytest.fixture
def mock_shoonya():
    api = MagicMock()
    api.login = MagicMock(return_value={"stat": "Ok"})
    api.place_order = MagicMock(return_value={"stat": "Ok", "norenordno": "12345"})
    api.subscribe = MagicMock()
    return api

@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    pool.acquire = MagicMock()
    
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()
    
    # Setup context managers
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock()
    
    return pool
