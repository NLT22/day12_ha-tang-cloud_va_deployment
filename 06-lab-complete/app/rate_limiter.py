"""
Rate limiter — Sliding Window Counter.

Dùng Redis nếu có (stateless, hoạt động đúng khi scale nhiều instance).
Fallback về in-memory khi không có Redis (chỉ phù hợp 1 instance).
"""
import time
from collections import defaultdict, deque
from fastapi import HTTPException
from app.config import settings

# ── Redis backend ─────────────────────────────────────────
_redis = None
_use_redis = False

if settings.redis_url:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(settings.redis_url, decode_responses=True)
        _redis.ping()
        _use_redis = True
    except Exception:
        pass

# ── In-memory fallback ────────────────────────────────────
_windows: dict[str, deque] = defaultdict(deque)


def check_rate_limit(user_id: str) -> None:
    """
    Raise HTTPException(429) nếu user vượt quá rate limit.
    Sliding window: đếm request trong 60 giây gần nhất.
    """
    limit = settings.rate_limit_per_minute
    window_seconds = 60
    now = time.time()

    if _use_redis:
        _check_redis(user_id, limit, window_seconds, now)
    else:
        _check_memory(user_id, limit, window_seconds, now)


def _check_redis(user_id: str, limit: int, window: int, now: float):
    key = f"ratelimit:{user_id}"
    pipe = _redis.pipeline()
    # Dùng sorted set: score = timestamp, member = unique request id
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zcard(key)
    pipe.zadd(key, {str(now): now})
    pipe.expire(key, window)
    _, count, *_ = pipe.execute()
    if count >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min. Try again later.",
            headers={"Retry-After": "60"},
        )


def _check_memory(user_id: str, limit: int, window: int, now: float):
    dq = _windows[user_id]
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min. Try again later.",
            headers={"Retry-After": "60"},
        )
    dq.append(now)
