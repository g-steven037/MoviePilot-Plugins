from __future__ import annotations

import datetime
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchfiles import Change, watch

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.meta.words import WordsMatcher
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.utils.system import SystemUtils


_operation_lock = threading.Lock()


def _has_suffix_in(file_path: Path, extensions: Iterable[str]) -> bool:
    if not file_path.suffix:
        return False
    return file_path.suffix.casefold() in {str(ext).casefold() for ext in extensions}


def _is_download_tmp_file(file_path: Path) -> bool:
    return _has_suffix_in(file_path, settings.DOWNLOAD_TMPEXT)


class WatchfilesEvent:
    def __init__(self, src_path: str, is_directory: bool):
        self.src_path = src_path
        self.dest_path = src_path
        self.is_directory = is_directory


class WatchfilesObserver:
    """与官方实时硬链接插件一致的 watchfiles 监控适配器。"""

    def __init__(self, timeout: int = 10, force_polling: Optional[bool] = None):
        self.daemon = True
        self._force_polling = force_polling
        self._poll_delay_ms = max(int(timeout * 1000), 300)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handler = None
        self._path: Optional[str] = None
        self._recursive = True

    def schedule(self, handler: Any, path: str, recursive: bool = True):
        self._handler = handler
        self._path = path
        self._recursive = recursive

    def start(self):
        if not self._handler or not self._path:
            raise ValueError("WATCH_CONFIG_INVALID")
        path = Path(self._path)
        if not path.exists() or not path.is_dir():
            raise ValueError("WATCH_PATH_INVALID")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="subscribe-assistant-watchfiles",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        try:
            self._run_watch(force_polling=self._force_polling)
        except Exception as exc:
            if self._stop_event.is_set():
                return
            if self._force_polling is True:
                logger.error(f"订阅助手：目录监控失败 [{type(exc).__name__.upper()}]")
                return
            logger.warning("#订阅助手# 性能模式不可用，自动切换兼容模式")
            try:
                self._run_watch(force_polling=True)
            except Exception as fallback_exc:
                if not self._stop_event.is_set():
                    logger.error(f"订阅助手：兼容模式监控失败 [{type(fallback_exc).__name__.upper()}]")

    def _run_watch(self, force_polling: Optional[bool]):
        for changes in watch(
            self._path,
            stop_event=self._stop_event,
            rust_timeout=1000,
            yield_on_timeout=True,
            force_polling=force_polling,
            poll_delay_ms=self._poll_delay_ms,
            recursive=self._recursive,
            ignore_permission_denied=True,
        ):
            if self._stop_event.is_set():
                break
            for change_type, event_path in sorted(changes or [], key=lambda item: item[1]):
                self._handler.dispatch(change_type=change_type, event_path=event_path)


class FileMonitorHandler:
    def __init__(self, monpath: str, plugin: "SubscribeAssistant"):
        self._watch_path = monpath
        self._plugin = plugin

    def dispatch(self, change_type: Change, event_path: str):
        if change_type not in {Change.added, Change.modified}:
            return
        path = Path(event_path)
        if not path.exists():
            return
        is_directory = path.is_dir()
        if not is_directory and _is_download_tmp_file(path):
            return
        event = WatchfilesEvent(src_path=event_path, is_directory=is_directory)
        self._plugin.event_handler(
            event=event,
            text="修改" if change_type == Change.modified else "创建",
            mon_path=self._watch_path,
            event_path=event_path,
        )


