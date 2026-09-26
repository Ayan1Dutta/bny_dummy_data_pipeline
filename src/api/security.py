from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status

from src.config import settings

_buckets: dict[str, deque[float]] = defaultdict(deque)


def verify_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    if not settings.api_keys_enabled:
        return "anonymous"
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return "service-account"


def rate_limit(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    identity = (x_api_key or request.client.host if request.client else "unknown") or "unknown"
    window = _buckets[identity]
    now = time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
    window.append(now)


def reset_rate_limits() -> None:
    _buckets.clear()
