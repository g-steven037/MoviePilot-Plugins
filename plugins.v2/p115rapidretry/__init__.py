from __future__ import annotations

import os
import queue
import re
import threading
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from secrets import randbelow, token_hex
from time import monotonic, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from p115client import P115Client
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.log import logger
from app.plugins import _PluginBase

from .rapid import FileIdentity, RapidResult, same_identity, secure_identity, try_rapid_upload
SAFE_CODES = {
    "RAPID_SUCCESS", "RAPID_MISS", "AUTH_FAILED", "RATE_LIMITED",
    "NETWORK_TIMEOUT", "NETWORK_ERROR", "INVALID_RESPONSE", "CLIENT_ERROR",
    "API_REJECTED", "FILE_NOT_FOUND", "EMPTY_FILE", "FILE_CHANGED",
    "PATH_OUTSIDE_ROOT", "LINK_OR_REPARSE_POINT", "NOT_REGULAR_FILE",
    "NOT_A_HARDLINK", "INVALID_FILE", "CIRCUIT_OPEN", "MOVE_FAILED",
    "DELETE_FAILED", "QUEUE_OVERFLOW",
}


class _WatchHandler(FileSystemEventHandler):
    def __init__(self, plugin: "P115RapidRetry"):
        self._plugin = plugin

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory:
            self._plugin.queue_watch_file(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._plugin.queue_watch_file(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            self._plugin.cancel_watch_file(Path(event.src_path))
            self._plugin.queue_watch_file(Path(event.dest_path))

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory:
            self._plugin.cancel_watch_file(Path(event.src_path))


class P115RapidRetry(_PluginBase):
    plugin_name = "115秒传重试"
    plugin_desc = "安全监控硬链接目录，未命中秒传时原子移入临时目录并限速重试"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/v2/src/assets/images/misc/u115.png"
    plugin_version = "0.4.2"
    plugin_author = "115-transmission"
    author_url = "https://github.com"
    plugin_config_prefix = "p115rapidretry_"
    plugin_order = 30
    auth_level = 2

    _enabled = False
    _watch_dir = Path()
    _retry_dir = Path()
    _protected_pt_dir = Path()
    _target_pid = "0"
    _cron = "*/10 * * * *"
    _stable_seconds = 10
    _max_batch = 10
    _client: Optional[P115Client] = None
    _observer: Optional[Observer] = None
    _worker: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _events: queue.Queue = queue.Queue(maxsize=1024)
    _operation_lock = threading.Lock()
    _overflow = False
    _auth_blocked = False
    _circuit_until = 0.0

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._client = None
        self._auth_blocked = False
        self._circuit_until = 0.0
        if not self._enabled:
            return

        try:
            self._cron = str(config.get("cron", "*/10 * * * *")).strip()
            CronTrigger.from_crontab(self._cron)
            self._stable_seconds = self._bounded_int(config.get("stable_seconds", 10), 1, 3600)
            self._max_batch = self._bounded_int(config.get("max_batch", 10), 1, 100)
            self._target_pid = self._validate_pid(str(config.get("target_pid", "0")).strip())
            self._watch_dir = self._prepare_path(config.get("watch_dir"), create=True)
            self._retry_dir = self._prepare_path(config.get("retry_dir"), create=True)
            self._protected_pt_dir = self._prepare_path(config.get("protected_pt_dir"), create=False)
            self._validate_directory_isolation()
            cookie = self._validate_cookie(config.get("cookie", ""))
            self._client = self._create_client(cookie)
            del cookie
            self._start_realtime_monitor()
        except Exception as exc:
            self._enabled = False
            self._client = None
            logger.error(f"115秒传重试：安全初始化失败 [{self._safe_code(exc)}]")

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError("CONFIG_OUT_OF_RANGE")
        return number

    @staticmethod
    def _validate_pid(value: str) -> str:
        if re.fullmatch(r"\d{1,20}", value) or re.fullmatch(r"[US]_\d{1,20}_\d{1,20}", value):
            return value
        raise ValueError("TARGET_PID_INVALID")

    @staticmethod
    def _validate_cookie(value: Any) -> str:
        cookie = str(value or "").strip()
        if not cookie or len(cookie) > 8192:
            raise ValueError("COOKIE_INVALID")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cookie):
            raise ValueError("COOKIE_INVALID")
        parts = [part.strip() for part in cookie.split(";") if part.strip()]
        if not parts or any("=" not in part or not part.split("=", 1)[0].strip() for part in parts):
            raise ValueError("COOKIE_INVALID")
        names = {part.split("=", 1)[0].strip().upper() for part in parts}
        if not {"UID", "SEID"}.issubset(names):
            raise ValueError("COOKIE_INVALID")
        return cookie

    @staticmethod
    def _create_client(cookie: str) -> P115Client:
        # p115client >= 0.0.9 removed the legacy check_for_relogin argument.
        return P115Client(cookie)

    @staticmethod
    def _prepare_path(value: Any, create: bool) -> Path:
        text = str(value or "").strip()
        if not text:
            raise ValueError("DIRECTORY_MISSING")
        path = Path(text)
        if not path.is_absolute():
            raise ValueError("DIRECTORY_NOT_ABSOLUTE")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        if resolved.parent == resolved or not resolved.is_dir():
            raise ValueError("DIRECTORY_UNSAFE")
        return resolved

    @staticmethod
    def _overlap(left: Path, right: Path) -> bool:
        return left == right or left.is_relative_to(right) or right.is_relative_to(left)

    def _validate_directory_isolation(self):
        paths = (self._watch_dir, self._retry_dir, self._protected_pt_dir)
        if any(self._overlap(paths[i], paths[j]) for i in range(3) for j in range(i + 1, 3)):
            raise ValueError("DIRECTORIES_OVERLAP")
        devices = {path.stat().st_dev for path in paths}
        if len(devices) != 1:
            raise ValueError("DIRECTORIES_NOT_SAME_FILESYSTEM")

    @staticmethod
    def _safe_code(exc: Exception) -> str:
        text = str(exc)
        if text and text.isupper() and " " not in text and len(text) <= 64:
            return text
        return type(exc).__name__.upper()[:64]

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        if not self._enabled:
            return None
        return [{
            "id": "P115RapidRetry_retry",
            "name": "115秒传临时目录限速重试",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.retry_pending,
            "kwargs": {},
        }]

    def _start_realtime_monitor(self):
        self._stop_event = threading.Event()
        self._events = queue.Queue(maxsize=1024)
        self._overflow = False
        self._worker = threading.Thread(target=self._worker_loop, name="p115-rapid-worker", daemon=True)
        self._worker.start()
        observer = Observer()
        observer.schedule(_WatchHandler(self), str(self._watch_dir), recursive=True)
        observer.start()
        self._observer = observer
        logger.info("115秒传重试：安全实时监控已启动")
        self._queue_existing_files()

    def _put_event(self, action: str, path: Path):
        if not self._enabled:
            return
        try:
            self._events.put_nowait((action, str(path)))
        except queue.Full:
            self._overflow = True

    def queue_watch_file(self, path: Path):
        if ".p115-delete-" in path.name:
            return
        self._put_event("upsert", path)

    def cancel_watch_file(self, path: Path):
        self._put_event("cancel", path)

    def _queue_existing_files(self):
        try:
            for path in self._watch_dir.rglob("*"):
                if path.is_file():
                    self.queue_watch_file(path)
        except OSError:
            self._record("QUEUE_SCAN", False, "QUEUE_OVERFLOW")

    def _worker_loop(self):
        pending: Dict[str, float] = {}
        while not self._stop_event.is_set():
            try:
                action, raw_path = self._events.get(timeout=0.5)
                if action == "stop":
                    break
                if action == "cancel":
                    pending.pop(raw_path, None)
                elif len(pending) < 4096:
                    pending[raw_path] = monotonic() + self._stable_seconds
                else:
                    self._overflow = True
            except queue.Empty:
                pass

            now = monotonic()
            due = [raw for raw, deadline in pending.items() if deadline <= now]
            for raw in due[:1]:
                pending.pop(raw, None)
                self._process_watch_file(Path(raw))

            if self._overflow and self._events.empty() and len(pending) < 2048:
                self._overflow = False
                self._record("QUEUE_OVERFLOW", False, "QUEUE_OVERFLOW")
                self._queue_existing_files()

    def _process_watch_file(self, path: Path):
        if not self._enabled or not self._client:
            return
        try:
            identity = secure_identity(path, self._watch_dir, require_hardlink=True)
            if time() - path.stat().st_mtime < self._stable_seconds:
                self.queue_watch_file(path)
                return
        except (OSError, ValueError) as exc:
            self._record(self._task_id(path, self._watch_dir), False, self._safe_code(exc))
            return
        with self._operation_lock:
            self._handle(path, self._watch_dir, identity, from_retry=False)

    def retry_pending(self):
        if not self._enabled or not self._client or self._auth_blocked or time() < self._circuit_until:
            return
        if not self._operation_lock.acquire(blocking=False):
            return
        try:
            state = self.get_data("retry_state") or {}
            files = self._secure_files(self._retry_dir)
            active_ids = {self._task_id(path, self._retry_dir) for path in files}
            pruned = {key: value for key, value in state.items() if key in active_ids}
            if pruned != state:
                self.save_data("retry_state", pruned)
            state = pruned
            processed = 0
            for path in files:
                task_id = self._task_id(path, self._retry_dir)
                task_state = state.get(task_id, {})
                if float(task_state.get("next_at", 0)) > time():
                    continue
                self._handle(path, self._retry_dir, None, from_retry=True)
                processed += 1
                if processed >= self._max_batch or self._auth_blocked or time() < self._circuit_until:
                    break
            self._remove_empty_dirs(self._retry_dir)
        finally:
            self._operation_lock.release()

    @staticmethod
    def _secure_files(root: Path):
        found = []
        try:
            for path in root.rglob("*"):
                try:
                    secure_identity(path, root, require_hardlink=False)
                    found.append(path)
                except (OSError, ValueError):
                    continue
        except OSError:
            return []
        return sorted(found)

    def _handle(self, path: Path, root: Path, identity: FileIdentity | None, from_retry: bool):
        task_id = self._task_id(path, root)
        if self._auth_blocked or time() < self._circuit_until:
            result = RapidResult(False, True, "CIRCUIT_OPEN", identity)
        else:
            result = try_rapid_upload(
                self._client, path, self._target_pid, root=root,
                require_hardlink=not from_retry,
            )
        identity = result.identity or identity
        self._record(task_id, result.success, result.code)
        self._audit_rapid(path, root, result, from_retry)

        if result.code == "AUTH_FAILED":
            self._auth_blocked = True
        elif result.code == "RATE_LIMITED":
            self._circuit_until = time() + 3600

        if result.success:
            if identity and self._verified_unlink(path, identity, root):
                self._clear_retry_state(task_id)
            else:
                self._record(task_id, False, "FILE_CHANGED")
            return

        if from_retry:
            self._schedule_retry(task_id, result.code, result.retryable)
            return
        if not result.retryable or not identity or not same_identity(path, identity, root):
            return
        self._move_to_retry(path, identity, task_id)

    def _move_to_retry(self, path: Path, identity: FileIdentity, task_id: str):
        try:
            relative = path.resolve(strict=True).relative_to(self._watch_dir)
            destination = self._unique_destination(self._retry_dir / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.parent.resolve(strict=True).relative_to(self._retry_dir)
            if path.stat().st_dev != destination.parent.stat().st_dev:
                raise OSError("CROSS_DEVICE")
            if not same_identity(path, identity, self._watch_dir):
                self._record(task_id, False, "FILE_CHANGED")
                return
            os.replace(path, destination)
            if not same_identity(destination, identity, self._retry_dir):
                if not path.exists():
                    os.replace(destination, path)
                self._record(task_id, False, "FILE_CHANGED")
                return
            self._schedule_retry(self._task_id(destination, self._retry_dir), "RAPID_MISS", True)
        except (OSError, ValueError):
            self._record(task_id, False, "MOVE_FAILED")

    @staticmethod
    def _verified_unlink(path: Path, identity: FileIdentity, root: Path) -> bool:
        """Rename, verify, then unlink so a swapped path is never directly deleted."""
        quarantine = path.with_name(f".{path.name}.p115-delete-{token_hex(8)}")
        try:
            if not same_identity(path, identity, root):
                return False
            os.replace(path, quarantine)
            if not same_identity(quarantine, identity, root):
                if not path.exists():
                    os.replace(quarantine, path)
                return False
            quarantine.unlink()
            return True
        except OSError:
            try:
                if quarantine.exists() and not path.exists():
                    os.replace(quarantine, path)
            except OSError:
                pass
            return False

    @staticmethod
    def _unique_destination(destination: Path) -> Path:
        if not destination.exists():
            return destination
        for index in range(1, 10001):
            candidate = destination.with_name(f"{destination.stem}.{index}{destination.suffix}")
            if not candidate.exists():
                return candidate
        raise OSError("DESTINATION_EXHAUSTED")

    @staticmethod
    def _task_id(path: Path, root: Path) -> str:
        try:
            value = path.resolve(strict=False).relative_to(root.resolve(strict=True)).as_posix()
        except (OSError, ValueError):
            value = "invalid"
        return sha256(value.encode("utf-8")).hexdigest()[:16]

    def _schedule_retry(self, task_id: str, code: str, retryable: bool):
        state = self.get_data("retry_state") or {}
        previous = state.get(task_id, {})
        attempts = min(int(previous.get("attempts", 0)) + 1, 1000)
        if retryable:
            delay = min(86400, 300 * (2 ** min(attempts - 1, 8)))
            delay += randbelow(max(1, delay // 5 + 1))
            next_at = time() + delay
        else:
            next_at = time() + 86400
        state[task_id] = {"attempts": attempts, "next_at": next_at, "code": self._normalize_code(code)}
        self.save_data("retry_state", state)

    def _clear_retry_state(self, task_id: str):
        state = self.get_data("retry_state") or {}
        if task_id in state:
            state.pop(task_id, None)
            self.save_data("retry_state", state)

    @staticmethod
    def _normalize_code(code: str) -> str:
        return code if code in SAFE_CODES else "CLIENT_ERROR"

    def _record(self, task_id: str, success: bool, code: str):
        history = self.get_data("history") or []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task": task_id[:16],
            "success": bool(success),
            "code": self._normalize_code(code),
        })
        self.save_data("history", history[:200])

    @staticmethod
    def _safe_log_value(value: Any, limit: int = 1024) -> str:
        text = "".join(char if ord(char) >= 0x20 and ord(char) != 0x7F else "?" for char in str(value))
        return text[:limit]

    def _audit_rapid(self, path: Path, root: Path, result: RapidResult, from_retry: bool):
        if result.success:
            status, matched = "成功", "是"
        elif result.code == "RAPID_MISS":
            status, matched = "未命中", "否"
        else:
            status, matched = "失败", "未知"
        logger.info(
            "115秒传审计 | 时间=%s | 来源=%s | 文件夹=%s | 文件名=%s | SHA1=%s | SHA1服务端匹配=%s | 秒传=%s | 代码=%s"
            % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "定时重试" if from_retry else "实时监控",
                self._safe_log_value(path.parent),
                self._safe_log_value(path.name),
                result.sha1 or "未计算",
                matched,
                status,
                self._normalize_code(result.code),
            )
        )

    @staticmethod
    def _remove_empty_dirs(root: Path):
        try:
            directories = (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink())
            for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except OSError:
            pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        fields = [
            ("cookie", "115 Cookie（明文，密码框隐藏）", "password"),
            ("protected_pt_dir", "受保护的PT下载目录（不扫描）", None),
            ("watch_dir", "硬链接实时监控目录", None),
            ("retry_dir", "失败临时目录", None),
            ("target_pid", "115目标目录ID（根目录为0）", None),
            ("cron", "临时目录重试 Cron（5段）", None),
            ("stable_seconds", "文件稳定等待秒数（1-3600）", "number"),
            ("max_batch", "每轮最大重试文件数（1-100）", "number"),
        ]
        content = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "Cookie 仅用于登录115官方接口，不发送给其他第三方，不写入插件日志或历史；MoviePilot 会将其保存在自身配置中，请保护管理端和数据目录。"}}]}]}]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}}]},
        ]})
        for model, label, field_type in fields:
            props = {"model": model, "label": label, "clearable": False}
            if field_type:
                props["type"] = field_type
            if model == "cookie":
                props["autocomplete"] = "new-password"
            content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": props}]}]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False, "cookie": "",
            "protected_pt_dir": "", "watch_dir": "", "retry_dir": "", "target_pid": "0",
            "cron": "*/10 * * * *", "stable_seconds": 10, "max_batch": 10,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        rows = [[item.get("time"), item.get("task"), "成功" if item.get("success") else "受控等待", item.get("code")] for item in history]
        return [{"component": "VTable", "props": {"hover": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": title} for title in ("时间", "匿名任务ID", "状态", "安全码")]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [{"component": "td", "text": str(value or "")} for value in row]} for row in rows]},
        ]}]

    def get_command(self):
        return None

    def get_api(self):
        return None

    def stop_service(self):
        self._enabled = False
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=10)
            self._observer = None
        self._stop_event.set()
        try:
            self._events.put_nowait(("stop", ""))
        except queue.Full:
            pass
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=10)
        self._worker = None
        self._client = None