class SubscribeAssistant(_PluginBase):
    plugin_name = "订阅助手"
    plugin_desc = "基于实时硬链接，将订阅自定义识别词应用到目标文件名；未命中时保持原名。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/subscribe-assistant.svg"
    plugin_version = "0.2.0"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "subscribeassistant_"
    plugin_order = 35
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _observers: List[WatchfilesObserver] = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _mode = "fast"
    _monitor_dirs = ""
    _exclude_keywords = ""
    _cron = ""
    _size = 0.0
    _dirconf: Dict[str, Path] = {}
    _subscription_words: Optional[List[Tuple[int, List[str]]]] = None
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._mode = str(config.get("mode", "fast") or "fast")
        self._monitor_dirs = str(config.get("monitor_dirs", "") or "")
        self._exclude_keywords = str(config.get("exclude_keywords", "") or "")
        self._cron = str(config.get("cron", "") or "").strip()
        try:
            self._size = self._parse_size(config.get("size", 0))
        except ValueError as exc:
            self._enabled = False
            self._onlyonce = False
            logger.error(f"订阅助手：最小文件大小配置无效 [{str(exc)}]")
            return
        self._dirconf = {}
        self._subscription_words = None

        if not self._enabled and not self._onlyonce:
            return

        for line in self._monitor_dirs.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                source, target = self._parse_directory_line(line)
                self._dirconf[str(source)] = target
            except ValueError as exc:
                logger.error(f"订阅助手：监控目录配置无效 [{str(exc)}]")

        if self._enabled:
            for source_text, target in self._dirconf.items():
                try:
                    observer = WatchfilesObserver(
                        timeout=10,
                        force_polling=True if self._mode == "compatibility" else None,
                    )
                    observer.schedule(FileMonitorHandler(source_text, self), path=source_text, recursive=True)
                    observer.start()
                    self._observers.append(observer)
                    logger.info(
                        f"#订阅助手# 实时硬链接监控已启动 | 来源={source_text} | 目标={target} | "
                        f"模式={self._mode}"
                    )
                except Exception as exc:
                    logger.error(f"订阅助手：目录监控启动失败 [{type(exc).__name__.upper()}]")

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sync_all,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
            )
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config)
            self._scheduler.start()

    @staticmethod
    def _parse_size(value: object) -> float:
        if value in (None, ""):
            return 0.0
        try:
            size = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("SIZE_INVALID") from exc
        if not 0 <= size <= 1024 * 1024:
            raise ValueError("SIZE_OUT_OF_RANGE")
        return size

    @staticmethod
    def _parse_directory_line(line: str) -> Tuple[Path, Path]:
        if SystemUtils.is_windows():
            match = re.fullmatch(r"([A-Za-z]:[^:]+):([A-Za-z]:[^:]+)", line)
            if not match:
                raise ValueError("DIRECTORY_FORMAT_INVALID")
            source, target = Path(match.group(1)), Path(match.group(2))
        else:
            parts = line.split(":", 1)
            if len(parts) != 2:
                raise ValueError("DIRECTORY_FORMAT_INVALID")
            source, target = Path(parts[0]), Path(parts[1])
        if not source.is_absolute() or not target.is_absolute():
            raise ValueError("DIRECTORY_MUST_BE_ABSOLUTE")
        if source.is_symlink():
            raise ValueError("DIRECTORY_SYMLINK_FORBIDDEN")
        source = source.resolve(strict=True)
        target_candidate = target.resolve(strict=False)
        if (
            source == target_candidate
            or target_candidate.is_relative_to(source)
            or source.is_relative_to(target_candidate)
        ):
            raise ValueError("DIRECTORIES_MUST_BE_DISJOINT")
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("DIRECTORY_SYMLINK_FORBIDDEN")
        target = target.resolve(strict=True)
        if not source.is_dir() or not target.is_dir():
            raise ValueError("DIRECTORY_NOT_FOUND")
        if source == Path(source.anchor) or target == Path(target.anchor):
            raise ValueError("FILESYSTEM_ROOT_FORBIDDEN")
        if source == target or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("DIRECTORIES_MUST_BE_DISJOINT")
        if source.stat().st_dev != target.stat().st_dev:
            raise ValueError("HARDLINK_CROSS_DEVICE")
        return source, target

    @eventmanager.register([EventType.SubscribeAdded, EventType.SubscribeModified, EventType.SubscribeDeleted])
    def invalidate_subscription_words(self, event: Event):
        if self._enabled:
            self._subscription_words = None

    def _load_subscription_words(self) -> List[Tuple[int, List[str]]]:
        if self._subscription_words is not None:
            return self._subscription_words
        words: List[Tuple[int, List[str]]] = []
        try:
            for subscribe in SubscribeOper().list() or []:
                raw = getattr(subscribe, "custom_words", None)
                if isinstance(raw, str):
                    current = [line.strip() for line in raw.splitlines() if line.strip()]
                elif isinstance(raw, (list, tuple)):
                    current = [str(line).strip() for line in raw if str(line).strip()]
                else:
                    current = []
                if current:
                    words.append((int(getattr(subscribe, "id", 0) or 0), current))
        except Exception as exc:
            logger.warning(f"#订阅助手# 读取订阅自定义识别词失败 [{type(exc).__name__.upper()}]")
            words = []
        self._subscription_words = words
        return words

    def _renamed_filename(self, filename: str) -> Tuple[str, int, str]:
        results: Dict[str, int] = {}
        for subscribe_id, words in self._load_subscription_words():
            prepared, applied = WordsMatcher().prepare(filename, custom_words=words)
            if applied and prepared != filename:
                results.setdefault(prepared, subscribe_id)
        if not results:
            return filename, 0, "NO_CUSTOM_WORD_MATCH"
        if len(results) > 1:
            return filename, 0, "AMBIGUOUS_CUSTOM_WORDS"
        renamed, subscribe_id = next(iter(results.items()))
        if not renamed or Path(renamed).name != renamed or renamed in {".", ".."}:
            return filename, 0, "UNSAFE_RENAMED_FILENAME"
        return renamed, subscribe_id, "CUSTOM_WORD_APPLIED"

    def sync_all(self):
        logger.info("#订阅助手# 开始全量实时硬链接")
        self._subscription_words = None
        for source_text in list(self._dirconf):
            for file_path in SystemUtils.list_files(Path(source_text), [".*"]):
                self._handle_file(event_path=str(file_path), mon_path=source_text)
        logger.info("#订阅助手# 全量实时硬链接完成")

    def event_handler(self, event: WatchfilesEvent, mon_path: str, text: str, event_path: str):
        if not event.is_directory:
            logger.debug(f"文件{text}：{event_path}")
            self._handle_file(event_path=event_path, mon_path=mon_path)

    def _link_file(
        self,
        src_path: Path,
        mon_path: str,
        target_path: Path,
        transfer_type: str = "link",
    ) -> Tuple[bool, str, Path, str, int]:
        try:
            rel_path = src_path.relative_to(Path(mon_path))
        except ValueError:
            return False, "SOURCE_OUTSIDE_MONITOR", target_path, "SOURCE_OUTSIDE_MONITOR", 0
        renamed, subscribe_id, rename_status = self._renamed_filename(rel_path.name)
        new_path = target_path / rel_path.parent / renamed
        if new_path.exists():
            return True, "TARGET_EXISTS", new_path, rename_status, subscribe_id
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if transfer_type == "copy":
            code, message = SystemUtils.copy(src_path, new_path)
        else:
            code, message = SystemUtils.link(src_path, new_path)
        return code == 0, str(message or ""), new_path, rename_status, subscribe_id

    def _handle_file(self, event_path: str, mon_path: str):
        file_path = Path(event_path)
        try:
            if not file_path.exists() or not file_path.is_file() or _is_download_tmp_file(file_path):
                return
            with _operation_lock:
                normalized = event_path.replace("\\", "/")
                if any(marker in normalized for marker in ("/@Recycle/", "/#recycle/", "/.", "/@eaDir")):
                    return
                for keyword in self._exclude_keywords.splitlines():
                    if keyword and re.search(keyword, event_path):
                        logger.info(f"#订阅助手# 文件命中排除关键词，跳过 | 文件={file_path.name}")
                        return
                transfer_type = "copy" if self._size > 0 and file_path.stat().st_size < self._size * 1024 else "link"
                target = self._dirconf.get(mon_path)
                if not target:
                    return
                state, message, destination, rename_status, subscribe_id = self._link_file(
                    src_path=file_path,
                    mon_path=mon_path,
                    target_path=target,
                    transfer_type=transfer_type,
                )
                if not state:
                    logger.warning(
                        f"#订阅助手# {'复制' if transfer_type == 'copy' else '硬链接'}失败 | "
                        f"文件={file_path.name} | 代码={message or 'TRANSFER_FAILED'}"
                    )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{file_path.name} 硬链接失败",
                            text=f"状态：{message or 'TRANSFER_FAILED'}",
                        )
                    return
                if rename_status == "AMBIGUOUS_CUSTOM_WORDS":
                    logger.warning(f"#订阅助手# 多个订阅识别词产生不同文件名，已保持原名 | 文件={file_path.name}")
                elif rename_status == "UNSAFE_RENAMED_FILENAME":
                    logger.warning(f"#订阅助手# 识别词产生不安全文件名，已保持原名 | 文件={file_path.name}")
                logger.info(
                    f"#订阅助手# {'复制' if transfer_type == 'copy' else '硬链接'}成功 | "
                    f"源文件={file_path.name} | 目标文件={destination.name} | "
                    f"识别词={'订阅ID ' + str(subscribe_id) if subscribe_id else '未命中，保持原名'}"
                )
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Manual,
                        title=f"{destination.name} 硬链接完成",
                        text=f"目标目录：{destination.parent}",
                    )
        except Exception as exc:
            logger.error(f"订阅助手：目录监控处理失败 [{type(exc).__name__.upper()}]")

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        if event:
            data = event.event_data or {}
            if data.get("action") != "subscribe_assistant_link":
                return
        self.sync_all()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/subscribe_assistant_link",
            "event": EventType.PluginAction,
            "desc": "订阅识别词硬链接",
            "category": "管理",
            "data": {"action": "subscribe_assistant_link"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/realtime_link",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "订阅识别词实时硬链接",
        }]

    def sync(self, apikey: str) -> schemas.Response:
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        self.sync_all()
        return schemas.Response(success=True)

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "SubscribeAssistantLinkMonitor",
                "name": "订阅识别词全量硬链接",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_all,
                "kwargs": {},
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"},
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VSwitch", "props": {"model": "notify", "label": "发送通知"},
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"},
                    }]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VSelect", "props": {
                            "model": "mode", "label": "监控模式",
                            "items": [
                                {"title": "兼容模式", "value": "compatibility"},
                                {"title": "性能模式", "value": "fast"},
                            ],
                        },
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField", "props": {
                            "model": "cron", "label": "定时全量同步", "placeholder": "5位Cron，留空关闭",
                        },
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField", "props": {
                            "model": "size", "label": "最小硬链接大小（KB）",
                        },
                    }]},
                ]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                    "component": "VTextarea", "props": {
                        "model": "monitor_dirs", "label": "监控目录",
                        "rows": 5,
                        "placeholder": "/path/to/pt-downloads:/path/to/hardlinks\n每行一组：源目录:目标目录",
                    },
                }]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                    "component": "VTextarea", "props": {
                        "model": "exclude_keywords", "label": "排除关键词（每行一个正则）", "rows": 2,
                    },
                }]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                    "component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "核心目录监控与官方“实时硬链接”一致。目标文件名会依次尝试现有订阅中的自定义识别词；唯一命中时重命名，没有命中时保持原名，多个订阅产生不同结果时也保持原名。插件不会新增、修改或删除订阅。小于最小硬链接大小的文件按官方逻辑复制。",
                    },
                }]}]},
            ],
        }], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "mode": "fast",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "cron": "",
            "size": "",
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        for observer in self._observers:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                pass
        self._observers = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown(wait=False)
                self._event.clear()
            self._scheduler = None
