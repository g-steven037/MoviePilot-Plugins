from __future__ import annotations

import os
import base64
import gzip
import hashlib
import io
import re
import stat
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase

from .client import EmbyClient, EmbyError, Library
from .renderer import CoverRenderer, DEFAULT_RENDER_CONFIG


DEFAULT_LIBRARY_MAP = """动画电影|动画电影|ANIME\\nMOVIE
华语电影|华语电影|CHINESE\\nMOVIES
外语电影|外语电影|FOREIGN\\nMOVIES
华语剧集|华语剧集|CHINESE\\nSERIES
外语剧集|外语剧集|FOREIGN\\nSERIES
日漫|日漫|JAPANESE\\nANIME
国漫|国漫|CHINESE\\nANIME
综艺|综艺|TV\\nSHOWS
儿童|儿童|CHILDREN
纪录片|纪录片|DOCUMENTARY
精选合集|精选合集|CURATED\\nCOLLECTION"""

EMBEDDED_ZH_FONT_NAME = "MoviePilotCJKsc-Bold.otf"
EMBEDDED_EN_FONT_NAME = "Melete-Bold.otf"
EMBEDDED_FONT_ASSETS = {
    EMBEDDED_ZH_FONT_NAME: (
        "MoviePilotCJKsc-Bold.otf.gz.b64",
        "8C0629726B4BA7D5B4ABAF97C0EF606AF1C8FA1F810E4F8A90DCF0643D051E3D",
    ),
    EMBEDDED_EN_FONT_NAME: (
        "Melete-Bold.otf.gz.b64",
        "DB19306598D42E57DBB6EA6D6381A61F1228F818229BE0D6D48F6E5E2BEF989B",
    ),
}
MAX_FONT_ASSET_BYTES = 20 * 1024 * 1024
MAX_DECODED_FONT_BYTES = 20 * 1024 * 1024


