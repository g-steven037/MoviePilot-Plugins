from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load_variety_v3():
    app = types.ModuleType("app")
    app.__path__ = []
    sdk = types.ModuleType("app.sdk")
    sdk.__path__ = []
    sdk_config = types.ModuleType("app.sdk.config")
    sdk_config.settings = types.SimpleNamespace(SUPERUSER="admin")
    sdk_events = types.ModuleType("app.sdk.events")
    sdk_events.Event = object
    sdk_events.eventmanager = types.SimpleNamespace(
        register=lambda *_args, **_kwargs: (lambda fn: fn)
    )
    sdk_logging = types.ModuleType("app.sdk.logging")
    sdk_logging.logger = types.SimpleNamespace(info=lambda *_a, **_k: None,
                                               warning=lambda *_a, **_k: None,
                                               error=lambda *_a, **_k: None)
    sdk_media = types.ModuleType("app.sdk.media")
    sdk_media.WordsMatcher = type("WordsMatcher", (), {})
    sdk_media.MetaInfo = type("MetaInfo", (), {})
    sdk_utilities = types.ModuleType("app.sdk.utilities")
    sdk_utilities.SystemUtils = type("SystemUtils", (), {})
    db_oper = types.ModuleType("app.db.oper")
    db_oper.__path__ = []
    for name, cls_name in (
        ("subscribe", "SubscribeOper"),
        ("subscribehistory", "SubscribeHistoryOper"),
        ("downloadhistory", "DownloadHistoryOper"),
    ):
        module = types.ModuleType(f"app.db.oper.{name}")
        setattr(module, cls_name, type(cls_name, (), {}))
        sys.modules[module.__name__] = module
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = type("_PluginBase", (), {
        "update_config": lambda *_a, **_k: True,
        "post_message": lambda *_a, **_k: None,
    })
    schema_types = types.ModuleType("app.schemas.types")
    schema_types.EventType = types.SimpleNamespace(
        SubscribeAdded="subscribe.added",
        SubscribeModified="subscribe.modified",
        SubscribeDeleted="subscribe.deleted",
        PluginAction="plugin.action",
    )
    schema_types.MediaType = types.SimpleNamespace(TV="电视剧")
    schema_types.NotificationType = types.SimpleNamespace(Plugin="plugin")
    schemas = types.ModuleType("app.schemas")
    schemas.Response = lambda **kwargs: types.SimpleNamespace(**kwargs)
    schemas.NotificationType = schema_types.NotificationType
    aps_cron = types.ModuleType("apscheduler.triggers.cron")
    aps_cron.CronTrigger = type("CronTrigger", (), {
        "from_crontab": classmethod(lambda cls, _value: cls())
    })
    aps_background = types.ModuleType("apscheduler.schedulers.background")
    aps_background.BackgroundScheduler = type("BackgroundScheduler", (), {})
    for name, module in {
        "app": app,
        "app.sdk": sdk,
        "app.sdk.config": sdk_config,
        "app.sdk.events": sdk_events,
        "app.sdk.logging": sdk_logging,
        "app.sdk.media": sdk_media,
        "app.sdk.utilities": sdk_utilities,
        "app.db": types.ModuleType("app.db"),
        "app.db.oper": db_oper,
        "app.plugins": plugins,
        "app.schemas": schemas,
        "app.schemas.types": schema_types,
        "apscheduler": types.ModuleType("apscheduler"),
        "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
        "apscheduler.triggers.cron": aps_cron,
        "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
        "apscheduler.schedulers.background": aps_background,
    }.items():
        sys.modules[name] = module

    path = ROOT / "plugins.v3/varietysubscribeassistant/__init__.py"
    spec = importlib.util.spec_from_file_location("variety_v3_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_renamer_v3():
    sys.modules.pop("app.chain.media", None)
    sys.modules.pop("app.chain", None)
    sys.modules.pop("app.runtime.settings", None)
    sys.modules.pop("app.runtime", None)
    _load_variety_v3()
    path = ROOT / "plugins.v3/subscribelinkrenamer/__init__.py"
    spec = importlib.util.spec_from_file_location("renamer_v3_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v3_event_payload_accepts_model_dump_object():
    module = _load_variety_v3()

    class TypedEvent:
        def model_dump(self, **_kwargs):
            return {"subscribe_id": 7}

    assert module._event_payload(TypedEvent()) == {"subscribe_id": 7}


def test_v3_matching_does_not_use_bare_legacy_ids_when_media_pairs_differ():
    module = _load_variety_v3()
    download = types.SimpleNamespace(
        name="同名",
        year="2026",
        media_source="tmdb",
        media_id="100",
        tmdbid="100",
    )
    subscription = types.SimpleNamespace(
        name="其他",
        year="2026",
        media_source="douban",
        media_id="100",
        tmdbid="100",
    )
    assert module._match_subscription(download, [subscription]) is None


def test_v3_link_renamer_imports_with_sdk_contract():
    module = _load_renamer_v3()
    assert module.SubscribeLinkRenamer.plugin_version == "1.1.6"


def test_v3_native_recognition_is_filename_only_and_has_no_file_side_effects():
    module = _load_renamer_v3()
    calls = []

    class ParsedMeta:
        name = "斗罗大陆Ⅱ绝世唐门"
        season_episode = "S01 E160"

    def readonly_recognizer(title, **kwargs):
        assert isinstance(title, str)
        assert "custom_words" not in kwargs
        calls.append(title)
        return ParsedMeta()

    module.MetaInfo = readonly_recognizer
    plugin = module.SubscribeLinkRenamer()
    plugin._use_mp_recognition = True

    result = plugin._native_renamed_filename(
        "Soul.Land.S02E160.2023.2160p.WEB-DL.H265.AAC-ADWeb.mp4"
    )

    assert result == "斗罗大陆Ⅱ绝世唐门 S01E160.mp4"
    assert calls == ["Soul.Land.S02E160.2023.2160p.WEB-DL.H265.AAC-ADWeb.mp4"]


def test_v3_native_recognition_status_has_explicit_log_label():
    module = _load_renamer_v3()
    assert module.SubscribeLinkRenamer._rename_status_label("MP_NATIVE_RECOGNIZED", 0) == "MP原生识别"


def test_v3_complete_media_recognition_uses_moviepilot_title_without_file_io():
    module = _load_renamer_v3()
    chain_package = types.ModuleType("app.chain")
    chain_package.__path__ = []
    chain_media = types.ModuleType("app.chain.media")

    class FakeMediaChain:
        def recognize_by_meta(self, meta, **kwargs):
            assert kwargs["obtain_images"] is False
            return types.SimpleNamespace(
                title="与你相恋到生命尽头",
                media_source="tmdb",
                media_id="285574",
            )

    chain_media.MediaChain = FakeMediaChain
    sys.modules["app.chain"] = chain_package
    sys.modules["app.chain.media"] = chain_media

    class ParsedMeta:
        name = "Kimi Ga Shinu Made Koi Wo Shitai"
        season_episode = "S01 E11"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    plugin = module.SubscribeLinkRenamer()
    target, details = plugin._complete_media_renamed_filename(
        "Kimi.ga.Shinu.made.Koi.wo.Shitai.S01E11.2026.1080p.mp4"
    )

    assert target == "与你相恋到生命尽头 S01E11.mp4"
    assert details["recognized_title"] == "与你相恋到生命尽头"
    assert details["media_id"] == "285574"


def test_v3_complete_media_recognition_uses_moviepilot_standard_naming_template():
    module = _load_renamer_v3()
    chain_package = types.ModuleType("app.chain")
    chain_package.__path__ = []
    chain_media = types.ModuleType("app.chain.media")

    class FakeMediaChain:
        def recognize_by_meta(self, meta, **kwargs):
            assert kwargs["obtain_images"] is False
            return types.SimpleNamespace(
                title="与你相恋到生命尽头",
                media_source="tmdb",
                media_id="285574",
                type="电视剧",
            )

    chain_media.MediaChain = FakeMediaChain
    sys.modules["app.chain"] = chain_package
    sys.modules["app.chain.media"] = chain_media

    filemanager_package = types.ModuleType("app.modules")
    filemanager_package.__path__ = []
    filemanager_module_package = types.ModuleType("app.modules.filemanager")
    filemanager_module_package.__path__ = []
    filemanager_module = types.ModuleType("app.modules.filemanager.module")

    class FakeFileManagerModule:
        @staticmethod
        def recommend_name(meta, mediainfo, episodes_info=None):
            assert meta is not None
            assert mediainfo.title == "与你相恋到生命尽头"
            assert episodes_info is None
            return (
                "/media/与你相恋到生命尽头/Season 01/"
                "与你相恋到生命尽头 S01E11 2026 1080p Baha WEB-DL x264 AAC-ADWeb.mp4"
            )

    filemanager_module.FileManagerModule = FakeFileManagerModule
    sys.modules["app.modules"] = filemanager_package
    sys.modules["app.modules.filemanager"] = filemanager_module_package
    sys.modules["app.modules.filemanager.module"] = filemanager_module

    class ParsedMeta:
        name = "Kimi Ga Shinu Made Koi Wo Shitai"
        title = "Kimi.ga.Shinu.made.Koi.wo.Shitai.S01E11.2026.1080p.Baha.WEB-DL.x264.AAC-ADWeb.mp4"
        season_episode = "S01 E11"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    plugin = module.SubscribeLinkRenamer()
    target, details = plugin._complete_media_renamed_filename(ParsedMeta.title)

    assert target == (
        "与你相恋到生命尽头 S01E11 2026 1080p Baha WEB-DL x264 AAC-ADWeb.mp4"
    )
    assert details["naming_mode"] == "MOVIEPILOT_STANDARD"


def test_v3_complete_media_recognition_caches_same_filename():
    module = _load_renamer_v3()
    chain_package = types.ModuleType("app.chain")
    chain_package.__path__ = []
    chain_media = types.ModuleType("app.chain.media")
    calls = []

    class FakeMediaChain:
        def recognize_by_meta(self, meta, **kwargs):
            calls.append(meta)
            return types.SimpleNamespace(title="缓存标题", media_source="tmdb", media_id="1")

    chain_media.MediaChain = FakeMediaChain
    sys.modules["app.chain"] = chain_package
    sys.modules["app.chain.media"] = chain_media

    class ParsedMeta:
        name = "Cache Title"
        season_episode = "S01 E01"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    plugin = module.SubscribeLinkRenamer()
    filename = "Cache.Title.S01E01.mp4"
    first = plugin._complete_media_renamed_filename(filename)
    second = plugin._complete_media_renamed_filename(filename)

    assert first == second
    assert len(calls) == 1


def test_v3_recognition_cache_invalidates_when_moviepilot_rename_format_changes():
    module = _load_renamer_v3()
    chain_package = types.ModuleType("app.chain")
    chain_package.__path__ = []
    chain_media = types.ModuleType("app.chain.media")
    filemanager_package = types.ModuleType("app.modules")
    filemanager_package.__path__ = []
    filemanager_module_package = types.ModuleType("app.modules.filemanager")
    filemanager_module_package.__path__ = []
    filemanager_module = types.ModuleType("app.modules.filemanager.module")
    runtime_package = types.ModuleType("app.runtime")
    runtime_package.__path__ = []
    runtime_settings = types.ModuleType("app.runtime.settings")
    current_format = ["format-a"]
    recognize_calls = []

    class FakeMediaChain:
        def recognize_by_meta(self, meta, **kwargs):
            recognize_calls.append(meta)
            return types.SimpleNamespace(
                title="缓存标题",
                media_source="tmdb",
                media_id="1",
                type="电视剧",
            )

    class FakeFileManagerModule:
        @staticmethod
        def recommend_name(meta, mediainfo, episodes_info=None):
            return f"/media/{current_format[0]}.mp4"

    runtime_settings.get_runtime_setting = lambda key: (
        (lambda _media_type: current_format[0]) if key == "RENAME_FORMAT" else None
    )
    chain_media.MediaChain = FakeMediaChain
    filemanager_module.FileManagerModule = FakeFileManagerModule
    sys.modules["app.chain"] = chain_package
    sys.modules["app.chain.media"] = chain_media
    sys.modules["app.modules"] = filemanager_package
    sys.modules["app.modules.filemanager"] = filemanager_module_package
    sys.modules["app.modules.filemanager.module"] = filemanager_module
    sys.modules["app.runtime"] = runtime_package
    sys.modules["app.runtime.settings"] = runtime_settings

    class ParsedMeta:
        name = "Cache Title"
        title = "Cache.Title.S01E01.mp4"
        season_episode = "S01 E01"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    plugin = module.SubscribeLinkRenamer()
    first, _ = plugin._complete_media_renamed_filename(ParsedMeta.title)
    current_format[0] = "format-b"
    second, _ = plugin._complete_media_renamed_filename(ParsedMeta.title)

    assert first == "format-a.mp4"
    assert second == "format-b.mp4"
    assert len(recognize_calls) == 2


def test_v3_recognition_test_api_returns_read_only_preview():
    module = _load_renamer_v3()
    module.settings.API_TOKEN = "test-token"
    plugin = module.SubscribeLinkRenamer()

    class ParsedMeta:
        name = "Kimi Ga Shinu Made Koi Wo Shitai"
        season_episode = "S01 E11"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    response = plugin.test_recognition(
        filename="Kimi.ga.Shinu.made.Koi.wo.Shitai.S01E11.2026.1080p.mp4",
        apikey="test-token",
    )

    assert response.success is True
    assert response.data["target_filename"] == "Kimi Ga Shinu Made Koi Wo Shitai S01E11.mp4"
    assert response.data["file_operation"] == "none"


def test_v3_recognition_test_api_accepts_page_bearer_without_query_token():
    module = _load_renamer_v3()
    module.settings.API_TOKEN = "test-token"
    plugin = module.SubscribeLinkRenamer()

    class ParsedMeta:
        name = "示例"
        season_episode = "S01 E01"
        type = "电视剧"

    module.MetaInfo = lambda title: ParsedMeta()
    response = plugin.test_recognition(filename="Example.S01E01.mp4")
    assert response.success is True
    assert response.data["file_operation"] == "none"


def test_v3_recognition_test_api_does_not_fallback_to_saved_filename_for_explicit_empty_value():
    module = _load_renamer_v3()
    module.settings.API_TOKEN = "test-token"
    plugin = module.SubscribeLinkRenamer()
    plugin._test_filename = "Saved.Old.Title.S01E01.mp4"

    response = plugin.test_recognition(filename="", apikey="test-token")

    assert response.success is False
    assert response.message == "请输入文件名"


def test_v3_form_exposes_read_only_recognition_test_input():
    module = _load_renamer_v3()
    plugin = module.SubscribeLinkRenamer()
    form, defaults = plugin.get_form()
    serialized = repr(form)
    assert "test_filename" in serialized
    assert "只读识别测试" in serialized
    assert defaults["test_filename"] == ""


def test_v3_form_exposes_read_only_recognition_button():
    module = _load_renamer_v3()
    plugin = module.SubscribeLinkRenamer()
    form, _ = plugin.get_form()
    serialized = str(form)
    assert "VBtn" in serialized
    assert "开始只读测试" in serialized
    assert "window.MoviePilotAPI.get('plugin/SubscribeLinkRenamer/test_recognition'" in serialized
    assert "subscribelinkrenamer-test-filename" in serialized
    assert "input#subscribelinkrenamer-test-filename" in serialized
    assert "model.test_filename" not in serialized
    assert "href" not in serialized
