from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from PIL import Image


MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_JSON_BYTES = 10 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EmbyError(RuntimeError):
    """An error carrying only a fixed, log-safe code."""

    def __init__(self, code: str):
        self.code = code if code.isupper() and len(code) <= 64 else "EMBY_ERROR"
        super().__init__(self.code)


@dataclass(frozen=True)
class Library:
    id: str
    name: str


def validate_base_url(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 2048 or any(ord(char) < 0x20 for char in text):
        raise EmbyError("URL_INVALID")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EmbyError("URL_INVALID")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EmbyError("URL_INVALID")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class EmbyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str = "",
        timeout: int = 30,
        verify_ssl: bool = True,
        verify_upload: bool = False,
        upload_target: str = "item",
        stop_event: threading.Event | None = None,
    ):
        key = str(api_key or "").strip()
        if not key or len(key) > 4096 or any(ord(char) < 0x20 for char in key):
            raise EmbyError("API_KEY_INVALID")
        if user_id and not SAFE_ID.fullmatch(user_id):
            raise EmbyError("USER_ID_INVALID")
        if upload_target not in {"item", "virtual_folder", "both"}:
            raise EmbyError("UPLOAD_TARGET_INVALID")
        self.base_url = validate_base_url(base_url)
        self.user_id = user_id
        self.timeout = max(5, min(int(timeout), 120))
        self.verify_ssl = bool(verify_ssl)
        self.verify_upload = bool(verify_upload)
        self.upload_target = upload_target
        self.stop_event = stop_event or threading.Event()
        self.session = requests.Session()
        self.session.headers.update({"X-Emby-Token": key, "Accept": "application/json"})

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        if self.stop_event.is_set():
            raise EmbyError("STOPPED")
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                verify=self.verify_ssl,
                allow_redirects=False,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise EmbyError("NETWORK_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise EmbyError("NETWORK_ERROR") from exc
        if 300 <= response.status_code < 400:
            response.close()
            raise EmbyError("REDIRECT_BLOCKED")
        if response.status_code in {401, 403}:
            response.close()
            raise EmbyError("AUTH_FAILED")
        if response.status_code >= 400:
            response.close()
            raise EmbyError(f"HTTP_{response.status_code}" if response.status_code < 600 else "HTTP_ERROR")
        return response

    @staticmethod
    def _json(response: requests.Response) -> Any:
        try:
            if len(response.content) > MAX_JSON_BYTES:
                raise EmbyError("RESPONSE_TOO_LARGE")
            return response.json()
        except (ValueError, TypeError) as exc:
            raise EmbyError("INVALID_RESPONSE") from exc
        finally:
            response.close()

    @staticmethod
    def _safe_id(value: Any) -> str:
        text = str(value or "")
        if not SAFE_ID.fullmatch(text):
            raise EmbyError("ITEM_ID_INVALID")
        return text

    def get_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        users = self._json(self._request("GET", "/Users"))
        if not isinstance(users, list) or not users:
            raise EmbyError("USER_NOT_FOUND")
        self.user_id = self._safe_id(users[0].get("Id") if isinstance(users[0], dict) else "")
        return self.user_id

    def get_libraries(self) -> list[Library]:
        folders = self._json(self._request("GET", "/Library/VirtualFolders"))
        if not isinstance(folders, list):
            raise EmbyError("INVALID_RESPONSE")
        libraries: list[Library] = []
        for folder in folders[:1000]:
            if not isinstance(folder, dict):
                continue
            name = str(folder.get("Name") or "").strip()
            try:
                item_id = self._safe_id(folder.get("ItemId") or folder.get("Id"))
            except EmbyError:
                continue
            if name and len(name) <= 256:
                libraries.append(Library(item_id, name))
        return libraries

    def get_latest_items(self, library_id: str, limit: int) -> list[dict]:
        user_id = self.get_user_id()
        result = self._json(self._request(
            "GET",
            f"/Users/{user_id}/Items/Latest",
            params={
                "ParentId": self._safe_id(library_id),
                "Limit": max(1, min(int(limit), 100)),
                "Fields": "PrimaryImageAspectRatio,BackdropImageTags,ImageTags,DateCreated",
                "EnableImages": "true",
            },
        ))
        if not isinstance(result, list):
            raise EmbyError("INVALID_RESPONSE")
        return [item for item in result[:100] if isinstance(item, dict)]

    def download_image(self, item_id: str, image_type: str = "Primary", tag: str | None = None) -> Image.Image | None:
        if image_type not in {"Primary", "Backdrop"}:
            return None
        params = {"tag": str(tag)[:256]} if tag else None
        try:
            response = self._request(
                "GET", f"/Items/{self._safe_id(item_id)}/Images/{image_type}",
                params=params, stream=True,
            )
            length = int(response.headers.get("Content-Length", "0") or 0)
            if length > MAX_IMAGE_BYTES:
                response.close()
                return None
            payload = bytearray()
            for chunk in response.iter_content(64 * 1024):
                if self.stop_event.is_set() or len(payload) + len(chunk) > MAX_IMAGE_BYTES:
                    response.close()
                    return None
                payload.extend(chunk)
            response.close()
            image = Image.open(io.BytesIO(payload))
            image.load()
            return image.convert("RGB")
        except (EmbyError, OSError, ValueError):
            return None

    def get_posters(self, items: Iterable[dict], limit: int) -> list[Image.Image]:
        posters: list[Image.Image] = []
        for item in items:
            tags = item.get("ImageTags") or {}
            if isinstance(tags, dict) and tags.get("Primary"):
                image = self.download_image(item.get("Id", ""), "Primary", tags.get("Primary"))
                if image:
                    posters.append(image)
            if len(posters) >= limit or self.stop_event.is_set():
                break
        return posters

    def get_backdrop(self, items: Iterable[dict]) -> Image.Image | None:
        values = list(items)
        for item in values:
            tags = item.get("BackdropImageTags") or []
            if isinstance(tags, list) and tags:
                image = self.download_image(item.get("Id", ""), "Backdrop", tags[0])
                if image:
                    return image
        for item in values:
            tags = item.get("ImageTags") or {}
            if isinstance(tags, dict) and tags.get("Primary"):
                image = self.download_image(item.get("Id", ""), "Primary", tags.get("Primary"))
                if image:
                    return image
        return None

    @staticmethod
    def _content_type(path: Path) -> str:
        return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"

    def _upload(self, path: str, image_path: Path) -> None:
        size = image_path.stat().st_size
        if size <= 0 or size > MAX_IMAGE_BYTES or not image_path.is_file():
            raise EmbyError("IMAGE_FILE_INVALID")
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = self._request(
            "POST", path,
            headers={"Content-Type": self._content_type(image_path), "Accept": "application/json"},
            data=encoded,
        )
        response.close()

    def upload_library_primary_image(self, library: Library, image_path: Path) -> str:
        verify_item = self.verify_upload and self.upload_target in {"item", "both"}
        before = self.get_image_fingerprint(library.id) if verify_item else None
        targets = []
        if self.upload_target in {"item", "both"}:
            targets.append(("ItemId", f"/Items/{self._safe_id(library.id)}/Images/Primary"))
        if self.upload_target in {"virtual_folder", "both"}:
            targets.append(("VirtualFolder", f"/Library/VirtualFolders/{quote(library.name, safe='')}/Images/Primary"))
        completed = []
        for label, endpoint in targets:
            self._upload(endpoint, image_path)
            completed.append(label)
        if verify_item and "ItemId" in completed:
            after = self.get_image_fingerprint(library.id)
            if not after or (before and before == after):
                raise EmbyError("UPLOAD_VERIFY_FAILED")
        return "+".join(completed)

    def get_image_fingerprint(self, item_id: str) -> str | None:
        try:
            response = self._request(
                "GET", f"/Items/{self._safe_id(item_id)}/Images/Primary",
                headers={"Cache-Control": "no-cache"}, stream=True,
            )
            digest = hashlib.sha256()
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    response.close()
                    return None
                digest.update(chunk)
            response.close()
            return f"{size}:{digest.hexdigest()}" if size else None
        except EmbyError:
            return None
