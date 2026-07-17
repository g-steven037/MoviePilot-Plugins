from __future__ import annotations

import os
import queue
import re
import stat
import threading
import hmac
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from time import monotonic, time
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from .dependency import ensure_asynctools_compatible

ensure_asynctools_compatible()
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
    "HOURLY_LIMIT", "CONSECUTIVE_FAILURES",
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
    plugin_version = "1.0.5"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
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
    _min_request_interval = 30
    _hourly_request_limit = 30
    _consecutive_failure_limit = 5
    _failure_cooldown_minutes = 60
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
    _circuit_reason = ""
    _cookie_tag = ""
    _request_times: List[float] = []
    _last_request_at = 0.0
    _consecutive_failures = 0
    _risk_notices: set[str] = set()
    _sha1_cache: Dict[FileIdentity, str] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        run_rapid_once = bool(config.get("run_rapid_once", False))
        run_retry_once = bool(config.get("run_retry_once", False))
        self._enabled = bool(config.get("enabled", False))
        self._client = None
        self._auth_blocked = False
        self._circuit_until = 0.0
        self._circuit_reason = ""
        self._cookie_tag = ""
        self._request_times = []
        self._last_request_at = 0.0
        self._consecutive_failures = 0
        self._risk_notices = set()
        self._sha1_cache = {}
        if not self._enabled:
            return

        try:
            self._cron = str(config.get("cron", "*/10 * * * *")).strip()
            CronTrigger.from_crontab(self._cron)
            self._stable_seconds = self._bounded_int(config.get("stable_seconds", 10), 1, 3600)
            self._max_batch = self._bounded_int(config.get("max_batch", 10), 1, 100)
            self._max_retries = self._bounded_int(config.get("max_retries", 10), 1, 100)
            self._min_request_interval = self._bounded_int(config.get("min_request_interval", 30), 5, 300)
            self._hourly_request_limit = self._bounded_int(config.get("hourly_request_limit", 30), 1, 120)
            self._consecutive_failure_limit = self._bounded_int(
                config.get("consecutive_failure_limit", 5), 2, 20
            )
            self._failure_cooldown_minutes = self._bounded_int(
                config.get("failure_cooldown_minutes", 60), 10, 1440
            )
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
            self._load_risk_control(cookie)
            self._client = self._create_client(cookie)
            del cookie
            if run_rapid_once or run_retry_once:
                config = dict(config)
                config["run_rapid_once"] = False
                config["run_retry_once"] = False
                self.update_config(config)
            self._start_realtime_monitor(queue_existing=False)
            if run_retry_once:
                self._put_control_event("retry_now")
            if run_rapid_once:
                self._put_control_event("scan_now")
            if not run_rapid_once:
                self._queue_existing_files()
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
    def _cookie_fingerprint(cookie: str) -> str:
        secret = str(getattr(settings, "SECRET_KEY", "") or "").strip()
        if not secret:
            raise ValueError("SECRET_KEY_UNAVAILABLE")
        return hmac.new(secret.encode("utf-8"), cookie.encode("utf-8"), sha256).hexdigest()[:32]

    @staticmethod
    def _safe_timestamp(value: Any, now: float, maximum_future: float = 86400) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if timestamp != timestamp or timestamp < 0 or timestamp > now + maximum_future:
            return 0.0
        return timestamp

    def _load_risk_control(self, cookie: str):
        now = time()
        state = self.get_data("risk_control") or {}
        self._cookie_tag = self._cookie_fingerprint(cookie)
        stored_tag = str(state.get("cookie_tag", ""))
        self._auth_blocked = bool(state.get("auth_blocked", False)) and stored_tag == self._cookie_tag
        self._circuit_until = self._safe_timestamp(state.get("circuit_until", 0), now)
        self._circuit_reason = str(state.get("circuit_reason", ""))[:64] if self._circuit_until > now else ""
        if self._circuit_until <= now:
            self._circuit_until = 0.0
        request_times = state.get("request_times", [])
        if not isinstance(request_times, list):
            request_times = []
        self._request_times = sorted(
            timestamp for timestamp in (
                self._safe_timestamp(value, now, maximum_future=60) for value in request_times[-240:]
            )
            if now - 3600 < timestamp <= now + 60
        )
        self._last_request_at = self._safe_timestamp(state.get("last_request_at", 0), now, maximum_future=60)
        try:
            self._consecutive_failures = min(max(int(state.get("consecutive_failures", 0)), 0), 1000)
        except (TypeError, ValueError, OverflowError):
            self._consecutive_failures = 0
        notices = state.get("notices", [])
        self._risk_notices = {
            str(code) for code in notices
            if str(code) in {"AUTH_FAILED", "RATE_LIMITED", "HOURLY_LIMIT", "CONSECUTIVE_FAILURES"}
        } if isinstance(notices, list) else set()
        if self._circuit_until <= now:
            self._risk_notices.discard(str(state.get("circuit_reason", "")))
        if stored_tag != self._cookie_tag:
            self._auth_blocked = False
            self._risk_notices.discard("AUTH_FAILED")
        self._save_risk_control()
        if self._auth_blocked:
            logger.warning("#115秒传# 已恢复认证熔断；更换有效Cookie后才会解除")
        elif self._circuit_until > now:
            logger.warning(
                f"#115秒传# 已恢复风控暂停 | 代码={self._normalize_code(self._circuit_reason)} | "
                f"剩余秒数={int(self._circuit_until - now)}"
            )

    def _save_risk_control(self):
        self.save_data("risk_control", {
            "version": 1,
            "cookie_tag": self._cookie_tag,
            "auth_blocked": self._auth_blocked,
            "circuit_until": int(self._circuit_until),
            "circuit_reason": self._normalize_code(self._circuit_reason) if self._circuit_reason else "",
            "request_times": [int(value) for value in self._request_times[-240:]],
            "last_request_at": int(self._last_request_at),
            "consecutive_failures": self._consecutive_failures,
            "notices": sorted(self._risk_notices),
        })

    def _risk_notice_once(self, code: str, title: str, text: str):
        if not self._notify_enabled or code in self._risk_notices:
            return
        self._risk_notices.add(code)
        self._save_risk_control()
        self._post_bot(title, text)

    def _open_circuit(self, code: str, seconds: int, title: str, text: str):
        until = time() + max(int(seconds), 1)
        if until > self._circuit_until:
            self._circuit_until = until
            self._circuit_reason = self._normalize_code(code)
        self._save_risk_control()
        self._risk_notice_once(code, title, text)

    def _acquire_request_slot(self) -> bool:
        now = time()
        if self._circuit_until and now >= self._circuit_until:
            self._risk_notices.discard(self._circuit_reason)
            self._circuit_until = 0.0
            self._circuit_reason = ""
            self._save_risk_control()
        if self._auth_blocked or now < self._circuit_until:
            return False
        self._request_times = [value for value in self._request_times if now - 3600 < value <= now + 60]
        if len(self._request_times) >= self._hourly_request_limit:
            seconds = max(int(self._request_times[0] + 3600 - now), 60)
            self._open_circuit(
                "HOURLY_LIMIT", seconds,
                "115秒传已触发小时请求保护",
                f"过去一小时已达到 {self._hourly_request_limit} 次秒传初始化操作，暂停 {seconds} 秒。",
            )
            logger.warning(
                f"#115秒传# 小时请求额度已用尽 | 上限={self._hourly_request_limit} | 暂停秒数={seconds}"
            )
            return False
        delay = self._min_request_interval - (now - self._last_request_at)
        if delay > 0:
            if self._detailed_logs:
                logger.info(f"#115秒传# 风控间隔等待 {int(delay) + 1} 秒")
            if self._stop_event.wait(delay):
                return False
            now = time()
            if self._auth_blocked or now < self._circuit_until:
                return False
            self._request_times = [value for value in self._request_times if now - 3600 < value <= now + 60]
            if len(self._request_times) >= self._hourly_request_limit:
                return False
        self._request_times.append(now)
        self._last_request_at = now
        self._save_risk_control()
        return True

    def _apply_risk_result(self, result: RapidResult):
        if result.code == "CIRCUIT_OPEN":
            return
        if result.code == "AUTH_FAILED":
            self._auth_blocked = True
            self._save_risk_control()
            self._risk_notice_once(
                "AUTH_FAILED", "115秒传认证已熔断",
                "检测到115登录失效，已停止所有115请求。请更新有效Cookie后重新启用插件。",
            )
            return
        if result.code == "RATE_LIMITED":
            self._consecutive_failures = 0
            self._open_circuit(
                "RATE_LIMITED", 3600,
                "115秒传已触发限流保护",
                "检测到115频率限制，已暂停所有115请求一小时；重启插件不会提前解除。",
            )
            return
        if result.success or result.code == "RAPID_MISS":
            self._consecutive_failures = 0
            self._risk_notices.discard("CONSECUTIVE_FAILURES")
            self._save_risk_control()
            return
        if result.code in {"NETWORK_TIMEOUT", "NETWORK_ERROR", "INVALID_RESPONSE", "CLIENT_ERROR", "API_REJECTED"}:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._consecutive_failure_limit:
                seconds = self._failure_cooldown_minutes * 60
                self._open_circuit(
                    "CONSECUTIVE_FAILURES", seconds,
                    "115秒传连续失败保护已启动",
                    f"连续 {self._consecutive_failures} 次接口或网络失败，已暂停请求 {self._failure_cooldown_minutes} 分钟。",
                )
            else:
                self._save_risk_control()

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
        reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE…4517 tokens truncated…D")
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
            ("min_request_interval", "115请求最小间隔秒数（5-300）", "number"),
            ("hourly_request_limit", "每小时最多115请求数（1-120）", "number"),
            ("consecutive_failure_limit", "连续技术失败熔断次数（2-20）", "number"),
            ("failure_cooldown_minutes", "连续失败暂停分钟数（10-1440）", "number"),
            ("empty_cleanup_root", "定时清理空文件夹根目录（每行一个绝对路径）", None),
            ("empty_cleanup_cron", "空文件夹清理 Cron（5段）", None),
        ]
        content = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": "Cookie 仅用于登录115官方接口，不发送给其他第三方，不写入插件日志或历史；MoviePilot 会将其保存在自身配置中，请保护管理端和数据目录。"}}]}]}]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "run_rapid_once", "label": "立即运行秒传一次"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSwitch", "props": {"model": "run_retry_once", "label": "立即重试秒传一次"}}]},
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
            "enabled": False, "run_rapid_once": False, "run_retry_once": False,
            "notify_enabled": False, "detailed_logs": True,
            "delete_exhausted_enabled": False,
            "empty_cleanup_enabled": False, "cookie": "",
            "protected_pt_dir": "", "watch_dir": "", "retry_dir": "", "target_pid": "0",
            "cron": "*/10 * * * *", "stable_seconds": 10, "max_batch": 10, "max_retries": 10,
            "min_request_interval": 30, "hourly_request_limit": 30,
            "consecutive_failure_limit": 5, "failure_cooldown_minutes": 60,
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
