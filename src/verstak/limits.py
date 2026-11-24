import hashlib
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis

from verstak.config import settings

_BUCKET = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) + tonumber(stamp[2]) / 1000000
local capacity, rate = tonumber(ARGV[1]), tonumber(ARGV[2])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local prev = tonumber(data[2]) or now
local tokens = math.min(capacity, (tonumber(data[1]) or capacity) + math.max(0, now-prev)*rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil((1-tokens)/rate)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity/rate) + 1)
return wait
"""


async def retry_after(key: str) -> int:
    cfg = settings()
    digest = hashlib.sha256(key.encode()).hexdigest()
    async with Redis.from_url(cfg.redis_url, socket_timeout=3, socket_connect_timeout=3) as redis:
        reply = await cast(
            Awaitable[int],
            redis.eval(
                _BUCKET, 1, f"verstak:quota:{digest}", cfg.rate_capacity, cfg.rate_per_minute / 60
            ),
        )
    return reply
