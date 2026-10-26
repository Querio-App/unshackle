"""Small HTTP client with retries and a per-minute rate limit. Every source uses it."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx


class SourceError(RuntimeError):
    pass


def missing_extra(extra: str, what: str) -> str:
    """The error for a feature whose optional dependencies aren't installed, naming the extra and how to add it."""
    spec = f"unshackle[run,{extra}]"
    return f'{what} needs the `{extra}` extra: uv tool install --force "{spec}" (or pip install "{spec}")'


def env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SourceError(f"missing environment variable {name}")
    return value


class Client:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        per_minute: int = 60,
        timeout: float = 60.0,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            headers=headers or {},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self.min_interval = 60.0 / per_minute if per_minute else 0.0
        self.max_retries = max_retries
        self._last = 0.0

    def set_header(self, key: str, value: str) -> None:
        self.http.headers[key] = value

    def _wait(self) -> None:
        if not self.min_interval:
            return
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self._wait()
            try:
                resp = self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 30)
                continue
            if resp.status_code >= 400:
                raise SourceError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp
        raise SourceError(f"{method} {path} failed after retries")

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.get(path, **kwargs).json()

    def post_json(self, path: str, **kwargs: Any) -> Any:
        return self.post(path, **kwargs).json()
