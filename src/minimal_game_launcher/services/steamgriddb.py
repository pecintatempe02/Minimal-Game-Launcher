"""Minimal SteamGridDB client used only for explicit online cover searches.

The provider is optional. An API key is read from the user-private settings
store (or ``STEAMGRIDDB_API_KEY``); it is never embedded in source code. Only
the game name is sent to the provider. Executable paths, working directories,
Wine prefixes, and library records are never uploaded.

Search requests return cover metadata. Image bytes are fetched only when the
user selects a result for preview and when the user explicitly applies a cover.
Every HTTP response is checked for status, content type, and a bounded byte
size; cover dimensions and decodability are validated by ``CoverManager``.
All calls are synchronous and are intended to run on a worker thread so the GTK
main loop stays responsive. Network and API failures become readable typed
errors, never tracebacks.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, ContextManager

from minimal_game_launcher import APP_VERSION
from minimal_game_launcher.errors import (
    SteamGridDBAuthError,
    SteamGridDBError,
    SteamGridDBNotConfiguredError,
    SteamGridDBOfflineError,
)

LOGGER = logging.getLogger(__name__)

API_BASE_URL = "https://www.steamgriddb.com/api/v2"
USER_AGENT = f"MinimalGameLauncher/{APP_VERSION}"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SEARCH_GAMES = 3
MAX_COVER_RESULTS = 24
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})

UrlOpenFactory = Callable[..., ContextManager[object]]


@dataclass(frozen=True, slots=True)
class CoverSearchResult:
    """One selectable cover returned by SteamGridDB."""

    game_id: int
    game_name: str
    url: str
    thumb_url: str
    width: int | None
    height: int | None

    @property
    def dimensions(self) -> str:
        """Return a human-readable dimensions label for the results list."""
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "Unknown size"


class SteamGridDBClient:
    """Search SteamGridDB and download selected covers with strict validation."""

    def __init__(
        self,
        *,
        base_url: str = API_BASE_URL,
        timeout: float = 15.0,
        urlopen_factory: UrlOpenFactory | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._urlopen = urlopen_factory or urllib.request.urlopen

    def search_covers(
        self, game_name: str, api_key: str | None
    ) -> list[CoverSearchResult]:
        """Return covers for games matching ``game_name``.

        The API key must be supplied by the caller from local settings. Only
        the search term is transmitted. Results from several matching games
        are aggregated so the user can pick a cover, not an automatic match.
        """
        key = (api_key or "").strip()
        if not key:
            raise SteamGridDBNotConfiguredError(
                "Online cover search needs a SteamGridDB API key. Add one in "
                "Settings, or select a local image instead."
            )

        query = game_name.strip()
        if not query:
            raise SteamGridDBError("A game name is required to search covers.")

        encoded_query = urllib.parse.quote(query, safe="")
        payload = self._get_json(
            f"{self._base_url}/search/autocomplete/{encoded_query}", key
        )
        games = payload.get("data")
        if not isinstance(games, list):
            raise SteamGridDBError(
                "SteamGridDB returned an unexpected search response."
            )

        results: list[CoverSearchResult] = []
        seen_urls: set[str] = set()
        for game in games[:MAX_SEARCH_GAMES]:
            if not isinstance(game, dict):
                continue
            game_id = game.get("id")
            matched_name = game.get("name")
            if not isinstance(game_id, int) or not isinstance(matched_name, str):
                continue

            try:
                grid_payload = self._get_json(
                    self._grids_url(game_id), key
                )
            except (SteamGridDBAuthError, SteamGridDBOfflineError):
                # Connectivity and credential failures affect every match, so
                # surface them instead of reporting an empty result list.
                raise
            except SteamGridDBError as error:
                # One malformed match should not discard covers from others.
                LOGGER.warning(
                    "Could not load SteamGridDB grids for %s: %s",
                    matched_name,
                    error,
                )
                continue

            grids = grid_payload.get("data")
            if not isinstance(grids, list):
                continue
            for grid in grids:
                result = self._to_result(game_id, matched_name, grid)
                if result is None or result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                results.append(result)
                if len(results) >= MAX_COVER_RESULTS:
                    return results

        return results

    def fetch_image(self, url: str) -> tuple[bytes, str]:
        """Download one image with content-type and size validation.

        The API key is deliberately not sent to image CDN URLs; it is only
        used for authenticated API requests.
        """
        content_type, data = self._request(
            url, max_bytes=MAX_IMAGE_BYTES, authenticated=False
        )
        if content_type not in _IMAGE_TYPES:
            raise SteamGridDBError(
                "The downloaded cover has an unsupported content type. "
                "Expected PNG, JPEG, or WebP."
            )
        return data, content_type

    def _grids_url(self, game_id: int) -> str:
        query = urllib.parse.urlencode(
            {
                "types": "static",
                "dimensions": "600x900,342x482,460x215,920x430",
            }
        )
        return f"{self._base_url}/grids/game/{game_id}?{query}"

    @staticmethod
    def _to_result(
        game_id: int, game_name: str, grid: object
    ) -> CoverSearchResult | None:
        if not isinstance(grid, dict):
            return None
        url = grid.get("url")
        if not isinstance(url, str) or not _is_https_url(url):
            return None
        thumb = grid.get("thumb")
        thumb_url = thumb if isinstance(thumb, str) and _is_https_url(thumb) else url
        width = grid.get("width")
        height = grid.get("height")
        return CoverSearchResult(
            game_id=game_id,
            game_name=game_name,
            url=url,
            thumb_url=thumb_url,
            width=width if isinstance(width, int) else None,
            height=height if isinstance(height, int) else None,
        )

    def _get_json(self, url: str, api_key: str) -> dict:
        content_type, data = self._request(
            url,
            max_bytes=MAX_JSON_BYTES,
            authenticated=True,
            api_key=api_key,
        )
        if content_type not in {"application/json", "text/json"}:
            raise SteamGridDBError(
                "SteamGridDB returned an unexpected content type."
            )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SteamGridDBError(
                "SteamGridDB returned an unreadable response."
            ) from error
        if not isinstance(payload, dict):
            raise SteamGridDBError(
                "SteamGridDB returned an unexpected response."
            )
        if payload.get("success") is False:
            message = payload.get("errors")
            detail = f" ({message})" if message else ""
            raise SteamGridDBError(
                f"SteamGridDB rejected the request{detail}."
            )
        return payload

    def _request(
        self,
        url: str,
        *,
        max_bytes: int,
        authenticated: bool,
        api_key: str | None = None,
    ) -> tuple[str, bytes]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SteamGridDBError("SteamGridDB returned an invalid URL.")

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, image/png, image/jpeg, image/webp",
        }
        if authenticated and api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                status = _response_status(response)
                if status < 200 or status >= 300:
                    raise SteamGridDBError(
                        f"SteamGridDB returned HTTP status {status}."
                    )
                content_type = _response_content_type(response)
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise SteamGridDBAuthError(
                    "SteamGridDB rejected the API key. Check the key in "
                    "Settings."
                ) from error
            if error.code == 429:
                raise SteamGridDBError(
                    "SteamGridDB rate limit reached. Try again later."
                ) from error
            raise SteamGridDBError(
                f"SteamGridDB returned HTTP status {error.code}."
            ) from error
        except urllib.error.URLError as error:
            LOGGER.warning("SteamGridDB request failed: %s", error)
            raise SteamGridDBOfflineError(
                "Could not reach SteamGridDB. Check the internet connection "
                "and try again."
            ) from error
        except (TimeoutError, OSError) as error:
            LOGGER.warning("SteamGridDB request failed: %s", error)
            raise SteamGridDBOfflineError(
                "The SteamGridDB request timed out or the network is "
                "unavailable."
            ) from error
        except SteamGridDBError:
            raise
        except Exception as error:
            LOGGER.exception("Unexpected SteamGridDB request failure")
            raise SteamGridDBError(
                "The SteamGridDB request failed unexpectedly."
            ) from error

        if not isinstance(data, bytes):
            data = bytes(data)
        if len(data) > max_bytes:
            raise SteamGridDBError(
                "The SteamGridDB response is larger than the allowed limit."
            )
        return content_type, data


def _is_https_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _response_status(response: object) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status)


def _response_content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    get_content_type = getattr(headers, "get_content_type", None)
    if callable(get_content_type):
        return str(get_content_type()).split(";", 1)[0].strip().lower()
    raw = headers.get("Content-Type", "")
    return str(raw).split(";", 1)[0].strip().lower()
