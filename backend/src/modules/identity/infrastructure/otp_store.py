"""Redis-backed OTP store. Codes live only in Redis (TTL-native); Postgres
keeps the audit trail. Attempts and resend cooldown are atomic Redis ops.
"""

from __future__ import annotations

from redis.asyncio import Redis

from src.modules.identity.application.ports import OtpChallenge
from src.shared_kernel.domain.value_objects import Mobile


class RedisOtpStore:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    @staticmethod
    def _key(mobile: Mobile) -> str:
        return f"otp:{mobile}"

    @staticmethod
    def _cooldown_key(mobile: Mobile) -> str:
        return f"otp:cooldown:{mobile}"

    async def save(self, mobile: Mobile, code_hash: str, ttl: int) -> None:
        key = self._key(mobile)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping={"hash": code_hash, "attempts": 0})
            pipe.expire(key, ttl)
            await pipe.execute()

    async def get(self, mobile: Mobile) -> OtpChallenge | None:
        data = await self._r.hgetall(self._key(mobile))
        if not data:
            return None
        return OtpChallenge(
            code_hash=data["hash"],
            attempts=int(data.get("attempts", 0)),
            expires_in=await self._r.ttl(self._key(mobile)),
        )

    async def increment_attempts(self, mobile: Mobile) -> int:
        return int(await self._r.hincrby(self._key(mobile), "attempts", 1))

    async def delete(self, mobile: Mobile) -> None:
        await self._r.delete(self._key(mobile))

    async def seconds_until_resend(self, mobile: Mobile) -> int:
        ttl = await self._r.ttl(self._cooldown_key(mobile))
        return ttl if ttl and ttl > 0 else 0

    async def mark_sent(self, mobile: Mobile, cooldown: int) -> None:
        await self._r.set(self._cooldown_key(mobile), "1", ex=cooldown)
