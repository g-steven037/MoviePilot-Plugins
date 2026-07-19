import os
import queue
import sys
import threading
import time
import types
from pathlib import Path


class _Logger:
    def info(self, *_args, **_kwargs): pass
    def warning(self, *_args, **_kwargs): pass
    def error(self, *_args, **_kwargs): pass


class _PluginBase:
    def __init__(self):
        self._test_data = {}
        self._test_messages = []

    def get_data(self, key):
        return self._test_data.get(key)

    def save_data(self, key, value):
        self._test_data[key] = value

    def post_message(self, **kwargs):
        self._test_messages.append(kwargs)


class _CronTrigger:
    def __init__(self, **_kwargs):
        pass

    @classmethod
    def from_crontab(cls, _value):
        return cls()


class _IntervalTrigger:
    def __init__(self, **_kwargs):
        pass


def _install_stubs():
    app = types.ModuleType("app")
    app_core = types.ModuleType("app.core")
    app_core_config = types.ModuleType("app.core.config")
    app_core_config.settings = types.SimpleNamespace(SUPERUSER="admin", SECRET_KEY="unit-test-secret")
    app_log = types.ModuleType("app.log")
    app_log.logger = _Logger()
    app_plugins = types.ModuleType("app.plugins")
    app_plugins._PluginBase = _PluginBase
    app_schemas = types.ModuleType("app.schemas")
    app_schema_types = types.ModuleType("app.schemas.types")

    class _NotificationType:
        Plugin = "plugin"

    app_schema_types.NotificationType = _NotificationType
    p115 = types.ModuleType("p115client")
    p115.P115Client = object
    asynctools = types.ModuleType("asynctools")
    asynctools.__version__ = (0, 2, 2)
    asynctools.ensure_async = lambda function, **_kwargs: function
    asynctools.async_collect = object()
    asynctools.async_chain_from_iterable = object()
    apscheduler = types.ModuleType("apscheduler")
    triggers = types.ModuleType("apscheduler.triggers")
    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = _CronTrigger
    interval = types.ModuleType("apscheduler.triggers.interval")
    interval.IntervalTrigger = _IntervalTrigger
    sys.modules.update({
        "app": app, "app.core": app_core, "app.core.config": app_core_config,
        "app.log": app_log, "app.plugins": app_plugins,
        "app.schemas": app_schemas, "app.schemas.types": app_schema_types,
        "p115client": p115, "asynctools": asynctools, "apscheduler": apscheduler,
        "apscheduler.triggers": triggers, "apscheduler.triggers.cron": cron,
        "apscheduler.triggers.interval": interval,
    })


class FakeClient:
    def __init__(self, reuse=False):
        self.reuse = reuse
        self.calls = 0

    def upload_file_init(self, **_kwargs):
        self.calls += 1
        return {"state": True, "reuse": self.reuse}


def test_cookie_validation_rejects_unsafe_values():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    assert P115RapidRetry._validate_cookie("UID=1; CID=2; SEID=3") == "UID=1; CID=2; SEID=3"
    for unsafe in ("", "UID=1", "UID=1; SEID=3\r\nInjected=1", "UID=1; broken; SEID=3"):
        try:
            P115RapidRetry._validate_cookie(unsafe)
        except ValueError as exc:
            assert str(exc) == "COOKIE_INVALID"
        else:
            raise AssertionError("unsafe Cookie was accepted")


def test_client_constructor_uses_current_signature():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    import p115rapidretry as plugin_module

    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    original = plugin_module.P115Client
    plugin_module.P115Client = Client
    try:
        result = plugin_module.P115RapidRetry._create_client("UID=1; SEID=2")
        assert isinstance(result, Client)
        assert calls == [(("UID=1; SEID=2",), {})]
    finally:
        plugin_module.P115Client = original


def test_detailed_audit_log_is_default_and_sanitized():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    import p115rapidretry as plugin_module
    from p115rapidretry.rapid import RapidResult

    messages = []

    class Logger:
        def info(self, message):
            messages.append(message)

    original = plugin_module.logger
    plugin_module.logger = Logger()
    try:
        plugin = plugin_module.P115RapidRetry()
        result = RapidResult(True, False, "RAPID_SUCCESS", sha1="A" * 40)
        plugin._audit_rapid(Path("unsafe\nname.mkv"), Path("/watch"), result, False)
        assert len(messages) == 1
        assert "unsafe?name.mkv" in messages[0]
        assert "文件夹=." in messages[0]
        assert "SHA1=" + "A" * 40 in messages[0]
        assert "SHA1服务端匹配=是" in messages[0]
        assert "秒传=成功" in messages[0]
        assert "\n" not in messages[0]
    finally:
        plugin_module.logger = original


