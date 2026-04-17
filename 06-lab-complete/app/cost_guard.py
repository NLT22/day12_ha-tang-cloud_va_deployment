"""
Cost Guard — giới hạn $10/tháng mỗi user.

Track spending trong Redis (key reset đầu tháng tự động qua TTL).
Fallback về in-memory khi không có Redis.
"""
from datetime import datetime
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
_monthly_spend: dict[str, float] = {}


def check_budget(user_id: str, estimated_cost: float = 0.001) -> None:
    """
    Raise HTTPException(402) nếu user đã vượt budget tháng này.
    Tự động track và cộng dồn chi phí.
    """
    month_key = datetime.now().strftime("%Y-%m")
    if _use_redis:
        _check_redis(user_id, month_key, estimated_cost)
    else:
        _check_memory(user_id, month_key, estimated_cost)


def _check_redis(user_id: str, month_key: str, cost: float):
    key = f"budget:{user_id}:{month_key}"
    current = float(_redis.get(key) or 0)
    if current + cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget of ${settings.monthly_budget_usd:.2f} exceeded. "
                f"Current spend: ${current:.4f}. Resets next month."
            ),
        )
    _redis.incrbyfloat(key, cost)
    _redis.expire(key, 32 * 24 * 3600)  # 32 ngày TTL — tự xóa sau tháng


def _check_memory(user_id: str, month_key: str, cost: float):
    bucket = f"{user_id}:{month_key}"
    current = _monthly_spend.get(bucket, 0.0)
    if current + cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget of ${settings.monthly_budget_usd:.2f} exceeded. "
                f"Current spend: ${current:.4f}. Resets next month."
            ),
        )
    _monthly_spend[bucket] = current + cost


def get_monthly_spend(user_id: str) -> float:
    """Trả về tổng chi phí tháng hiện tại của user."""
    month_key = datetime.now().strftime("%Y-%m")
    if _use_redis:
        key = f"budget:{user_id}:{month_key}"
        return float(_redis.get(key) or 0)
    return _monthly_spend.get(f"{user_id}:{month_key}", 0.0)
