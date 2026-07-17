from __future__ import annotations

import copy
import re
import threading
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase

from .client import EmbyActorClient, EmbyActorError


class EmbyActorChinese(_PluginBase):
    plugin_name = "Emby演员中文化"
    plugin_desc = "按影视名称和年份匹配豆瓣演员中文名，预览确认后同步到Emby，仅自用测试。"
    plugin_icon = "https://raw.githubusercontent.com/g-steven037/MoviePilot-Plugins/main/assets/emby-actor-chinese.svg"
    plugin_version = "0.1.1"
    plugin_author = "g-steven037"
    author_url = "https://github.com/g-steven037"
    plugin_config_prefix = "embyactorchinese_"
    plugin_order = 35
    auth_level = 2

    _enabled = False
    _client: Optional[EmbyActorClient] = None
    _thread: Optional[threading.Thread] = None
    _run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        if not self._enabled:
            return
        try:
            if bool(config.get("use_mp_config", True)):
                url, api_key, user_id, server_name = self._load_moviepilot_emby(
                    str(config.get("media_server", "")).strip()
                )
                logger.info(f"#Emby演员中文化# 已读取MoviePilot媒体服务器 | 名称={self._safe_text(server_name)}")
            else:
                url = config.get("emby_url", "")
                api_key = config.get("emby_api_key", "")
                user_id = str(config.get("emby_user_id", "")).strip()
            self._client = EmbyActorClient(
                url,
                api_key,
                user_id,
                timeout=self._bounded_int(config.get("timeout", 30), 5, 120),
                verify_https=bool(config.get("verify_https", True)),
            )
            if str(url).lower().startswith("https://") and not bool(config.get("verify_https", True)):
                logger.warning("#Emby演员中文化# HTTPS证书校验已关闭，仅应在可信内网使用")
            if bool(config.get("run_once", False)):
                config["run_once"] = False
                self.update_config(config)
                self._thread = threading.Thread(
                    target=self.run_test,
                    args=(config,),
                    name="emby-actor-chinese-worker",
                    daemon=True,
                )
                self._thread.start()
            logger.info("Emby演员中文化：插件已启用")
        except Exception as exc:
            self._enabled = False
            if self._client:
                self._client.close()
            self._client = None
            logger.error(f"Emby演员中文化：初始化失败 [{self._safe_code(exc)}]")

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
    def _safe_code(exc: Exception) -> str:
        code = getattr(exc, "code", "") or str(exc)
        return code if re.fullmatch(r"[A-Z0-9_]{3,64}", str(code)) else type(exc).__name__.upper()

    @staticmethod
    def _safe_text(value: Any, limit: int = 200) -> str:
        return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]

    @staticmethod
    def _normalize_name(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
        return "".join(char for char in text if char.isalnum())

    @staticmethod
    def _latin_tokens(value: Any) -> List[str]:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char))
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
        return [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", text) if token]

    @classmethod
    def _latin_order_keys(cls, value: Any) -> set:
        """生成姓氏前置/后置的有限变体，不进行模糊相似度猜测。"""
        tokens = cls._latin_tokens(value)
        if not tokens:
            return set()
        variants = {"".join(tokens)}
        if len(tokens) >= 2:
            variants.add("".join(tokens[-1:] + tokens[:-1]))
            variants.add("".join(tokens[1:] + tokens[:1]))
        return {key for key in variants if len(key) >= 4}

    @staticmethod
    def _contains_cjk(value: Any) -> bool:
        return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(value or "")))

    @classmethod
    def build_actor_mapping(cls, people: List[dict], credits: List[Any]) -> Tuple[List[dict], List[dict]]:
        updated, changes, _stats = cls._build_actor_mapping_detailed(people, credits)
        return updated, changes

    @classmethod
    def _build_actor_mapping_detailed(
        cls, people: List[dict], credits: List[Any]
    ) -> Tuple[List[dict], List[dict], Dict[str, int]]:
        """按唯一拉丁名及有限姓名顺序变体匹配；绝不按演员列表顺序套用。"""
        exact_aliases: Dict[str, set] = {}
        order_aliases: Dict[str, set] = {}
        latin_credit_count = 0
        for credit in credits[:500]:
            getter = credit.get if isinstance(credit, dict) else lambda key, default=None: getattr(credit, key, default)
            chinese = str(getter("name", "") or "").strip()
            candidates = [str(getter("latin_name", "") or "").strip()]
            aliases = getter("also_known_as", []) or []
            if isinstance(aliases, list):
                candidates.extend(str(alias or "").strip() for alias in aliases[:20])
            candidates = [name for name in candidates if name and not cls._contains_cjk(name)]
            if not cls._contains_cjk(chinese) or not candidates:
                continue
            latin_credit_count += 1
            for latin in candidates:
                key = cls._normalize_name(latin)
                if key:
                    exact_aliases.setdefault(key, set()).add(chinese)
                for order_key in cls._latin_order_keys(latin):
                    order_aliases.setdefault(order_key, set()).add(chinese)

        updated = copy.deepcopy(people)
        changes: List[dict] = []
        stats = {
            "emby_actors": 0, "emby_english": 0, "douban_credits": min(len(credits), 500),
            "douban_latin": latin_credit_count, "exact": 0, "order_variant": 0,
            "ambiguous": 0, "unmatched": 0,
        }
        for index, person in enumerate(updated[:500]):
            if not isinstance(person, dict) or str(person.get("Type") or "").casefold() != "actor":
                continue
            stats["emby_actors"] += 1
            current = str(person.get("Name") or "").strip()
            if not current or cls._contains_cjk(current):
                continue
            stats["emby_english"] += 1
            method = "exact"
            targets = exact_aliases.get(cls._normalize_name(current), set())
            if not targets:
                method = "order_variant"
                targets = set()
                for key in cls._latin_order_keys(current):
                    targets.update(order_aliases.get(key, set()))
            if len(targets) != 1:
                stats["ambiguous" if len(targets) > 1 else "unmatched"] += 1
                continue
            target = next(iter(targets))
            if target == current:
                continue
            person["Name"] = target
            stats[method] += 1
            changes.append({"index": index, "from": current[:200], "to": target[:200], "method": method})
        return updated, changes, stats

    @classmethod
    def select_exact_item(cls, items: List[dict], title: str, year: int) -> dict:
        wanted = cls._normalize_name(title)
        matches = []
        for item in items:
            names = {cls._normalize_name(item.get("Name")), cls._normalize_name(item.get("OriginalTitle"))}
            try:
                item_year = int(item.get("ProductionYear"))
            except (TypeError, ValueError, OverflowError):
                continue
            if wanted in names and item_year == year and str(item.get("Type") or "") in {"Movie", "Series"}:
                matches.append(item)
        if not matches:
            raise ValueError("EMBY_ITEM_NOT_FOUND")
        ids = {str(item.get("Id") or "") for item in matches}
        if len(matches) != 1 or len(ids) != 1:
            raise ValueError("EMBY_ITEM_AMBIGUOUS")
        return matches[0]

    @staticmethod
    def _douban_value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    def _load_douban_credits(self, title: str, year: int, emby_type: str) -> Tuple[str, str, List[Any]]:
        try:
            from app.chain.douban import DoubanChain
            from app.schemas import MediaType
        except (ImportError, AttributeError) as exc:
            raise ValueError("DOUBAN_MODULE_UNAVAILABLE") from exc
        media_type = MediaType.MOVIE if emby_type == "Movie" else MediaType.TV
        chain = DoubanChain()
        match = chain.run_module(
            "match_doubaninfo",
            name=title,
            mtype=media_type,
            year=str(year),
            raise_exception=True,
        ) or {}
        douban_id = str(self._douban_value(match, "id", "") or "").strip()
        matched_title = str(self._douban_value(match, "title", "") or "").strip()
        matched_year = str(self._douban_value(match, "year", "") or "").strip()
        if not douban_id or matched_year != str(year):
            raise ValueError("DOUBAN_ITEM_NOT_FOUND")
        credits = chain.movie_credits(douban_id) if emby_type == "Movie" else chain.tv_credits(douban_id)
        if not isinstance(credits, list) or not credits:
            raise ValueError("DOUBAN_CREDITS_EMPTY")
        return douban_id, matched_title or title, credits

    def run_test(self, config: dict):
        if not self._client or not self._run_lock.acquire(blocking=False):
            logger.warning("#Emby演员中文化# 当前已有任务运行，本次跳过")
            return
        action = str(config.get("action", "preview")).strip().lower()
        started = datetime.now()
        try:
            if action not in {"preview", "sync"}:
                raise ValueError("ACTION_INVALID")
            title = self._safe_text(config.get("title", ""), 200)
            if not title:
                raise ValueError("TITLE_REQUIRED")
            year = self._bounded_int(config.get("year", 0), 1888, 2100)
            item_type = str(config.get("media_type", "auto")).strip().lower()
            if item_type not in {"auto", "movie", "series"}:
                raise ValueError("MEDIA_TYPE_INVALID")
            logger.info(f"#Emby演员中文化# 开始{('预览' if action == 'preview' else '同步')} | 影视={title} | 年份={year}")
            summary = self._client.search_items(title, item_type)
            selected = self.select_exact_item(summary, title, year)
            item_id = str(selected.get("Id") or "")
            detail = self._client.get_item(item_id)
            douban_id, douban_title, credits = self._load_douban_credits(title, year, str(selected.get("Type")))
            provider_ids = detail.get("ProviderIds") or {}
            if isinstance(provider_ids, dict):
                emby_douban_id = next(
                    (str(value).strip() for key, value in provider_ids.items()
                     if str(key).casefold() == "douban" and str(value).strip()),
                    "",
                )
                if emby_douban_id and emby_douban_id != douban_id:
                    raise ValueError("DOUBAN_ID_MISMATCH")
            updated_people, changes, stats = self._build_actor_mapping_detailed(detail.get("People") or [], credits)
            logger.info(
                "#Emby演员中文化# 匹配统计 | "
                f"Emby演员={stats['emby_actors']} | Emby英文名={stats['emby_english']} | "
                f"豆瓣演员={stats['douban_credits']} | 豆瓣拉丁名={stats['douban_latin']} | "
                f"精确匹配={stats['exact']} | 姓名顺序变体={stats['order_variant']} | "
                f"歧义跳过={stats['ambiguous']} | 未匹配={stats['unmatched']}"
            )
            if not changes:
                raise ValueError("NO_SAFE_ACTOR_MATCH")
            for change in changes:
                logger.info(
                    f"#Emby演员中文化# 演员匹配 | {self._safe_text(change['from'])} => {self._safe_text(change['to'])}"
                )

            status = "预览完成"
            if action == "sync":
                original = copy.deepcopy(detail)
                updated = copy.deepcopy(detail)
                updated["People"] = updated_people
                self.save_data("last_backup", {
                    "time": started.strftime("%Y-%m-%d %H:%M:%S"),
                    "item_id": item_id,
                    "title": str(detail.get("Name") or title)[:200],
                    "year": year,
                    "people": original.get("People") or [],
                })
                self._client.update_item(item_id, updated)
                verified = self._client.get_item(item_id)
                actual = verified.get("People") or []
                if any(
                    change["index"] >= len(actual)
                    or not isinstance(actual[change["index"]], dict)
                    or actual[change["index"]].get("Name") != change["to"]
                    for change in changes
                ):
                    try:
                        self._client.update_item(item_id, original)
                    except Exception:
                        raise ValueError("VERIFY_FAILED_ROLLBACK_FAILED")
                    raise ValueError("VERIFY_FAILED_ROLLED_BACK")
                status = "同步成功"
            logger.info(
                f"#Emby演员中文化# {status} | Emby={self._safe_text(detail.get('Name') or title)} "
                f"| 豆瓣={self._safe_text(douban_title)} | 豆瓣ID={douban_id} | 演员修改={len(changes)}"
            )
            self._record(status, title, year, len(changes), changes, "OK")
        except Exception as exc:
            code = self._safe_code(exc)
            logger.error(f"#Emby演员中文化# 任务失败 [{code}]")
            self._record("失败", self._safe_text(config.get("title", "")), config.get("year", ""), 0, [], code)
        finally:
            self._run_lock.release()

    def _record(self, status: str, title: Any, year: Any, count: int, changes: List[dict], code: str):
        history = self.get_data("history") or []
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "title": self._safe_text(title),
            "year": str(year)[:4],
            "count": int(count),
            "changes": [f"{item['from']} → {item['to']}" for item in changes[:50]],
            "code": code,
        })
        self.save_data("history", history[:100])

    @staticmethod
    def _resolve_moviepilot_emby(helper: Any, selected_name: str = "") -> Tuple[str, str, str, str]:
        configs = helper.get_configs() or {}
        try:
            services = helper.get_services(type_filter="emby") or {}
        except TypeError:
            services = helper.get_services() or {}
        candidates = []
        for name, conf in configs.items():
            conf_type = getattr(conf, "type", "")
            conf_type = getattr(conf_type, "value", conf_type)
            if str(conf_type).lower() == "emby":
                candidates.append(name)
        if not candidates:
            raise ValueError("MP_EMBY_NOT_FOUND")
        name = selected_name or sorted(candidates)[0]
        if name not in candidates:
            raise ValueError("MP_EMBY_NOT_FOUND")
        service = services.get(name)
        conf = (getattr(service, "config", None) if service else None) or configs.get(name)
        values = getattr(conf, "config", None) or {}
        if not isinstance(values, dict):
            raise ValueError("MP_EMBY_CONFIG_INVALID")
        host = str(values.get("host") or "").strip()
        api_key = str(values.get("apikey") or "").strip()
        user_id = str(getattr(getattr(service, "instance", None), "user", "") or "").strip()
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
    def _moviepilot_emby_items() -> List[dict]:
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

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        content: List[dict] = [{"component": "VRow", "content": [{
            "component": "VCol", "props": {"cols": 12}, "content": [{
                "component": "VAlert", "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "测试版仅处理一部影视。默认“仅预览”不会写入Emby；确认匹配结果后再选择“确认同步”。只修改英文演员名，无法按豆瓣拉丁名唯一匹配的演员会跳过。",
                }
            }]
        }]}]
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "插件启用"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "run_once", "label": "立即运行一次"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "use_mp_config", "label": "读取MoviePilot的Emby配置"}}]},
        ]})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "title", "label": "影视剧名称（要求与Emby名称完全一致）", "clearable": True}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VTextField", "props": {"model": "year", "label": "年份", "type": "number"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [{"component": "VSelect", "props": {"model": "media_type", "label": "类型", "items": [{"title": "自动", "value": "auto"}, {"title": "电影", "value": "movie"}, {"title": "电视剧", "value": "series"}], "clearable": False}}]},
        ]})
        content.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VSelect", "props": {
            "model": "action", "label": "本次动作", "items": [{"title": "仅预览（不写入）", "value": "preview"}, {"title": "确认同步到Emby", "value": "sync"}], "clearable": False,
        }}]}]})
        content.append({"component": "VRow", "props": {"show": "{{use_mp_config}}"}, "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VSelect", "props": {"model": "media_server", "label": "MoviePilot Emby服务器（留空使用第一个）", "items": self._moviepilot_emby_items(), "clearable": True}}]}]})
        for model, label, field_type in (
            ("emby_url", "手动Emby地址", "text"),
            ("emby_api_key", "手动Emby API Key", "password"),
            ("emby_user_id", "手动Emby用户ID（可留空）", "text"),
        ):
            content.append({"component": "VRow", "props": {"show": "{{!use_mp_config}}"}, "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": {"model": model, "label": label, "type": field_type, "clearable": model != "emby_api_key"}}]}]})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "verify_https", "label": "校验HTTPS证书"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "timeout", "label": "请求超时秒数（5-120）", "type": "number"}}]},
        ]})
        return [{"component": "VForm", "content": content}], {
            "enabled": False, "run_once": False, "use_mp_config": True, "media_server": "",
            "title": "", "year": datetime.now().year, "media_type": "auto", "action": "preview",
            "emby_url": "", "emby_api_key": "", "emby_user_id": "", "verify_https": True, "timeout": 30,
        }

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        rows = []
        for item in history[:100]:
            rows.append([
                item.get("time"), item.get("status"), item.get("title"), item.get("year"),
                item.get("count"), "；".join(item.get("changes") or []), item.get("code"),
            ])
        return [{"component": "VTable", "props": {"hover": True}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": title} for title in ("时间", "状态", "影视", "年份", "修改数", "演员变更", "代码")]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [{"component": "td", "text": str(value if value is not None else "")} for value in row]} for row in rows]},
        ]}]

    def get_service(self):
        return None

    def get_api(self):
        return None

    def get_command(self):
        return None

    def stop_service(self):
        self._enabled = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        if self._client:
            self._client.close()
        self._client = None
