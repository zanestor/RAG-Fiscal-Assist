from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


STATE_VERSION = 1
REPLACE_MAX_ATTEMPTS = 8
REPLACE_RETRY_DELAY_SECONDS = 0.25


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "vector_store_id": None,
        "vector_store_name": "RDC Fiscal Reference",
        "documents": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_state()
    if data.get("version") != STATE_VERSION or not isinstance(data.get("documents"), dict):
        return empty_state()
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix="state_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temporary:
            json.dump(state, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        # On Windows, os.replace() can transiently raise PermissionError (WinError 5)
        # if another process - a concurrent reader, antivirus, OneDrive sync - briefly
        # has the target file open at that exact instant. This is a race, not a real
        # conflict, and normally clears within milliseconds; a long-running indexing
        # job saves state after every single document, so without a retry here, one
        # unlucky concurrent read anywhere on the system crashes the whole run despite
        # every prior save having already succeeded.
        for attempt in range(REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(temporary_name, path)
                break
            except PermissionError:
                if attempt == REPLACE_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_RETRY_DELAY_SECONDS)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)

