from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

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


def _episode_numbers(record: Any) -> List[int]:
    numbers = set()
    for value in getattr(record, "note", None) or []:
        episode = _integer(value, 0)
        if episode > 0:
            numbers.add(episode)
    for value, priority in (getattr(record, "episode_priority", None) or {}).items():
        episode = _integer(value, 0)
        try:
            downloaded = float(priority) > 0
        except (TypeError, ValueError, OverflowError):
            downloaded = False
        if episode > 0 and downloaded:
            numbers.add(episode)
    if numbers:
        return sorted(numbers)

    start = max(_integer(getattr(record, "start_episode", 1), 1), 1)
    total = max(_integer(getattr(record, "total_episode", 0), 0), 0)
    lack = max(_integer(getattr(record, "lack_episode", 0), 0), 0)
    end = total if not hasattr(record, "lack_episode") else max(total - lack, 0)
    if end >= start:
        return list(range(start, end + 1))
    return []


def _episode_range(numbers: Sequence[int]) -> str:
    values = sorted({number for number in numbers if number > 0})
    if not values:
        return ""
    ranges: List[Tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append((start, previous))
        start = previous = value
    ranges.append((start, previous))
    return ",".join(
        f"E{start:02}" if start == end else f"E{start:02}-E{end:02}"
        for start, end in ranges
    )


def _record_line(record: Any) -> str:
    name = _record_name(record)
    year = str(getattr(record, "year", "") or "").strip()
    season = max(_integer(getattr(record, "season", 1), 1), 0)
    episode_text = _episode_range(_episode_numbers(record))
    year_text = f" ({year})" if year else ""
    season_text = f" S{season:02}" if season > 0 else ""
    return f"📺︎{name}{year_text}{season_text}{episode_text}"


def _is_updated_today(record: Any, now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    for field in ("last_update", "date"):
        if str(getattr(record, field, "") or "").strip().startswith(today):
            return True
    return False


def normalize_summary_scopes(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        raw = value
    elif value is None:
        raw = []
    else:
        raw = str(value).replace("，", ",").split(",")
    aliases = {
        "unfinished": "not_updated",
        "completed": "updated",
        "未更新": "not_updated",
        "已更新": "updated",
        "全部": "all",
    }
    scopes = []
    for item in raw:
        scope = aliases.get(str(item or "").strip(), str(item or "").strip())
        if scope in {"all", "not_updated", "updated"} and scope not in scopes:
            scopes.append(scope)
    if not scopes or "all" in scopes:
        return ["all"]
    return scopes


def format_subscription_summary(
    now: datetime,
    active: Sequence[Any],
    completed_today: Sequence[Any],
    scopes: Any = ("all",),
    max_items: int = 80,
) -> str:
    """按更新状态生成电视剧订阅汇总。"""
    selected_scopes = normalize_summary_scopes(scopes)
    max_items = min(max(_integer(max_items, 80), 1), 200)
    records = [item for item in [*active, *completed_today] if _record_type(item) == "电视剧"]
    deduplicated: Dict[Tuple[str, str, int], Any] = {}
    for item in records:
        key = (
            _record_name(item),
            str(getattr(item, "year", "") or ""),
            _integer(getattr(item, "season", 0), 0),
        )
        current = deduplicated.get(key)
        if current is None or _is_updated_today(item, now):
            deduplicated[key] = item
    updated = sorted(
        [item for item in deduplicated.values() if _is_updated_today(item, now)],
        key=lambda item: (_record_name(item), _integer(getattr(item, "season", 0), 0)),
    )
    not_updated = sorted(
        [item for item in deduplicated.values() if not _is_updated_today(item, now)],
        key=lambda item: (_record_name(item), _integer(getattr(item, "season", 0), 0)),
    )
    include_updated = "all" in selected_scopes or "updated" in selected_scopes
    include_not_updated = "all" in selected_scopes or "not_updated" in selected_scopes
    lines: List[str] = []
    remaining = max_items
    if include_updated:
        lines.append("**电视剧更新**")
        selected = updated[:remaining]
        lines.extend(_record_line(item) for item in selected)
        remaining -= len(selected)
        if len(updated) > len(selected):
            lines.append(f"……另有 {len(updated) - len(selected)} 部")
        if not updated:
            lines.append("暂无")
    if include_not_updated:
        if lines:
            lines.append("")
        lines.append("**电视剧未更新**")
        selected = not_updated[:remaining]
        lines.extend(_record_line(item) for item in selected)
        if len(not_updated) > len(selected):
            lines.append(f"……另有 {len(not_updated) - len(selected)} 部")
        if not not_updated:
            lines.append("暂无")
    return "\n".join(lines)


class VarietySubscribeAssistant(_PluginBase):
    """新增订阅规则与每日电视剧更新汇总。"""

    plugin_name = "订阅助手"
    plugin_desc = "为新增订阅应用类型、关键词和规则组策略，并按Cron发送电视剧更新汇总。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/subscribe-assistant.svg"
    plugin_version = "0.3.0"
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
    _summary_enabled = False
    _summary_scopes: List[str] = ["all"]
    _summary_cron = "0 9 * * *"
    _summary_max_items = 80
    _summary_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
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
        self._summary_enabled = bool(config.get("summary_enabled", False))
        self._summary_scopes = normalize_summary_scopes(
            config.get("summary_scopes", config.get("summary_scope", ["all"]))
        )
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

        if not self._enabled:
            return
        logger.info(
            f"#订阅助手# 已启用 | 规则={'开' if self._rule_enabled else '关'} | "
            f"类型={self._media_type} | 类别={self._media_category or '全部'} | "
            f"包含={self._include or '无'} | 排除={self._exclude or '无'} | "
            f"规则组={','.join(self._filter_groups) or '无'}"
        )
        self._handle_run_once(config)

    def _handle_run_once(self, config: Dict[str, Any]) -> None:
        summary_once = bool(config.get("summary_run_once", False))
        if summary_once:
            updated = dict(config)
            updated["summary_run_once"] = False
            try:
                self.update_config(updated)
            except Exception:
                logger.warning("#订阅助手# 一次性运行开关复位失败 [CONFIG_RESET_FAILED]")
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
            for page in range(1, 6):
                batch = SubscribeHistory.list_by_type(
                    db, mtype="电视剧", page=page, count=200
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
            active = [
                item for item in (SubscribeOper().list() or [])
                if _record_type(item) == "电视剧"
            ]
            completed = self._completed_today(now)
            message = format_subscription_summary(
                now,
                active,
                completed,
                self._summary_scopes,
                self._summary_max_items,
            )
            self.post_message(
                mtype=NotificationType.Plugin,
                title="电视剧更新",
                text=message,
                username=settings.SUPERUSER,
            )
            logger.info(
                f"#订阅助手# 每日订阅汇总已提交 | 来源={self._safe_name(source)} | "
                f"活动订阅={len(active)} | 今日完成={len(completed)} | "
                f"范围={','.join(self._summary_scopes)}"
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
        content: List[dict] = [{
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": "新增订阅规则只作用于插件启用后创建的订阅。电视剧更新汇总直接读取MoviePilot订阅和今日完成历史。",
            },
        }, {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSwitch", "props": {"model": "rule_enabled", "label": "新增订阅规则"}
                }]},
                {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
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
            "props": {"type": "warning", "variant": "tonal", "text": "电视剧更新汇总"},
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
                        "model": "summary_scopes",
                        "label": "订阅通知范围",
                        "items": [
                            {"title": "全部", "value": "all"},
                            {"title": "未更新", "value": "not_updated"},
                            {"title": "已更新", "value": "updated"},
                        ],
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
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
            "summary_enabled": False,
            "summary_run_once": False,
            "summary_scopes": ["all"],
            "summary_cron": "0 9 * * *",
            "summary_max_items": 80,
        }

    def get_command(self):
        return None

    def get_api(self):
        return None

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        self._enabled = False

