from __future__ import annotations

import sys
import types
from pathlib import Path

import test_plugin_security as security

security._install_stubs()
PLUGIN_ROOT = Path(__file__).parents[1] / "plugins.v2"
sys.path.insert(0, str(PLUGIN_ROOT))

from embylibrarycover import (
    DEFAULT_LIBRARY_MAP, EMBEDDED_EN_FONT_NAME, EMBEDDED_ZH_FONT_NAME,
    EmbyLibraryCover,
)
from embylibrarycover.client import EmbyClient, EmbyError, validate_base_url
from embylibrarycover.renderer import CoverRenderer, DEFAULT_RENDER_CONFIG
from app.core.config import settings
from fontTools.ttLib import TTFont
from PIL import Image, ImageFont


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    def close(self):
        pass


def test_url_and_api_key_are_hardened():
    assert validate_base_url("https://emby.example.test:8920/") == "https://emby.example.test:8920"
    for unsafe in (
        "ftp://emby.example.test", "https://user:pass@emby.example.test",
        "https://emby.example.test/?api_key=secret", "https://emby.example.test/#token",
    ):
        try:
            validate_base_url(unsafe)
        except EmbyError as exc:
            assert exc.code == "URL_INVALID"
        else:
            raise AssertionError("unsafe Emby URL was accepted")

    secret = "test-api-key-never-in-url"
    client = EmbyClient("http://emby:8096", secret)
    assert client.session.headers["X-Emby-Token"] == secret
    response = FakeResponse(302)
    fake = FakeSession(response)
    client.session = fake
    try:
        client._request("GET", "/Users")
    except EmbyError as exc:
        assert exc.code == "REDIRECT_BLOCKED"
    else:
        raise AssertionError("redirect was not blocked")
    _, url, kwargs = fake.calls[0]
    assert secret not in url
    assert kwargs["allow_redirects"] is False
    assert response.closed


