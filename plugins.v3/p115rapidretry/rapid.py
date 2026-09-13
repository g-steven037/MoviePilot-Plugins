from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol


class RapidUploadClient(Protocol):
    def upload_file_init(self, **kwargs: Any) -> dict: ...


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    links: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)


@dataclass(frozen=True)
class RapidResult:
    success: bool
    retryable: bool
    code: str
    identity: FileIdentity | None = None
    sha1: str | None = None


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def secure_identity(path: Path, root: Path, require_hardlink: bool) -> FileIdentity:
    root_resolved = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise ValueError("PATH_OUTSIDE_ROOT")
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or _is_reparse_point(value):
        raise ValueError("LINK_OR_REPARSE_POINT")
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("NOT_REGULAR_FILE")
    if require_hardlink and value.st_nlink < 2:
        raise ValueError("NOT_A_HARDLINK")
    return FileIdentity.from_stat(value)


def same_identity(path: Path, expected: FileIdentity, root: Path) -> bool:
    try:
        return secure_identity(path, root, require_hardlink=False) == expected
    except (OSError, ValueError):
        return False


def _sha1_stream(stream: BinaryIO, start: int = 0, length: int | None = None) -> str:
    digest = sha1()
    stream.seek(start)
    remaining = length
    while remaining is None or remaining > 0:
        size = 8 * 1024 * 1024 if remaining is None else min(8 * 1024 * 1024, remaining)
        block = stream.read(size)
        if not block:
            if remaining not in (None, 0):
                raise EOFError("RANGE_OUT_OF_BOUNDS")
            break
        digest.update(block)
        if remaining is not None:
            remaining -= len(block)
    return digest.hexdigest().upper()


def file_sha1(path: Path) -> str:
    with path.open("rb") as stream:
        return _sha1_stream(stream)


def range_sha1_reader(path: Path) -> Callable[[str], str]:
    def read(sign_check: str) -> str:
        start_text, end_text = sign_check.split("-", 1)
        start, end = int(start_text), int(end_text)
        if start < 0 or end < start:
            raise ValueError("INVALID_RANGE_CHALLENGE")
        with path.open("rb") as stream:
            return _sha1_stream(stream, start, end - start + 1)

    return read


def _range_reader(stream: BinaryIO) -> Callable[[str], str]:
    def read(sign_check: str) -> str:
        start_text, end_text = sign_check.split("-", 1)
        start, end = int(start_text), int(end_text)
        if start < 0 or end < start:
            raise ValueError("INVALID_RANGE_CHALLENGE")
        return _sha1_stream(stream, start, end - start + 1)

    return read


def _safe_response_code(response: dict) -> tuple[str, bool]:
    values = " ".join(
        str(response.get(key, "")) for key in ("code", "errno", "status", "statuscode", "message", "error", "statusmsg")
    ).lower()
    if any(token in values for token in ("401", "403", "login", "cookie", "auth", "未登录", "登录失效")):
        return "AUTH_FAILED", False
    if any(token in values for token in ("405", "429", "frequent", "rate", "频繁", "限制")):
        return "RATE_LIMITED", True
    if response.get("state"):
        return "RAPID_MISS", True
    return "API_REJECTED", True


def _safe_exception_code(exc: Exception) -> str:
    status_values = []
    for target in (exc, getattr(exc, "response", None)):
        if target is None:
            continue
        for attribute in ("status_code", "status", "code", "errno"):
            value = getattr(target, attribute, None)
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                status_values.append(str(value))
    if any(value in {"401", "403"} for value in status_values):
        return "AUTH_FAILED"
    if any(value in {"405", "429"} for value in status_values):
        return "RATE_LIMITED"
    name = type(exc).__name__.lower()
    if any(token in name for token in ("auth", "login", "cookie", "unauthorized", "forbidden")):
        return "AUTH_FAILED"
    if any(token in name for token in ("ratelimit", "too_many", "toomany")):
        return "RATE_LIMITED"
    if "timeout" in name:
        return "NETWORK_TIMEOUT"
    if isinstance(exc, (ConnectionError, OSError)):
        return "NETWORK_ERROR"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "INVALID_RESPONSE"
    return "CLIENT_ERROR"


def try_rapid_upload(
    client: RapidUploadClient,
    path: Path,
    pid: int | str,
    root: Path | None = None,
    require_hardlink: bool = False,
    known_sha1: str | None = None,
    progress: Callable[[str], None] | None = None,
    request_guard: Callable[[], bool] | None = None,
) -> RapidResult:
    """Only initialize an upload. It never invokes a content-upload API."""
    root = root or path.parent
    try:
        identity = secure_identity(path, root, require_hardlink=require_hardlink)
        if identity.size <= 0:
            return RapidResult(False, False, "EMPTY_FILE", identity)
        # Keep one handle open for full and range hashes. fstat binds all hashes
        # to the same file object even if the directory entry is replaced.
        with path.open("rb") as stream:
            opened_identity = FileIdentity.from_stat(os.fstat(stream.fileno()))
            if opened_identity != identity:
                return RapidResult(False, True, "FILE_CHANGED")
            if known_sha1 and len(known_sha1) == 40 and all(char in "0123456789abcdefABCDEF" for char in known_sha1):
                full_sha1 = known_sha1.upper()
                if progress:
                    progress("SHA1_CACHE")
            else:
                if progress:
                    progress("SHA1_START")
                full_sha1 = _sha1_stream(stream)
                if progress:
                    progress("SHA1_DONE")
            if request_guard and not request_guard():
                return RapidResult(False, True, "CIRCUIT_OPEN", identity, full_sha1)
            response = client.upload_file_init(
                filename=path.name,
                filesize=identity.size,
                filesha1=full_sha1,
                pid=pid,
                read_range_bytes_or_hash=_range_reader(stream),
                timeout=60,
            )
    except FileNotFoundError:
        return RapidResult(False, False, "FILE_NOT_FOUND")
    except ValueError as exc:
        code = str(exc) if str(exc).isupper() and " " not in str(exc) else "INVALID_FILE"
        return RapidResult(False, False, code)
    except Exception as exc:
        return RapidResult(False, True, _safe_exception_code(exc))

    if not isinstance(response, dict):
        return RapidResult(False, True, "INVALID_RESPONSE", identity, full_sha1)
    if response.get("state") and response.get("reuse"):
        return RapidResult(True, False, "RAPID_SUCCESS", identity, full_sha1)
    code, retryable = _safe_response_code(response)
    return RapidResult(False, retryable, code, identity, full_sha1)
