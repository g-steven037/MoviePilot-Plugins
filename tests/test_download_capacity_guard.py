from __future__ import annotations

import sys
import types
from pathlib import Path

import test_plugin_security as security


security._install_stubs()


class _EventManager:
    @staticmethod
    def register(**_kwargs):
        return lambda function: function


class _DownloadChain:
    tasks = []

    def list_torrents(self):
        return list(self.tasks)


class _ChainEventType:
    ResourceDownload = "resource.download"


app_chain = types.ModuleType("app.chain")
app_chain_download = types.ModuleType("app.chain.download")
app_chain_download.DownloadChain = _DownloadChain
app_core_event = types.ModuleType("app.core.event")
app_core_event.Event = object
app_core_event.eventmanager = _EventManager()
sys.modules["app.chain"] = app_chain
sys.modules["app.chain.download"] = app_chain_download
sys.modules["app.core.event"] = app_core_event
sys.modules["app.schemas"].ResourceDownloadEventData = object
sys.modules["app.schemas.types"].ChainEventType = _ChainEventType

PLUGIN_ROOT = Path(__file__).parents[1] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_ROOT))

import downloadcapacityguard as guard_module
from downloadcapacityguard import DownloadCapacityGuard, GIB


def _event(size: int, title: str = "movie.mkv"):
    torrent = types.SimpleNamespace(
        size=size, title=title, site=1, enclosure="https://tracker.invalid/torrent/1"
    )
    data = types.SimpleNamespace(
        cancel=False,
        source="",
        reason="",
        context=types.SimpleNamespace(torrent_info=torrent),
        options={},
    )
    return types.SimpleNamespace(event_data=data)


def _configured_plugin(tmp_path: Path) -> DownloadCapacityGuard:
    plugin = DownloadCapacityGuard()
    plugin._enabled = True
    plugin._notify_enabled = False
    plugin._reject_unknown_size = True
    plugin._monitor_path = tmp_path
    plugin._monitor_device = tmp_path.stat().st_dev
    plugin._reserve_bytes = 10 * GIB
    plugin._size_multiplier = 1.05
    plugin._reservation_seconds = 120
    plugin._reservations = {}
    return plugin


def test_guard_counts_active_remaining_and_concurrent_reservations(tmp_path: Path):
    plugin = _configured_plugin(tmp_path)
    original_disk_usage = guard_module.shutil.disk_usage
    plugin._active_remaining_bytes = lambda: 20 * GIB
    guard_module.shutil.disk_usage = lambda _path: types.SimpleNamespace(
        total=200 * GIB, used=100 * GIB, free=100 * GIB
    )
    try:
        first = _event(60 * GIB, "first.mkv")
        plugin.handle_resource_download(first)
        assert first.event_data.cancel is False
        assert plugin._reservations

        second = _event(10 * GIB, "second.mkv")
        plugin.handle_resource_download(second)
        assert second.event_data.cancel is True
        assert second.event_data.source == "下载容量控制"
        assert second.event_data.reason == "本地磁盘安全可用空间不足"
        assert plugin.get_data("history")[0]["code"] == "INSUFFICIENT_SPACE"
    finally:
        guard_module.shutil.disk_usage = original_disk_usage


def test_guard_rejects_unknown_size_and_calculates_remaining(tmp_path: Path):
    plugin = _configured_plugin(tmp_path)
    unknown = _event(0, "unknown.mkv")
    plugin.handle_resource_download(unknown)
    assert unknown.event_data.cancel is True
    assert "大小未知" in unknown.event_data.reason

    task = types.SimpleNamespace(size=80 * GIB, progress=25)
    assert plugin._task_remaining_bytes(task) == 60 * GIB
    assert plugin._task_remaining_bytes(types.SimpleNamespace(size=10, progress=100)) == 0
    for task in (
        types.SimpleNamespace(size=-1, progress=20),
        types.SimpleNamespace(size=0, progress=0),
        types.SimpleNamespace(size=float("nan"), progress=20),
    ):
        try:
            plugin._task_remaining_bytes(task)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown active task size was not rejected")

    remote = _event(500 * GIB, "remote.mkv")
    remote.event_data.options = {"save_path": "rclone:/downloads"}
    plugin.handle_resource_download(remote)
    assert remote.event_data.cancel is False


def test_guard_form_defaults_are_fail_closed():
    plugin = DownloadCapacityGuard()
    form, defaults = plugin.get_form()
    serialized = repr(form)
    assert defaults["enabled"] is False
    assert defaults["reject_unknown_size"] is True
    assert defaults["reserve_gb"] == 10
    assert defaults["size_multiplier_percent"] == 105
    assert defaults["reservation_seconds"] == 120
    assert "cron" not in defaults
    assert "Cron" not in serialized
    assert "不创建定时任务" in serialized
    assert "本地磁盘监控路径" in serialized
    assert plugin.get_service() == []
