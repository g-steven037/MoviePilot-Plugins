from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path

import test_plugin_security as security


security._install_stubs()


class _EventManager:
    @staticmethod
    def register(*_args, **_kwargs):
        return lambda function: function


class _EventType:
    SubscribeAdded = "subscribe.added"
    SubscribeModified = "subscribe.modified"
    SubscribeDeleted = "subscribe.deleted"
    PluginAction = "plugin.action"


class _WordsMatcher:
    def prepare(self, title, custom_words=None):
        applied = []
        for word in custom_words or []:
            if " => " not in word:
                continue
            source, target = word.split(" => ", 1)
            title, count = re.subn(source, target, title)
            if count:
                applied.append(word)
        return title, applied


class _SubscribeOper:
    records = []
    update_calls = []

    def list(self):
        return list(self.records)

    def get(self, sid):
        return next((record for record in self.records if record.id == sid), None)

    def update(self, sid, payload):
        record = self.get(sid)
        if not record:
            return None
        self.update_calls.append((sid, dict(payload)))
        for key, value in payload.items():
            setattr(record, key, value)
        return record


class _SystemUtils:
    @staticmethod
    def is_windows():
        return os.name == "nt"

    @staticmethod
    def list_files(root, _patterns):
        return [path for path in Path(root).rglob("*") if path.is_file()]

    @staticmethod
    def link(source, target):
        try:
            os.link(source, target)
            return 0, ""
        except OSError as exc:
            return 1, type(exc).__name__

    @staticmethod
    def copy(source, target):
        Path(target).write_bytes(Path(source).read_bytes())
        return 0, ""


class _Change:
    added = 1
    modified = 2


class _BackgroundScheduler:
    def __init__(self, **_kwargs):
        self.running = False

    def add_job(self, **_kwargs):
        pass

    def start(self):
        self.running = True

    def remove_all_jobs(self):
        pass

    def shutdown(self, **_kwargs):
        self.running = False


app_core_event = types.ModuleType("app.core.event")
app_core_event.Event = object
app_core_event.eventmanager = _EventManager()
app_core_meta = types.ModuleType("app.core.meta")
app_core_meta_words = types.ModuleType("app.core.meta.words")
app_core_meta_words.WordsMatcher = _WordsMatcher
app_db = types.ModuleType("app.db")
app_db_subscribe = types.ModuleType("app.db.subscribe_oper")
app_db_subscribe.SubscribeOper = _SubscribeOper
app_utils = types.ModuleType("app.utils")
app_utils_system = types.ModuleType("app.utils.system")
app_utils_system.SystemUtils = _SystemUtils
sys.modules["app.core.event"] = app_core_event
sys.modules["app.core.meta"] = app_core_meta
sys.modules["app.core.meta.words"] = app_core_meta_words
sys.modules["app.db"] = app_db
sys.modules["app.db.subscribe_oper"] = app_db_subscribe
sys.modules["app.utils"] = app_utils
sys.modules["app.utils.system"] = app_utils_system
sys.modules["app.schemas.types"].EventType = _EventType
sys.modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Manual="manual")
sys.modules["app.schemas"].NotificationType = types.SimpleNamespace(Manual="manual")
sys.modules["app.schemas"].Response = lambda **kwargs: types.SimpleNamespace(**kwargs)
settings = sys.modules["app.core.config"].settings
settings.DOWNLOAD_TMPEXT = [".!qB", ".part"]
settings.TZ = "Asia/Shanghai"
settings.API_TOKEN = "test-token"

apscheduler_background = types.ModuleType("apscheduler.schedulers.background")
apscheduler_background.BackgroundScheduler = _BackgroundScheduler
sys.modules["apscheduler.schedulers"] = types.ModuleType("apscheduler.schedulers")
sys.modules["apscheduler.schedulers.background"] = apscheduler_background

watchfiles = types.ModuleType("watchfiles")
watchfiles.Change = _Change
watchfiles.watch = lambda *_args, **_kwargs: []
sys.modules["watchfiles"] = watchfiles

PLUGIN_ROOT = Path(__file__).parents[1] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_ROOT))

from subscribelinkrenamer import SubscribeLinkRenamer, _is_download_tmp_file
from varietysubscribeassistant import VarietySubscribeAssistant


def test_plugin_is_visible_without_site_authentication():
    assert SubscribeLinkRenamer.plugin_name == "识别词硬链接"
    assert SubscribeLinkRenamer.plugin_version == "0.3.1"
    assert SubscribeLinkRenamer.auth_level == 1
    assert VarietySubscribeAssistant.plugin_name == "订阅助手"
    assert VarietySubscribeAssistant.plugin_version == "0.1.0"
    assert VarietySubscribeAssistant.plugin_config_prefix == "varietysubscribeassistant_"
    assert VarietySubscribeAssistant.auth_level == 1


def _subscription(sid, words, **kwargs):
    values = {
        "id": sid,
        "name": f"订阅{sid}",
        "custom_words": words,
        "media_category": "",
        "include": "",
        "filter_groups": [],
    }
    values.update(kwargs)
    return types.SimpleNamespace(**values)


