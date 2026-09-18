from __future__ import annotations

import sys
import importlib.util
import types
from urllib.error import HTTPError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins.v3" / "p115rapidretry"))

import cms
from cms import CmsClient, cms_batch_due, cms_delay_remaining


def _load_plugin_class():
    app = types.ModuleType("app")
    app.__path__ = []
    sdk = types.ModuleType("app.sdk")
    sdk.__path__ = []
    config = types.ModuleType("app.sdk.config")
    config.settings = types.SimpleNamespace(SUPERUSER="admin")
    logging = types.ModuleType("app.sdk.logging")
    logging.logger = types.SimpleNamespace(info=lambda *_a, **_k: None,
                                           warning=lambda *_a, **_k: None,
                                           error=lambda *_a, **_k: None)
    plugins = types.ModuleType("app.plugins")
    plugins._PluginBase = type("_PluginBase", (), {})
    schemas = types.ModuleType("app.schemas")
    schemas.NotificationType = types.SimpleNamespace(Plugin="plugin")
    sys.modules.update({
        "app": app, "app.sdk": sdk, "app.sdk.config": config,
        "app.sdk.logging": logging, "app.plugins": plugins, "app.schemas": schemas,
    })
    path = Path(__file__).parents[1] / "plugins.v3" / "p115rapidretry" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "p115rapidretry_v3", path,
        submodule_search_locations=[str(path.parent)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.P115RapidRetry


def test_cms_url_encodes_token_and_selects_auto_organize():
    client = CmsClient("https://cms.example/", "a token&value", "auto_organize")
    assert client.request_url == (
        "https://cms.example/api/sync/lift_by_token?token=a+token%26value"
        "&type=auto_organize"
    )


def test_cms_sync_returns_success_and_does_not_expose_token():
    calls = []

    def opener(request, **kwargs):
        calls.append((request.full_url, kwargs))
        return type("Response", (), {
            "status": 200,
            "read": lambda self: b"ok",
            "__enter__": lambda self: self,
            "__exit__": lambda self, *_args: None,
        })()

    client = CmsClient("https://cms.example", "secret-token", opener=opener)
    assert client.sync() == (True, "HTTP_200")
    assert "secret-token" not in client.safe_description
    assert calls[0][1] == {"timeout": 30}


def test_cms_sync_failure_is_reported_without_raising():
    def opener(_request, **_kwargs):
        raise HTTPError("https://cms.example", 503, "busy", {}, None)

    client = CmsClient("https://cms.example", "token", opener=opener)
    assert client.sync() == (False, "HTTP_503")


def test_cms_client_keeps_a_safe_exception_type_for_diagnostics():
    class BrokenResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            raise ValueError("token=secret must not be logged")

    client = CmsClient(
        "https://cms.example",
        "secret-token",
        opener=lambda _request, **_kwargs: BrokenResponse(),
    )
    assert client.sync() == (False, "CLIENT_ERROR")
    assert client.last_error == "response_read:ValueError"
    assert "secret" not in client.last_error


def test_cms_client_reports_the_failed_response_stage():
    class BrokenResponse:
        status = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"ok"

    client = CmsClient(
        "https://cms.example",
        "token",
        opener=lambda _request, **_kwargs: BrokenResponse(),
    )
    assert client.sync() == (False, "CLIENT_ERROR")
    assert client.last_error == "response_status:TypeError"


def test_cms_batch_waits_for_quiet_delay():
    assert cms_batch_due([{"created_at": 100.0}], now=159.0, delay=60) is False
    assert cms_batch_due([{"created_at": 100.0}], now=160.0, delay=60) is True
    assert cms_batch_due([], now=999.0, delay=60) is False


def test_cms_delay_remaining_uses_the_latest_success():
    items = [{"created_at": 100.0}, {"created_at": 105.0}]
    assert cms_delay_remaining(items, now=110.0, delay=10) == 5
    assert cms_delay_remaining(items, now=115.0, delay=10) == 0


def test_cms_delay_cannot_be_shorter_than_original_plugin_quiet_window():
    normalise_cms_delay = getattr(cms, "normalise_cms_delay", None)
    assert callable(normalise_cms_delay)
    assert normalise_cms_delay(10) == 60
    assert normalise_cms_delay(60) == 60
    assert normalise_cms_delay(3600) == 3600


def test_cms_enqueue_uses_minute_service_instead_of_per_file_timer():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    store = {}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin._cms_enabled = True
    plugin._cms_client = object()
    plugin._target_pid = "123"
    scheduled = []
    plugin._schedule_cms_sync = lambda: scheduled.append(True)

    plugin._enqueue_cms_sync(Path("episode.mkv"), "ABC123")

    assert scheduled == []


def test_cms_is_registered_as_a_minute_cron_service():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    plugin._enabled = True
    plugin._cms_enabled = True
    plugin._cms_client = object()
    plugin._empty_cleanup_enabled = False
    plugin._cron = "*/5 * * * *"
    services = plugin.get_service()

    cms_services = [item for item in services if item["id"] == "P115RapidRetry_cms_sync"]
    assert len(cms_services) == 1
    assert cms_services[0]["func"] == plugin.cms_sync_pending
    assert cms_services[0]["trigger"] is not None


def test_success_queue_is_flushed_without_touching_115_retry_state():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    store = {}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin._cms_enabled = True
    plugin._cms_client = types.SimpleNamespace(sync=lambda: (True, "HTTP_200"))
    plugin._cms_delay_seconds = 0
    plugin._cms_mode = "auto_organize"
    plugin._target_pid = "123"
    plugin._schedule_cms_sync = lambda: None
    plugin._enqueue_cms_sync(Path("episode.mkv"), "ABC123")

    plugin.cms_sync_pending()

    assert store["cms_pending"] == []
    assert list(store["cms_completed"]) == ["ABC123:123:episode.mkv"]
    assert "retry_state" not in store


def test_form_exposes_cms_switch_and_safe_defaults():
    plugin_class = _load_plugin_class()
    _form, defaults = plugin_class().get_form()
    assert defaults["cms_enabled"] is False
    assert defaults["cms_mode"] == "auto_organize"
    assert defaults["cms_delay_seconds"] == 60
    assert "cms_enabled" in str(_form)
    assert "cms_api_token" in str(_form)


def test_form_uses_progressive_disclosure_for_long_configuration():
    plugin_class = _load_plugin_class()
    form, defaults = plugin_class().get_form()
    serialized = repr(form)

    assert "VExpansionPanels" in serialized
    for section in ("快速配置", "通知与 CMS", "普通重试策略", "限流与安全", "空文件夹清理", "日志与诊断"):
        assert section in serialized
    assert "'show': '{{cms_enabled}}'" in serialized
    assert "'show': '{{empty_cleanup_enabled}}'" in serialized
    assert "notify_enabled" not in serialized
    assert "detailed_logs" not in serialized
    assert "exhausted_cron" not in serialized
    # Compatibility keys remain in the model so existing installations are not reset.
    assert "run_action" in defaults
    assert "exhausted_cron" in defaults


def test_cms_failure_is_queued_for_independent_retry():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    store = {}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin._cms_enabled = True
    plugin._cms_client = types.SimpleNamespace(sync=lambda: (False, "HTTP_503"))
    plugin._cms_delay_seconds = 0
    plugin._cms_mode = "auto_organize"
    plugin._target_pid = "123"
    plugin._schedule_cms_sync = lambda: None
    plugin._enqueue_cms_sync(Path("episode.mkv"), "ABC123")

    plugin.cms_sync_pending()

    assert store["cms_pending"] == []
    assert "retry_state" not in store


def test_cms_status_is_bound_to_the_matching_history_item():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    store = {"history": [{"task": "task-1", "media": "九门", "episode": "S01E08", "status": "成功"}]}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin._cms_enabled = True
    plugin._cms_client = types.SimpleNamespace()
    plugin._target_pid = "123"
    plugin._schedule_cms_sync = lambda: None

    plugin._enqueue_cms_sync(Path("episode.mkv"), "ABC123", task_id="task-1")

    item = store["history"][0]
    assert item["cms_status"] == "等待CMS"
    assert item["cms_key"] == "ABC123:123:episode.mkv"


def test_cms_result_updates_each_bound_history_item():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    store = {"history": [{"task": "task-1", "media": "九门", "episode": "S01E08", "status": "成功"}]}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    plugin._cms_enabled = True
    plugin._cms_client = types.SimpleNamespace(sync=lambda: (True, "HTTP_200"), last_error="", safe_description="CMS")
    plugin._cms_delay_seconds = 0
    plugin._cms_mode = "auto_organize"
    plugin._target_pid = "123"
    plugin._schedule_cms_sync = lambda: None
    plugin._enqueue_cms_sync(Path("episode.mkv"), "ABC123", task_id="task-1")

    plugin.cms_sync_pending()

    item = store["history"][0]
    assert item["cms_status"] == "已提交CMS"
    assert item["cms_code"] == "HTTP_200"
    assert item["cms_time"]


def test_page_shows_per_item_cms_status_and_mobile_cards():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    plugin._enabled = True
    plugin._cms_enabled = True
    plugin._max_retries = 10
    plugin.get_data = lambda key: {
        "history": [{
            "task": "task-1", "media": "九门", "episode": "S01E08",
            "attempts": 2, "status": "成功", "time": "2026-09-17 18:20:05",
            "cms_status": "已提交CMS", "cms_time": "2026-09-17 18:20:06",
        }],
    }.get(key)

    page = plugin.get_page()

    assert "CMS状态" in repr(page)
    assert "已提交CMS" in repr(page)
    assert any(item.get("props", {}).get("class") == "d-sm-none" for item in page)


def test_cms_service_is_separate_from_retry_and_cleanup_services():
    plugin_class = _load_plugin_class()
    plugin = plugin_class.__new__(plugin_class)
    plugin._enabled = True
    plugin._cms_enabled = True
    plugin._cms_client = object()
    plugin._empty_cleanup_enabled = False
    services = plugin.get_service()
    assert any(item["id"] == "P115RapidRetry_cms_sync" for item in services)
    assert all(item["id"] != "P115RapidRetry_exhausted_retry" for item in services)


def _make_exhausted_plugin(plugin_class, retry_dir, state, now=1000.0):
    plugin = plugin_class.__new__(plugin_class)
    plugin._enabled = True
    plugin._client = object()
    plugin._auth_blocked = False
    plugin._circuit_until = 0.0
    plugin._retry_dir = retry_dir
    plugin._max_batch = 10
    plugin._max_retries = 10
    plugin._delete_exhausted_enabled = False
    plugin._detailed_logs = False
    plugin._operation_lock = __import__("threading").Lock()
    plugin._schedule_exhausted_retry = lambda: None
    store = {"retry_state": state}
    plugin.get_data = lambda key: store.get(key)
    plugin.save_data = lambda key, value: store.__setitem__(key, value)
    return plugin, store


def test_exhausted_retry_waits_for_each_file_hourly_due_time(tmp_path, monkeypatch):
    plugin_class = _load_plugin_class()
    retry_dir = tmp_path / "retry"
    retry_dir.mkdir()
    path = retry_dir / "episode.mkv"
    path.write_bytes(b"episode")
    task_id = plugin_class._task_id(path, retry_dir)
    plugin, store = _make_exhausted_plugin(
        plugin_class,
        retry_dir.resolve(),
        {task_id: {
            "attempts": 10,
            "code": "RAPID_MISS",
            "exhausted": True,
            "phase": "hourly",
            "hourly_attempts": 0,
            "next_retry_at": 2000.0,
        }},
        now=1000.0,
    )
    handled = []
    plugin._handle = lambda *args, **kwargs: handled.append(args[0])
    module = sys.modules[plugin_class.__module__]
    monkeypatch.setattr(module, "time", lambda: 1000.0)

    plugin.retry_exhausted_pending()

    assert handled == []
    assert store["retry_state"][task_id]["hourly_attempts"] == 0


def test_hourly_cycle_is_not_taken_by_normal_retry_worker(tmp_path):
    plugin_class = _load_plugin_class()
    retry_dir = tmp_path / "retry"
    retry_dir.mkdir()
    path = retry_dir / "episode.mkv"
    path.write_bytes(b"episode")
    task_id = plugin_class._task_id(path, retry_dir)
    plugin, _store = _make_exhausted_plugin(
        plugin_class,
        retry_dir.resolve(),
        {task_id: {
            "attempts": 10,
            "code": "RAPID_MISS",
            "exhausted": True,
            "phase": "hourly",
            "hourly_attempts": 1,
            "next_retry_at": 0.0,
        }},
    )
    handled = []
    plugin._handle = lambda *args, **kwargs: handled.append(args[0])

    plugin.retry_pending()

    assert handled == []


def test_hourly_retry_keeps_separate_counter_and_schedules_next_due(tmp_path, monkeypatch):
    plugin_class = _load_plugin_class()
    retry_dir = tmp_path / "retry"
    retry_dir.mkdir()
    path = retry_dir / "episode.mkv"
    path.write_bytes(b"episode")
    task_id = plugin_class._task_id(path, retry_dir)
    plugin, store = _make_exhausted_plugin(
        plugin_class,
        retry_dir.resolve(),
        {task_id: {
            "attempts": 10,
            "code": "RAPID_MISS",
            "exhausted": True,
            "phase": "hourly",
            "hourly_attempts": 2,
            "next_retry_at": 1000.0,
        }},
    )
    module = sys.modules[plugin_class.__module__]
    monkeypatch.setattr(module, "time", lambda: 1000.0)
    plugin._handle = lambda *args, **kwargs: plugin._schedule_hourly_retry(task_id, "RAPID_MISS")

    plugin.retry_exhausted_pending()

    state = store["retry_state"][task_id]
    assert state["attempts"] == 10
    assert state["hourly_attempts"] == 3
    assert state["phase"] == "hourly"
    assert state["next_retry_at"] == 4600.0
