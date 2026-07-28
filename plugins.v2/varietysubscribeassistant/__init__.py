from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

from .calendar import DramaCalendar as _DramaCalendar


WEEKDAYS = "一二三四五六日"
ACTIVE_STATE_NAMES = {
    "N": "新建",
    "R": "订阅中",
    "P": "待定",
    "S": "暂停",
}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _record_name(record: Any) -> str:
    return str(getattr(record, "name", "") or "未命名订阅").strip()


def _record_type(record: Any) -> str:
    value = getattr(record, "type", "")
    return str(getattr(value, "value", value) or "").strip()


def _record_label(record: Any) -> str:
    name = _record_name(record)
    year = str(getattr(record, "year", "") or "").strip()
    season = _integer(getattr(record, "season", 0), 0)
    if _record_type(record) == "电视剧" and season > 0:
        return f"{name} S{season:02}"
    if year:
        return f"{name} ({year})"
    return name


def _active_line(record: Any) -> str:
    label = _record_label(record)
    state = ACTIVE_STATE_NAMES.get(
        str(getattr(record, "state", "") or "").upper(),
        str(getattr(record, "state", "") or "订阅中"),
    )
    if _record_type(record) != "电视剧":
        return f"{label}｜{state}"
    total = max(_integer(getattr(record, "total_episode", 0), 0), 0)
    lack = max(_integer(getattr(record, "lack_episode", 0), 0), 0)
    if total <= 0:
        return f"{label}｜{state}｜总集数待刷新"
    completed = min(max(total - lack, 0), total)
    return f"{label}｜{completed}/{total}集｜缺失{lack}集｜{state}"


def format_subscription_summary(
    now: datetime,
    unfinished: Sequence[Any],
    completed_today: Sequence[Any],
    scope: str = "all",
    max_items: int = 80,
) -> str:
    """生成每日订阅状态汇总。"""
    if scope not in {"all", "unfinished", "completed"}:
        raise ValueError("SUMMARY_SCOPE_INVALID")
    max_items = min(max(_integer(max_items, 80), 1), 200)
    active = sorted(unfinished, key=lambda item: (_record_type(item), _record_name(item), _integer(getattr(item, "season", 0))))
    completed = sorted(
        completed_today,
        key=lambda item: str(getattr(item, "date", "") or ""),
        reverse=True,
    )
    lines = [
        f"📺 每日订阅汇总｜{now.strftime('%m月%d日')} 周{WEEKDAYS[now.weekday()]}",
        "",
    ]
    remaining = max_items
    if scope in {"all", "unfinished"}:
        lines.append("🔴 未完成订阅")
        selected = active[:remaining]
        lines.extend(_active_line(item) for item in selected)
        remaining -= len(selected)
        if len(active) > len(selected):
            lines.append(f"……另有 {len(active) - len(selected)} 个未完成订阅")
        if not active:
            lines.append("暂无")
        lines.append("")
    if scope in {"all", "completed"}:
        lines.append("🟢 今日已完成")
        selected = completed[:remaining]
        lines.extend(_record_label(item) for item in selected)
        if len(completed) > len(selected):
            lines.append(f"……另有 {len(completed) - len(selected)} 个今日完成订阅")
        if not completed:
            lines.append("暂无")
        lines.append("")
    lines.append(
        f"共 {len(active) + len(completed)} 个 · "
        f"未完成 {len(active)} · 今日完成 {len(completed)}"
    )
    return "\n".join(lines)


