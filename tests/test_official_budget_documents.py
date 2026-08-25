from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fiscal_rag.dedup import SOURCE_PRIORITY
from scripts.sync_official_budget_documents import (
    DEFAULT_MANIFEST,
    BudgetDocument,
    DuplicateDocumentError,
    HashMismatchError,
    PdfValidationError,
    ensure_not_state_duplicate,
    configured_state_path,
    load_manifest,
    load_state_documents,
    sync_document,
    validate_pdf_file,
)


REVENUE_HASH = "6b1f0c92f07d6fda86a1e5178da3b3924e5a838e4b56e0dec1e3f953de63bd24"
CIRCULAR_HASH = "b780cf6ff3f3ac1496f68789498b617b9adaa79c246df0da45e8b94dd7db8a41"


def test_committed_manifest_is_hash_pinned_to_the_two_reviewed_documents() -> None:
    documents = load_manifest(DEFAULT_MANIFEST)

    assert [(document.filename, document.sha256) for document in documents] == [
        ("lf_2026_recettes.pdf", REVENUE_HASH),
        ("circulaire_exec_budget2026.pdf", CIRCULAR_HASH),
    ]
    assert all(document.source_url.startswith("https://www.budget.gouv.cd/") for document in documents)
    assert "29 décembre 2025" in documents[0].title
    assert SOURCE_PRIORITY[0] == "budget_officiel"


def test_default_state_path_honors_the_application_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "configured-data"
    monkeypatch.setenv("FISCAL_RAG_DATA_DIR", str(data_dir))

    assert configured_state_path() == data_dir.resolve() / "state.json"


def test_refuses_state_hash_at_a_different_target_but_allows_same_target(tmp_path: Path) -> None:
    expected_hash = "a" * 64
    document = BudgetDocument("budget.pdf", "Budget", "https://example.test/budget.pdf", expected_hash)
    destination = tmp_path / "official_documents" / "budget"
    target = destination / document.filename
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "documents": {
                    "existing": {
                        "sha256": expected_hash.upper(),
                        "title": "Existing canonical copy",
                        "absolute_path": str(tmp_path / "elsewhere" / "budget.pdf"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state_documents = load_state_documents(state_path)

    with pytest.raises(DuplicateDocumentError, match="Existing canonical copy"):
        ensure_not_state_duplicate(document, target, state_documents, tmp_path)

    state_documents["existing"]["absolute_path"] = str(target)
    ensure_not_state_duplicate(document, target, state_documents, tmp_path)


def test_pdf_signature_and_sha256_are_both_required(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.pdf"
    valid_payload = b"%PDF-1.7\nsmall unit-test fixture\n%%EOF\n"
    valid_path.write_bytes(valid_payload)
    expected_hash = hashlib.sha256(valid_payload).hexdigest()

    assert validate_pdf_file(valid_path, expected_hash) == expected_hash
    with pytest.raises(HashMismatchError, match="SHA-256 mismatch"):
        validate_pdf_file(valid_path, "0" * 64)

    invalid_path = tmp_path / "not-a-pdf.pdf"
    invalid_payload = b"HTML error page"
    invalid_path.write_bytes(invalid_payload)
    with pytest.raises(PdfValidationError, match="does not start"):
        validate_pdf_file(invalid_path, hashlib.sha256(invalid_payload).hexdigest())


def test_check_and_dry_run_never_open_the_network(tmp_path: Path) -> None:
    document = BudgetDocument("missing.pdf", "Missing", "https://example.test/missing.pdf", "b" * 64)

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("network must not be used")

    checked = sync_document(document, tmp_path, {}, mode="check", opener=fail_if_called)
    previewed = sync_document(document, tmp_path, {}, mode="dry-run", opener=fail_if_called)

    assert checked.status == "missing"
    assert previewed.status == "would-download"
