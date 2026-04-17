"""API Key authentication."""
from fastapi import Header, HTTPException
from app.config import settings


def verify_api_key(x_api_key: str = Header(default=None)) -> str:
    """
    Verify X-API-Key header.
    Return the key (used as user_id bucket) if valid.
    Raise 401 if missing, 403 if wrong.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Include header: X-API-Key: <key>",
        )
    if x_api_key != settings.agent_api_key:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key.",
        )
    return x_api_key
