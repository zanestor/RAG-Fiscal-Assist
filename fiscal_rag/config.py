from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    """Load a minimal .env file without overriding the process environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = os.path.expandvars(value.strip().strip('"').strip("'"))
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class SourceConfig:
    id: str
    label: str
    description: str
    paths: tuple[Path, ...]
    catalogs: tuple[Path, ...]
    enabled: bool = True
    extensions: tuple[str, ...] = (".pdf",)
    review_extensions: tuple[str, ...] = ()
    review_all: bool = False
    excluded_filenames: tuple[str, ...] = ()
    superseded_filenames: tuple[tuple[str, str], ...] = ()
    title_overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Settings:
    app_dir: Path
    repository_root: Path
    data_dir: Path
    static_dir: Path
    sources: tuple[SourceConfig, ...]
    model: str
    reasoning_effort: str
    port: int
    api_key: str | None
    provider: str = "auto"
    openai_model: str = "gpt-5.6-terra"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-5.6-terra"
    fallback_enabled: bool = True
    openrouter_data_collection: str = "deny"
    openrouter_zdr: bool = False
    openai_retry_seconds: int = 300
    history_enabled: bool = True
    rerank_enabled: bool = True

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"

    @property
    def local_index_path(self) -> Path:
        return self.data_dir / "local_retrieval.sqlite3"

    @property
    def legal_graph_path(self) -> Path:
        return self.data_dir / "legal_graph.sqlite3"

    @property
    def history_path(self) -> Path:
        return self.data_dir / "chat_history.sqlite3"

    @property
    def indexing_lock_path(self) -> Path:
        return self.data_dir / "indexing.lock"

    @property
    def learned_notes_path(self) -> Path:
        return self.data_dir / "learned_notes.md"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _resolve_config_path(repository_root: Path, value: str) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    return (expanded if expanded.is_absolute() else repository_root / expanded).resolve()


def get_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(APP_DIR / ".env")
    config_path = config_path or APP_DIR / "config" / "sources.json"
    config: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))

    root_value = Path(config.get("repository_root", "../.."))
    repository_root = root_value if root_value.is_absolute() else (APP_DIR / root_value)
    repository_root = repository_root.resolve()

    sources: list[SourceConfig] = []
    for item in config.get("sources", []):
        sources.append(
            SourceConfig(
                id=str(item["id"]),
                label=str(item.get("label", item["id"])),
                description=str(item.get("description", "")),
                paths=tuple(
                    _resolve_config_path(repository_root, str(value))
                    for value in item.get("paths", [])
                ),
                catalogs=tuple(
                    _resolve_config_path(repository_root, str(value))
                    for value in item.get("catalogs", [])
                ),
                enabled=bool(item.get("enabled", True)),
                extensions=tuple(
                    extension if str(extension).startswith(".") else f".{extension}"
                    for extension in (str(value).lower() for value in item.get("extensions", [".pdf"]))
                ),
                review_extensions=tuple(
                    extension if str(extension).startswith(".") else f".{extension}"
                    for extension in (str(value).lower() for value in item.get("review_extensions", []))
                ),
                review_all=bool(item.get("review_all", False)),
                excluded_filenames=tuple(str(value) for value in item.get("exclude", [])),
                superseded_filenames=tuple(
                    (str(filename), str(note)) for filename, note in dict(item.get("superseded", {})).items()
                ),
                title_overrides=tuple(
                    (str(filename), str(title)) for filename, title in dict(item.get("title_overrides", {})).items()
                ),
            )
        )

    data_value = os.getenv("FISCAL_RAG_DATA_DIR")
    data_dir = Path(os.path.expandvars(data_value)).expanduser().resolve() if data_value else APP_DIR / "rag_data"

    provider = os.getenv("FISCAL_RAG_PROVIDER", "auto").strip().casefold()
    if provider not in {"auto", "openai", "openrouter"}:
        raise ValueError("FISCAL_RAG_PROVIDER must be auto, openai, or openrouter.")
    legacy_model = os.getenv("FISCAL_RAG_MODEL", "").strip()
    inferred_openai_model = legacy_model.split("/", 1)[-1] if legacy_model else "gpt-5.6-terra"
    inferred_openrouter_model = legacy_model if "/" in legacy_model else f"openai/{inferred_openai_model}"
    data_collection = os.getenv("FISCAL_RAG_OPENROUTER_DATA_COLLECTION", "deny").strip().casefold()
    if data_collection not in {"allow", "deny"}:
        raise ValueError("FISCAL_RAG_OPENROUTER_DATA_COLLECTION must be allow or deny.")

    openai_model = os.getenv("FISCAL_RAG_OPENAI_MODEL", inferred_openai_model)
    openrouter_model = os.getenv("FISCAL_RAG_OPENROUTER_MODEL", inferred_openrouter_model)
    display_model = openai_model if provider == "openai" else openrouter_model

    return Settings(
        app_dir=APP_DIR,
        repository_root=repository_root,
        data_dir=data_dir,
        static_dir=APP_DIR / "static",
        sources=tuple(sources),
        model=display_model,
        reasoning_effort=os.getenv("FISCAL_RAG_REASONING", "medium"),
        port=int(os.getenv("FISCAL_RAG_PORT", "8010")),
        api_key=os.getenv("OPENAI_API_KEY") or None,
        provider=provider,
        openai_model=openai_model,
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        openrouter_model=openrouter_model,
        fallback_enabled=env_bool("FISCAL_RAG_FALLBACK_ENABLED", True),
        openrouter_data_collection=data_collection,
        openrouter_zdr=env_bool("FISCAL_RAG_OPENROUTER_ZDR", False),
        openai_retry_seconds=max(0, int(os.getenv("FISCAL_RAG_OPENAI_RETRY_SECONDS", "300"))),
        history_enabled=env_bool("FISCAL_RAG_HISTORY_ENABLED", True),
        rerank_enabled=env_bool("FISCAL_RAG_RERANK_ENABLED", True),
    )
