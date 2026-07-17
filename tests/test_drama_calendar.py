from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import test_plugin_security as security


security._install_stubs()


PLUGIN_ROOT = Path(__file__).parents[1] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_ROOT))

from dramacalendar import DramaCalendar, format_calendar
from dramacalendar.cache import ShowCache
from dramacalendar.client import EpisodeUpdate


def test_calendar_formats_multiple_days_and_episode_ranges():
    now = datetime(2026, 7, 17, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    updates = [
        EpisodeUpdate(now.date(), "测试剧", 1, 9, "a", True),
        EpisodeUpdate(now.date(), "测试剧", 1, 10, "a", True),
        EpisodeUpdate(now.date() + timedelta(days=1), "明日剧", 2, 3, "b", False),
    ]
    message = format_calendar(now, updates, 7)
    assert "未来7天剧集更新" in message
    assert "🟢 测试剧 S01E9-10" in message
    assert "🟢 测试剧" in message
    assert "🔴 明日剧 S02E3" in message
    assert "明天" in message


def test_cache_persists_and_prunes_safely(tmp_path: Path):
    path = tmp_path / "cache" / "calendar.db"
    first = ShowCache(path, 24)
    first.set("show:1", {"id": 1, "name": "测试剧"})
    first.close()
    second = ShowCache(path, 24)
    assert second.get("show:1") == {"id": 1, "name": "测试剧"}
    assert second.prune() >= 0
    second.close()


def test_moviepilot_media_config_is_resolved_without_logging_secrets():
    conf = types.SimpleNamespace(
        type=types.SimpleNamespace(value="emby"),
        config={"host": "https://emby.internal:8096", "apikey": "secret-key"},
    )
    service = types.SimpleNamespace(
        config=conf,
        instance=types.SimpleNamespace(user="user-1"),
    )
    helper = types.SimpleNamespace(
        get_configs=lambda: {"主Emby": conf},
        get_services=lambda: {"主Emby": service},
    )
    host, key, user, name = DramaCalendar._resolve_moviepilot_media(helper)
    assert (host, key, user, name) == (
        "https://emby.internal:8096", "secret-key", "user-1", "主Emby"
    )
    assert "secret-key" not in DramaCalendar._safe_code(ValueError("SECRET_INVALID"))


def test_form_uses_moviepilot_notification_defaults_without_bot_commands():
    plugin = DramaCalendar()
    form, defaults = plugin.get_form()
    serialized = repr(form)
    assert defaults["enabled"] is False
    assert defaults["notify_enabled"] is True
    assert defaults["use_mp_config"] is True
    assert defaults["cron"] == "0 9 * * *"
    assert defaults["calendar_days"] == 7
    assert "password" in serialized
    assert "TELEGRAM_BOT_TOKEN" not in serialized
    assert "/calendar" not in serialized
    assert "/today" not in serialized
    assert not hasattr(plugin, "get_command")