def test_retry_limit_and_bot_notifications_are_bounded():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    plugin = P115RapidRetry()
    plugin._max_retries = 2
    plugin._initialize_retry("task", "RAPID_MISS")
    state = plugin.get_data("retry_state")["task"]
    assert state["attempts"] == 0
    assert state["exhausted"] is False
    assert plugin._schedule_retry("task", "RAPID_MISS") == (1, False)
    assert plugin._schedule_retry("task", "RAPID_MISS") == (2, True)
    state = plugin.get_data("retry_state")["task"]
    assert state["exhausted"] is True

    path = Path("private-folder") / "movie.mkv"
    plugin._notify_enabled = False
    plugin._send_bot_success(path, False)
    assert not plugin._test_messages
    plugin._notify_enabled = True
    plugin._send_bot_success(path, False)
    plugin._send_bot_success(path, True, retry_attempts=2)
    plugin._send_bot_exhausted(path, 2, "RAPID_MISS")
    plugin._send_bot_exhausted(path, 2, "RAPID_MISS", deleted=True, delete_requested=True)
    assert len(plugin._test_messages) == 4
    assert all(message.get("username") == "admin" for message in plugin._test_messages)
    assert "重试次数：0" in plugin._test_messages[0]["text"]
    assert "重试次数：2" in plugin._test_messages[1]["text"]
    assert "临时目录重试" not in plugin._test_messages[1]["text"]
    assert "失败文件已安全删除" in plugin._test_messages[-1]["text"]
    serialized = repr(plugin._test_messages)
    assert "movie.mkv" in serialized
    assert "private-folder" not in serialized
    assert "SHA1" not in serialized
    assert "Cookie" not in serialized


def test_risk_control_persists_and_cookie_change_releases_auth_block():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry
    from p115rapidretry.rapid import RapidResult

    cookie = "UID=12345678; SEID=abcdefgh"
    plugin = P115RapidRetry()
    plugin._notify_enabled = False
    plugin._hourly_request_limit = 2
    plugin._min_request_interval = 5
    plugin._consecutive_failure_limit = 2
    plugin._failure_cooldown_minutes = 10
    plugin._load_risk_control(cookie)
    plugin._auth_blocked = True
    plugin._save_risk_control()

    resumed = P115RapidRetry()
    resumed._test_data = plugin._test_data
    resumed._notify_enabled = False
    resumed._hourly_request_limit = 2
    resumed._min_request_interval = 5
    resumed._consecutive_failure_limit = 2
    resumed._failure_cooldown_minutes = 10
    resumed._load_risk_control(cookie)
    assert resumed._auth_blocked is True
    resumed._load_risk_control("UID=87654321; SEID=hgfedcba")
    assert resumed._auth_blocked is False

    quota = P115RapidRetry()
    quota._notify_enabled = False
    quota._hourly_request_limit = 2
    quota._min_request_interval = 5
    quota._cookie_tag = "test"
    quota._request_times = [time.time() - 20, time.time() - 10]
    quota._last_request_at = time.time() - 10
    assert quota._acquire_request_slot() is False
    assert quota._circuit_reason == "HOURLY_LIMIT"
    assert quota.get_data("risk_control")["circuit_until"] > int(time.time())

    failures = P115RapidRetry()
    failures._notify_enabled = False
    failures._cookie_tag = "test"
    failures._consecutive_failure_limit = 2
    failures._failure_cooldown_minutes = 10
    failures._apply_risk_result(RapidResult(False, True, "NETWORK_ERROR"))
    failures._apply_risk_result(RapidResult(False, True, "INVALID_RESPONSE"))
    assert failures._circuit_reason == "CONSECUTIVE_FAILURES"
    assert failures._circuit_until > time.time()