def test_library_mapping_is_bounded_and_supports_line_breaks():
    parsed = EmbyLibraryCover._parse_library_map("电影|电影|MOVIE\\nLIBRARY")
    assert parsed == {"电影": {"zh": "电影", "en": "MOVIE\nLIBRARY"}}
    assert len(EmbyLibraryCover._parse_library_map(DEFAULT_LIBRARY_MAP)) == 11
    for unsafe in ("", "missing separators", "a||b", "a|b|c\x00"):
        try:
            EmbyLibraryCover._parse_library_map(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe library mapping was accepted")


def test_form_defaults_to_generation_only_and_password_field():
    plugin = EmbyLibraryCover()
    form, defaults = plugin.get_form()
    assert defaults["upload_enabled"] is False
    assert defaults["verify_ssl"] is True
    assert defaults["run_once"] is False
    assert defaults["use_mp_config"] is True
    assert defaults["media_server"] == ""
    serialized = repr(form)
    assert "'model': 'api_key'" in serialized
    assert "'type': 'password'" in serialized
    for model in (
        "s1_snow_density", "s1_overlay_alpha", "s2_poster_rotation",
        "s2_accent_bar_color", "output_width", "cron",
        "s1_en_letter_spacing", "s2_en_letter_spacing",
    ):
        assert f"'model': '{model}'" in serialized
    for removed in (
        "schedule_time", "update_interval_hours", "upload_target",
        "s1_text_pos_zh_x", "s1_text_pos_en_y",
        "s1_poster_width", "s1_poster_height", "s2_text_pos_x",
        "s2_poster_width", "s2_poster_height",
        "s1_background_blur_enable", "s1_bottom_gradient_enable",
        "s1_snow_enable", "s1_blur_percent", "s2_accent_bar_enable", "s2_bg_auto_color",
    ):
        assert f"'model': '{removed}'" not in serialized
        assert removed not in defaults
    assert "'show': '{{use_mp_config}}'" in serialized
    assert "'show': '{{!use_mp_config}}'" in serialized
    assert "'show': '{{upload_enabled}}'" in serialized
    assert defaults["s1_en_letter_spacing"] == 10
    assert defaults["s2_en_letter_spacing"] == 6


def test_moviepilot_emby_config_is_resolved_without_copying_secrets():
    first = types.SimpleNamespace(type=types.SimpleNamespace(value="emby"), config={"host": "http://emby-a:8096", "apikey": "secret-a"})
    second = types.SimpleNamespace(type="emby", config={"host": "https://emby-b:8920", "apikey": "secret-b"})
    service = types.SimpleNamespace(config=second, instance=types.SimpleNamespace(user="abcdef123456"))

    class Helper:
        @staticmethod
        def get_configs():
            return {"B服务器": second, "A服务器": first}

        @staticmethod
        def get_services(type_filter=None):
            assert type_filter == "emby"
            return {"B服务器": service}

    assert EmbyLibraryCover._resolve_moviepilot_emby(Helper()) == (
        "http://emby-a:8096", "secret-a", "", "A服务器"
    )
    assert EmbyLibraryCover._resolve_moviepilot_emby(Helper(), "B服务器") == (
        "https://emby-b:8920", "secret-b", "abcdef123456", "B服务器"
    )

    class ConfigOnlyHelper(Helper):
        @staticmethod
        def get_services(type_filter=None):
            raise RuntimeError("module manager not ready")

    assert EmbyLibraryCover._resolve_moviepilot_emby(ConfigOnlyHelper()) == (
        "http://emby-a:8096", "secret-a", "", "A服务器"
    )
    try:
        EmbyLibraryCover._resolve_moviepilot_emby(Helper(), "不存在")
    except ValueError as exc:
        assert str(exc) == "MP_EMBY_NOT_FOUND"
    else:
        raise AssertionError("unknown MoviePilot Emby server was accepted")


def test_renderer_creates_both_styles(tmp_path: Path):
    posters = [Image.new("RGB", (120, 180), (40 + index * 10, 80, 120)) for index in range(9)]
    backdrop = Image.new("RGB", (320, 180), (20, 30, 40))
    renderer = CoverRenderer({"output_format": "jpg", "jpeg_quality": 80, "output_size": (1280, 720)})
    for style in ("style_1", "style_2"):
        path = tmp_path / f"{style}.jpg"
        renderer.render(style, {"zh": "电影", "en": "MOVIES"}, posters, backdrop, path)
        with Image.open(path) as image:
            assert image.size == (1280, 720)
            assert image.format == "JPEG"


def test_embedded_font_exists_and_renders_chinese(tmp_path: Path):
    settings.CONFIG_PATH = str(tmp_path)
    zh_font_path = EmbyLibraryCover._embedded_font_path(EMBEDDED_ZH_FONT_NAME)
    en_font_path = EmbyLibraryCover._embedded_font_path(EMBEDDED_EN_FONT_NAME)
    assert Path(zh_font_path).name == "MoviePilotCJKsc-Bold.otf"
    assert Path(en_font_path).name == "Melete-Bold.otf"
    zh_font = ImageFont.truetype(zh_font_path, 32)
    en_font = ImageFont.truetype(en_font_path, 32)
    for text in ("华语电影", "动画剧集", "综艺儿童", "纪录片精选合集"):
        assert zh_font.getbbox(text) is not None
    parsed = TTFont(zh_font_path, lazy=True)
    cmap = parsed.getBestCmap() or {}
    assert all(ord(char) in cmap for char in "华语电影动画剧集综艺儿童纪录片精选合集")
    parsed.close()
    assert en_font.getbbox("ANIME MOVIES TV SHOWS") is not None
    assert EmbyLibraryCover._embedded_font_path("../outside.ttf") == ""
    try:
        CoverRenderer({"font_zh_path": str(tmp_path / "missing.otf")}).validate_fonts()
    except ValueError as exc:
        assert str(exc) == "FONT_FILE_MISSING"
    else:
        raise AssertionError("missing configured font silently fell back")
    renderer = CoverRenderer({
        "font_zh_path": zh_font_path,
        "font_en_path": en_font_path,
        "output_format": "png",
        "output_size": (640, 360),
        "s1_poster_count": 1,
        "s1_poster_size": (100, 150),
        "s1_poster_y_pos": 190,
        "s1_snow_enable": False,
    })
    path = tmp_path / "embedded-font.png"
    renderer.render(
        "style_1", {"zh": "华语电影", "en": "MOVIES"},
        [Image.new("RGB", (100, 150), (40, 80, 120))],
        Image.new("RGB", (640, 360), (20, 30, 40)), path,
    )
    assert path.is_file() and path.stat().st_size > 0


def test_visual_config_is_validated_and_applied():
    config = EmbyLibraryCover._build_render_config({
        "output_width": 1280, "output_height": 720,
        "s1_snow_density": 60, "s1_snow_radius_min": 2, "s1_snow_radius_max": 8,
        "s2_accent_bar_color": "#FF8C00", "s2_bg_default_color": "#1E1E23",
    }, "", "")
    assert config["output_size"] == (1280, 720)
    assert config["s1_snow_density"] == 60
    assert config["s2_accent_bar_color"] == (255, 140, 0)
    migrated = EmbyLibraryCover._build_render_config({
        "s1_text_pos_zh_x": 999, "s1_text_pos_en_y": 999,
        "s1_en_letter_spacing": 99, "s1_poster_width": 999,
        "s2_text_pos_x": 999, "s2_en_letter_spacing": 88,
        "s2_poster_height": 999, "s1_background_blur_enable": True,
        "s1_bottom_gradient_enable": False, "s1_snow_enable": False,
        "s2_accent_bar_enable": False, "s2_bg_auto_color": False,
    }, "", "")
    for key in (
        "s1_text_pos_zh", "s1_text_pos_en",
        "s1_poster_size", "s2_text_pos",
        "s2_poster_size", "s1_background_blur_enable",
        "s1_bottom_gradient_enable", "s1_snow_enable",
        "s2_accent_bar_enable", "s2_bg_auto_color",
    ):
        assert migrated[key] == DEFAULT_RENDER_CONFIG[key]
    assert migrated["s1_en_letter_spacing"] == 99
    assert migrated["s2_en_letter_spacing"] == 88
    for unsafe in (
        {"output_width": 99999},
        {"s1_snow_radius_min": 9, "s1_snow_radius_max": 8},
        {"s2_accent_bar_color": "orange"},
        {"s1_en_letter_spacing": 101},
        {"s2_en_letter_spacing": -1},
    ):
        try:
            EmbyLibraryCover._build_render_config(unsafe, "", "")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe visual config was accepted")


def test_safe_filename_does_not_escape_output_directory():
    filename = EmbyLibraryCover._safe_filename("../../媒体库\r\n", "abc-123")
    assert "/" not in filename and "\\" not in filename
    assert ".." not in filename
