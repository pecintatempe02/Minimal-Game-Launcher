"""Tests for optional SteamGridDB cover search and download validation."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from minimal_game_launcher.errors import (
    SteamGridDBAuthError,
    SteamGridDBError,
    SteamGridDBNotConfiguredError,
    SteamGridDBOfflineError,
)
from minimal_game_launcher.services.steamgriddb import (
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    SteamGridDBClient,
)


class FakeHeaders:
    """Minimal stand-in for ``http.client.HTTPMessage``."""

    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type

    def get(self, name: str, default: str | None = None) -> str | None:
        if name.casefold() == "content-type":
            return self._content_type
        return default


class FakeResponse:
    """Synchronous context manager matching the subset used by the client."""

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        status: int = 200,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = FakeHeaders(content_type)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self.body
        return self.body[:size]


class FakeUrlOpen:
    """Record requests and return queued responses or raise queued errors."""

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected extra SteamGridDB request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _json_response(payload: dict) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def _search_payload() -> dict:
    return {
        "success": True,
        "data": [{"id": 7, "name": "My Game"}],
    }


def _grids_payload(url: str = "https://cdn.example/cover.png") -> dict:
    return {
        "success": True,
        "data": [
            {
                "url": url,
                "thumb": "https://cdn.example/thumb.png",
                "width": 600,
                "height": 900,
            }
        ],
    }


def test_search_requires_a_configured_key() -> None:
    urlopen = FakeUrlOpen()

    client = SteamGridDBClient(urlopen_factory=urlopen)

    with pytest.raises(SteamGridDBNotConfiguredError, match="API key"):
        client.search_covers("My Game", None)
    assert urlopen.requests == []


def test_search_aggregates_covers_and_sends_only_the_query() -> None:
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(_grids_payload()),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    results = client.search_covers("My Game", "secret-key")

    assert len(results) == 1
    assert results[0].game_name == "My Game"
    assert results[0].url == "https://cdn.example/cover.png"
    assert results[0].dimensions == "600×900"

    autocomplete_request = urlopen.requests[0]
    grids_request = urlopen.requests[1]
    assert "search/autocomplete/My%20Game" in autocomplete_request.full_url
    assert "/grids/game/7" in grids_request.full_url
    # The key is sent only to the API host, never to image CDN URLs.
    assert autocomplete_request.get_header("Authorization") == "Bearer secret-key"
    # Search performs no image downloads; those happen only after selection.
    assert all(
        "search/autocomplete" in request.full_url
        or "/grids/game/" in request.full_url
        for request in urlopen.requests
    )


def test_search_does_not_include_library_paths() -> None:
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(_grids_payload()),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    client.search_covers("My Game", "secret-key")

    serialized = " ".join(request.full_url for request in urlopen.requests)
    assert "/home/" not in serialized
    assert "prefixes" not in serialized
    assert ".exe" not in serialized


def test_missing_api_key_error_does_not_contact_network() -> None:
    urlopen = FakeUrlOpen()
    client = SteamGridDBClient(urlopen_factory=urlopen)

    with pytest.raises(SteamGridDBNotConfiguredError):
        client.search_covers("Game", "   ")
    assert urlopen.requests == []


def test_rejected_api_key_raises_authentication_error() -> None:
    error = urllib.error.HTTPError(
        "https://www.steamgriddb.com/api/v2/search/autocomplete/Game",
        401,
        "Unauthorized",
        hdrs=None,
        fp=None,
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(error))

    with pytest.raises(SteamGridDBAuthError, match="rejected the API key"):
        client.search_covers("Game", "bad-key")


def test_offline_network_error_is_reported_as_offline() -> None:
    error = urllib.error.URLError("Network is unreachable")
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(error))

    with pytest.raises(SteamGridDBOfflineError, match="internet connection"):
        client.search_covers("Game", "key")


def test_offline_mid_search_is_not_reported_as_empty_results() -> None:
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        urllib.error.URLError("Network is unreachable"),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    with pytest.raises(SteamGridDBOfflineError):
        client.search_covers("Game", "key")


def test_timeout_is_reported_as_offline() -> None:
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(TimeoutError()))

    with pytest.raises(SteamGridDBOfflineError, match="timed out"):
        client.search_covers("Game", "key")


def test_server_error_reports_http_status() -> None:
    error = urllib.error.HTTPError(
        "https://www.steamgriddb.com/api/v2/search/autocomplete/Game",
        500,
        "Server Error",
        hdrs=None,
        fp=None,
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(error))

    with pytest.raises(SteamGridDBError, match="HTTP status 500"):
        client.search_covers("Game", "key")


def test_rate_limit_has_a_readable_error() -> None:
    error = urllib.error.HTTPError(
        "https://www.steamgriddb.com/api/v2/search/autocomplete/Game",
        429,
        "Too Many Requests",
        hdrs=None,
        fp=None,
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(error))

    with pytest.raises(SteamGridDBError, match="rate limit"):
        client.search_covers("Game", "key")


def test_unexpected_json_content_type_is_rejected() -> None:
    response = FakeResponse(b"<html></html>", content_type="text/html")
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="content type"):
        client.search_covers("Game", "key")


def test_unreadable_json_is_rejected() -> None:
    response = FakeResponse(
        b"{not json", content_type="application/json"
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="unreadable"):
        client.search_covers("Game", "key")


def test_api_failure_payload_is_rejected() -> None:
    response = _json_response(
        {"success": False, "errors": ["Invalid search term"]}
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="rejected the request"):
        client.search_covers("Game", "key")


def test_oversized_json_response_is_rejected() -> None:
    response = FakeResponse(
        b"x" * (MAX_JSON_BYTES + 1), content_type="application/json"
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="larger than"):
        client.search_covers("Game", "key")


def test_image_download_validates_content_type() -> None:
    response = FakeResponse(b"\x89PNG", content_type="image/gif")
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="unsupported content type"):
        client.fetch_image("https://cdn.example/cover.gif")


def test_image_download_does_not_send_api_key() -> None:
    urlopen = FakeUrlOpen(FakeResponse(b"\x89PNG", content_type="image/png"))
    client = SteamGridDBClient(urlopen_factory=urlopen)

    data, content_type = client.fetch_image("https://cdn.example/cover.png")

    assert data == b"\x89PNG"
    assert content_type == "image/png"
    request = urlopen.requests[0]
    assert request.get_header("Authorization") is None


def test_oversized_image_response_is_rejected() -> None:
    response = FakeResponse(
        b"x" * (MAX_IMAGE_BYTES + 1), content_type="image/png"
    )
    client = SteamGridDBClient(urlopen_factory=FakeUrlOpen(response))

    with pytest.raises(SteamGridDBError, match="larger than"):
        client.fetch_image("https://cdn.example/cover.png")


def test_non_https_images_are_not_returned() -> None:
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(_grids_payload("http://cdn.example/cover.png")),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    assert client.search_covers("Game", "key") == []


# ---------------------------------------------------------------------------
# Official API v2.10.0 OpenAPI conformance (verified 2026-11-04 against
# https://www.steamgriddb.com/static/openapi.yml).
#
# The documented Grid schema contains only id, score, style, url, thumb,
# tags, and author: there are no width or height fields. Mocked responses
# elsewhere in this file include width/height for legacy reasons; the tests
# below pin the client's tolerance of the *documented* shape.


def test_documented_grid_shape_without_width_height_still_works() -> None:
    """A response matching the official schema yields "Unknown size"."""
    grids_payload = {
        "success": True,
        "page": 0,
        "total": 1,
        "limit": 50,
        "data": [
            {
                "id": 12345,
                "score": 42,
                "style": "alternate",
                "url": "https://cdn2.steamgriddb.com/grid/abc123.png",
                "thumb": "https://cdn2.steamgriddb.com/thumb/abc123.png",
                "tags": {},
                "author": {
                    "name": "Some Author",
                    "steam64": "76561190000000000",
                    "avatar": "https://cdn2.steamgriddb.com/avatar.png",
                },
            }
        ],
    }
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(grids_payload),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    results = client.search_covers("My Game", "secret-key")

    assert len(results) == 1
    result = results[0]
    assert result.url == "https://cdn2.steamgriddb.com/grid/abc123.png"
    assert result.thumb_url == "https://cdn2.steamgriddb.com/thumb/abc123.png"
    assert result.width is None and result.height is None
    assert result.dimensions == "Unknown size"


def test_missing_thumb_falls_back_to_full_image_url() -> None:
    """thumb is optional in practice; results must keep a usable URL."""
    grids_payload = {
        "success": True,
        "data": [{"id": 1, "url": "https://cdn.example/cover.png"}],
    }
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(grids_payload),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    results = client.search_covers("My Game", "key")

    assert results[0].thumb_url == "https://cdn.example/cover.png"


def test_pagination_envelope_is_tolerated() -> None:
    """page/total/limit fields are ignored without breaking parsing."""
    grids_payload = {
        "success": True,
        "page": 0,
        "total": 150,
        "limit": 50,
        "data": [
            {"id": 1, "url": "https://cdn.example/a.png"},
            {"id": 2, "url": "https://cdn.example/b.png"},
        ],
    }
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(grids_payload),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)

    results = client.search_covers("My Game", "key")

    assert [result.url for result in results] == [
        "https://cdn.example/a.png",
        "https://cdn.example/b.png",
    ]


def test_live_verified_401_payload_is_understood() -> None:
    """Live probe (2026-11-04): 401 body {"success": false,
    "errors": ["Authentication Required"]} arrives as a raised HTTPError,
    which must map to SteamGridDBAuthError."""
    error = urllib.error.HTTPError(
        "https://www.steamgriddb.com/api/v2/search/autocomplete/portal",
        401,
        "Unauthorized",
        FakeHeaders("application/json"),
        io.BytesIO(
            json.dumps(
                {"success": False, "errors": ["Authentication Required"]}
            ).encode()
        ),
    )

    def raise_http_error(*args, **kwargs):
        raise error

    client = SteamGridDBClient(urlopen_factory=raise_http_error)

    with pytest.raises(SteamGridDBAuthError):
        client.search_covers("My Game", "invalid-key")


def test_invalid_key_format_error_is_reported() -> None:
    """Live probe (2026-11-04): a malformed key returns
    {"success": false, "errors": ["Invalid key format"]} with HTTP 401."""
    error = urllib.error.HTTPError(
        "https://www.steamgriddb.com/api/v2/search/autocomplete/portal",
        401,
        "Unauthorized",
        FakeHeaders("application/json"),
        io.BytesIO(
            json.dumps({"success": False, "errors": ["Invalid key format"]}).encode()
        ),
    )

    def raise_http_error(*args, **kwargs):
        raise error

    client = SteamGridDBClient(urlopen_factory=raise_http_error)

    with pytest.raises(SteamGridDBAuthError):
        client.search_covers("My Game", "dummy-invalid-key-abc123")


def test_search_request_url_matches_documented_endpoint() -> None:
    """Pin the documented endpoint structure: /search/autocomplete/{term}
    and /grids/game/{gameId} with documented filter names."""
    urlopen = FakeUrlOpen(
        _json_response(_search_payload()),
        _json_response(_grids_payload()),
    )
    client = SteamGridDBClient(urlopen_factory=urlopen)
    client.search_covers("My Game", "key")

    autocomplete_url = urlopen.requests[0].full_url
    grids_url = urlopen.requests[1].full_url
    assert "/api/v2/search/autocomplete/My%20Game" in autocomplete_url
    assert "/api/v2/grids/game/7?" in grids_url
    assert "types=static" in grids_url
    assert "dimensions=" in grids_url
    # Every requested dimension must exist in the official enum.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(grids_url).query)
    requested = query["dimensions"][0].split(",")
    assert set(requested) <= {
        "460x215", "920x430", "600x900", "342x482",
        "660x930", "512x512", "1024x1024",
    }
    assert query["types"] == ["static"]
