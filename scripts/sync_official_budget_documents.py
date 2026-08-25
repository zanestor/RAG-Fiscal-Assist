"""Safely synchronize the small, reviewed set of official 2026 Budget PDFs.

This command only places hash-pinned files in ``official_documents/budget``.
It deliberately does not scan, prepare, index, or apply a deduplication plan.
Use ``--check`` in scheduled verification jobs and ``--dry-run`` to preview
which files would be downloaded or replaced.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse


APP_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = APP_DIR.parent.parent
DEFAULT_MANIFEST = APP_DIR / "config" / "official_budget_documents.csv"
DEFAULT_DESTINATION = APP_DIR / "official_documents" / "budget"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
Mode = Literal["sync", "check", "dry-run"]


class BudgetSyncError(RuntimeError):
    """Base error for a safe, user-actionable synchronization failure."""


class DuplicateDocumentError(BudgetSyncError):
    """The incoming bytes already belong to another corpus target."""


class PdfValidationError(BudgetSyncError):
    """The downloaded or existing file is not a hash-pinned PDF."""


class HashMismatchError(PdfValidationError):
    """The file differs from the reviewed manifest entry."""


@dataclass(frozen=True)
class BudgetDocument:
    filename: str
    title: str
    source_url: str
    sha256: str


@dataclass(frozen=True)
class SyncResult:
    filename: str
    status: str
    detail: str


def configured_state_path() -> Path:
    """Use the same .env-aware data directory as the main application."""
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from fiscal_rag.config import get_settings

    return get_settings().state_path


def load_manifest(path: Path) -> list[BudgetDocument]:
    """Load and strictly validate the committed, hash-pinned allow-list."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"full_name", "pdf_filename", "source_url", "sha256"}
            missing = required.difference(reader.fieldnames or ())
            if missing:
                raise BudgetSyncError(f"Manifest is missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        raise BudgetSyncError(f"Could not read manifest {path}: {exc}") from exc

    documents: list[BudgetDocument] = []
    filenames: set[str] = set()
    hashes: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        filename = (row.get("pdf_filename") or "").strip()
        title = (row.get("full_name") or "").strip()
        source_url = (row.get("source_url") or "").strip()
        expected_hash = (row.get("sha256") or "").strip().casefold()
        parsed_url = urlparse(source_url)
        if not filename or filename != Path(filename).name or "/" in filename or "\\" in filename:
            raise BudgetSyncError(f"Manifest row {row_number} has an unsafe filename: {filename!r}")
        if Path(filename).suffix.casefold() != ".pdf":
            raise BudgetSyncError(f"Manifest row {row_number} must target a .pdf file")
        if not title:
            raise BudgetSyncError(f"Manifest row {row_number} has no title")
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise BudgetSyncError(f"Manifest row {row_number} must use an HTTPS source URL")
        if not SHA256_PATTERN.fullmatch(expected_hash):
            raise BudgetSyncError(f"Manifest row {row_number} has an invalid SHA-256")
        filename_key = filename.casefold()
        if filename_key in filenames:
            raise BudgetSyncError(f"Manifest repeats target filename {filename!r}")
        if expected_hash in hashes:
            raise BudgetSyncError(f"Manifest repeats SHA-256 {expected_hash}")
        filenames.add(filename_key)
        hashes.add(expected_hash)
        documents.append(BudgetDocument(filename, title, source_url, expected_hash))
    if not documents:
        raise BudgetSyncError(f"Manifest {path} contains no documents")
    return documents


def load_state_documents(path: Path) -> dict[str, dict[str, Any]]:
    """Read corpus records used for the exact-hash duplicate safety gate."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetSyncError(f"Cannot verify duplicates because state is unreadable: {path}: {exc}") from exc
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise BudgetSyncError(f"Cannot verify duplicates because state has no document map: {path}")
    return {str(document_id): record for document_id, record in documents.items() if isinstance(record, dict)}


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _record_targets(record: dict[str, Any], repository_root: Path) -> set[str]:
    paths: set[str] = set()
    if absolute_path := record.get("absolute_path"):
        paths.add(_normalized_path(Path(str(absolute_path))))
    if relative_path := record.get("relative_path"):
        paths.add(_normalized_path(repository_root / Path(str(relative_path))))
    return paths


def ensure_not_state_duplicate(
    document: BudgetDocument,
    target: Path,
    state_documents: dict[str, dict[str, Any]],
    repository_root: Path = REPOSITORY_ROOT,
) -> None:
    """Refuse a known exact duplicate unless every match is this same target."""
    target_key = _normalized_path(target)
    conflicts: list[str] = []
    for document_id, record in state_documents.items():
        if str(record.get("sha256") or "").strip().casefold() != document.sha256:
            continue
        record_targets = _record_targets(record, repository_root)
        if target_key in record_targets:
            continue
        location = record.get("relative_path") or record.get("absolute_path") or "unknown path"
        label = record.get("title") or record.get("filename") or document_id
        conflicts.append(f"{label!r} ({location})")
    if conflicts:
        joined = "; ".join(conflicts)
        raise DuplicateDocumentError(
            f"Refusing {document.filename}: SHA-256 {document.sha256} already exists at another "
            f"corpus target: {joined}. Review the existing canonical copy instead."
        )


def validate_pdf_file(path: Path, expected_sha256: str) -> str:
    """Require a PDF signature and exact SHA-256, returning the actual hash."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise PdfValidationError(f"{path} does not start with %PDF-")
            handle.seek(0)
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PdfValidationError(f"Could not read {path}: {exc}") from exc
    actual = digest.hexdigest()
    expected = expected_sha256.strip().casefold()
    if actual != expected:
        raise HashMismatchError(f"SHA-256 mismatch for {path.name}: expected {expected}, received {actual}")
    return actual


def _download_atomic(
    document: BudgetDocument,
    target: Path,
    timeout: float,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".part",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        request = urllib.request.Request(
            document.source_url,
            headers={"User-Agent": "fiscal-rag-official-document-sync/1.0"},
        )
        with os.fdopen(descriptor, "wb") as output, opener(request, timeout=timeout) as response:
            while block := response.read(1024 * 1024):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        validate_pdf_file(temporary_path, document.sha256)
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def sync_document(
    document: BudgetDocument,
    destination: Path,
    state_documents: dict[str, dict[str, Any]],
    *,
    mode: Mode = "sync",
    repository_root: Path = REPOSITORY_ROOT,
    timeout: float = 120.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> SyncResult:
    target = destination / document.filename
    ensure_not_state_duplicate(document, target, state_documents, repository_root)

    if target.is_file():
        try:
            validate_pdf_file(target, document.sha256)
        except PdfValidationError as exc:
            if mode == "check":
                return SyncResult(document.filename, "invalid", str(exc))
            if mode == "dry-run":
                return SyncResult(document.filename, "would-replace", str(exc))
        else:
            return SyncResult(document.filename, "verified", "already matches the pinned SHA-256")
    elif mode == "check":
        return SyncResult(document.filename, "missing", f"not found at {target}")
    elif mode == "dry-run":
        return SyncResult(document.filename, "would-download", document.source_url)

    _download_atomic(document, target, timeout, opener)
    return SyncResult(document.filename, "downloaded", f"verified SHA-256 {document.sha256}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument(
        "--state",
        type=Path,
        help="state.json to inspect (defaults to the application's configured data directory)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate local files without downloading")
    mode.add_argument("--dry-run", action="store_true", help="show downloads/replacements without changing files")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode: Mode = "check" if args.check else "dry-run" if args.dry_run else "sync"
    try:
        documents = load_manifest(args.manifest)
        state_documents = load_state_documents(args.state or configured_state_path())
        results = [
            sync_document(
                document,
                args.destination,
                state_documents,
                mode=mode,
                timeout=args.timeout,
            )
            for document in documents
        ]
    except (BudgetSyncError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for result in results:
        print(f"{result.status:>14}  {result.filename}  {result.detail}")
    print("No indexing or deduplication changes were applied; review and run those stages explicitly.")
    if mode == "check" and any(result.status != "verified" for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