class EmbyLibraryCover(_PluginBase):
    plugin_name = "Emby媒体库封面"
    plugin_desc = "根据Emby最新媒体海报生成横版媒体库封面，可按Cron定时生成并选择性上传覆盖，仅自用测试。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/emby-library-cover.svg"
    plugin_version = "0.2.1"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "embylibrarycover_"
    plugin_order = 31
    auth_level = 2

    _enabled = False
    _cron = "0 3 * * *"
    _client: Optional[EmbyClient] = None
    _thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _run_lock = threading.Lock()
    _library_map: Dict[str, Dict[str, str]] = {}
    _output_dir = Path()
    _style = "style_1"
    _output_format = "jpg"
    _upload_enabled = False
    _renderer: Optional[CoverRenderer] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        if not self._enabled:
            return
        self._stop_event = threading.Event()
        try:
            self._cron = str(config.get("cron", "0 3 * * *")).strip()
            CronTrigger.from_crontab(self._cron)
            self._library_map = self._parse_library_map(config.get("library_map", DEFAULT_LIBRARY_MAP))
            self._style = str(config.get("style", "style_1")).strip()
            if self._style not in {"style_1", "style_2", "style_3"}:
                raise ValueError("STYLE_INVALID")
            timeout = self._bounded_int(config.get("timeout", 30), 5, 120)
            self._output_dir = self._prepare_output_dir(config.get("output_dir", ""))
            embedded_zh_font = self._embedded_font_path(EMBEDDED_ZH_FONT_NAME)
            embedded_en_font = self._embedded_font_path(EMBEDDED_EN_FONT_NAME)
            custom_zh_font = self._prepare_font(config.get("font_zh_path", ""))
            custom_en_font = self._prepare_font(config.get("font_en_path", ""))
            font_zh = custom_zh_font or embedded_zh_font
            font_en = custom_en_font or embedded_en_font
            render_config = self._build_render_config(config, font_zh, font_en)
            self._output_format = render_config["output_format"]
            self._upload_enabled = bool(config.get("upload_enabled", False))
            self._renderer = CoverRenderer(render_config)
            self._renderer.validate_fonts()
            logger.info(
                "#Emby媒体库封面# 字体加载成功 | "
                f"中文={'自定义' if custom_zh_font else '内置静态CJK'} | "
                f"英文={'自定义' if custom_en_font else '内置Melete'}"
            )
            if bool(config.get("use_mp_config", True)):
                emby_url, api_key, user_id, server_name = self._load_moviepilot_emby(
                    str(config.get("media_server", "")).strip()
                )
                logger.info(f"#Emby媒体库封面# 使用MoviePilot媒体服务器配置 | 名称={self._safe_log(server_name)}")
            else:
                emby_url = config.get("emby_url", "")
                api_key = config.get("api_key", "")
                user_id = str(config.get("user_id", "")).strip()
            self._client = EmbyClient(
                base_url=emby_url,
                api_key=api_key,
                user_id=user_id,
                timeout=timeout,
                verify_ssl=bool(config.get("verify_ssl", True)),
                verify_upload=bool(config.get("verify_upload", False)),
                upload_target="item",
                stop_event=self._stop_event,
            )
            if str(emby_url).lower().startswith("https://") and not bool(config.get("verify_ssl", True)):
                logger.warning("#Emby媒体库封面# HTTPS证书校验已关闭，请仅在可信内网中使用")
            if bool(config.get("run_once", False)):
                config["run_once"] = False
                self.update_config(config)
                self._thread = threading.Thread(target=self.generate_covers, name="emby-cover-worker", daemon=True)
                self._thread.start()
            logger.info("Emby媒体库封面：插件已启用")
        except Exception as exc:
            self._enabled = False
            if self._client:
                self._client.close()
            self._client = None
            logger.error(f"Emby媒体库封面：安全初始化失败 [{self._safe_code(exc)}]")

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("CONFIG_INVALID") from exc
        if not minimum <= number <= maximum:
            raise ValueError("CONFIG_OUT_OF_RANGE")
        return number

    @staticmethod
    def _parse_color(value: Any, default: Tuple[int, int, int]) -> Tuple[int, int, int]:
        text = str(value or "").strip()
        if not text:
            return default
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
            raise ValueError("COLOR_INVALID")
        return tuple(int(text[index:index + 2], 16) for index in (1, 3, 5))

    @classmethod
    def _build_render_config(cls, config: Dict[str, Any], font_zh: str, font_en: str) -> Dict[str, Any]:
        result = dict(DEFAULT_RENDER_CONFIG)
        result["font_zh_path"] = font_zh
        result["font_en_path"] = font_en
        output_format = str(config.get("output_format", result["output_format"])).strip().lower()
        if output_format not in {"jpg", "png"}:
            raise ValueError("OUTPUT_FORMAT_INVALID")
        result["output_format"] = output_format
        result["jpeg_quality"] = cls._bounded_int(config.get("jpeg_quality", 92), 70, 100)
        width = cls._bounded_int(config.get("output_width", 1920), 640, 3840)
        height = cls._bounded_int(config.get("output_height", 1080), 360, 2160)
        if width * height > 8_294_400:
            raise ValueError("OUTPUT_SIZE_TOO_LARGE")
        result["output_size"] = (width, height)

        integer_specs = {
            "s1_font_size_zh": (8, 500), "s1_font_size_en": (8, 300), "s1_title_gap": (0, 500),
            "s1_en_letter_spacing": (0, 100), "s1_en_line_spacing": (0, 200),
            "s1_poster_count": (1, 12), "s1_poster_spacing": (0, 500),
            "s1_poster_y_pos": (-2160, 4320),
            "s1_overlay_alpha": (0, 255), "s1_gradient_width": (1, 3840),
            "s1_gradient_max_alpha": (0, 255), "s1_bottom_gradient_max_alpha": (0, 255),
            "s1_snow_density": (0, 5000), "s1_snow_radius_min": (1, 100),
            "s1_snow_radius_max": (1, 100), "s1_snow_alpha_min": (0, 255),
            "s1_snow_alpha_max": (0, 255), "s1_snow_seed": (0, 2_147_483_647),
            "s2_poster_count": (1, 12), "s2_poster_spacing_x": (0, 500),
            "s2_poster_spacing_y": (0, 500), "s2_poster_stagger": (-2000, 2000),
            "s2_poster_rotation": (-180, 180), "s2_font_size_zh": (8, 500),
            "s2_font_size_en": (8, 300), "s2_title_gap": (0, 500), "s2_en_letter_spacing": (0, 100),
            "s2_en_line_spacing": (0, 200),
        }
        for key, bounds in integer_specs.items():
            result[key] = cls._bounded_int(config.get(key, result[key]), *bounds)

        pair_specs = {
            "s2_poster_center": ("s2_poster_center_x", "s2_poster_center_y", (1600, 540)),
        }
        for key, (x_model, y_model, default) in pair_specs.items():
            if "size" in key:
                x = cls._bounded_int(config.get(x_model, default[0]), 20, 2000)
                y = cls._bounded_int(config.get(y_model, default[1]), 20, 3000)
            else:
                x = cls._bounded_int(config.get(x_model, default[0]), -4096, 8192)
                y = cls._bounded_int(config.get(y_model, default[1]), -4096, 8192)
            result[key] = (x, y)

        result["s2_accent_bar_color"] = cls._parse_color(
            config.get("s2_accent_bar_color", "#FF8C00"), (255, 140, 0)
        )
        result["s2_bg_default_color"] = cls._parse_color(
            config.get("s2_bg_default_color", "#1E1E23"), (30, 30, 35)
        )
        if result["s1_snow_radius_min"] > result["s1_snow_radius_max"]:
            raise ValueError("SNOW_RADIUS_INVALID")
        if result["s1_snow_alpha_min"] > result["s1_snow_alpha_max"]:
            raise ValueError("SNOW_ALPHA_INVALID")
        return result

    @staticmethod
    def _safe_code(exc: Exception) -> str:
        if isinstance(exc, EmbyError):
            return exc.code
        text = str(exc)
        if text and text.isupper() and " " not in text and len(text) <= 64:
            return text
        return type(exc).__name__.upper()[:64]

    @staticmethod
    def _safe_log(value: Any) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", "_", str(value or ""))[:256]

    @staticmethod
    def _parse_library_map(value: Any) -> Dict[str, Dict[str, str]]:
        text = str(value or "")
        if len(text) > 20000:
            raise ValueError("LIBRARY_MAP_TOO_LARGE")
        result: Dict[str, Dict[str, str]] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                raise ValueError("LIBRARY_MAP_INVALID")
            name, zh, en = parts
            en = en.replace("\\n", "\n")
            if len(name) > 256 or len(zh) > 128 or len(en) > 256:
                raise ValueError("LIBRARY_MAP_INVALID")
            if any(ord(char) < 0x20 and char != "\n" for char in name + zh + en):
                raise ValueError("LIBRARY_MAP_INVALID")
            result[name] = {"zh": zh, "en": en}
            if len(result) > 100:
                raise ValueError("LIBRARY_MAP_TOO_LARGE")
        if not result:
            raise ValueError("LIBRARY_MAP_EMPTY")
        return result

    @staticmethod
    def _resolve_moviepilot_emby(helper: Any, selected_name: str = "") -> Tuple[str, str, str, str]:
        configs = helper.get_configs() or {}
        try:
            services = helper.get_services(type_filter="emby") or {}
        except Exception:
            services = {}
        candidates = sorted(
            name for name, conf in configs.items()
            if str(getattr(getattr(conf, "type", ""), "value", getattr(conf, "type", ""))).lower() == "emby"
        )
        if not candidates:
            raise ValueError("MP_EMBY_NOT_FOUND")
        name = selected_name or candidates[0]
        if name not in candidates:
            raise ValueError("MP_EMBY_NOT_FOUND")
        service = services.get(name)
        conf = (getattr(service, "config", None) if service else None) or configs.get(name)
        values = getattr(conf, "config", None) or {}
        if not isinstance(values, dict):
            raise ValueError("MP_EMBY_CONFIG_INVALID")
        host = str(values.get("host") or "").strip()
        api_key = str(values.get("apikey") or "").strip()
        instance = getattr(service, "instance", None) if service else None
        user_id = str(getattr(instance, "user", "") or "").strip()
        if not host or not api_key:
            raise ValueError("MP_EMBY_CONFIG_INCOMPLETE")
        return host, api_key, user_id, name

    @classmethod
    def _load_moviepilot_emby(cls, selected_name: str = "") -> Tuple[str, str, str, str]:
        try:
            from app.helper.mediaserver import MediaServerHelper
            return cls._resolve_moviepilot_emby(MediaServerHelper(), selected_name)
        except (ImportError, AttributeError) as exc:
            raise ValueError("MP_EMBY_HELPER_UNAVAILABLE") from exc

    @staticmethod
    def _moviepilot_emby_items() -> List[Dict[str, str]]:
        try:
            from app.helper.mediaserver import MediaServerHelper
            configs = MediaServerHelper().get_configs() or {}
            names = sorted(
                name for name, conf in configs.items()
                if str(getattr(getattr(conf, "type", ""), "value", getattr(conf, "type", ""))).lower() == "emby"
            )
            return [{"title": name, "value": name} for name in names]
        except Exception:
            return []

    @staticmethod
    def _default_output_dir() -> Path:
        base = Path(str(getattr(settings, "CONFIG_PATH", "/config")))
        return base / "plugins" / "embylibrarycover" / "output"

    def _prepare_output_dir(self, value: Any) -> Path:
        path = Path(str(value or "").strip()) if str(value or "").strip() else self._default_output_dir()
        if not path.is_absolute():
            raise ValueError("OUTPUT_DIR_NOT_ABSOLUTE")
        if path.is_symlink():
            raise ValueError("OUTPUT_DIR_UNSAFE")
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise ValueError("OUTPUT_DIR_ROOT_FORBIDDEN")
        info = resolved.lstat()
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & marker):
            raise ValueError("OUTPUT_DIR_UNSAFE")
        test_path = resolved / f".write-test-{os.getpid()}-{threading.get_ident()}"
        try:
            with test_path.open("xb") as stream:
                stream.write(b"")
        finally:
            test_path.unlink(missing_ok=True)
        return resolved

    @staticmethod
    def _prepare_font(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute() or path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            raise ValueError("FONT_PATH_INVALID")
        if path.is_symlink():
            raise ValueError("FONT_PATH_INVALID")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("FONT_PATH_INVALID")
        return str(resolved)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()

    @classmethod
    def _embedded_font_path(cls, font_name: str) -> str:
        """Materialize a text-encoded font for MoviePilot v2.14.2 safely."""
        asset_info = EMBEDDED_FONT_ASSETS.get(font_name)
        if not asset_info:
            return ""
        asset_name, expected_sha256 = asset_info
        asset_dir = (Path(__file__).resolve().parent / "fonts").resolve()
        asset = asset_dir / asset_name
        if asset.is_symlink() or not asset.is_file():
            raise ValueError("FONT_ASSET_MISSING")
        if asset.stat().st_size > MAX_FONT_ASSET_BYTES:
            raise ValueError("FONT_ASSET_TOO_LARGE")

        cache_dir = Path(str(getattr(settings, "CONFIG_PATH", "/config"))) / "plugins" / "embylibrarycover" / "fonts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if cache_dir.is_symlink():
            raise ValueError("FONT_CACHE_UNSAFE")
        try:
            cache_dir = cache_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("FONT_CACHE_UNSAFE")
        target = cache_dir / font_name
        if target.is_symlink():
            raise ValueError("FONT_CACHE_UNSAFE")
        if target.is_file() and cls._sha256(target) == expected_sha256:
            return str(target)

        try:
            encoded = b"".join(asset.read_bytes().split())
            compressed = base64.b64decode(encoded, validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
                decoded = archive.read(MAX_DECODED_FONT_BYTES + 1)
        except (OSError, ValueError, EOFError, gzip.BadGzipFile) as exc:
            raise ValueError("FONT_ASSET_INVALID") from exc
        if len(decoded) > MAX_DECODED_FONT_BYTES:
            raise ValueError("FONT_DECODE_TOO_LARGE")
        if hashlib.sha256(decoded).hexdigest().upper() != expected_sha256:
            raise ValueError("FONT_HASH_MISMATCH")

        temporary = cache_dir / f".{font_name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(decoded)
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)

    @staticmethod
    def _safe_filename(name: str, library_id: str) -> str:
        safe = re.sub(r'[^\w\-.\u4e00-\u9fff]+', "_", name, flags=re.UNICODE).strip("_.")[:100]
        suffix = re.sub(r"[^A-Za-z0-9]", "", library_id)[:12]
        return f"{safe or 'library'}-{suffix or 'unknown'}"

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        if not self._enabled:
            return None
        trigger = CronTrigger.from_crontab(self._cron)
        return [{
            "id": "EmbyLibraryCover_generate",
            "name": f"Emby媒体库封面定时生成（{self._cron}）",
            "trigger": trigger,
            "func": self.generate_covers,
            "kwargs": {},
        }]

    def generate_covers(self):
        if not self._enabled or not self._client or not self._renderer:
            return
        if not self._run_lock.acquire(blocking=False):
            logger.warning("#Emby媒体库封面# 上一轮仍在运行，本轮已跳过")
            return
        success_count = 0
        failure_count = 0
        try:
            libraries = {library.name: library for library in self._client.get_libraries()}
            logger.info(f"#Emby媒体库封面# 开始处理 | 配置={len(self._library_map)} | Emby媒体库={len(libraries)}")
            max_posters = self._renderer.poster_count(self._style)
            for library_name, title in self._library_map.items():
                if self._stop_event.is_set():
                    break
                library = libraries.get(library_name)
                safe_name = self._safe_log(library_name)
                if not library:
                    failure_count += 1
                    self._record(safe_name, False, "LIBRARY_NOT_FOUND")
                    logger.warning(f"#Emby媒体库封面# 媒体库未找到 | 名称={safe_name}")
                    continue
                try:
                    self._process_library(library, title, max_posters)
                    success_count += 1
                    self._record(safe_name, True, "SUCCESS")
                except Exception as exc:
                    failure_count += 1
                    code = self._safe_code(exc)
                    self._record(safe_name, False, code)
                    logger.error(f"#Emby媒体库封面# 处理失败 | 媒体库={safe_name} | 代码={code}")
                if self._stop_event.wait(0.5):
                    break
            logger.info(f"#Emby媒体库封面# 本轮完成 | 成功={success_count} | 失败={failure_count}")
        except Exception as exc:
            logger.error(f"#Emby媒体库封面# 本轮终止 | 代码={self._safe_code(exc)}")
        finally:
            self._run_lock.release()

    def _process_library(self, library: Library, title: Dict[str, str], max_posters: int):
        items = self._client.get_latest_items(library.id, min(max_posters * 3, 100))
        posters = self._client.get_posters(items, max_posters)
        backdrop = self._client.get_backdrop(items)
        if not posters:
            raise EmbyError("POSTERS_NOT_FOUND")
        extension = "png" if self._output_format == "png" else "jpg"
        name = self._safe_filename(library.name, library.id)
        output_path = self._output_dir / f"{name}.{extension}"
        temporary = self._output_dir / f".{name}.{threading.get_ident()}.tmp.{extension}"
        try:
            self._renderer.render(self._style, title, posters, backdrop, temporary)
            os.replace(temporary, output_path)
            logger.info(f"#Emby媒体库封面# 生成成功 | 媒体库={self._safe_log(library.name)} | 文件={output_path.name}")
            if self._upload_enabled:
                target = self._client.upload_library_primary_image(library, output_path)
                logger.info(f"#Emby媒体库封面# 上传成功 | 媒体库={self._safe_log(library.name)} | 目标={target}")
        finally:
            temporary.unlink(missing_ok=True)
            for image in posters:
                image.close()
            if backdrop:
                backdrop.close()

    def _record(self, library: str, success: bool, code: str):
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "library": library,
            "success": bool(success),
            "code": code if str(code).isupper() else "UNKNOWN",
        })
        self.save_data("history", history[:100])

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        server_items = self._moviepilot_emby_items()
        if server_items:
            detected_text = (
                f"已从MoviePilot检测到 {len(server_items)} 个已启用的Emby服务器。"
                "自动读取不会把API Key回填到下方手动输入框；启用插件并保存时才读取使用。"
            )
            detected_type = "info"
        else:
            detected_text = "未检测到MoviePilot中已启用的Emby服务器；请先检查媒体服务器设置，或关闭自动读取后使用手动参数。"
            detected_type = "error"
        alert = {
            "component": "VAlert",
            "props": {
                "type": "warning", "variant": "tonal",
                "text": "默认读取MoviePilot中已启用的Emby地址、API Key和用户ID，不复制到插件配置或日志；关闭自动读取后才使用下方手动参数。默认只生成图片，不上传覆盖。",
            },
        }
        switches = [
            ("enabled", "插件启用"), ("run_once", "立即运行一次"),
            ("use_mp_config", "使用MoviePilot的Emby配置"),
            ("upload_enabled", "上传覆盖Emby封面"), ("verify_upload", "上传后验证"),
            ("verify_ssl", "校验HTTPS证书"),
        ]
        content = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [alert]}]}]
        content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
            "component": "VAlert", "props": {"type": detected_type, "variant": "tonal", "text": detected_text}
        }]}]})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {
                "cols": 12, "md": 4,
                **({"show": "{{upload_enabled}}"} if model == "verify_upload" else {}),
            }, "content": [
                {"component": "VSwitch", "props": {"model": model, "label": label}}
            ]} for model, label in switches
        ]})
        content.append({"component": "VRow", "props": {"show": "{{use_mp_config}}"}, "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{
            "component": "VSelect", "props": {
                "model": "media_server", "label": "MoviePilot中的Emby服务器（留空自动选择第一个）",
                "clearable": True, "items": server_items,
            }
        }]}]})
        fields = [
            ("emby_url", "手动Emby地址（关闭自动读取时使用）", "text"),
            ("api_key", "手动Emby API Key（关闭自动读取时使用）", "password"),
            ("user_id", "手动Emby用户ID（可留空自动获取）", "text"),
            ("cron", "生成计划 Cron（5段）", "text"),
            ("output_dir", "输出目录（留空使用MoviePilot配置目录）", "text"),
            ("font_zh_path", "中文字体绝对路径（留空使用内置静态中文字体）", "text"),
            ("font_en_path", "英文字体绝对路径（留空使用内置Melete Bold）", "text"),
            ("timeout", "请求超时秒数（5-120）", "number"),
            ("output_width", "输出宽度（640-3840）", "number"),
            ("output_height", "输出高度（360-2160）", "number"),
            ("jpeg_quality", "JPG质量（70-100）", "number"),
        ]
        for model, label, field_type in fields:
            props = {"model": model, "label": label, "clearable": False, "type": field_type}
            if model == "api_key":
                props["autocomplete"] = "new-password"
            row_props = {"show": "{{!use_mp_config}}"} if model in {"emby_url", "api_key", "user_id"} else {}
            content.append({"component": "VRow", "props": row_props, "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VTextField", "props": props}
            ]}]})
        selects = [
            ("style", "封面样式", [
                ("经典横排", "style_1"), ("倾斜海报墙", "style_2"),
                ("横版电影卡片", "style_3"),
            ]),
            ("output_format", "输出格式", [("JPG", "jpg"), ("PNG", "png")]),
        ]
        for model, label, items in selects:
            content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VSelect", "props": {
                    "model": model, "label": label, "clearable": False,
                    "items": [{"title": title, "value": value} for title, value in items],
                }}
            ]}]})
        visual_groups = [
            ("Style 1 经典横排参数", [
                ("s1_font_size_zh", "中文字号"), ("s1_font_size_en", "英文字号"),
                ("s1_title_gap", "中英文垂直间距"),
                ("s1_en_letter_spacing", "英文字距"), ("s1_en_line_spacing", "英文行距"),
                ("s1_poster_count", "海报数量"),
                ("s1_poster_spacing", "海报间距"),
                ("s1_poster_y_pos", "海报Y坐标"),
                ("s1_overlay_alpha", "全局遮罩透明度"), ("s1_gradient_width", "左侧渐变宽度"),
                ("s1_gradient_max_alpha", "左侧渐变最大透明度"),
                ("s1_bottom_gradient_max_alpha", "底部渐变最大透明度"),
                ("s1_snow_density", "雪花数量"), ("s1_snow_radius_min", "雪花最小半径"),
                ("s1_snow_radius_max", "雪花最大半径"), ("s1_snow_alpha_min", "雪花最小透明度"),
                ("s1_snow_alpha_max", "雪花最大透明度"), ("s1_snow_seed", "雪花随机种子"),
            ]),
            ("Style 2 倾斜海报墙参数", [
                ("s2_poster_count", "海报数量"), ("s2_poster_spacing_x", "海报横向间距"),
                ("s2_poster_spacing_y", "海报纵向间距"), ("s2_poster_stagger", "列错位距离"),
                ("s2_poster_rotation", "海报墙旋转角度"), ("s2_poster_center_x", "海报墙中心X"),
                ("s2_poster_center_y", "海报墙中心Y"), ("s2_font_size_zh", "中文字号"),
                ("s2_title_gap", "中英文垂直间距"),
                ("s2_font_size_en", "英文字号"), ("s2_en_letter_spacing", "英文字距"),
                ("s2_en_line_spacing", "英文行距"),
            ]),
        ]
        for title, visual_fields in visual_groups:
            content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VCardTitle", "text": title}
            ]}]})
            for index in range(0, len(visual_fields), 3):
                content.append({"component": "VRow", "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                        "component": "VTextField", "props": {
                            "model": model, "label": label, "type": "number", "clearable": False,
                        }
                    }]} for model, label in visual_fields[index:index + 3]
                ]})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                "component": "VTextField", "props": {
                    "model": "s2_accent_bar_color", "label": "Style 2装饰条颜色（#RRGGBB）", "clearable": False,
                }
            }]},
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                "component": "VTextField", "props": {
                    "model": "s2_bg_default_color", "label": "Style 2默认背景颜色（#RRGGBB）", "clearable": False,
                }
            }]},
        ]})
        content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VTextarea", "props": {
                "model": "library_map", "label": "媒体库标题映射（每行：媒体库名|中文标题|英文标题）",
                "rows": 6, "auto-grow": True, "clearable": False,
                "placeholder": "电影|电影|MOVIES\n剧集|剧集|TV SERIES",
            }}
        ]}]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False, "run_once": False, "use_mp_config": True,
            "media_server": "",
            "emby_url": "", "api_key": "", "user_id": "",
            "cron": "0 3 * * *",
            "library_map": DEFAULT_LIBRARY_MAP,
            "style": "style_1", "output_format": "jpg", "jpeg_quality": 92,
            "output_width": 1920, "output_height": 1080,
            "output_dir": "", "font_zh_path": "", "font_en_path": "",
            "upload_enabled": False, "verify_upload": False,
            "verify_ssl": True, "timeout": 30,
            "s1_font_size_zh": 150, "s1_font_size_en": 70, "s1_title_gap": 20,
            "s1_en_letter_spacing": 10, "s1_en_line_spacing": 12,
            "s1_poster_count": 6,
            "s1_poster_spacing": 20, "s1_poster_y_pos": 610,
            "s1_overlay_alpha": 110, "s1_gradient_width": 1000,
            "s1_gradient_max_alpha": 180,
            "s1_bottom_gradient_max_alpha": 155,
            "s1_snow_density": 60, "s1_snow_radius_min": 2, "s1_snow_radius_max": 8,
            "s1_snow_alpha_min": 120, "s1_snow_alpha_max": 200,
            "s1_snow_seed": 20260708,
            "s2_poster_count": 9,
            "s2_poster_spacing_x": 10, "s2_poster_spacing_y": 10,
            "s2_poster_stagger": 180, "s2_poster_rotation": -15,
            "s2_poster_center_x": 1600, "s2_poster_center_y": 540,
            "s2_font_size_zh": 180, "s2_font_size_en": 50, "s2_title_gap": 24,
            "s2_en_letter_spacing": 6, "s2_en_line_spacing": 12,
            "s2_accent_bar_color": "#FF8C00",
            "s2_bg_default_color": "#1E1E23",
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        rows = [[item.get("time"), item.get("library"), "成功" if item.get("success") else "失败", item.get("code")] for item in history[:100]]
        return [{"component": "VTable", "props": {"hover": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": title} for title in ("时间", "媒体库", "状态", "安全码")
            ]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [
                {"component": "td", "text": str(value or "")} for value in row
            ]} for row in rows]},
        ]}]

    def get_command(self):
        return None

    def get_api(self):
        return None

    def stop_service(self):
        self._enabled = False
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None
        if self._client:
            self._client.close()
        self._client = None
        self._renderer = None
