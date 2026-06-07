"""Shared async Redis client (cache, OTP store, rate limiting, sessions)."""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from src.config.settings import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Lazily-initialized singleton. Connection pooling handled by redis-py."""
    global _redis
    if _redis is None:
        _redis = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
