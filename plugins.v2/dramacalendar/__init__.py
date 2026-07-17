from __future__ import annotations

import math
import re
import threading
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .cache import ShowCache
from .client import ClientError, EpisodeUpdate, MediaServerClient, TmdbClient


WEEKDAYS = "一二三四五六日"


def _episode_ranges(numbers: List[int]) -> str:
    values = sorted(set(numbers))
    if not values:
        return ""
    ranges: List[str] = []
    start = previous = values[0]
    for number in values[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def format_calendar(now: datetime, updates: List[EpisodeUpdate], calendar_days: int) -> str:
    """将未来排期整理为适合 MoviePilot 通知渠道的纯文本日历。"""
    grouped: Dict[date, Dict[Tuple[str, int, bool], List[EpisodeUpdate]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for update in updates:
        grouped[update.air_date][
            (update.series_name, update.season, update.in_library)
        ].append(update)

    title = "今日剧集更新" if calendar_days == 1 else f"未来{calendar_days}天剧集更新"
    lines = [f"📺 {title}", ""]
    for air_date in sorted(grouped):
        delta = (air_date - now.date()).days
        prefix = "今天" if delta == 0 else "明天" if delta == 1 else air_date.strftime("%m月%d日")
        lines.append(f"【{prefix} 周{WEEKDAYS[air_date.weekday()]}】")
        entries = grouped[air_date]
        for (name, season, in_library), batch in sorted(
            entries.items(), key=lambda item: (not item[0][2], item[0][0], item[0][1])
        ):
            marker = "🟢" if in_library else "🔴"
            lines.append(
                f"{marker} {name} S{season:02}E{_episode_ranges([item.episode for item in batch])}"
            )
        lines.append("")
    if not grouped:
        lines.append(f"未来{calendar_days}天暂无剧集更新")
        lines.append("")
    series_count = len({item.series_name for item in updates})
    lines.append(f"共 {series_count} 部剧集有更新")
    lines.append("🟢 已入库 · 🔴 未入库")
    return "\n".join(lines)


class DramaCalendar(_PluginBase):
    plugin_name = "追剧更新日历"
    plugin_desc = "读取Emby/Jellyfin剧集与TMDB排期，定时向MoviePilot管理员发送更新日历，仅自用测试。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/drama-calendar.svg"
    plugin_version = "0.1.0"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "dramacalendar_"
    plugin_order = 33
    auth_level = 2

    _enabled = False
    _notify_enabled = True
    _use_mp_config = True
    _cron = "0 9 * * *"
    _timezone = ZoneInfo("Asia/Shanghai")
    _calendar_days = 7
    _requests_per_second = 3.0
    _max_retries = 5
    _cache_ttl_hours = 24.0
    _verify_https = True
    _emby_url = ""
    _emby_api_key = ""
    _emby_user_id = ""
    _tmdb_token = ""
    _selected_server = ""
    _run_lock = threading.Lock()
    _stop_event = threading.Event()
    _thread: Optional[threading.Thread] = None

    def init_plugin(self, config: dict = None):
        """读取插件配置并注册一次性任务。"""
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._notify_enabled = bool(config.get("notify_enabled", True))
        self._use_mp_config = bool(config.get("use_mp_config", True))
        self._verify_https = bool(config.get("verify_https", True))
        self._stop_event = threading.Event()
        if not self._enabled:
            return
        try:
            self._cron = str(config.get("cron", "0 9 * * *")).strip()
            CronTrigger.from_crontab(self._cron)
            timezone_name = str(config.get("timezone", "Asia/Shanghai")).strip()
            self._timezone = ZoneInfo(timezone_name)
            self._calendar_days = self._bounded_int(config.get("calendar_days", 7), 1, 31)
            self._requests_per_second = self._bounded_float(
                config.get("tmdb_requests_per_second", 3), 0.2, 20
            )
            self._max_retries = self._bounded_int(config.get("tmdb_max_retries", 5), 0, 10)
            self._cache_ttl_hours = self._bounded_float(
                config.get("cache_ttl_hours", 24), 1, 720
            )
            self._selected_server = str(config.get("media_server", "")).strip()
            if self._use_mp_config:
                (
                    self._emby_url,
                    self._emby_api_key,
                    self._emby_user_id,
                    server_name,
                ) = self._load_moviepilot_media(self._selected_server)
                logger.info(
                    f"#追剧更新日历# 使用MoviePilot媒体服务器配置 | 名称={self._safe(server_name)}"
                )
            else:
                self._emby_url = self._validate_url(config.get("emby_url", ""))
                self._emby_api_key = self._validate_secret(config.get("emby_api_key", ""))
                self._emby_user_id = str(config.get("emby_user_id", "")).strip()
            manual_tmdb = str(config.get("tmdb_token", "") or "").strip()
            self._tmdb_token = self._validate_secret(
                manual_tmdb or str(getattr(settings, "TMDB_API_KEY", "") or "")
            )
            if len(self._emby_user_id) > 256:
                raise ValueError("MEDIA_USER_ID_INVALID")
            if not self._verify_https:
                logger.warning("#追剧更新日历# HTTPS证书校验已关闭，请仅连接可信内网服务器")
            logger.info(
                f"#追剧更新日历# 已启用 | Cron={self._safe(self._cron)} | "
                f"时区={self._safe(str(self._timezone))} | 日历天数={self._calendar_days}"
            )
            if bool(config.get("run_once", False)):
                config["run_once"] = False
                self.update_config(config)
                self._start_worker("立即运行")
        except (ValueError, ZoneInfoNotFoundError) as exc:
            self._enabled = False
            logger.error(f"追剧更新日历：初始化失败 [{self._safe_code(exc)}]")

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
    def _bounded_float(value: Any, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("CONFIG_INVALID") from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError("CONFIG_OUT_OF_RANGE")
        return number

    @staticmethod
    def _validate_secret(value: Any) -> str:
        secret = str(value or "").strip()
        if not secret or len(secret) > 2048 or any(char in secret for char in "\r\n\x00"):
            raise ValueError("SECRET_INVALID")
        return secret

    @staticmethod
    def _validate_url(value: Any) -> str:
        url = str(value or "").strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or len(url) > 2048
            or any(char in url for char in "\r\n\x00")
        ):
            raise ValueError("MEDIA_URL_INVALID")
        return url

    @staticmethod
    def _resolve_moviepilot_media(helper: Any, selected_name: str = "") -> Tuple[str, str, str, str]:
        configs = helper.get_configs() or {}
        try:
            services = helper.get_services() or {}
        except Exception:
            services = {}
        candidates = sorted(
            name for name, conf in configs.items()
            if str(getattr(getattr(conf, "type", ""), "value", getattr(conf, "type", ""))).lower()
            in {"emby", "jellyfin"}
        )
        if not candidates:
            raise ValueError("MP_MEDIA_SERVER_NOT_FOUND")
        name = selected_name or candidates[0]
        if name not in candidates:
            raise ValueError("MP_MEDIA_SERVER_NOT_FOUND")
        service = services.get(name)
        conf = (getattr(service, "config", None) if service else None) or configs.get(name)
        values = getattr(conf, "config", None) or {}
        if not isinstance(values, dict):
            raise ValueError("MP_MEDIA_CONFIG_INVALID")
        host = DramaCalendar._validate_url(values.get("host", ""))
        api_key = DramaCalendar._validate_secret(values.get("apikey", ""))
        instance = getattr(service, "instance", None) if service else None
        user_id = str(getattr(instance, "user", "") or "").strip()
        return host, api_key, user_id, name

    @classmethod
    def _load_moviepilot_media(cls, selected_name: str = "") -> Tuple[str, str, str, str]:
        try:
            from app.helper.mediaserver import MediaServerHelper
            return cls._resolve_moviepilot_media(MediaServerHelper(), selected_name)
        except (ImportError, AttributeError) as exc:
            raise ValueError("MP_MEDIA_HELPER_UNAVAILABLE") from exc

    @staticmethod
    def _moviepilot_media_items() -> List[Dict[str, str]]:
        try:
            from app.helper.mediaserver import MediaServerHelper
            configs = MediaServerHelper().get_configs() or {}
            names = sorted(
                name for name, conf in configs.items()
                if str(getattr(getattr(conf, "type", ""), "value", getattr(conf, "type", ""))).lower()
                in {"emby", "jellyfin"}
            )
            return [{"title": name, "value": name} for name in names]
        except Exception:
            return []

    @staticmethod
    def _safe(value: Any, limit: int = 300) -> str:
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
        return text[:limit]

    @staticmethod
    def _safe_code(exc: Exception) -> str:
        text = str(exc or "")
        if re.fullmatch(r"[A-Z0-9_]{3,80}", text):
            return text
        return type(exc).__name__.upper()[:80]

    def _cache_path(self) -> Path:
        root = Path(str(getattr(settings, "PLUGIN_DATA_PATH", "/config/plugins")))
        return root / "dramacalendar" / "calendar.db"

    def _start_worker(self, source: str) -> bool:
        if self._thread and self._thread.is_alive():
            logger.warning(f"#追剧更新日历# 上一轮仍在运行，本次已跳过 | 来源={self._safe(source)}")
            return False
        self._thread = threading.Thread(
            target=self.generate_calendar,
            kwargs={"source": source},
            name="drama-calendar-worker",
            daemon=True,
        )
        self._thread.start()
        return True

    def generate_calendar(self, source: str = "Cron") -> None:
        """生成日历并通过 MoviePilot 的插件通知渠道发送。"""
        if not self._enabled:
            return
        if not self._run_lock.acquire(blocking=False):
            logger.warning("#追剧更新日历# 上一轮仍在运行，本轮已跳过")
            return
        started = time.monotonic()
        media: Optional[MediaServerClient] = None
        tmdb: Optional[TmdbClient] = None
        cache: Optional[ShowCache] = None
        try:
            cache = ShowCache(self._cache_path(), self._cache_ttl_hours)
            cache.prune()
            media = MediaServerClient(
                self._emby_url,
                self._emby_api_key,
                self._emby_user_id,
                self._verify_https,
            )
            tmdb = TmdbClient(
                self._tmdb_token,
                self._requests_per_second,
                self._max_retries,
                cache,
                str(getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")),
            )
            tmdb.validate()
            library = media.continuing_series()
            series = list({item.tmdb_id: item for item in library}.values())
            now = datetime.now(self._timezone)
            end = now.date() + timedelta(days=self._calendar_days - 1)
            logger.info(
                f"#追剧更新日历# 开始生成 | 来源={self._safe(source)} | "
                f"待查询剧集={len(series)} | 日期={now.date()}~{end}"
            )
            updates: List[EpisodeUpdate] = []
            failed = 0
            for index, item in enumerate(series, start=1):
                if self._stop_event.is_set():
                    raise ClientError("PLUGIN_STOPPED")
                try:
                    updates.extend(tmdb.updates_for(item, now.date(), end))
                except ClientError as exc:
                    failed += 1
                    logger.warning(
                        f"#追剧更新日历# TMDB查询失败 | 剧集={self._safe(item.name)} | "
                        f"代码={self._safe_code(exc)}"
                    )
                if index == len(series) or index % 25 == 0:
                    logger.info(
                        f"#追剧更新日历# 查询进度={index}/{len(series)} | "
                        f"缓存命中={tmdb.cache_hits} | 联网请求={tmdb.network_requests} | 失败={failed}"
                    )
            inventories: Dict[str, set] = {}
            for series_id in {item.media_series_id for item in updates}:
                try:
                    inventories[series_id] = media.available_episodes(series_id)
                except ClientError:
                    inventories[series_id] = set()
            checked = [
                replace(
                    item,
                    in_library=(item.season, item.episode)
                    in inventories.get(item.media_series_id, set()),
                )
                for item in updates
            ]
            message = format_calendar(now, checked, self._calendar_days)
            notification_state = "已关闭"
            if self._notify_enabled:
                try:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="追剧更新日历",
                        text=message,
                        username=settings.SUPERUSER,
                    )
                    notification_state = "已提交"
                except Exception:
                    notification_state = "提交失败"
                    logger.warning("#追剧更新日历# 通知提交失败 [NOTIFY_FAILED]")
            elapsed = time.monotonic() - started
            logger.info(
                f"#追剧更新日历# 生成完成 | 剧集={len(series)} | 排期={len(checked)} | "
                f"失败={failed} | 耗时={elapsed:.1f}秒 | 通知={notification_state}"
            )
            status = "SUCCESS" if notification_state != "提交失败" else "SUCCESS_NOTIFY_FAILED"
            self._record(status, len(series), len(checked), failed, elapsed)
        except Exception as exc:
            code = self._safe_code(exc)
            logger.error(f"#追剧更新日历# 生成失败 | 代码={code}")
            self._record(code, 0, 0, 1, time.monotonic() - started)
            if self._notify_enabled and code != "PLUGIN_STOPPED":
                try:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title="追剧更新日历生成失败",
                        text=f"状态：{code}\n请检查插件配置和MoviePilot日志。",
                        username=settings.SUPERUSER,
                    )
                except Exception:
                    logger.warning("#追剧更新日历# 失败通知提交失败 [NOTIFY_FAILED]")
        finally:
            if media:
                media.close()
            if tmdb:
                tmdb.close()
            if cache:
                cache.close()
            self._run_lock.release()

    def _record(self, status: str, series: int, updates: int, failed: int, elapsed: float) -> None:
        history = self.get_data("history") or []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "series": int(series),
            "updates": int(updates),
            "failed": int(failed),
            "seconds": round(max(elapsed, 0), 1),
        })
        self.save_data("history", history[:100])

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def get_service(self) -> List[dict]:
        """返回Cron公共服务。"""
        if not self._enabled:
            return []
        return [{
            "id": "DramaCalendar_generate",
            "name": f"追剧更新日历（{self._cron}）",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.generate_calendar,
            "kwargs": {"source": "Cron"},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认值。"""
        server_items = self._moviepilot_media_items()
        content: List[dict] = [{
            "component": "VRow",
            "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "默认读取MoviePilot中已启用的Emby/Jellyfin配置和内置TMDB Key，凭据不会写入日志。通过MoviePilot插件通知发送未来剧集排期，可使用立即运行一次手动触发。",
                },
            }]}],
        }]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                "component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                "component": "VSwitch", "props": {"model": "run_once", "label": "立即运行一次"}
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                "component": "VSwitch", "props": {"model": "notify_enabled", "label": "插件通知"}
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                "component": "VSwitch", "props": {"model": "use_mp_config", "label": "读取MoviePilot媒体服务器"}
            }]},
        ]})
        content.append({"component": "VRow", "props": {"show": "{{use_mp_config}}"}, "content": [{
            "component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VSelect", "props": {
                    "model": "media_server",
                    "label": "MoviePilot媒体服务器（留空自动选择第一个）",
                    "items": server_items,
                    "clearable": True,
                }
            }]
        }]})
        fields = [
            ("emby_url", "手动Emby/Jellyfin地址（关闭自动读取时使用）", "text", True),
            ("emby_api_key", "手动媒体服务器API Key（关闭自动读取时使用）", "password", True),
            ("emby_user_id", "手动媒体服务器用户ID（可留空）", "text", True),
            ("tmdb_token", "TMDB Key/Read Token（留空使用MoviePilot内置值）", "password", False),
            ("cron", "发送计划 Cron（5段）", "text", False),
            ("timezone", "时区", "text", False),
            ("calendar_days", "日历天数（1-31）", "number", False),
            ("tmdb_requests_per_second", "TMDB每秒请求数（0.2-20）", "number", False),
            ("tmdb_max_retries", "TMDB限流最大重试次数（0-10）", "number", False),
            ("cache_ttl_hours", "TMDB缓存有效小时数（1-720）", "number", False),
        ]
        for model, label, field_type, manual_only in fields:
            row_props = {"show": "{{!use_mp_config}}"} if manual_only else {}
            content.append({"component": "VRow", "props": row_props, "content": [{
                "component": "VCol", "props": {"cols": 12}, "content": [{
                    "component": "VTextField", "props": {
                        "model": model,
                        "label": label,
                        "type": field_type,
                        "clearable": model in {"emby_user_id", "tmdb_token"},
                    }
                }]
            }]})
        content.append({"component": "VRow", "content": [{
            "component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VSwitch", "props": {"model": "verify_https", "label": "校验HTTPS证书"}
            }]
        }]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "run_once": False,
            "notify_enabled": True,
            "use_mp_config": True,
            "media_server": "",
            "emby_url": "",
            "emby_api_key": "",
            "emby_user_id": "",
            "tmdb_token": "",
            "cron": "0 9 * * *",
            "timezone": "Asia/Shanghai",
            "calendar_days": 7,
            "tmdb_requests_per_second": 3,
            "tmdb_max_retries": 5,
            "cache_ttl_hours": 24,
            "verify_https": True,
        }

    def get_page(self) -> List[dict]:
        """返回最近100次运行历史。"""
        history = self.get_data("history") or []
        rows = [[
            item.get("time"), item.get("status"), item.get("series"), item.get("updates"),
            item.get("failed"), item.get("seconds"),
        ] for item in history[:100]]
        return [{"component": "VTable", "props": {"hover": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": title}
                for title in ("时间", "状态", "剧集数", "排期数", "失败数", "耗时秒")
            ]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [
                {"component": "td", "text": str(value if value is not None else "")}
                for value in row
            ]} for row in rows]},
        ]}]

    def get_api(self):
        """本插件不暴露外部HTTP API。"""
        return None

    def stop_service(self):
        """停止后台任务并阻止当前批次继续查询。"""
        self._enabled = False
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