def test_brief_log_contains_only_required_fields(tmp_path: Path):
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    import p115rapidretry as plugin_module
    from p115rapidretry.rapid import RapidResult, secure_identity

    messages = []

    class Logger:
        def info(self, message): messages.append(message)
        def warning(self, message): messages.append(message)

    original_logger = plugin_module.logger
    plugin_module.logger = Logger()
    try:
        plugin = plugin_module.P115RapidRetry()
        plugin._detailed_logs = False
        plugin._max_retries = 5
        result = RapidResult(False, True, "RAPID_MISS", sha1="B" * 40)
        plugin._audit_rapid(tmp_path / "source" / "movie.mkv", tmp_path / "source", result, True, 2)
        assert "秒传状态=未命中" in messages[-1]
        assert "文件=movie.mkv" in messages[-1]
        assert "重试次数=2/5" in messages[-1]
        assert "SHA1" not in messages[-1]
        assert str(tmp_path) not in messages[-1]

        watch = tmp_path / "watch"
        retry = tmp_path / "retry"
        watch.mkdir()
        retry.mkdir()
        path = watch / "movie.mkv"
        peer = tmp_path / "peer.mkv"
        path.write_bytes(b"content")
        os.link(path, peer)
        plugin._watch_dir = watch.resolve()
        plugin._retry_dir = retry.resolve()
        identity = secure_identity(path, watch, require_hardlink=True)
        plugin._move_to_retry(path, identity, "task")
        transfer = messages[-1]
        assert "已转移临时文件夹" in transfer
        assert "文件=movie.mkv" in transfer
        assert f"临时目录={retry.resolve()}" in transfer

        nested = retry / "series" / "season"
        nested.mkdir(parents=True)
        plugin._remove_empty_parent_dirs(nested, retry)
        assert not nested.exists()
        assert not (retry / "series").exists()
        assert retry.exists()
        assert "[简短] 已删除空文件夹" in messages[-1]

        nonempty = retry / "keep"
        nonempty.mkdir()
        (nonempty / "other.mkv").write_bytes(b"keep")
        plugin._remove_empty_parent_dirs(nonempty, retry)
        assert nonempty.exists()
        assert (nonempty / "other.mkv").exists()
    finally:
        plugin_module.logger = original_logger


def test_scheduled_empty_cleanup_never_deletes_roots_or_nonempty_dirs(tmp_path: Path):
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    root = tmp_path / "cleanup"
    second_root = tmp_path / "cleanup-two"
    watch = root / "watch"
    retry = root / "retry"
    protected_pt = tmp_path / "pt"
    empty_leaf = root / "old" / "season"
    second_empty_leaf = second_root / "old" / "season"
    nonempty = root / "keep"
    for directory in (watch, retry, protected_pt, empty_leaf, second_empty_leaf, nonempty):
        directory.mkdir(parents=True, exist_ok=True)
    (nonempty / "movie.mkv").write_bytes(b"keep")

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = root / "linked"
    symlink_created = False
    try:
        linked.symlink_to(outside, target_is_directory=True)
        symlink_created = True
    except OSError:
        pass

    plugin = P115RapidRetry()
    plugin._enabled = True
    plugin._empty_cleanup_enabled = True
    plugin._empty_cleanup_roots = plugin._prepare_cleanup_roots(
        f"{root}\n{second_root}\n{root}"
    )
    assert plugin._empty_cleanup_roots == [root.resolve(), second_root.resolve()]
    plugin._empty_cleanup_identities = {
        cleanup_root: (
            plugin._safe_directory_stat(cleanup_root).st_dev,
            plugin._safe_directory_stat(cleanup_root).st_ino,
        )
        for cleanup_root in plugin._empty_cleanup_roots
    }
    plugin._watch_dir = watch.resolve()
    plugin._retry_dir = retry.resolve()
    plugin._protected_pt_dir = protected_pt.resolve()

    plugin._operation_lock.acquire()
    try:
        plugin.cleanup_empty_directories()
        assert empty_leaf.exists()
        assert second_empty_leaf.exists()
        assert plugin.get_data("empty_cleanup_pending") is None
    finally:
        plugin._operation_lock.release()

    plugin.save_data("retry_state", {"existing-task": {"attempts": 2, "exhausted": False}})
    plugin.cleanup_empty_directories()
    assert plugin.get_data("retry_state")["existing-task"]["attempts"] == 2
    assert root.exists()
    assert second_root.exists()
    assert watch.exists()
    assert retry.exists()
    assert protected_pt.exists()
    assert not empty_leaf.exists()
    assert not second_empty_leaf.exists()
    assert not (root / "old").exists()
    assert not (second_root / "old").exists()
    assert nonempty.exists()
    assert (nonempty / "movie.mkv").exists()
    if symlink_created:
        assert linked.is_symlink()
        assert outside.exists()

    services = plugin.get_service()
    assert services and len(services) == 2
    assert any(item["id"] == "P115RapidRetry_empty_cleanup" for item in services)
    form, defaults = plugin.get_form()
    assert "VTextarea" in str(form)
    assert "/path/to/cleanup-root-1\\n/path/to/cleanup-root-2" in str(form)
    assert defaults["empty_cleanup_root"] == ""
    assert defaults["delete_exhausted_enabled"] is False
    assert "重试耗尽后删除文件及空文件夹" in str(form)


