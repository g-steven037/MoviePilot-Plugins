from __future__ import annotations

from time import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_CMS_TIMEOUT = 30
CMS_ENDPOINT = "/api/sync/lift_by_token"


def _normalise_domain(value: str) -> str:
    domain = str(value or "").strip().rstrip("/")
    parts = urlsplit(domain)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("CMS_DOMAIN_INVALID")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _normalise_mode(value: str) -> str:
    mode = str(value or "auto_organize").strip().lower()
    if mode not in {"lift_sync", "auto_organize"}:
        raise ValueError("CMS_MODE_INVALID")
    return mode


class CmsClient:
    """Small, dependency-free client for the CMS incremental sync endpoint."""

    def __init__(
        self,
        domain: str,
        token: str,
        mode: str = "auto_organize",
        *,
        timeout: int = DEFAULT_CMS_TIMEOUT,
        opener: Callable[..., Any] | None = None,
    ):
        self.domain = _normalise_domain(domain)
        self.token = str(token or "").strip()
        if not self.token or len(self.token) > 4096:
            raise ValueError("CMS_TOKEN_INVALID")
        self.mode = _normalise_mode(mode)
        self.timeout = max(int(timeout), 1)
        self._opener = opener or urlopen
        self._last_error = ""

    @property
    def request_url(self) -> str:
        query = urlencode({"token": self.token, "type": self.mode})
        return f"{self.domain}{CMS_ENDPOINT}?{query}"

    @property
    def safe_description(self) -> str:
        return f"{self.domain}{CMS_ENDPOINT}?type={self.mode}"

    @property
    def last_error(self) -> str:
        """Safe exception type only; never expose URL, token, or response text."""
        return self._last_error

    def sync(self) -> tuple[bool, str]:
        self._last_error = ""
        stage = "request"
        try:
            request = Request(
                self.request_url,
                method="GET",
                headers={"User-Agent": "MoviePilot-P115RapidRetry/2.0.3"},
            )
            stage = "open"
            with self._opener(request, self.timeout) as response:
                stage = "response_status"
                status = int(getattr(response, "status", 200))
                stage = "response_read"
                response.read()
            if 200 <= status < 300:
                return True, f"HTTP_{status}"
            return False, f"HTTP_{status}"
        except HTTPError as exc:
            self._last_error = f"{stage}:{type(exc).__name__}"
            return False, f"HTTP_{int(exc.code)}"
        except TimeoutError:
            self._last_error = f"{stage}:TimeoutError"
            return False, "TIMEOUT"
        except OSError as exc:
            self._last_error = f"{stage}:{type(exc).__name__}"
            return False, "NETWORK_ERROR"
        except Exception as exc:
            self._last_error = f"{stage}:{type(exc).__name__}"
            return False, "CLIENT_ERROR"


def cms_batch_due(items: Iterable[dict], *, now: float | None = None, delay: int = 60) -> bool:
    pending = [item for item in items if isinstance(item, dict)]
    if not pending:
        return False
    latest = max(float(item.get("created_at", 0) or 0) for item in pending)
    current = time() if now is None else float(now)
    return current >= latest + max(int(delay), 0)


def cms_retry_delay(attempt: int) -> int:
    """Return an independent CMS retry delay; cap retries at one hour."""
    number = max(int(attempt), 1)
    return min(60 * (2 ** (number - 1)), 3600)
