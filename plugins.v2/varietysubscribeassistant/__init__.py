from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class VarietySubscribeAssistant(_PluginBase):
    """为新增的综艺订阅自动应用严格的正片筛选策略。"""

    plugin_name = "订阅助手"
    plugin_desc = "识别新增综艺订阅，自动要求资源包含正片，并应用指定过滤规则组。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/subscribe-assistant.svg"
    plugin_version = "0.1.0"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "varietysubscribeassistant_"
    plugin_order = 34
    auth_level = 1

    _enabled = False
    _category = "综艺"
    _include = "正片"
    _filter_group = "日常观影"

    def init_plugin(self, config: dict = None):
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._category = self._clean_value(config.get("category"), "综艺", 32)
        self._include = self._clean_value(config.get("include"), "正片", 128)
        self._filter_group = self._clean_value(config.get("filter_group"), "日常观影", 64)
        if self._enabled:
            logger.info(
                f"#订阅助手# 已启用 | 媒体类别={self._category} | "
                f"必须包含={self._include} | 过滤规则组={self._filter_group}"
            )

    @staticmethod
    def _clean_value(value: Any, default: str, max_length: int) -> str:
        text = str(value or "").strip()
        if not text:
            return default
        return text[:max_length]

    @staticmethod
    def _event_category(event_data: Dict[str, Any], subscribe: Any) -> str:
        mediainfo = event_data.get("mediainfo") or {}
        if isinstance(mediainfo, dict):
            category = mediainfo.get("category") or mediainfo.get("media_category")
            if category:
                return str(category).strip()
        return str(getattr(subscribe, "media_category", "") or "").strip()

    @eventmanager.register(EventType.SubscribeAdded)
    def apply_variety_policy(self, event: Event):
        if not self._enabled or not event or not isinstance(event.event_data, dict):
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
            category = self._event_category(event.event_data, subscribe)
            if category.casefold() != self._category.casefold():
                return

            payload = {
                "include": self._include,
                "filter_groups": [self._filter_group],
            }
            current_groups = list(getattr(subscribe, "filter_groups", None) or [])
            if getattr(subscribe, "include", None) == self._include \
                    and current_groups == payload["filter_groups"]:
                logger.info(
                    f"#订阅助手# 综艺订阅规则无需调整 | 订阅ID={sid} | "
                    f"名称={self._safe_name(getattr(subscribe, 'name', ''))}"
                )
                return

            updated = oper.update(sid, payload)
            if not updated:
                logger.error(f"#订阅助手# 综艺订阅规则写入失败 | 订阅ID={sid} [UPDATE_FAILED]")
                return
            logger.info(
                f"#订阅助手# 已应用综艺订阅规则 | 订阅ID={sid} | "
                f"名称={self._safe_name(getattr(subscribe, 'name', ''))} | "
                f"必须包含={self._include} | 过滤规则组={self._filter_group}"
            )
        except Exception as exc:
            logger.error(
                f"#订阅助手# 处理新增订阅失败 | 订阅ID={sid} "
                f"[{type(exc).__name__.upper()}]"
            )

    @staticmethod
    def _safe_name(value: Any) -> str:
        return "".join("?" if ord(char) < 32 or ord(char) == 127 else char for char in str(value or ""))[:200]

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "插件启用"},
                    }]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField",
                        "props": {"model": "category", "label": "识别媒体类别"},
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField",
                        "props": {"model": "include", "label": "订阅必须包含"},
                    }]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField",
                        "props": {"model": "filter_group", "label": "过滤规则组"},
                    }]},
                ]},
                {"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [{
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "仅处理启用插件后新增、且媒体类别为综艺的订阅。默认强制包含“正片”，并将过滤规则组设为“日常观影”；其他媒体类型和已有订阅不受影响。",
                        },
                    }]},
                ]},
            ],
        }], {
            "enabled": False,
            "category": "综艺",
            "include": "正片",
            "filter_group": "日常观影",
        }

    def get_service(self) -> List[dict]:
        return []

    def get_command(self):
        return None

    def get_api(self):
        return None

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        self._enabled = False