def test_manual_rapid_and_retry_actions_use_the_worker_queue():
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    plugin = P115RapidRetry()
    plugin._enabled = True
    plugin._events = queue.Queue(maxsize=8)
    plugin._stop_event = threading.Event()
    plugin._overflow = False
    calls = []
    plugin.retry_pending = lambda manual=False: calls.append(("retry", manual))
    plugin._queue_existing_files = lambda manual=False: calls.append(("rapid", manual)) or 0

    assert plugin._put_control_event("retry_now") is True
    assert plugin._put_control_event("scan_now") is True
    assert plugin._put_control_event("unknown") is False
    plugin._events.put_nowait(("stop", ""))
    plugin._worker_loop()

    assert calls == [("retry", True), ("rapid", True)]
    form, defaults = plugin.get_form()
    assert "立即运行秒传一次" in str(form)
    assert "立即重试秒传一次" in str(form)
    assert defaults["run_rapid_once"] is False
    assert defaults["run_retry_once"] is False


def test_realtime_failure_then_retry_success_keeps_pt_file(tmp_path: Path):
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    pt = tmp_path / "pt"
    watch = tmp_path / "watch"
    retry = tmp_path / "retry"
    for directory in (pt, watch, retry):
        directory.mkdir()
    original = pt / "movie.mkv"
    original.write_bytes(b"movie-content")

    plugin = P115RapidRetry()
    plugin._enabled = True
    plugin._watch_dir = watch.resolve()
    plugin._retry_dir = retry.resolve()
    plugin._protected_pt_dir = pt.resolve()
    plugin._target_pid = "0"
    plugin._stable_seconds = 1
    plugin._max_batch = 10
    plugin._min_request_interval = 0
    plugin._hourly_request_limit = 120
    miss_client = FakeClient(reuse=False)
    plugin._client = miss_client
    plugin._start_realtime_monitor()
    try:
        linked = watch / "movie.mkv"
        os.link(original, linked)
        deadline = time.time() + 8
        while time.time() < deadline and not (retry / "movie.mkv").exists():
            time.sleep(0.1)
        assert original.exists()
        assert not linked.exists()
        pending = retry / "movie.mkv"
        assert pending.exists()
        deadline = time.time() + 2
        while time.time() < deadline:
            if (plugin.get_data("retry_state") or {}) and not plugin._operation_lock.locked():
                break
            time.sleep(0.02)
        assert plugin.get_data("retry_state")
        assert not plugin._operation_lock.locked()
        state = plugin.get_data("retry_state") or {}
        for item in state.values():
            item["next_at"] = time.time() + 999999
        plugin.save_data("retry_state", state)
        success_client = FakeClient(reuse=True)
        plugin._client = success_client
        plugin.retry_pending()
        assert success_client.calls == 1
        assert original.exists()
        assert original.read_bytes() == b"movie-content"
        assert not pending.exists()
        serialized = repr(plugin.get_data("history"))
        assert "movie.mkv" not in serialized
        assert str(tmp_path) not in serialized
    finally:
        plugin.stop_service()


