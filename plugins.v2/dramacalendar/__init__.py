from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.log import logger
from app.plugins import _PluginBase


class DramaCalendar(_PluginBase):
    """旧追剧更新日历的配置迁移入口。"""

    plugin_name = "追剧更新日历（已合并）"
    plugin_desc = "功能已合并到订阅助手；更新后自动迁移原配置并停用旧任务。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/drama-calendar.svg"
    plugin_version = "0.1.2"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "dramacalendar_"
    plugin_order = 33
    auth_level = 1

    _MAPPING = {
        "enabled": "calendar_enabled",
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
        legacy = dict(config or {})
        if not legacy:
            return
        try:
            target = dict(self.get_config("VarietySubscribeAssistant") or {})
            changed = False
            for old_key, new_key in self._MAPPING.items():
                if old_key in legacy and new_key not in target:
                    target[new_key] = legacy[old_key]
                    changed = True
            if changed:
                self.update_config(target, plugin_id="VarietySubscribeAssistant")
            if legacy.get("enabled"):
                legacy["enabled"] = False
                legacy["run_once"] = False
                self.update_config(legacy)
            logger.warning(
                "#追剧更新日历# 功能已合并到订阅助手，原配置已迁移，旧定时任务已停用"
            )
        except Exception as exc:
            logger.error(
                f"#追剧更新日历# 配置迁移失败 [{type(exc).__name__.upper()}]"
            )

    def get_state(self) -> bool:
        return False

    def get_service(self) -> List[dict]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "追剧更新通知已合并到“订阅助手”。更新订阅助手后，请在其配置页面管理排期通知和每日订阅汇总。",
                },
            }],
        }], {"enabled": False}

    def get_command(self):
        return None

    def get_api(self):
        return None

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        return None

