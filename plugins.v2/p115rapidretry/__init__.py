from __future__ import annotations

import os
import queue
import re
import stat
import threading
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from time import monotonic, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from p115client import P115Client
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .rapid import FileIdentity, RapidResult, same_identity, secure_identity, try_rapid_upload
SAFE_CODES = {
    "RAPID_SUCCESS", "RAPID_MISS", "AUTH_FAILED", "RATE_LIMITED",
    "NETWORK_TIMEOUT", "NETWORK_ERROR", "INVALID_RESPONSE", "CLIENT_ERROR",
    "API_REJECTED", "FILE_NOT_FOUND", "EMPTY_FILE", "FILE_CHANGED",
    "PATH_OUTSIDE_ROOT", "LINK_OR_REPARSE_POINT", "NOT_REGULAR_FILE",
    "NOT_A_HARDLINK", "INVALID_FILE", "CIRCUIT_OPEN", "MOVE_FAILED",
    "DELETE_FAILED", "QUEUE_OVERFLOW", "RETRY_EXHAUSTED",
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
    plugin_desc = "（仅自用）监控目录，秒传失败时转移到临时目录，定时重试，秒传成功后删除本地文件，仅自用测试。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/v2/src/assets/images/misc/u115.png"
    plugin_version = "1.0.1"
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
    _max_retries = 10
    _delete_exhausted_enabled = False
    _notify_enabled = False
    _detailed_logs = True
    _empty_cleanup_enabled = False
    _empty_cleanup_roots: List[Path] = []
    _empty_cleanup_cron = "0 4 * * *"
    _empty_cleanup_identities: Dict[Path, Tuple[int, int]] = {}
    _client: Optional[P115Client] = None
    _observer: Optional[Observer] = None
    _worker: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _events: queue.Queue = queue.Queue(maxsize=1024)
    _operation_lock = threading.Lock()
    _overflow = False
    _auth_blocked = False
    _circuit_until = 0.0
    _sha1_cache: Dict[FileIdentity, str] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._client = None
        self._auth_blocked = False
        self._circuit_until = 0.0
        self._sha1_cache = {}
        if not self._enabled:
            return

        try:
            self._cron = str(config.get("cron", "*/10 * * * *")).strip()
            CronTrigger.from_crontab(self._cron)
            self._stable_seconds = self._bounded_int(config.get("stable_seconds", 10), 1, 3600)
            self._max_batch = self._bounded_int(config.get("max_batch", 10), 1, 100)
            self._max_retries = self._bounded_int(config.get("max_retries", 10), 1, 100)
            self._delete_exhausted_enabled = bool(config.get("delete_exhausted_enabled", False))
            self._notify_enabled = bool(config.get("notify_enabled", False))
            self._detailed_logs = bool(
                config.get("detailed_logs", str(config.get("log_mode", "detailed")).strip().lower() == "detailed")
            )
            self._empty_cleanup_enabled = bool(config.get("empty_cleanup_enabled", False))
            self._target_pid = self._validate_pid(str(config.get("target_pid", "0")).strip())
            self._watch_dir = self._prepare_path(config.get("watch_dir"), create=True)
            self._retry_dir = self._prepare_path(config.get("retry_dir"), create=True)
            self._protected_pt_dir = self._prepare_path(config.get("protected_pt_dir"), create=False)
            self._validate_directory_isolation()
            self._empty_cleanup_roots = []
            self._empty_cleanup_identities = {}
            if self._empty_cleanup_enabled:
                self._empty_cleanup_cron = str(config.get("empty_cleanup_cron", "0 4 * * *")).strip()
                CronTrigger.from_crontab(self._empty_cleanup_cron)
                self._empty_cleanup_roots = self._prepare_cleanup_roots(config.get("empty_cleanup_root"))
                for cleanup_root in self._empty_cleanup_roots:
                    if self._overlap(cleanup_root, self._protected_pt_dir):
                        raise ValueError("CLEANUP_OVERLAPS_PT")
                    cleanup_stat = self._safe_directory_stat(cleanup_root)
                    self._empty_cleanup_identities[cleanup_root] = (cleanup_stat.st_dev, cleanup_stat.st_ino)
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

    def _prepare_cleanup_roots(self, value: Any) -> List[Path]:
        values = value if isinstance(value, (list, tuple)) else str(value or "").splitlines()
        roots: List[Path] = []
        seen = set()
        for raw in values:
            text = str(raw).strip()
            if not text:
                continue
            root = self._prepare_path(text, create=False)
            if root not in seen:
                seen.add(root)
                roots.append(root)
        if not roots:
            raise ValueError("CLEANUP_ROOT_REQUIRED")
        return roots

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
    def _safe_directory_stat(path: Path) -> os.stat_result:
        value = path.lstat()
        attributes = getattr(value, "st_file_attributes", 0)
        reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode) or bool(attributes & reparse_marker):
            raise ValueError("DIRECTORY_LINK_UNSAFE")
        return value

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
        services = [{
            "id": "P115RapidRetry_retry",
            "name": "115秒传临时目录限速重试",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.retry_pending,
            "kwargs": {},
        }]
        if self._empty_cleanup_enabled:
            services.append({
                "id": "P115RapidRetry_empty_cleanup",
                "name": "115秒传定时清理空文件夹",
                "trigger": CronTrigger.from_crontab(self._empty_cleanup_cron),
                "func": self.cleanup_empty_directories,
                "kwargs": {},
            })
        return services

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
            eligible = [
                path for path in files
                if not bool(state.get(self._task_id(path, self._retry_dir), {}).get("exhausted", False))
            ]
            exhausted_cleanup = [
                path for path in files
                if self._delete_exhausted_enabled
                and bool(state.get(self._task_id(path, self._retry_dir), {}).get("exhausted", False))
            ]
            if self._detailed_logs:
                submitted = min(len(eligible) + len(exhausted_cleanup), self._max_batch)
                logger.info(
                    f"#115秒传# 临时目录扫描完成，共发现 {len(files)} 个文件，本轮提交 {submitted} 个任务"
                    f"（可重试={len(eligible)}，耗尽清理={len(exhausted_cleanup)}）"
                )
            processed = 0
            for path in files:
                task_id = self._task_id(path, self._retry_dir)
                task_state = state.get(task_id, {})
                if bool(task_state.get("exhausted", False)):
                    if self._delete_exhausted_enabled:
                        self._delete_previously_exhausted(path, task_id, task_state)
                        processed += 1
                        if processed >= self._max_batch:
                            break
                    continue
                self._handle(path, self._retry_dir, None, from_retry=True)
                processed += 1
                if processed >= self._max_batch or self._auth_blocked or time() < self._circuit_until:
                    break
        finally:
            self._operation_lock.release()

    def _delete_previously_exhausted(self, path: Path, task_id: str, task_state: Dict[str, Any]) -> bool:
        filename = self._safe_log_value(path.name)
        attempts = min(max(int(task_state.get("attempts", self._max_retries)), 0), 1000)
        code = self._normalize_code(str(task_state.get("code", "RAPID_MISS")))
        try:
            identity = secure_identity(path, self._retry_dir, require_hardlink=False)
        except (OSError, ValueError):
            logger.warning(
                f"#115秒传# [简短] 重试耗尽清理=安全校验失败 | 文件={filename} | "
                f"重试次数={attempts}/{self._max_retries}"
            )
            return False
        if not self._verified_unlink(path, identity, self._retry_dir):
            logger.warning(
                f"#115秒传# [简短] 重试耗尽清理=删除失败 | 文件={filename} | "
                f"重试次数={attempts}/{self._max_retries}"
            )
            return False
        self._clear_retry_state(task_id)
        self._sha1_cache.pop(identity, None)
        if self._detailed_logs:
            logger.warning(f"#115秒传# 已安全删除此前重试耗尽的失败文件: {filename}")
        else:
            logger.warning(
                f"#115秒传# [简短] 重试耗尽清理=已删除 | 文件={filename} | "
                f"重试次数={attempts}/{self._max_retries}"
            )
        self._remove_empty_parent_dirs(path.parent, self._retry_dir)
        self._send_bot_exhausted(path, attempts, code, deleted=True, delete_requested=True)
        return True

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
        source = "重试" if from_retry else "后台线程"
        filename = self._safe_log_value(path.name)
        retry_state = (self.get_data("retry_state") or {}).get(task_id, {})
        attempt_no = int(retry_state.get("attempts", 0)) + 1 if from_retry else 0
        if self._detailed_logs:
            logger.info(f"#115秒传# [{source}-{threading.current_thread().name}] {'重试秒传' if from_retry else '开始秒传'}: {filename}")
        if self._auth_blocked or time() < self._circuit_until:
            result = RapidResult(False, True, "CIRCUIT_OPEN", identity)
        else:
            cache_identity = identity
            if cache_identity is None:
                try:
                    cache_identity = secure_identity(path, root, require_hardlink=False)
                except (OSError, ValueError):
                    cache_identity = None
            known_sha1 = self._sha1_cache.get(cache_identity) if cache_identity else None

            def progress(event: str):
                if not self._detailed_logs:
                    return
                if event == "SHA1_START":
                    logger.info(f"#115秒传# 开始计算SHA1: {filename}")
                elif event == "SHA1_DONE":
                    logger.info(f"#115秒传# SHA1计算完成: {filename}")
                elif event == "SHA1_CACHE":
                    logger.info(f"#115秒传# 从缓存加载SHA1: {filename}")

            result = try_rapid_upload(
                self._client, path, self._target_pid, root=root,
                require_hardlink=not from_retry,
                known_sha1=known_sha1,
                progress=progress,
            )
        identity = result.identity or identity
        if identity and result.sha1:
            if len(self._sha1_cache) >= 4096 and identity not in self._sha1_cache:
                self._sha1_cache.clear()
            self._sha1_cache[identity] = result.sha1
        self._record(task_id, result.success, result.code)
        self._audit_rapid(path, root, result, from_retry, attempt_no)

        if result.code == "AUTH_FAILED":
            self._auth_blocked = True
        elif result.code == "RATE_LIMITED":
            self._circuit_until = time() + 3600

        if result.success:
            if identity and self._verified_unlink(path, identity, root):
                self._clear_retry_state(task_id)
                self._sha1_cache.pop(identity, None)
                if from_retry:
                    if self._detailed_logs:
                        logger.info(f"#115秒传# 重试秒传成功后删除源文件: {filename}")
                        logger.info(f"#115秒传# [重试-{threading.current_thread().name}] 重试成功: {filename}")
                else:
                    if self._detailed_logs:
                        logger.info(f"#115秒传# 秒传成功后删除硬链接文件: {filename}")
                self._send_bot_success(path, from_retry, cleanup_success=True)
                self._remove_empty_parent_dirs(path.parent, root)
            else:
                self._record(task_id, False, "FILE_CHANGED")
                logger.warning(
                    f"#115秒传# {'秒传成功但本地文件身份变化，未删除' if self._detailed_logs else '[简短] 本地清理失败'}: {filename}"
                )
                self._send_bot_success(path, from_retry, cleanup_success=False)
            return

        if from_retry:
            attempts, exhausted = self._schedule_retry(task_id, result.code)
            if exhausted:
                self._record(task_id, False, "RETRY_EXHAUSTED")
                deleted = False
                if self._delete_exhausted_enabled and identity:
                    deleted = self._verified_unlink(path, identity, root)
                if deleted:
                    self._clear_retry_state(task_id)
                    self._sha1_cache.pop(identity, None)
                    if self._detailed_logs:
                        logger.warning(
                            f"#115秒传# 已达到最大重试次数({attempts}/{self._max_retries})，"
                            f"已安全删除失败文件: {filename}"
                        )
                    else:
                        logger.warning(
                            f"#115秒传# [简短] 重试耗尽清理=已删除 | 文件={filename} | "
                            f"重试次数={attempts}/{self._max_retries}"
                        )
                    self._remove_empty_parent_dirs(path.parent, root)
                else:
                    cleanup_state = "安全校验失败，文件已保留" if self._delete_exhausted_enabled else "文件已保留"
                    logger.warning(
                        f"#115秒传# {'已达到最大重试次数' if self._detailed_logs else '[简短] 重试已达上限'}"
                        f"({attempts}/{self._max_retries})，停止自动重试，{cleanup_state}: {filename}"
                    )
                self._send_bot_exhausted(
                    path, attempts, result.code,
                    deleted=deleted,
                    delete_requested=self._delete_exhausted_enabled,
                )
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
            self._initialize_retry(self._task_id(destination, self._retry_dir), "RAPID_MISS")
            if self._detailed_logs:
                logger.info(f"#115秒传# 文件已移动到临时目录: {self._safe_log_value(destination)}")
                logger.info(f"#115秒传# [后台线程-{threading.current_thread().name}] 文件处理完成(移至临时目录): {self._safe_log_value(destination.name)}")
            else:
                logger.info(
                    f"#115秒传# [简短] 已转移临时文件夹 | 文件={self._safe_log_value(destination.name)} | "
                    f"临时目录={self._safe_log_value(destination.parent)}"
                )
        except (OSError, ValueError):
            self._record(task_id, False, "MOVE_FAILED")
            logger.warning(f"#115秒传# 移动到临时目录失败: {self._safe_log_value(path.name)}")

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

    def _initialize_retry(self, task_id: str, code: str):
        state = self.get_data("retry_state") or {}
        state[task_id] = {
            "attempts": 0,
            "code": self._normalize_code(code),
            "exhausted": False,
        }
        self.save_data("retry_state", state)

    def _schedule_retry(self, task_id: str, code: str) -> Tuple[int, bool]:
        state = self.get_data("retry_state") or {}
        previous = state.get(task_id, {})
        attempts = min(int(previous.get("attempts", 0)) + 1, 1000)
        exhausted = attempts >= self._max_retries
        state[task_id] = {
            "attempts": attempts,
            "code": self._normalize_code(code),
            "exhausted": exhausted,
        }
        self.save_data("retry_state", state)
        return attempts, exhausted

    def _clear_retry_state(self, task_id: str):
        state = self.get_data("retry_state") or {}
        if task_id in state:
            state.pop(task_id, None)
            self.save_data("retry_state", state)

    def _send_bot_success(self, path: Path, from_retry: bool, cleanup_success: bool = True):
        if not self._notify_enabled:
            return
        self._post_bot(
            "115秒传成功",
            f"文件：{self._safe_log_value(path.name, 500)}\n"
            f"方式：{'临时目录重试' if from_retry else '实时监控'}\n"
            f"本地清理：{'已安全删除对应文件' if cleanup_success else '文件身份变化，未删除，请人工检查'}",
        )

    def _send_bot_exhausted(
        self,
        path: Path,
        attempts: int,
        code: str,
        deleted: bool = False,
        delete_requested: bool = False,
    ):
        if not self._notify_enabled:
            return
        if deleted:
            cleanup_text = "失败文件已安全删除；空父文件夹已按安全规则清理。"
        elif delete_requested:
            cleanup_text = "已启用耗尽删除，但安全校验或删除失败，文件已保留在临时目录。"
        else:
            cleanup_text = "文件已保留在临时目录。"
        self._post_bot(
            "115秒传重试已停止",
            f"文件：{self._safe_log_value(path.name, 500)}\n重试次数：{attempts}\n"
            f"状态：{self._normalize_code(code)}\n{cleanup_text}",
        )

    def _post_bot(self, title: str, text: str):
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=text,
                username=settings.SUPERUSER,
            )
            logger.info(
                f"#115秒传# {'Bot通知已提交给管理员' if self._detailed_logs else '[简短] Bot通知=已提交 | 接收者=管理员'}"
                f" | 标题={self._safe_log_value(title)}"
            )
        except Exception:
            logger.warning("#115秒传# MoviePilot Bot通知发送失败 [NOTIFY_FAILED]")

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

    def _audit_rapid(self, path: Path, root: Path, result: RapidResult, from_retry: bool, attempt_no: int = 0):
        if not self._detailed_logs:
            brief_status = "成功" if result.success else ("未命中" if result.code == "RAPID_MISS" else "失败")
            message = (
                f"#115秒传# [简短] 秒传状态={brief_status} | 文件={self._safe_log_value(path.name)} | "
                f"重试次数={attempt_no}/{self._max_retries} | 代码={self._normalize_code(result.code)}"
            )
            (logger.info if result.success else logger.warning)(message)
            return
        if result.success:
            status, matched = "成功", "是"
        elif result.code == "RAPID_MISS":
            status, matched = "未命中", "否"
        else:
            status, matched = "失败", "未知"
        if result.success:
            title = "🎉重试秒传成功" if from_retry else "🎉秒传成功"
        elif result.code == "RAPID_MISS":
            title = "秒传失败(未命中)"
        else:
            title = "秒传失败"
        message = (
            f"#115秒传# {title}: "
            f"{self._safe_log_value(path.name)} | 文件夹={self._safe_log_value(path.parent)} | "
            f"SHA1={result.sha1 or '未计算'} | SHA1服务端匹配={matched} | 秒传={status} | "
            f"代码={self._normalize_code(result.code)}"
        )
        if result.success:
            logger.info(message)
        elif result.code == "RAPID_MISS":
            logger.warning(message)
        else:
            logger.warning(message)

    def _remove_empty_parent_dirs(self, start: Path, root: Path):
        try:
            root_resolved = root.resolve(strict=True)
            current = start
            while True:
                resolved = current.resolve(strict=True)
                if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
                    break
                value = current.lstat()
                attributes = getattr(value, "st_file_attributes", 0)
                reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode) or bool(attributes & reparse_marker):
                    break
                parent = resolved.parent
                current.rmdir()
                if self._detailed_logs:
                    logger.info(f"#115秒传# 已删除空文件夹: {self._safe_log_value(resolved)}")
                else:
                    logger.info(f"#115秒传# [简短] 已删除空文件夹 | 文件夹={self._safe_log_value(resolved)}")
                current = parent
        except (OSError, ValueError):
            return

    def cleanup_empty_directories(self):
        if not self._enabled or not self._empty_cleanup_enabled or not self._empty_cleanup_identities:
            return
        if not self._operation_lock.acquire(blocking=False):
            logger.info("#115秒传# 定时空文件夹清理本轮跳过：当前有文件正在处理")
            return
        try:
            protected = {
                *self._empty_cleanup_roots,
                self._watch_dir,
                self._retry_dir,
                self._protected_pt_dir,
            }
            deleted = 0
            completed = 0
            for root in self._empty_cleanup_roots:
                try:
                    root_stat = self._safe_directory_stat(root)
                    if (root_stat.st_dev, root_stat.st_ino) != self._empty_cleanup_identities.get(root):
                        raise ValueError("CLEANUP_ROOT_CHANGED")
                    root_deleted = self._delete_empty_tree(root, protected)
                    deleted += root_deleted
                    completed += 1
                    if self._detailed_logs:
                        logger.info(
                            f"#115秒传# 定时空文件夹清理完成 | 根目录={self._safe_log_value(root)} | 删除={root_deleted}"
                        )
                except (OSError, ValueError) as exc:
                    logger.warning(
                        f"#115秒传# 定时空文件夹清理跳过根目录 | 根目录={self._safe_log_value(root)} | "
                        f"代码={self._safe_code(exc)}"
                    )
            if self._detailed_logs:
                logger.info(
                    f"#115秒传# 定时空文件夹清理本轮结束 | 根目录={completed}/{len(self._empty_cleanup_roots)} | 删除={deleted}"
                )
            else:
                logger.info(
                    f"#115秒传# [简短] 定时空文件夹清理完成 | 根目录={completed}/{len(self._empty_cleanup_roots)} | 删除={deleted}"
                )
        finally:
            self._operation_lock.release()

    def _delete_empty_tree(self, root: Path, protected: set[Path]) -> int:
        deleted = 0
        visited = 0
        root_resolved = root.resolve(strict=True)
        stack: List[Tuple[Path, bool, Optional[Tuple[int, int]]]] = [(root, False, None)]
        while stack:
            current, expanded, expected_identity = stack.pop()
            try:
                current_resolved = current.resolve(strict=True)
                if current_resolved != root_resolved and not current_resolved.is_relative_to(root_resolved):
                    continue
                if current_resolved != current:
                    continue
            except (OSError, ValueError):
                continue
            if expanded:
                if current in protected:
                    continue
                try:
                    value = self._safe_directory_stat(current)
                    if expected_identity != (value.st_dev, value.st_ino):
                        continue
                    current.rmdir()
                    deleted += 1
                    if self._detailed_logs:
                        logger.info(f"#115秒传# 定时删除空文件夹: {self._safe_log_value(current)}")
                    else:
                        logger.info(f"#115秒传# [简短] 定时删除空文件夹 | 文件夹={self._safe_log_value(current)}")
                except (OSError, ValueError):
                    continue
                continue

            try:
                value = self._safe_directory_stat(current)
            except (OSError, ValueError):
                continue
            identity = (value.st_dev, value.st_ino)
            stack.append((current, True, identity))
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 100000:
                            raise ValueError("CLEANUP_SCAN_LIMIT")
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                            attributes = getattr(entry_stat, "st_file_attributes", 0)
                            reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                            if (
                                entry.is_dir(follow_symlinks=False)
                                and not entry.is_symlink()
                                and not bool(attributes & reparse_marker)
                            ):
                                stack.append((Path(entry.path), False, None))
                        except OSError:
                            continue
            except OSError:
                continue
        return deleted

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
            ("max_retries", "单文件最大重试次数（1-100）", "number"),
            ("empty_cleanup_root", "定时清理空文件夹根目录（每行一个绝对路径）", None),
            ("empty_cleanup_cron", "空文件夹清理 Cron（5段）", None),
        ]
        content = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "Cookie 仅用于登录115官方接口，不发送给其他第三方，不写入插件日志或历史；MoviePilot 会将其保存在自身配置中，请保护管理端和数据目录。"}}]}]}]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "notify_enabled", "label": "Bot通知"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "detailed_logs", "label": "详细日志"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "empty_cleanup_enabled", "label": "定时清理空文件夹"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "delete_exhausted_enabled", "label": "重试耗尽后删除文件及空文件夹"}}]},
        ]})
        for model, label, field_type in fields:
            props = {"model": model, "label": label, "clearable": False}
            if field_type:
                props["type"] = field_type
            if model == "cookie":
                props["autocomplete"] = "new-password"
            component = "VTextField"
            if model == "empty_cleanup_root":
                component = "VTextarea"
                props.update({
                    "rows": 3,
                    "auto-grow": True,
                    "placeholder": "/path/to/cleanup-root-1\n/path/to/cleanup-root-2",
                })
            content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": component, "props": props}]}]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False, "notify_enabled": False, "detailed_logs": True,
            "delete_exhausted_enabled": False,
            "empty_cleanup_enabled": False, "cookie": "",
            "protected_pt_dir": "", "watch_dir": "", "retry_dir": "", "target_pid": "0",
            "cron": "*/10 * * * *", "stable_seconds": 10, "max_batch": 10, "max_retries": 10,
            "empty_cleanup_root": "", "empty_cleanup_cron": "0 4 * * *",
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
