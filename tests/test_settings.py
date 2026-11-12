"""Tests for optional API-key storage and its file permissions."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from minimal_game_launcher.settings import LauncherSettings, SettingsStore


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config" / "settings.json")


def test_api_key_is_stored_in_a_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEAMGRIDDB_API_KEY", raising=False)
    store = _store(tmp_path)

    store.save_api_key("abc123")

    assert store.api_key_path.is_file()
    assert store.api_key_path.read_text(encoding="utf-8").strip() == "abc123"
    assert stat.S_IMODE(store.api_key_path.stat().st_mode) == 0o600
    assert store.load_api_key() == "abc123"
    # The key is deliberately not part of the JSON settings file.
    assert not store.path.exists()


def test_api_key_is_not_world_readable_when_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEAMGRIDDB_API_KEY", raising=False)
    store = _store(tmp_path)
    store.api_key_path.parent.mkdir(parents=True)
    store.api_key_path.write_text("old\n", encoding="utf-8")
    store.api_key_path.chmod(0o644)

    store.save_api_key("new")

    assert stat.S_IMODE(store.api_key_path.stat().st_mode) == 0o600
    assert store.load_api_key() == "new"


def test_empty_api_key_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEAMGRIDDB_API_KEY", raising=False)
    store = _store(tmp_path)
    store.save_api_key("secret")

    store.save_api_key("   ")

    assert not store.api_key_path.exists()


def test_environment_key_is_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEAMGRIDDB_API_KEY", "from-environment")
    store = _store(tmp_path)

    assert store.load_api_key() == "from-environment"


def test_private_file_takes_precedence_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEAMGRIDDB_API_KEY", "from-environment")
    store = _store(tmp_path)
    store.save_api_key("from-file")

    assert store.load_api_key() == "from-file"


def test_stored_key_ignores_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEAMGRIDDB_API_KEY", "from-environment")
    store = _store(tmp_path)

    assert store.load_stored_api_key() is None

    store.save_api_key("from-file")
    assert store.load_stored_api_key() == "from-file"


def test_missing_key_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEAMGRIDDB_API_KEY", raising=False)

    assert _store(tmp_path).load_api_key() is None


def test_custom_api_key_path_is_honored(tmp_path: Path) -> None:
    store = SettingsStore(
        tmp_path / "config" / "settings.json",
        api_key_path=tmp_path / "secrets" / "key",
    )

    store.save_api_key("custom")

    assert store.api_key_path == tmp_path / "secrets" / "key"
    assert store.load_api_key() == "custom"


def test_setup_completed_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.load().setup_completed is False
    store.save(LauncherSettings(setup_completed=True))
    assert store.load().setup_completed is True


def test_invalid_setup_flag_falls_back_to_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"setup_completed": "yes"}', encoding="utf-8")

    assert store.load().setup_completed is False
