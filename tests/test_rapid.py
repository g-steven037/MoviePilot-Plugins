from hashlib import sha1
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins.v2" / "p115rapidretry"))

from rapid import file_sha1, range_sha1_reader, same_identity, secure_identity, try_rapid_upload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def upload_file_init(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_hash_and_range(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"0123456789")
    assert file_sha1(path) == sha1(b"0123456789").hexdigest().upper()
    assert range_sha1_reader(path)("2-5") == sha1(b"2345").hexdigest().upper()


def test_reuse_is_success(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    client = FakeClient({"state": True, "reuse": True})
    result = try_rapid_upload(client, path, "123")
    assert result.success is True
    assert result.code == "RAPID_SUCCESS"
    assert result.sha1 == sha1(b"content").hexdigest().upper()
    assert client.kwargs["pid"] == "123"
    assert client.kwargs["filename"] == "video.mkv"


def test_miss_is_retryable_and_does_not_upload_content(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    client = FakeClient({"state": True, "reuse": False})
    result = try_rapid_upload(client, path, "0")
    assert result.success is False
    assert result.retryable is True
    assert result.code == "RAPID_MISS"
    assert result.sha1 == sha1(b"content").hexdigest().upper()
    assert "file" not in client.kwargs


def test_known_sha1_cache_skips_full_rehash(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    expected = sha1(b"content").hexdigest().upper()
    events = []
    client = FakeClient({"state": True, "reuse": True})
    result = try_rapid_upload(client, path, "0", known_sha1=expected, progress=events.append)
    assert result.success is True
    assert result.sha1 == expected
    assert events == ["SHA1_CACHE"]


def test_raw_server_error_is_not_returned(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    secret = "UID=secret-cookie-value"
    result = try_rapid_upload(FakeClient({"state": False, "error": secret}), path, "0")
    assert secret not in result.code
    assert result.code in {"API_REJECTED", "AUTH_FAILED"}


def test_hardlink_and_replacement_detection(tmp_path: Path):
    path = tmp_path / "video.mkv"
    peer = tmp_path / "peer.mkv"
    path.write_bytes(b"original")
    try:
        secure_identity(path, tmp_path, require_hardlink=True)
        raise AssertionError("single-link file was accepted")
    except ValueError as exc:
        assert str(exc) == "NOT_A_HARDLINK"
    peer.hardlink_to(path)
    identity = secure_identity(path, tmp_path, require_hardlink=True)
    path.unlink()
    path.write_bytes(b"replacement")
    assert same_identity(path, identity, tmp_path) is False


def test_exception_text_is_not_returned(tmp_path: Path):
    class RaisingClient:
        def upload_file_init(self, **kwargs):
            raise RuntimeError("UID=secret-cookie")

    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    result = try_rapid_upload(RaisingClient(), path, "0")
    assert result.code == "CLIENT_ERROR"
    assert "secret" not in result.code


def test_request_guard_blocks_api_call(tmp_path: Path):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    client = FakeClient({"state": True, "reuse": True})
    result = try_rapid_upload(client, path, "0", request_guard=lambda: False)
    assert result.code == "CIRCUIT_OPEN"
    assert client.kwargs is None


def test_http_status_exception_triggers_risk_codes(tmp_path: Path):
    class Response:
        status_code = 429

    class RateLimitedClient:
        def upload_file_init(self, **_kwargs):
            error = RuntimeError("response text must not be returned")
            error.response = Response()
            raise error

    path = tmp_path / "video.mkv"
    path.write_bytes(b"content")
    result = try_rapid_upload(RateLimitedClient(), path, "0")
    assert result.code == "RATE_LIMITED"
