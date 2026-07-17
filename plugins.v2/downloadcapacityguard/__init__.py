from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Dict, List, Optional, Tuple

from app.chain.download import DownloadChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ResourceDownloadEventData
from app.schemas.types import ChainEventType, NotificationType


GIB = 1024 ** 3


class DownloadCapacityGuard(_PluginBase):
    plugin_name = "下载容量控制"
    plugin_desc = "监控VPS本地磁盘容量，在下载任务提交前拦截可能导致容量不足的PT下载，仅自用测试。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/download-capacity-guard.svg"
    plugin_version = "0.1.1"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "downloadcapacityguard_"
    plugin_order = 32
    auth_level = 2

    _enabled = False
    _notify_enabled = False
    _reject_unknown_size = True
    _monitor_path = Path()
    _monitor_device = 0
    _reserve_bytes = 10 * GIB
    _size_multiplier = 1.05
    _reservation_seconds = 120
    _lock = threading.Lock()
    _reservations: Dict[str, Tuple[int, float]] = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._notify_enabled = bool(config.get("notify_enabled", False))
        self._reject_unknown_size = bool(config.get("reject_unknown_size", True))
        self._reservations = {}
        if not self._enabled:
            return
        try:
            raw_path = str(config.get("monitor_path", "")).strip()
            if not raw_path or not Path(raw_path).is_absolute():
                raise ValueError("MONITOR_PATH_INVALID")
            self._monitor_path = Path(raw_path).resolve(strict=True)
            if not self._monitor_path.is_dir() or self._is_link_or_reparse(self._monitor_path):
                raise ValueError("MONITOR_PATH_UNSAFE")
            self._monitor_device = self._monitor_path.stat().st_dev
            reserve_gb = self._bounded_float(config.get("reserve_gb", 10), 0, 100000)
            multiplier_percent = self._bounded_float(config.get("size_multiplier_percent", 105), 100, 300)
            self._reserve_bytes = math.ceil(reserve_gb * GIB)
            self._size_multiplier = multiplier_percent / 100
            self._reservation_seconds = self._bounded_int(
                config.get("reservation_seconds", 120), 10, 600
            )
            logger.info(
                f"#下载容量控制# 已启用 | 总容量={self._gb(shutil.disk_usage(self._monitor_path).total)} | "
                f"保留空间={self._gb(self._reserve_bytes)} | 安全倍率={multiplier_percent:g}%"
            )
        except Exception as exc:
            self._enabled = False
            logger.error(f"下载容量控制：初始化失败 [{self._safe_code(exc)}]")

    @staticmethod
    def _bounded_float(value: Any, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("CONFIG_INVALID") from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError("CONFIG_OUT_OF_RANGE")
        return number

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("CONFIG_INVALID") from exc
        if not minimum <= number <= maximum:
            raise ValueError("CONFIG_OUT_OF_RANGE")
        return number

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        value = path.lstat()
        attributes = getattr(value, "st_file_attributes", 0)
        reparse_marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return path.is_symlink() or bool(attributes & reparse_marker)

    @staticmethod
    def _safe_code(exc: Exception) -> str:
        text = str(exc)
        if text and text.isupper() and " " not in text and len(text) <= 64:
            return text
        return type(exc).__name__.upper()[:64]

    @staticmethod
    def _safe_text(value: Any, limit: int = 300) -> str:
        text = "".join(
            char if ord(char) >= 0x20 and ord(char) != 0x7F else "?"
            for char in str(value or "")
        )
        return text[:limit]

    @staticmethod
    def _gb(value: int) -> str:
        return f"{max(int(value), 0) / GIB:.2f} GiB"

    @staticmethod
    def _task_remaining_bytes(task: Any) -> int:
        try:
            size = float(getattr(task, "size", 0) or 0)
            progress = min(max(float(getattr(task, "progress", 0) or 0), 0), 100)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("ACTIVE_TASK_INVALID") from exc
        if not math.isfinite(size) or not math.isfinite(progress):
            raise ValueError("ACTIVE_TASK_INVALID")
        if progress >= 100:
            return 0
        if size <= 0:
            raise ValueError("ACTIVE_TASK_SIZE_UNKNOWN")
        return math.ceil(size * (1 - progress / 100))

    def _path_on_monitored_device(self, value: Any) -> Optional[bool]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            candidate = Path(text)
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            if not candidate.exists():
                return None
            return candidate.stat().st_dev == self._monitor_device
        except (OSError, ValueError):
            return None

    def _active_remaining_bytes(self) -> int:
        tasks = DownloadChain().list_torrents() or []
        total = 0
        for task in tasks:
            remaining = self._task_remaining_bytes(task)
            if remaining <= 0:
                continue
            locations = (
                getattr(task, "save_path", None), getattr(task, "content_path", None),
                getattr(task, "path", None),
            )
            device_checks = [self._path_on_monitored_device(item) for item in locations if item]
            if any(check is True for check in device_checks) or not device_checks or all(
                check is None for check in device_checks
            ):
                total += remaining
        return total

    def _event_targets_monitored_device(self, event_data: ResourceDownloadEventData) -> bool:
        options = getattr(event_data, "options", None) or {}
        raw_path = str(options.get("save_path") or "").strip()
        if not raw_path:
            return True
        storage_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):/", raw_path)
        if storage_match and len(storage_match.group(1)) > 1:
            if storage_match.group(1).lower() != "local":
                return False
            raw_path = raw_path.split(":", 1)[1]
        device_check = self._path_on_monitored_device(raw_path)
        return device_check is not False

    def _cleanup_reservations(self, now: float) -> int:
        self._reservations = {
            key: value for key, value in self._reservations.items() if value[1] > now
        }
        return sum(size for size, _ in self._reservations.values())

    def _snapshot(self) -> Tuple[int, int, int, int]:
        usage = shutil.disk_usage(self._monitor_path)
        active = self._active_remaining_bytes()
        reserved = self._cleanup_reservations(monotonic())
        safe_available = max(0, int(usage.free) - self._reserve_bytes - active - reserved)
        return int(usage.free), active, reserved, safe_available

    @staticmethod
    def _request_key(event_data: ResourceDownloadEventData) -> str:
        torrent = getattr(getattr(event_data, "context", None), "torrent_info", None)
        material = "|".join((
            str(getattr(torrent, "site", "") or ""),
            str(getattr(torrent, "title", "") or ""),
            str(getattr(torrent, "enclosure", "") or ""),
            str(monotonic()),
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]

    def _reject(
        self,
        event_data: ResourceDownloadEventData,
        title: str,
        code: str,
        reason: str,
        requested: int = 0,
        safe_available: int = 0,
    ):
        event_data.cancel = True
        event_data.source = self.plugin_name
        event_data.reason = reason
        logger.warning(
            f"#下载容量控制# 已拒绝下载 | 文件={self._safe_text(title)} | "
            f"请求={self._gb(requested)} | 安全可用={self._gb(safe_available)} | 代码={code}"
        )
        self._record("REJECTED", code, requested, safe_available)
        if self._notify_enabled:
            self._post_bot(
                "下载已被容量控制拒绝",
                f"资源：{self._safe_text(title, 500)}\n"
                f"所需空间：{self._gb(requested)}\n"
                f"安全可用：{self._gb(safe_available)}\n"
                f"状态：{code}",
            )

    @eventmanager.register(etype=ChainEventType.ResourceDownload, priority=10000)
    def handle_resource_download(self, event: Event):
        if not self._enabled or not event or not event.event_data:
            return
        event_data: ResourceDownloadEventData = event.event_data
        if event_data.cancel:
            return
        if not self._event_targets_monitored_device(event_data):
            logger.info("#下载容量控制# 下载目标不在受监控本地磁盘，本插件跳过")
            return
        torrent = getattr(getattr(event_data, "context", None), "torrent_info", None)
        if not torrent:
            self._reject(event_data, "未知资源", "CONTEXT_INVALID", "下载上下文无有效种子信息")
            return
        title = str(getattr(torrent, "title", "") or "未命名资源")
        try:
            raw_size = float(getattr(torrent, "size", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            raw_size = 0
        if not math.isfinite(raw_size) or raw_size <= 0:
            if self._reject_unknown_size:
                self._reject(event_data, title, "SIZE_UNKNOWN", "种子大小未知，已按严格模式拒绝")
            else:
                logger.warning(f"#下载容量控制# 种子大小未知，按配置放行 | 文件={self._safe_text(title)}")
            return
        requested = math.ceil(raw_size * self._size_multiplier)
        with self._lock:
            try:
                free, active, reserved, safe_available = self._snapshot()
            except Exception as exc:
                self._reject(
                    event_data, title, "CAPACITY_CHECK_FAILED",
                    f"容量检查失败：{self._safe_code(exc)}", requested=requested,
                )
                return
            if requested > safe_available:
                self._reject(
                    event_data, title, "INSUFFICIENT_SPACE", "本地磁盘安全可用空间不足",
                    requested=requested, safe_available=safe_available,
                )
                return
            self._reservations[self._request_key(event_data)] = (
                requested, monotonic() + self._reservation_seconds
            )
            logger.info(
                f"#下载容量控制# 已放行下载 | 文件={self._safe_text(title)} | "
                f"请求={self._gb(requested)} | 磁盘空闲={self._gb(free)} | "
                f"未完成任务剩余={self._gb(active)} | 短期预留={self._gb(reserved)} | "
                f"放行前安全可用={self._gb(safe_available)}"
            )
            self._record("ALLOWED", "SPACE_OK", requested, safe_available)

    def _post_bot(self, title: str, text: str):
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=text,
                username=settings.SUPERUSER,
            )
            logger.info(f"#下载容量控制# Bot通知已提交 | 标题={self._safe_text(title)}")
        except Exception:
            logger.warning("#下载容量控制# Bot通知发送失败 [NOTIFY_FAILED]")

    def _record(self, action: str, code: str, requested: int, available: int):
        history = self.get_data("history") or []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "code": code,
            "requested_gib": round(max(requested, 0) / GIB, 2),
            "available_gib": round(max(available, 0) / GIB, 2),
        })
        self.save_data("history", history[:200])

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[dict]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        content = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
            "component": "VAlert", "props": {
                "type": "warning", "variant": "tonal",
                "text": "插件仅在MoviePilot准备向下载器提交任务时同步检查容量，不创建定时任务。安全可用空间会扣除保留空间、未完成任务剩余量和短期并发预留；检查异常默认拒绝下载。监控路径必须是MoviePilot容器内可见的本地绝对路径。",
            }
        }]}]}]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                "component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                "component": "VSwitch", "props": {"model": "notify_enabled", "label": "Bot通知"}
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                "component": "VSwitch", "props": {"model": "reject_unknown_size", "label": "拒绝大小未知的任务"}
            }]},
        ]})
        fields = [
            ("monitor_path", "本地磁盘监控路径，例如 /downloads", "text"),
            ("reserve_gb", "必须保留的空闲空间（GiB）", "number"),
            ("size_multiplier_percent", "新任务容量安全倍率（100-300%）", "number"),
            ("reservation_seconds", "并发任务短期预留秒数（10-600）", "number"),
        ]
        for model, label, field_type in fields:
            content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VTextField", "props": {
                    "model": model, "label": label, "type": field_type, "clearable": False,
                }
            }]}]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "notify_enabled": False,
            "reject_unknown_size": True,
            "monitor_path": "",
            "reserve_gb": 10,
            "size_multiplier_percent": 105,
            "reservation_seconds": 120,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        rows = [[
            item.get("time"), item.get("action"), item.get("code"),
            item.get("requested_gib"), item.get("available_gib"),
        ] for item in history[:200]]
        return [{"component": "VTable", "props": {"hover": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": title}
                for title in ("时间", "动作", "状态", "请求GiB", "安全可用GiB")
            ]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [
                {"component": "td", "text": str(value if value is not None else "")}
                for value in row
            ]} for row in rows]},
        ]}]

    def get_command(self):
        return None

    def get_api(self):
        return None

    def stop_service(self):
        self._enabled = False
        with self._lock:
            self._reservations = {}
