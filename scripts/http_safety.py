from __future__ import annotations

from typing import Any

import httpx


class FailedResponse:
    def __init__(self, error: Exception):
        self.status_code = 0
        self.text = f"{type(error).__name__}: {error}"

    def json(self):
        raise ValueError(self.text)


def safe_get(client: Any, path: str, params: dict | None = None) -> Any:
    for attempt in range(2):
        try:
            return client.get(path, params=params or {})
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError, httpx.WriteError) as exc:
            if attempt == 1:
                return FailedResponse(exc)
    raise AssertionError("unreachable")