def test_retry_exhaustion_delete_switch_is_safe_and_keeps_pt_file(tmp_path: Path):
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry

    pt = tmp_path / "pt"
    retry = tmp_path / "retry"
    pt.mkdir()
    retry.mkdir()
    original = pt / "movie.mkv"
    original.write_bytes(b"movie-content")

    plugin = P115RapidRetry()
    plugin._enabled = True
    plugin._retry_dir = retry.resolve()
    plugin._protected_pt_dir = pt.resolve()
    plugin._target_pid = "0"
    plugin._max_retries = 1
    plugin._min_request_interval = 0
    plugin._hourly_request_limit = 120
    plugin._notify_enabled = False
    plugin._detailed_logs = False
    plugin._client = FakeClient(reuse=False)

    retained = retry / "retained" / "movie-retained.mkv"
    retained.parent.mkdir()
    os.link(original, retained)
    plugin._delete_exhausted_enabled = False
    plugin._handle(retained, retry, None, from_retry=True)
    assert retained.exists()
    assert retained.parent.exists()
    assert any(item.get("exhausted") for item in (plugin.get_data("retry_state") or {}).values())

    deleted = retry / "deleted" / "movie-deleted.mkv"
    deleted.parent.mkdir()
    os.link(original, deleted)
    plugin._delete_exhausted_enabled = True
    plugin._handle(deleted, retry, None, from_retry=True)
    assert not deleted.exists()
    assert not deleted.parent.exists()
    assert retry.exists()
    assert original.exists()
    assert original.read_bytes() == b"movie-content"

    calls_before_cleanup = plugin._client.calls
    plugin.retry_pending()
    assert plugin._client.calls == calls_before_cleanup
    assert not retained.exists()
    assert not retained.parent.exists()
    assert retry.exists()
    assert original.exists()


def test_verified_unlink_rejects_replaced_file(tmp_path: Path):
    _install_stubs()
    plugin_root = Path(__file__).parents[1] / "plugins.v2"
    sys.path.insert(0, str(plugin_root))
    from p115rapidretry import P115RapidRetry
    from p115rapidretry.rapid import secure_identity

    original = tmp_path / "item.mkv"
    peer = tmp_path / "peer.mkv"
    original.write_bytes(b"original")
    os.link(original, peer)
    identity = secure_identity(original, tmp_path, require_hardlink=True)
    original.unlink()
    original.write_bytes(b"replacement")
    assert P115RapidRetry._verified_unlink(original, identity, tmp_path) is False
    assert original.read_bytes() == b"replacement"


def test_dependency_manifest_uses_correct_asynctools_distribution():
    requirements = (
        Path(__file__).parents[1] / "plugins.v2" / "p115rapidretry" / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()
    assert requirements[:2] == [
        "python-asynctools==0.2.2",
        "p115client==0.0.9.4.1",
    ]
    assert not any(line.partition("==")[0].strip() == "asynctools" for line in requirements)


def test_p115_sources_are_strict_utf8_without_replacement_characters():
    plugin_dir = Path(__file__).parents[1] / "plugins.v2" / "p115rapidretry"
    for path in plugin_dir.glob("*.py"):
        source = path.read_bytes().decode("utf-8", errors="strict")
        assert "\ufffd" not in source, f"replacement character found in {path.name}"
        compile(source, str(path), "exec")


def test_cached_legacy_asynctools_is_reloaded_after_install():
    import importlib.util

    path = Path(__file__).parents[1] / "plugins.v2" / "p115rapidretry" / "dependency.py"
    spec = importlib.util.spec_from_file_location("p115_dependency_test", path)
    dependency = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(dependency)

    legacy = types.ModuleType("asynctools")
    legacy.__version__ = (0, 2, 1)
    modern = types.ModuleType("asynctools")
    modern.__version__ = (0, 2, 2)
    for name in dependency.REQUIRED_ASYNCTOOLS_EXPORTS:
        setattr(modern, name, object())
    calls = []

    result = dependency.ensure_asynctools_compatible(
        legacy, reload_module=lambda module: calls.append(module) or modern
    )
    assert result is modern
    assert calls == [legacy]


def test_asynctools_021_is_rejected_even_when_exports_exist():
    import importlib.util

    path = Path(__file__).parents[1] / "plugins.v2" / "p115rapidretry" / "dependency.py"
    spec = importlib.util.spec_from_file_location("p115_dependency_version_test", path)
    dependency = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(dependency)

    broken = types.ModuleType("asynctools")
    broken.__version__ = (0, 2, 1)
    for name in dependency.REQUIRED_ASYNCTOOLS_EXPORTS:
        setattr(broken, name, object())
    try:
        dependency.ensure_asynctools_compatible(
            broken, reload_module=lambda module: module
        )
    except ImportError as exc:
        assert str(exc) == "python-asynctools>=0.2.2 is required"
    else:
        raise AssertionError("incompatible python-asynctools 0.2.1 was accepted")
