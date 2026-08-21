from __future__ import annotations

import csv
import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .config import Settings, SourceConfig


FILENAME_FIELDS = (
    "file_path",
    "pdf_filename",
    "file_name",
    "filename",
    "relative_path",
    "local_path",
    "path",
    "text_filename",
)
TITLE_FIELDS = ("full_name", "website_title", "title", "name")
URL_FIELDS = ("url", "source_url", "download_url")
CATEGORY_FIELDS = ("category", "folder", "type_label", "document_type", "storage_category")
DATE_FIELDS = (
    "date_publication",
    "upload_date",
    "website_published_at",
    "server_last_modified",
    "website_last_modified_at",
)


def first_value(row: dict[str, str], fields: Iterable[str]) -> str:
    for field in fields:
        value = (row.get(field) or "").strip()
        if value:
            return value
    return ""


def normalize_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name
    return unicodedata.normalize("NFKC", name).casefold().strip()


def read_catalog(path: Path) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except UnicodeDecodeError:
            continue
    return []


def catalog_index(source: SourceConfig) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for row in catalog_rows(source):
        filename = first_value(row, FILENAME_FIELDS)
        if filename:
            index.setdefault(normalize_filename(filename), row)
    return index


def catalog_rows(source: SourceConfig) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in source.catalogs:
        if path.is_file():
            rows.extend(read_catalog(path))
    return rows


def document_id(relative_path: str) -> str:
    return hashlib.sha256(relative_path.casefold().encode("utf-8")).hexdigest()[:20]


def title_from_stem(path: Path) -> str:
    title = re.sub(r"[_-]+", " ", path.stem)
    title = re.sub(r"\s+", " ", title).strip()
    return title or path.name


def iter_source_files(source_path: Path, extensions: tuple[str, ...]) -> Iterable[Path]:
    """Walk large OneDrive trees without repeating expensive per-file stat calls."""
    if source_path.is_file():
        if source_path.suffix.lower() in extensions:
            yield source_path
        return
    for directory, _, filenames in os.walk(source_path):
        for filename in filenames:
            if Path(filename).suffix.lower() in extensions:
                yield Path(directory) / filename


def iter_catalog_files(source: SourceConfig, rows: list[dict[str, str]]) -> Iterable[Path]:
    """Resolve catalogued files directly, avoiding slow cloud-directory traversal."""
    yielded: set[str] = set()
    for row in rows:
        raw_filename = first_value(row, FILENAME_FIELDS)
        if not raw_filename:
            continue
        relative_filename = Path(raw_filename.replace("\\", "/"))
        filename = relative_filename.name
        category = first_value(row, CATEGORY_FIELDS)
        for source_path in source.paths:
            if source_path.suffix.lower() in source.extensions:
                candidates = [source_path] if normalize_filename(source_path.name) == normalize_filename(filename) else []
            else:
                candidates = [source_path / relative_filename]
                if category:
                    candidates.append(source_path / category / filename)
                candidates.append(source_path / filename)
            match = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.suffix.lower() in source.extensions and candidate.is_file()
                ),
                None,
            )
            if match is None:
                continue
            key = os.path.abspath(match).casefold()
            if key not in yielded:
                yielded.add(key)
                yield match
            break


def discover_documents(
    settings: Settings,
    source_ids: set[str] | None = None,
    include_review_required: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for source in settings.sources:
        if not source.enabled:
            continue
        if source_ids and source.id not in source_ids:
            continue
        if source.review_all and not include_review_required:
            continue
        excluded = {normalize_filename(name) for name in source.excluded_filenames}
        superseded = {normalize_filename(name): note for name, note in source.superseded_filenames}
        rows = catalog_rows(source)
        metadata: dict[str, dict[str, str]] = {}
        for row in rows:
            filename = first_value(row, FILENAME_FIELDS)
            if filename:
                metadata.setdefault(normalize_filename(filename), row)
        if rows:
            source_candidates: Iterable[Path] = iter_catalog_files(source, rows)
        else:
            source_candidates = (
                pdf_path
                for source_path in source.paths
                if source_path.exists()
                for pdf_path in iter_source_files(source_path, source.extensions)
            )
        for pdf_path in source_candidates:
            if normalize_filename(pdf_path.name) in excluded:
                continue
            source_path = next(
                (path for path in source.paths if path == pdf_path or pdf_path.is_relative_to(path)),
                pdf_path.parent,
            )
            try:
                try:
                    # resolve() is disproportionately slow for thousands of
                    # OneDrive placeholders; abspath is sufficient because
                    # every configured source is already rooted in the repo.
                    resolved = Path(os.path.abspath(pdf_path))
                    stat = resolved.stat()
                except OSError:
                    continue
                try:
                    relative = resolved.relative_to(settings.repository_root).as_posix()
                except ValueError:
                    try:
                        source_relative = resolved.relative_to(source_path if source_path != resolved else source_path.parent).as_posix()
                    except ValueError:
                        source_relative = resolved.name
                    relative = f"external/{source.id}/{source_relative}"
                path_key = str(resolved).casefold()
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)

                row = metadata.get(normalize_filename(resolved.name), {})
                title = first_value(row, TITLE_FIELDS) or title_from_stem(resolved)
                superseded_note = superseded.get(normalize_filename(resolved.name), "")
                if superseded_note and not title.startswith("[TEXTE OBSOLÈTE"):
                    title = f"[TEXTE OBSOLÈTE — {superseded_note}] {title}"
                category = first_value(row, CATEGORY_FIELDS)
                if not category and source_path != resolved:
                    try:
                        parts = resolved.relative_to(source_path).parts
                        category = parts[0] if len(parts) > 1 else ""
                    except ValueError:
                        pass
                published_date = first_value(row, DATE_FIELDS)
                url = first_value(row, URL_FIELDS)
                documents.append(
                    {
                        "id": document_id(relative),
                        "source": source.id,
                        "source_label": source.label,
                        "title": title,
                        "category": category,
                        "published_date": published_date,
                        "source_url": url,
                        "superseded_note": superseded_note,
                        "relative_path": relative,
                        "absolute_path": str(resolved),
                        "filename": resolved.name,
                        "extension": resolved.suffix.lower(),
                        "requires_review": source.review_all or resolved.suffix.lower() in source.review_extensions,
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
                if limit is not None and len(documents) >= limit:
                    return sorted(
                        documents,
                        key=lambda item: (item["source"], item["title"].casefold(), item["relative_path"].casefold()),
                    )
            except OSError:
                continue

    return sorted(documents, key=lambda item: (item["source"], item["title"].casefold(), item["relative_path"].casefold()))


def source_summary(settings: Settings, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for document in documents:
        counts[document["source"]] = counts.get(document["source"], 0) + 1
    return [
        {
            "id": source.id,
            "label": source.label,
            "description": source.description,
            "document_count": counts.get(source.id, 0),
            "enabled": source.enabled,
        }
        for source in settings.sources
    ]
