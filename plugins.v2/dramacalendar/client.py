from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

import requests

from .cache import ShowCache


@dataclass(frozen=True)
class LibrarySeries:
    media_id: str
    name: str
    year: Optional[int]
    tmdb_id: str


@dataclass(frozen=True)
class EpisodeUpdate:
    air_date: date
    series_name: str
    season: int
    episode: int
    media_series_id: str
    in_library: bool = False


class ClientError(RuntimeError):
    """对外服务请求失败。"""


class MediaServerClient:
    def __init__(self, base_url: str, api_key: str, user_id: str = "", verify: bool = True):
        self._base_url = base_url.rstrip("/")
        self._user_id_value = user_id.strip()
        self._verify = verify
        self._session = requests.Session()
        self._session.headers.update({"X-Emby-Token": api_key})

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        try:
            response = self._session.get(
                f"{self._base_url}{path}", params=params, timeout=(10, 30), verify=self._verify
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ClientError(f"MEDIA_SERVER_{type(exc).__name__.upper()}") from exc

    def user_id(self) -> str:
        """返回配置用户ID，未配置时从媒体服务器选择第一个用户。"""
        if self._user_id_value:
            return self._user_id_value
        users = self._get("/Users")
        if not isinstance(users, list) or not users:
            raise ClientError("MEDIA_SERVER_USER_NOT_FOUND")
        self._user_id_value = str(users[0].get("Id") or "").strip()
        if not self._user_id_value:
            raise ClientError("MEDIA_SERVER_USER_INVALID")
        return self._user_id_value

    def continuing_series(self) -> List[LibrarySeries]:
        """分页读取带TMDB ID且未明确完结的剧集。"""
        user_id = self.user_id()
        start = 0
        result: List[LibrarySeries] = []
        while True:
            payload = self._get(
                f"/Users/{user_id}/Items",
                {
                    "IncludeItemTypes": "Series",
                    "Recursive": "true",
                    "Fields": "ProviderIds,Status",
                    "StartIndex": start,
                    "Limit": 200,
                },
            )
            if not isinstance(payload, dict):
                raise ClientError("MEDIA_SERVER_RESPONSE_INVALID")
            items = payload.get("Items") or []
            for item in items:
                provider_ids = item.get("ProviderIds") or {}
                tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TMDB")
                status = str(item.get("Status") or "").lower()
                if tmdb_id and status not in {"ended", "cancelled", "canceled"}:
                    result.append(
                        LibrarySeries(
                            media_id=str(item.get("Id") or ""),
                            name=str(item.get("Name") or "未知剧集"),
                            year=item.get("ProductionYear"),
                            tmdb_id=str(tmdb_id),
                        )
                    )
            start += len(items)
            total = int(payload.get("TotalRecordCount") or start)
            if not items or start >= total:
                return result

    def available_episodes(self, series_id: str) -> Set[Tuple[int, int]]:
        """读取指定剧集已经实际入库的季集编号。"""
        payload = self._get(
            f"/Shows/{series_id}/Episodes",
            {"UserId": self.user_id(), "Fields": "LocationType,IsMissing"},
        )
        if not isinstance(payload, dict):
            raise ClientError("MEDIA_SERVER_RESPONSE_INVALID")
        available: Set[Tuple[int, int]] = set()
        for item in payload.get("Items") or []:
            if item.get("IsMissing") or item.get("LocationType") == "Virtual":
                continue
            season = item.get("ParentIndexNumber")
            episode = item.get("IndexNumber")
            if season is not None and episode is not None:
                available.add((int(season), int(episode)))
        return available

    def close(self) -> None:
        """关闭媒体服务器HTTP会话。"""
        self._session.close()


class TmdbClient:
    def __init__(
        self,
        token: str,
        requests_per_second: float,
        max_retries: int,
        cache: ShowCache,
        api_domain: str = "api.themoviedb.org",
    ):
        self._token = token.strip()
        self._base_url = f"https://{api_domain.strip().strip('/')}" + "/3"
        self._interval = 1 / requests_per_second
        self._max_retries = max_retries
        self._cache = cache
        self._next_request_at = 0.0
        self._validated = False
        self.cache_hits = 0
        self.network_requests = 0
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._is_bearer = self._token.startswith("eyJ") or self._token.count(".") == 2
        if self._is_bearer:
            self._session.headers.update({"Authorization": f"Bearer {self._token}"})

    def _wait(self) -> None:
        delay = self._next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next_request_at = time.monotonic() + self._interval

    def _get(self, path: str, language: bool = True) -> Optional[dict]:
        for attempt in range(self._max_retries + 1):
            self._wait()
            params: Dict[str, str] = {}
            if not self._is_bearer:
                params["api_key"] = self._token
            if language:
                params["language"] = "zh-CN"
            self.network_requests += 1
            try:
                response = self._session.get(
                    f"{self._base_url}{path}", params=params, timeout=(10, 30)
                )
            except requests.RequestException as exc:
                raise ClientError(f"TMDB_{type(exc).__name__.upper()}") from exc
            if response.status_code == 401:
                raise ClientError("TMDB_AUTH_FAILED")
            if response.status_code == 404:
                return None
            if response.status_code == 429 and attempt < self._max_retries:
                value = response.headers.get("Retry-After", "")
                try:
                    delay = float(value) if value else min(2 ** attempt, 30)
                except ValueError:
                    delay = min(2 ** attempt, 30)
                time.sleep(max(delay, 0))
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ClientError(f"TMDB_{response.status_code or type(exc).__name__.upper()}") from exc
            if not isinstance(payload, dict):
                raise ClientError("TMDB_RESPONSE_INVALID")
            return payload
        raise ClientError("TMDB_RATE_LIMITED")

    def validate(self) -> None:
        """验证TMDB凭据，每个运行实例只验证一次。"""
        if not self._validated:
            self._get("/configuration", language=False)
            self._validated = True

    def _cached_get(self, key: str, path: str) -> Optional[dict]:
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        payload = self._get(path)
        if payload is not None:
            self._cache.set(key, payload)
        return payload

    def updates_for(self, series: LibrarySeries, start: date, end: date) -> List[EpisodeUpdate]:
        """查询单部剧集在指定日期范围内的播出排期。"""
        show = self._cached_get(f"show:{series.tmdb_id}", f"/tv/{series.tmdb_id}")
        if not show or show.get("status") in {"Ended", "Canceled"}:
            return []
        season_numbers = set()
        for item in (show.get("next_episode_to_air"), show.get("last_episode_to_air")):
            if item and item.get("season_number") is not None:
                season_numbers.add(int(item["season_number"]))
        if show.get("number_of_seasons"):
            season_numbers.add(int(show["number_of_seasons"]))
        updates: Dict[Tuple[date, int, int], EpisodeUpdate] = {}
        for season in sorted(season_numbers):
            payload = self._cached_get(
                f"season:{series.tmdb_id}:{season}", f"/tv/{series.tmdb_id}/season/{season}"
            )
            for item in (payload or {}).get("episodes") or []:
                air_value = item.get("air_date")
                if not air_value:
                    continue
                try:
                    air_date = date.fromisoformat(str(air_value))
                except ValueError:
                    continue
                if start <= air_date <= end:
                    episode = EpisodeUpdate(
                        air_date=air_date,
                        series_name=series.name,
                        season=int(item.get("season_number", season)),
                        episode=int(item.get("episode_number", 0)),
                        media_series_id=series.media_id,
                    )
                    updates[(air_date, episode.season, episode.episode)] = episode
        return list(updates.values())

    def close(self) -> None:
        """关闭TMDB HTTP会话。"""
        self._session.close()
