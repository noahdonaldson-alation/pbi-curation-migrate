"""Thin Alation REST client: refresh-token auth, retry with backoff, limit/skip paging."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    base_url: str
    user_id: int
    refresh_token: str
    verify_ssl: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        required = ["ALATION_BASE_URL", "ALATION_USER_ID", "ALATION_REFRESH_TOKEN"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                f"Missing environment variables: {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )
        return cls(
            base_url=os.environ["ALATION_BASE_URL"].rstrip("/"),
            user_id=int(os.environ["ALATION_USER_ID"]),
            refresh_token=os.environ["ALATION_REFRESH_TOKEN"],
            verify_ssl=os.environ.get("ALATION_VERIFY_SSL", "true").lower() != "false",
        )


class AlationError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AlationClient:
    def __init__(
        self,
        settings: Settings,
        *,
        page_size: int = 100,
        max_retries: int = 5,
        timeout: float = 60.0,
    ):
        self.settings = settings
        self.page_size = page_size
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=settings.base_url, verify=settings.verify_ssl, timeout=timeout
        )
        self._http.headers["TOKEN"] = self._create_access_token()

    def __enter__(self) -> AlationClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _create_access_token(self) -> str:
        r = self._http.post(
            "/integration/v1/createAPIAccessToken/",
            json={
                "refresh_token": self.settings.refresh_token,
                "user_id": self.settings.user_id,
            },
        )
        if r.status_code >= 400:
            # Don't echo the body: it can contain token details.
            raise AlationError(
                f"Could not create API access token (HTTP {r.status_code}). "
                "Check ALATION_USER_ID and that the refresh token is active.",
                r.status_code,
            )
        return r.json()["api_access_token"]

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.request(method, path, **kwargs)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                delay = 2**attempt
                log.warning("%s %s failed (%s); retrying in %ss", method, path, e, delay)
                time.sleep(delay)
                continue

            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                delay = _retry_after(r, default=2**attempt)
                log.warning("%s %s -> %s; retrying in %ss", method, path, r.status_code, delay)
                time.sleep(delay)
                continue
            if r.status_code >= 400:
                raise AlationError(
                    f"{method} {path} -> HTTP {r.status_code}: {r.text[:500]}", r.status_code
                )
            return r
        raise AssertionError("unreachable")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def put_json(self, path: str, body: Any) -> Any:
        r = self.request("PUT", path, json=body)
        return r.json() if r.content else None

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every item from a limit/skip paged list endpoint."""
        skip = 0
        while True:
            page = self.get_json(path, {**(params or {}), "limit": self.page_size, "skip": skip})
            # VERIFY: v2 list endpoints return a bare JSON list; tolerate {"results": [...]} too.
            items = page.get("results", []) if isinstance(page, dict) else page
            yield from items
            if len(items) < self.page_size:
                return
            skip += len(items)


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return float(r.headers.get("Retry-After", default))
    except ValueError:
        return default
