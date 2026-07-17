from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

import requests


MAX_JSON_BYTES = 20 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EmbyActorError(RuntimeError):
    """仅携带固定错误码，避免网络响应或凭据进入日志。"""

    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[A-Z0-9_]{3,64}", str(code or "")) else "EMBY_ERROR"
        super().__init__(self.code)


def validate_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 2048 or any(ord(char) < 0x20 for char in text):
        raise EmbyActorError("URL_INVALID")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EmbyActorError("URL_INVALID")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EmbyActorError("URL_INVALID")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class EmbyActorClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str = "",
        timeout: int = 30,
        verify_https: bool = True,
    ):
        key = str(api_key or "").strip()
        if not key or len(key) > 4096 or any(ord(char) < 0x20 for char in key):
            raise EmbyActorError("API_KEY_INVALID")
        if user_id and not SAFE_ID.fullmatch(str(user_id)):
            raise EmbyActorError("USER_ID_INVALID")
        self.base_url = validate_base_url(base_url)
        self.user_id = str(user_id or "")
        self.timeout = max(5, min(int(timeout), 120))
        self.verify_https = bool(verify_https)
        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _safe_id(value: Any) -> str:
        text = str(value or "")
        if not SAFE_ID.fullmatch(text):
            raise EmbyActorError("ITEM_ID_INVALID")
        return text

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                verify=self.verify_https,
                allow_redirects=False,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise EmbyActorError("NETWORK_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise EmbyActorError("NETWORK_ERROR") from exc
        if 300 <= response.status_code < 400:
            response.close()
            raise EmbyActorError("REDIRECT_BLOCKED")
        if response.status_code in {401, 403}:
            response.close()
            raise EmbyActorError("AUTH_FAILED")
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise EmbyActorError(f"HTTP_{status}" if status < 600 else "HTTP_ERROR")
        return response

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            if len(response.content) > MAX_JSON_BYTES:
                raise EmbyActorError("RESPONSE_TOO_LARGE")
            return response.json()
        except (ValueError, TypeError) as exc:
            raise EmbyActorError("INVALID_RESPONSE") from exc
        finally:
            response.close()

    def get_user_id(self) -> str:
        if self.user_id:
            return self._safe_id(self.user_id)
        users = self._json(self._request("GET", "/Users"))
        if not isinstance(users, list) or not users:
            raise EmbyActorError("USER_NOT_FOUND")
        first = users[0] if isinstance(users[0], dict) else {}
        self.user_id = self._safe_id(first.get("Id"))
        return self.user_id

    def search_items(self, title: str, item_type: str = "auto") -> List[Dict[str, Any]]:
        include_types = {"movie": "Movie", "series": "Series", "auto": "Movie,Series"}.get(item_type)
        if not include_types:
            raise EmbyActorError("MEDIA_TYPE_INVALID")
        user_id = self.get_user_id()
        payload = self._json(self._request(
            "GET",
            f"/Users/{user_id}/Items",
            params={
                "SearchTerm": title,
                "IncludeItemTypes": include_types,
                "Recursive": "true",
                "Fields": "People,ProviderIds,ProductionYear,OriginalTitle",
                "Limit": 50,
            },
        ))
        items = payload.get("Items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise EmbyActorError("INVALID_RESPONSE")
        return [item for item in items if isinstance(item, dict)][:50]

    def get_item(self, item_id: str) -> Dict[str, Any]:
        user_id = self.get_user_id()
        item = self._json(self._request(
            "GET",
            f"/Users/{user_id}/Items/{self._safe_id(item_id)}",
            params={"Fields": "People,ProviderIds,ProductionYear,OriginalTitle"},
        ))
        if not isinstance(item, dict) or not isinstance(item.get("People"), list):
            raise EmbyActorError("ITEM_DATA_INVALID")
        return item

    def update_item(self, item_id: str, item: Dict[str, Any]) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("People"), list):
            raise EmbyActorError("ITEM_DATA_INVALID")
        response = self._request("POST", f"/Items/{self._safe_id(item_id)}", json=deepcopy(item))
        response.close()