def test_variety_subscription_gets_strict_main_feature_policy():
    plugin = VarietySubscribeAssistant()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscription(21, "", name="食神·百厨大战", media_category="综艺")
    _SubscribeOper.records = [subscribe]
    _SubscribeOper.update_calls = []

    plugin.apply_variety_policy(types.SimpleNamespace(event_data={
        "subscribe_id": 21,
        "mediainfo": {"category": "综艺"},
    }))

    assert subscribe.include == "正片"
    assert subscribe.filter_groups == ["日常观影"]
    assert _SubscribeOper.update_calls == [(21, {
        "include": "正片",
        "filter_groups": ["日常观影"],
    })]


def test_variety_subscription_policy_does_not_touch_other_categories():
    plugin = VarietySubscribeAssistant()
    plugin.init_plugin({"enabled": True})
    subscribe = _subscription(
        22, "", name="普通剧集", media_category="电视剧",
        include="保留规则", filter_groups=["原规则组"],
    )
    _SubscribeOper.records = [subscribe]
    _SubscribeOper.update_calls = []

    plugin.apply_variety_policy(types.SimpleNamespace(event_data={
        "subscribe_id": 22,
        "mediainfo": {"category": "电视剧"},
    }))

    assert subscribe.include == "保留规则"
    assert subscribe.filter_groups == ["原规则组"]
    assert _SubscribeOper.update_calls == []


def test_subscription_words_rename_unique_match_and_keep_original_without_match():
    plugin = SubscribeLinkRenamer()
    _SubscribeOper.records = [
        _subscription(7, r"Game[ .]+of[ .]+Flame[ .]+S01 => 食神·百厨大战 S02"),
        _subscription(8, r"Soul[ .]+Land[ .]+S02 => 斗罗大陆Ⅱ绝世唐门 S01"),
    ]
    plugin._subscription_words = None
    renamed, sid, status = plugin._renamed_filename("Game.of.Flame.S01E04.mkv")
    assert renamed == "食神·百厨大战 S02E04.mkv"
    assert sid == 7 and status == "CUSTOM_WORD_APPLIED"
    original, sid, status = plugin._renamed_filename("Unknown.S01E01.mkv")
    assert original == "Unknown.S01E01.mkv"
    assert sid == 0 and status == "NO_CUSTOM_WORD_MATCH"


def test_ambiguous_subscription_words_keep_original_name():
    plugin = SubscribeLinkRenamer()
    _SubscribeOper.records = [
        _subscription(1, r"Show => 第一名称"),
        _subscription(2, r"Show => 第二名称"),
    ]
    plugin._subscription_words = None
    renamed, sid, status = plugin._renamed_filename("Show.S01E01.mkv")
    assert renamed == "Show.S01E01.mkv"
    assert sid == 0 and status == "AMBIGUOUS_CUSTOM_WORDS"


def test_subscription_events_only_invalidate_cache_and_never_write_database():
    plugin = SubscribeLinkRenamer()
    plugin._enabled = True
    plugin._subscription_words = [(1, ["A => B"])]
    _SubscribeOper.update_calls = []
    plugin.invalidate_subscription_words(types.SimpleNamespace(event_data={"subscribe_id": 1}))
    assert plugin._subscription_words is None
    assert _SubscribeOper.update_calls == []


def test_link_file_uses_subscription_rename_and_preserves_source(tmp_path: Path):
    source_root = tmp_path / "pt"
    target_root = tmp_path / "links"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "Game.of.Flame.S01E04.mkv"
    source.write_bytes(b"video")
    plugin = SubscribeLinkRenamer()
    _SubscribeOper.records = [_subscription(7, r"Game[ .]+of[ .]+Flame[ .]+S01 => 食神·百厨大战 S02")]
    plugin._subscription_words = None
    state, _, destination, rename_status, sid = plugin._link_file(
        source, str(source_root), target_root, "link"
    )
    assert state and rename_status == "CUSTOM_WORD_APPLIED" and sid == 7
    assert destination.name == "食神·百厨大战 S02E04.mkv"
    assert source.exists() and source.name == "Game.of.Flame.S01E04.mkv"
    assert source.stat().st_ino == destination.stat().st_ino


def test_link_file_without_custom_words_uses_original_relative_path(tmp_path: Path):
    source_root = tmp_path / "pt"
    target_root = tmp_path / "links"
    nested = source_root / "Season 01"
    nested.mkdir(parents=True)
    target_root.mkdir()
    source = nested / "Unknown.S01E01.mkv"
    source.write_bytes(b"video")
    plugin = SubscribeLinkRenamer()
    _SubscribeOper.records = []
    plugin._subscription_words = None
    state, _, destination, rename_status, sid = plugin._link_file(
        source, str(source_root), target_root, "link"
    )
    assert state and rename_status == "NO_CUSTOM_WORD_MATCH" and sid == 0
    assert destination == target_root / "Season 01" / source.name


def test_download_temp_extensions_are_skipped():
    assert _is_download_tmp_file(Path("episode.mkv.!qB"))
    assert _is_download_tmp_file(Path("episode.part"))
    assert not _is_download_tmp_file(Path("episode.mkv"))