class VarietySubscribeAssistant(_PluginBase):
    """订阅规则、追剧排期与每日订阅汇总的统一助手。"""

    plugin_name = "订阅助手"
    plugin_desc = "为新增订阅应用类型、关键词和规则组策略，并按Cron发送追剧排期及订阅状态汇总。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/subscribe-assistant.svg"
    plugin_version = "0.2.1"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "varietysubscribeassistant_"
    plugin_order = 34
    auth_level = 1

    _enabled = False
    _rule_enabled = True
    _media_type = "电视剧"
    _media_category = "综艺"
    _include = "正片"
    _exclude = ""
    _filter_groups: List[str] = ["日常观影"]
    _calendar_enabled = False
    _summary_enabled = False
    _summary_scope = "all"
    _summary_cron = "0 9 * * *"
    _summary_max_items = 80
    _summary_lock = threading.Lock()
    _calendar: Optional[_DramaCalendar] = None

    _CALENDAR_KEYS = {
        "notify_enabled": "calendar_notify_enabled",
        "notification_scope": "calendar_notification_scope",
        "use_mp_config": "calendar_use_mp_config",
        "media_server": "calendar_media_server",
        "emby_url": "calendar_emby_url",
        "emby_api_key": "calendar_emby_api_key",
        "emby_user_id": "calendar_emby_user_id",
        "tmdb_token": "calendar_tmdb_token",
        "cron": "calendar_cron",
        "timezone": "calendar_timezone",
        "calendar_days": "calendar_days",
        "tmdb_requests_per_second": "calendar_tmdb_requests_per_second",
        "tmdb_max_retries": "calendar_tmdb_max_retries",
        "cache_ttl_hours": "calendar_cache_ttl_hours",
        "verify_https": "calendar_verify_https",
    }

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = self._with_legacy_calendar_config(dict(config or {}))
        self._enabled = bool(config.get("enabled", False))
        self._rule_enabled = bool(config.get("rule_enabled", True))
        self._media_type = self._normalize_media_type(config.get("media_type", "电视剧"))
        self._media_category = self._clean_value(
            config.get("media_category", config.get("category", "综艺")), "", 64
        )
        self._include = self._clean_value(
            config.get("include") if "include" in config else "正片", "", 256
        )
        self._exclude = self._clean_value(config.get("exclude"), "", 256)
        self._filter_groups = self._normalize_groups(
            config.get("filter_groups", config.get("filter_group", "日常观影"))
        )
        self._calendar_enabled = bool(config.get("calendar_enabled", False))
        self._summary_enabled = bool(config.get("summary_enabled", False))
        self._summary_scope = str(config.get("summary_scope", "all") or "all").strip()
        if self._summary_scope not in {"all", "unfinished", "completed"}:
            self._summary_scope = "all"
            logger.warning("#订阅助手# 无效的订阅汇总范围，已回退为全部 [SUMMARY_SCOPE_INVALID]")
        self._summary_cron = str(config.get("summary_cron", "0 9 * * *") or "").strip()
        self._summary_max_items = min(
            max(_integer(config.get("summary_max_items", 80), 80), 1), 200
        )

        if self._enabled and self._summary_enabled:
            try:
                CronTrigger.from_crontab(self._summary_cron)
            except Exception:
                self._summary_enabled = False
                logger.error("#订阅助手# 每日订阅汇总Cron无效，汇总任务未启动 [CRON_INVALID]")

        self._calendar = _DramaCalendar()
        calendar_config = self._calendar_config(config)
        try:
            self._calendar.init_plugin(calendar_config)
        except Exception as exc:
            self._calendar_enabled = False
            logger.error(
                f"#订阅助手# 追剧排期模块初始化失败 [{type(exc).__name__.upper()}]"
            )

        if not self._enabled:
            return
        logger.info(
            f"#订阅助手# 已启用 | 规则={'开' if self._rule_enabled else '关'} | "
            f"类型={self._media_type} | 类别={self._media_category or '全部'} | "
            f"包含={self._include or '无'} | 排除={self._exclude or '无'} | "
            f"规则组={','.join(self._filter_groups) or '无'}"
        )
        self._handle_run_once(config)

    def _with_legacy_calendar_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        if any(key.startswith("calendar_") for key in config):
            return config
        try:
            legacy = self.get_config("DramaCalendar") or {}
        except Exception:
            legacy = {}
        if not isinstance(legacy, dict) or not legacy:
            return config
        changed = False
        if "calendar_enabled" not in config:
            config["calendar_enabled"] = bool(legacy.get("enabled", False))
            changed = True
        for old_key, new_key in self._CALENDAR_KEYS.items():
            if old_key in legacy and new_key not in config:
                config[new_key] = legacy[old_key]
                changed = True
        if changed:
            try:
                self.update_config(config)
            except Exception:
                logger.warning("#订阅助手# 原追剧更新配置已读取，但持久化迁移失败 [MIGRATION_SAVE_FAILED]")
            logger.info("#订阅助手# 已读取原追剧更新日历配置")
        return config

    def _calendar_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "enabled": self._enabled and self._calendar_enabled,
            "run_once": False,
        }
        defaults = {
            "notify_enabled": True,
            "notification_scope": "all",
            "use_mp_config": True,
            "media_server": "",
            "emby_url": "",
            "emby_api_key": "",
            "emby_user_id": "",
            "tmdb_token": "",
            "cron": "0 8 * * *",
            "timezone": "Asia/Shanghai",
            "calendar_days": 7,
            "tmdb_requests_per_second": 3,
            "tmdb_max_retries": 5,
            "cache_ttl_hours": 24,
            "verify_https": True,
        }
        for child_key, parent_key in self._CALENDAR_KEYS.items():
            payload[child_key] = config.get(parent_key, defaults[child_key])
        return payload

    def _handle_run_once(self, config: Dict[str, Any]) -> None:
        calendar_once = bool(config.get("calendar_run_once", False))
        summary_once = bool(config.get("summary_run_once", False))
        if calendar_once or summary_once:
            updated = dict(config)
            updated["calendar_run_once"] = False
            updated["summary_run_once"] = False
            try:
                self.update_config(updated)
            except Exception:
                logger.warning("#订阅助手# 一次性运行开关复位失败 [CONFIG_RESET_FAILED]")
        if calendar_once and self._calendar_enabled and self._calendar:
            threading.Thread(
                target=self._calendar.generate_calendar,
                kwargs={"source": "立即运行"},
                name="subscribe-assistant-calendar",
                daemon=True,
            ).start()
        if summary_once:
            threading.Thread(
                target=self.send_subscription_summary,
                kwargs={"source": "立即运行"},
                name="subscribe-assistant-summary",
                daemon=True,
            ).start()

    @staticmethod
    def _clean_value(value: Any, default: str, max_length: int) -> str:
        text = str(value or "").strip()
        return (text or default)[:max_length]

    @staticmethod
    def _normalize_media_type(value: Any) -> str:
        text = str(getattr(value, "value", value) or "").strip()
        aliases = {
            "all": "全部",
            "movie": "电影",
            "tv": "电视剧",
            "series": "电视剧",
        }
        text = aliases.get(text.casefold(), text)
        return text if text in {"全部", "电影", "电视剧"} else "电视剧"

    @staticmethod
    def _normalize_groups(value: Any) -> List[str]:
        values: Iterable[Any]
        if isinstance(value, (list, tuple, set)):
            values = value
        elif value is None:
            values = []
        else:
            values = str(value).replace("，", ",").split(",")
        result: List[str] = []
        for item in values:
            name = str(item or "").strip()[:64]
            if name and name not in result:
                result.append(name)
        return result[:20]

    @staticmethod
    def _event_category(event_data: Dict[str, Any], subscribe: Any) -> str:
        mediainfo = event_data.get("mediainfo") or {}
        if isinstance(mediainfo, dict):
            category = mediainfo.get("category") or mediainfo.get("media_category")
            if category:
                return str(category).strip()
        else:
            category = getattr(mediainfo, "category", None)
            if category:
                return str(category).strip()
        return str(getattr(subscribe, "media_category", "") or "").strip()

    @staticmethod
    def _event_media_type(event_data: Dict[str, Any], subscribe: Any) -> str:
        mediainfo = event_data.get("mediainfo") or {}
        if isinstance(mediainfo, dict):
            value = mediainfo.get("type") or mediainfo.get("media_type")
        else:
            value = getattr(mediainfo, "type", None)
        value = value or getattr(subscribe, "type", "")
        return str(getattr(value, "value", value) or "").strip()

    def _matches_policy(self, event_data: Dict[str, Any], subscribe: Any) -> bool:
        media_type = self._event_media_type(event_data, subscribe)
        if self._media_type != "全部" and media_type != self._media_type:
            return False
        category = self._event_category(event_data, subscribe)
        if self._media_category and category.casefold() != self._media_category.casefold():
            return False
        return True

    @eventmanager.register(EventType.SubscribeAdded)
    def apply_variety_policy(self, event: Event):
        """兼容旧方法名，为符合范围的新增订阅应用统一规则。"""
        if (
            not self._enabled
            or not self._rule_enabled
            or not event
            or not isinstance(event.event_data, dict)
        ):
            return
        raw_sid = event.event_data.get("subscribe_id")
        try:
            sid = int(raw_sid)
        except (TypeError, ValueError):
            logger.warning("#订阅助手# 新增订阅事件缺少有效ID，已跳过 [INVALID_SUBSCRIBE_ID]")
            return
        try:
            oper = SubscribeOper()
            subscribe = oper.get(sid)
            if not subscribe:
                logger.warning(f"#订阅助手# 未找到新增订阅，已跳过 | 订阅ID={sid}")
                return
            if not self._matches_policy(event.event_data, subscribe):
                return
            payload = {
                "include": self._include,
                "exclude": self._exclude,
                "filter_groups": list(self._filter_groups),
            }
            unchanged = all(
                (
                    list(getattr(subscribe, key, None) or [])
                    if key == "filter_groups"
                    else str(getattr(subscribe, key, "") or "")
                )
                == value
                for key, value in payload.items()
            )
            if unchanged:
                logger.info(
                    f"#订阅助手# 新增订阅规则无需调整 | 订阅ID={sid} | "
                    f"名称={self._safe_name(getattr(subscribe, 'name', ''))}"
                )
                return
            if not oper.update(sid, payload):
                logger.error(f"#订阅助手# 订阅规则写入失败 | 订阅ID={sid} [UPDATE_FAILED]")
                return
            logger.info(
                f"#订阅助手# 已应用新增订阅规则 | 订阅ID={sid} | "
                f"名称={self._safe_name(getattr(subscribe, 'name', ''))} | "
                f"类型={self._event_media_type(event.event_data, subscribe)} | "
                f"类别={self._event_category(event.event_data, subscribe) or '未分类'} | "
                f"包含={self._include or '无'} | 排除={self._exclude or '无'} | "
                f"规则组={','.join(self._filter_groups) or '无'}"
            )
        except Exception as exc:
            logger.error(
                f"#订阅助手# 处理新增订阅失败 | 订阅ID={sid} "
                f"[{type(exc).__name__.upper()}]"
            )

    @staticmethod
    def _safe_name(value: Any) -> str:
        return "".join(
            "?" if ord(char) < 32 or ord(char) == 127 else char
            for char in str(value or "")
        )[:200]

    @staticmethod
    def _is_today(record: Any, now: datetime) -> bool:
        text = str(getattr(record, "date", "") or "").strip()
        return text.startswith(now.strftime("%Y-%m-%d"))

    def _completed_today(self, now: datetime) -> List[Any]:
        try:
            from app.db.models.subscribehistory import SubscribeHistory

            oper = SubscribeOper()
            db = getattr(oper, "_db", None)
            if db is None:
                raise RuntimeError("SUBSCRIBE_DB_UNAVAILABLE")
            result: List[Any] = []
            for media_type in ("电视剧", "电影"):
                for page in range(1, 6):
                    batch = SubscribeHistory.list_by_type(
                        db, mtype=media_type, page=page, count=200
                    ) or []
                    result.extend(item for item in batch if self._is_today(item, now))
                    if len(batch) < 200:
                        break
                    oldest = str(getattr(batch[-1], "date", "") or "")
                    if oldest and oldest[:10] < now.strftime("%Y-%m-%d"):
                        break
            return result
        except Exception as exc:
            logger.warning(
                f"#订阅助手# 读取今日完成订阅失败 [{type(exc).__name__.upper()}]"
            )
            return []

    def send_subscription_summary(self, source: str = "Cron"):
        if not self._enabled or not self._summary_enabled:
            return
        if not self._summary_lock.acquire(blocking=False):
            logger.info("#订阅助手# 每日订阅汇总仍在运行，本次跳过")
            return
        try:
            now = datetime.now()
            unfinished = SubscribeOper().list() or []
            completed = self._completed_today(now)
            message = format_subscription_summary(
                now,
                unfinished,
                completed,
                self._summary_scope,
                self._summary_max_items,
            )
            self.post_message(
                mtype=NotificationType.Plugin,
                title="每日订阅汇总",
                text=message,
                username=settings.SUPERUSER,
            )
            logger.info(
                f"#订阅助手# 每日订阅汇总已提交 | 来源={self._safe_name(source)} | "
                f"未完成={len(unfinished)} | 今日完成={len(completed)} | "
                f"范围={self._summary_scope}"
            )
        except Exception as exc:
            logger.error(
                f"#订阅助手# 每日订阅汇总失败 [{type(exc).__name__.upper()}]"
            )
        finally:
            self._summary_lock.release()

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[dict]:
        if not self._enabled:
            return []
        services: List[dict] = []
        if self._calendar_enabled and self._calendar:
            try:
                for service in self._calendar.get_service() or []:
                    item = dict(service)
                    item["id"] = "VarietySubscribeAssistant_calendar"
                    item["name"] = f"订阅助手·追剧排期（{self._calendar._cron}）"
                    services.append(item)
            except Exception:
                logger.error("#订阅助手# 追剧排期服务创建失败 [CALENDAR_SERVICE_FAILED]")
        if self._summary_enabled:
            services.append({
                "id": "VarietySubscribeAssistant_summary",
                "name": f"订阅助手·每日订阅汇总（{self._summary_cron}）",
                "trigger": CronTrigger.from_crontab(self._summary_cron),
                "func": self.send_subscription_summary,
                "kwargs": {"source": "Cron"},
            })
        return services

    def _rule_group_items(self) -> List[Dict[str, str]]:
        names: List[str] = []
        try:
            from app.helper.rule import RuleHelper

            names = [
                str(getattr(item, "name", "") or "").strip()
                for item in RuleHelper.get_rule_groups()
            ]
        except Exception:
            try:
                from app.db.systemconfig_oper import SystemConfigOper
                from app.schemas.types import SystemConfigKey

                raw_groups = SystemConfigOper().get(SystemConfigKey.UserFilterRuleGroups) or []
                names = [
                    str(item.get("name", "") or "").strip()
                    for item in raw_groups
                    if isinstance(item, dict)
                ]
            except Exception:
                names = []
        for name in self._filter_groups:
            if name and name not in names:
                names.append(name)
        return [{"title": name, "value": name} for name in names if name]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        rule_items = self._rule_group_items()
        try:
            server_items = _DramaCalendar._moviepilot_media_items()
        except Exception:
            server_items = []
        content: List[dict] = [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": "新增订阅规则只作用于插件启用后创建的订阅。追剧排期由原“追剧更新日历”合并而来；每日订阅汇总直接读取MoviePilot订阅和今日完成历史。",
            },
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "rule_enabled", "label": "新增订阅规则"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "calendar_enabled", "label": "追剧排期通知"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "summary_enabled", "label": "每日订阅汇总"}
                }]},
            ],
        }, {
            "component": "VDivider",
            "props": {"class": "my-3"},
        }, {
            "component": "VAlert",
            "props": {"type": "success", "variant": "tonal", "text": "新增订阅规则"},
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSelect",
                    "props": {
                        "model": "media_type",
                        "label": "媒体类型",
                        "items": [
                            {"title": "全部", "value": "全部"},
                            {"title": "电视剧", "value": "电视剧"},
                            {"title": "电影", "value": "电影"},
                        ],
                        "clearable": False,
                    },
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {
                        "model": "media_category",
                        "label": "媒体类别（留空表示全部）",
                        "placeholder": "例如：综艺、国产剧、动漫",
                        "clearable": True,
                    },
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSelect",
                    "props": {
                        "model": "filter_groups",
                        "label": "过滤规则组",
                        "items": rule_items,
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
                    },
                }]},
            ],
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VTextField",
                    "props": {
                        "model": "include",
                        "label": "包含关键词（支持MoviePilot正则）",
                        "clearable": True,
                    },
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VTextField",
                    "props": {
                        "model": "exclude",
                        "label": "排除关键词（支持MoviePilot正则）",
                        "clearable": True,
                    },
                }]},
            ],
        }, {
            "component": "VDivider",
            "props": {"class": "my-3"},
        }, {
            "component": "VAlert",
            "props": {"type": "info", "variant": "tonal", "text": "追剧排期通知"},
        }, {
            "component": "VRow",
            "props": {"show": "{{calendar_enabled}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "calendar_notify_enabled", "label": "发送插件通知"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "calendar_run_once", "label": "立即生成一次"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "calendar_use_mp_config", "label": "读取MP媒体服务器"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "calendar_verify_https", "label": "校验HTTPS证书"}
                }]},
            ],
        }, {
            "component": "VRow",
            "props": {"show": "{{calendar_enabled}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_cron", "label": "追剧排期 Cron（5段）"},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_timezone", "label": "时区"},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_days", "label": "排期天数（1-31）", "type": "number"},
                }]},
            ],
        }, {
            "component": "VRow",
            "props": {"show": "{{calendar_enabled}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VSelect",
                    "props": {
                        "model": "calendar_notification_scope",
                        "label": "排期通知范围",
                        "items": [
                            {"title": "全部排期", "value": "all"},
                            {"title": "仅已入库", "value": "in_library"},
                            {"title": "仅未入库", "value": "missing"},
                        ],
                        "clearable": False,
                    },
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VSelect",
                    "props": {
                        "model": "calendar_media_server",
                        "label": "MoviePilot媒体服务器",
                        "items": server_items,
                        "clearable": True,
                    },
                }]},
            ],
        }, {
            "component": "VRow",
            "props": {"show": "{{calendar_enabled && !calendar_use_mp_config}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_emby_url", "label": "Emby/Jellyfin地址"},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_emby_api_key", "label": "API Key", "type": "password"},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "calendar_emby_user_id", "label": "用户ID（可留空）", "clearable": True},
                }]},
            ],
        }, {
            "component": "VRow",
            "props": {"show": "{{calendar_enabled}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [{
                    "component": "VTextField",
                    "props": {
                        "model": "calendar_tmdb_token",
                        "label": "TMDB Key/Read Token（留空使用MoviePilot内置值）",
                        "type": "password",
                        "clearable": True,
                    },
                }]},
            ],
        }, {
            "component": "VDivider",
            "props": {"class": "my-3"},
        }, {
            "component": "VAlert",
            "props": {"type": "warning", "variant": "tonal", "text": "每日订阅状态汇总"},
        }, {
            "component": "VRow",
            "props": {"show": "{{summary_enabled}}"},
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSwitch", "props": {"model": "summary_run_once", "label": "立即发送一次"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "summary_cron", "label": "汇总通知 Cron（5段）"},
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VSelect",
                    "props": {
                        "model": "summary_scope",
                        "label": "订阅通知范围",
                        "items": [
                            {"title": "全部订阅", "value": "all"},
                            {"title": "仅未完成订阅", "value": "unfinished"},
                            {"title": "仅今日已完成", "value": "completed"},
                        ],
                        "clearable": False,
                    },
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{
                    "component": "VTextField",
                    "props": {"model": "summary_max_items", "label": "最多列出条数（1-200）", "type": "number"},
                }]},
            ],
        }]
        return [{"component": "VForm", "content": content}], {
            "enabled": False,
            "rule_enabled": True,
            "media_type": "电视剧",
            "media_category": "综艺",
            "include": "正片",
            "exclude": "",
            "filter_groups": ["日常观影"],
            "calendar_enabled": False,
            "calendar_notify_enabled": True,
            "calendar_run_once": False,
            "calendar_notification_scope": "all",
            "calendar_use_mp_config": True,
            "calendar_media_server": "",
            "calendar_emby_url": "",
            "calendar_emby_api_key": "",
            "calendar_emby_user_id": "",
            "calendar_tmdb_token": "",
            "calendar_cron": "0 8 * * *",
            "calendar_timezone": "Asia/Shanghai",
            "calendar_days": 7,
            "calendar_tmdb_requests_per_second": 3,
            "calendar_tmdb_max_retries": 5,
            "calendar_cache_ttl_hours": 24,
            "calendar_verify_https": True,
            "summary_enabled": False,
            "summary_run_once": False,
            "summary_scope": "all",
            "summary_cron": "0 9 * * *",
            "summary_max_items": 80,
        }

    def get_command(self):
        return None

    def get_api(self):
        return None

    def get_page(self) -> List[dict]:
        calendar_page = self._calendar.get_page() if self._calendar else []
        return calendar_page or []

    def stop_service(self):
        if self._calendar:
            try:
                self._calendar.stop_service()
            except Exception:
                pass
        self._calendar = None
        self._enabled = False

