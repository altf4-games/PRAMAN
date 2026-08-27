"""Redis client provider. Functions elsewhere accept a redis client as a
parameter (duck-typed against `redis.asyncio.Redis`'s interface) rather
than importing this module — tests pass a `fakeredis.aioredis.FakeRedis`
instance instead, so nothing needs a real Redis server to be exercised.
"""

from __future__ import annotations

from redis.asyncio import Redis

from praman.config import get_settings


def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)
