import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
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
