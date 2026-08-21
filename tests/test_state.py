from pathlib import Path

import pytest

import fiscal_rag.state as state_module
from fiscal_rag.state import empty_state, load_state, save_state


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = empty_state()
    state["documents"]["abc"] = {"title": "Test document"}

    save_state(path, state)
    loaded = load_state(path)

    assert loaded["documents"]["abc"]["title"] == "Test document"


def test_load_missing_or_corrupt_file_returns_empty_state(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") == empty_state()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    assert load_state(corrupt) == empty_state()


def test_save_state_retries_through_a_transient_windows_permission_error(tmp_path, monkeypatch) -> None:
    """Reproduces the exact crash a long OCR run hit: os.replace() raising
    PermissionError (WinError 5) because something else had the file briefly open -
    e.g. a concurrent status check reading state.json mid-write. The save must
    survive a few transient failures rather than losing an otherwise-successful run."""
    path = tmp_path / "state.json"
    real_replace = state_module.os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(state_module.os, "replace", flaky_replace)
    monkeypatch.setattr(state_module.time, "sleep", lambda _seconds: None)

    save_state(path, empty_state())

    assert calls["count"] == 3
    assert load_state(path) == empty_state()


def test_save_state_gives_up_after_max_attempts(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(state_module.os, "replace", always_denied)
    monkeypatch.setattr(state_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        save_state(path, empty_state())

    # The failed attempt must not leave a stray temp file behind.
    assert list(tmp_path.iterdir()) == []
