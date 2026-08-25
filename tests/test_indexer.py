from pathlib import Path

import fiscal_rag.indexer as indexer_module
from fiscal_rag.config import Settings
from fiscal_rag.extractor import sha256_file
from fiscal_rag.indexer import FiscalIndexer, _flatten_duplicate_chains
from fiscal_rag.retrieval import LOCAL_INDEX_REVISION


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_dir=tmp_path,
        repository_root=tmp_path,
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "static",
        sources=(),
        model="test-model",
        reasoning_effort="low",
        port=8010,
        api_key=None,
    )


def make_record(path: Path, text: str, page_count: int) -> dict[str, object]:
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    return {
        "id": path.stem,
        "source": "test",
        "source_label": "Test",
        "title": path.stem,
        "category": "",
        "published_date": "",
        "source_url": "",
        "filename": path.name,
        "extension": path.suffix,
        "absolute_path": str(path),
        "relative_path": path.name,
        "requires_review": False,
        "present": True,
        "status": "needs_ocr",
        "page_count": page_count,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def make_ready_index_record(tmp_path: Path, document_id: str, text: str) -> dict[str, object]:
    source_path = tmp_path / f"{document_id}.txt"
    record = make_record(source_path, "Texte source. " * 20, page_count=1)
    extracted_path = tmp_path / "extracted" / f"{document_id}.md"
    extracted_path.parent.mkdir(parents=True, exist_ok=True)
    extracted_path.write_text(f"## Page 1\n\n{text}", encoding="utf-8")
    record.update(
        {
            "id": document_id,
            "status": "ready",
            "extracted_path": str(extracted_path),
            "extracted_sha256": sha256_file(extracted_path),
            "superseded_note": "",
        }
    )
    return record


def test_duplicate_chains_are_flattened_without_guessing_cycles_or_dangling_targets() -> None:
    documents = {
        "leaf": {"duplicate_of": "middle"},
        "middle": {"duplicate_of": "canonical"},
        "canonical": {},
        "dangling": {"duplicate_of": "missing"},
        "cycle-a": {"duplicate_of": "cycle-b"},
        "cycle-b": {"duplicate_of": "cycle-a"},
    }

    assert _flatten_duplicate_chains(documents) == 1
    assert documents["leaf"]["duplicate_of"] == "canonical"
    assert documents["middle"]["duplicate_of"] == "canonical"
    assert documents["dangling"]["duplicate_of"] == "missing"
    assert documents["cycle-a"]["duplicate_of"] == "cycle-b"


def test_max_pages_defers_large_documents_to_a_later_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    indexer.state["documents"] = {
        "small01": make_record(tmp_path / "small.txt", "Petit texte. " * 20, page_count=5),
        "giant01": make_record(tmp_path / "giant.txt", "Texte volumineux simule. " * 20, page_count=500),
    }

    result = indexer.prepare(max_pages=50)

    assert result["processed_this_run"] == 1
    assert indexer.state["documents"]["small01"]["status"] == "ready"
    # The giant record was never handed to extract_document at all: no extraction
    # fields were written to it, and its original placeholder status survives.
    assert "character_count" not in indexer.state["documents"]["giant01"]
    assert indexer.state["documents"]["giant01"]["status"] == "needs_ocr"


def test_min_pages_selects_only_the_deferred_large_documents(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    indexer.state["documents"] = {
        "small01": make_record(tmp_path / "small.txt", "Petit texte. " * 20, page_count=5),
        "giant01": make_record(tmp_path / "giant.txt", "Texte volumineux simule. " * 20, page_count=500),
    }

    result = indexer.prepare(min_pages=51)

    assert result["processed_this_run"] == 1
    assert indexer.state["documents"]["giant01"]["status"] == "ready"
    assert "character_count" not in indexer.state["documents"]["small01"]


def test_page_filters_do_not_skip_documents_never_extracted_before(tmp_path: Path) -> None:
    """A record with no known page_count yet (never extracted) must still be
    processed - the filter only applies once a size is actually known."""
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    record = make_record(tmp_path / "unknown.txt", "Texte pas encore extrait. " * 20, page_count=0)
    del record["page_count"]
    indexer.state["documents"] = {"unknown01": record}

    result = indexer.prepare(max_pages=50)

    assert result["processed_this_run"] == 1
    assert indexer.state["documents"]["unknown01"]["status"] == "ready"


def test_workers_greater_than_one_processes_every_document_correctly(tmp_path: Path) -> None:
    """The parallel path must reach the same end state as the sequential one: every
    record extracted, correctly matched back to itself (not mixed up across workers),
    with state.json's single-writer invariant intact (verified indirectly - the run
    would raise or corrupt data if two processes wrote it at once)."""
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    indexer.state["documents"] = {
        f"doc{i:02d}": make_record(tmp_path / f"doc{i:02d}.txt", f"Contenu du document numero {i}. " * 20, page_count=1)
        for i in range(5)
    }

    result = indexer.prepare(workers=3)

    assert result["processed_this_run"] == 5
    for i in range(5):
        record = indexer.state["documents"][f"doc{i:02d}"]
        assert record["status"] == "ready"
        assert f"document numero {i}" in Path(record["extracted_path"]).read_text(encoding="utf-8")


def test_workers_greater_than_one_records_per_document_errors_independently(tmp_path: Path) -> None:
    """One document failing extraction must not affect the others' results, whichever
    worker process happened to handle it."""
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    good = make_record(tmp_path / "good.txt", "Document valide et complet. " * 20, page_count=1)
    broken = make_record(tmp_path / "missing.txt", "placeholder", page_count=1)
    broken["absolute_path"] = str(tmp_path / "this-file-does-not-exist.txt")
    indexer.state["documents"] = {"good01": good, "broken01": broken}

    result = indexer.prepare(workers=2)

    assert result["processed_this_run"] == 2
    assert indexer.state["documents"]["good01"]["status"] == "ready"
    assert indexer.state["documents"]["broken01"]["status"] == "error"


def test_prepare_refreshes_catalog_header_without_discarding_ocr_evidence(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    record = make_record(tmp_path / "law.txt", "Disposition fiscale applicable. " * 20, page_count=1)
    record["title"] = "Ancien titre"
    indexer.state["documents"] = {"law01": record}

    assert indexer.prepare()["processed_this_run"] == 1
    old_metadata_hash = record["prepared_metadata_hash"]
    old_character_count = record["character_count"]
    record["ocr_attempted"] = True
    record["ocr_pages"] = [1]
    record["empty_pages"] = [2]

    # The source file itself is unchanged; only catalog metadata changed.
    record["title"] = "Titre officiel corrigé"
    monkeypatch.setattr(
        indexer_module,
        "extract_document",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("metadata-only update re-extracted content")),
    )
    result = indexer.prepare()

    assert result["processed_this_run"] == 1
    assert record["prepared_metadata_hash"] != old_metadata_hash
    assert record["character_count"] == old_character_count
    assert record["ocr_attempted"] is True
    assert record["ocr_pages"] == [1]
    assert record["empty_pages"] == [2]
    assert "Title: Titre officiel corrigé" in Path(record["extracted_path"]).read_text(encoding="utf-8")


def test_prepare_backfills_legacy_metadata_hash_without_reextracting(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    record = make_record(tmp_path / "legacy.txt", "Disposition fiscale applicable. " * 20, page_count=1)
    indexer.state["documents"] = {"legacy01": record}

    assert indexer.prepare()["processed_this_run"] == 1
    del record["prepared_metadata_hash"]

    result = indexer.prepare()

    assert result["processed_this_run"] == 0
    assert record["prepared_metadata_hash"]

    # A legacy record whose catalog metadata really changed must not be backfilled
    # from state alone: its on-disk header is refreshed while its body is preserved.
    del record["prepared_metadata_hash"]
    record["source_url"] = "https://example.test/corrected-official-url"
    result = indexer.prepare()

    assert result["processed_this_run"] == 1
    assert "Original URL: https://example.test/corrected-official-url" in Path(record["extracted_path"]).read_text(
        encoding="utf-8"
    )


def test_local_index_reindexes_when_state_claims_a_missing_database_row(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    record = make_ready_index_record(tmp_path, "missing-row", "Article 1er : contenu courant.")
    record.update(
        {
            "local_index_status": "indexed",
            "local_indexed_content_hash": record["extracted_sha256"],
            "local_index_revision": LOCAL_INDEX_REVISION,
        }
    )
    indexer.state["documents"] = {record["id"]: record}

    assert indexer.index_local(prepare_first=False) == 1
    assert indexer.local_index.document_content_hashes() == {
        record["id"]: record["extracted_sha256"]
    }


def test_local_index_reconciles_state_from_database_and_honors_revision(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    record = make_ready_index_record(tmp_path, "current-row", "Article 2 : contenu déjà indexé.")
    indexer.state["documents"] = {record["id"]: record}
    assert indexer.local_index.index_document(record) > 0

    record["local_index_status"] = None
    record["local_indexed_content_hash"] = "stale-state-hash"
    record["local_index_revision"] = LOCAL_INDEX_REVISION
    assert indexer.index_local(prepare_first=False) == 0
    assert record["local_index_status"] == "indexed"
    assert record["local_indexed_content_hash"] == record["extracted_sha256"]

    # A chunking/retrieval revision change must rebuild even when content is unchanged.
    record["local_index_revision"] = LOCAL_INDEX_REVISION - 1
    assert indexer.index_local(prepare_first=False) == 1
    assert record["local_index_revision"] == LOCAL_INDEX_REVISION


def test_local_reconciliation_removes_documents_flagged_as_duplicates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    duplicate = make_ready_index_record(tmp_path, "duplicate-row", "Article 3 : copie du texte.")
    assert indexer.local_index.index_document(duplicate) > 0
    duplicate["duplicate_of"] = "canonical-row"
    indexer.state["documents"] = {duplicate["id"]: duplicate}

    assert indexer.index_local(prepare_first=False) == 0
    assert indexer.local_index.document_content_hashes() == {}


def test_local_index_checkpoints_large_batches_instead_of_rewriting_state_per_document(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    records = {
        f"doc-{position}": make_ready_index_record(tmp_path, f"doc-{position}", f"Article {position} : texte.")
        for position in range(101)
    }
    indexer.state["documents"] = records
    monkeypatch.setattr(indexer.local_index, "document_content_hashes", lambda: {})
    monkeypatch.setattr(indexer.local_index, "index_document", lambda record: 1)
    saves: list[Path] = []
    monkeypatch.setattr(indexer_module, "save_state", lambda path, state: saves.append(path))

    assert indexer.index_local(prepare_first=False) == 101
    assert saves == [settings.state_path, settings.state_path]


def test_summary_reports_hash_verified_canonical_index_coverage(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    indexer = FiscalIndexer(settings)
    covered = make_ready_index_record(tmp_path, "covered", "Article 4 : texte indexé.")
    missing = make_ready_index_record(tmp_path, "missing", "Article 5 : texte absent.")
    duplicate = make_ready_index_record(tmp_path, "duplicate", "Article 4 : autre copie.")

    assert indexer.local_index.index_document(covered) > 0
    assert indexer.local_index.index_document(duplicate) > 0
    covered.update(
        {
            "local_index_revision": LOCAL_INDEX_REVISION,
            "openai_file_id": "file-covered",
            "indexed_content_hash": covered["extracted_sha256"],
        }
    )
    missing.update(
        {
            "local_index_status": "indexed",
            "local_indexed_content_hash": missing["extracted_sha256"],
            "local_index_revision": LOCAL_INDEX_REVISION,
            "openai_file_id": "file-stale",
            "indexed_content_hash": "stale-remote-hash",
        }
    )
    duplicate.update(
        {
            "duplicate_of": covered["id"],
            "local_index_revision": LOCAL_INDEX_REVISION,
            "openai_file_id": "file-duplicate",
            "indexed_content_hash": duplicate["extracted_sha256"],
        }
    )
    indexer.state["documents"] = {
        covered["id"]: covered,
        missing["id"]: missing,
        duplicate["id"]: duplicate,
    }

    result = indexer.summary()

    assert result["eligible"] == 2
    assert result["duplicates"] == 1
    assert result["locally_indexed"] == result["local_covered"] == 1
    assert result["local_missing"] == 1
    assert result["local_coverage_percent"] == 50.0
    assert result["indexed"] == result["remote_covered"] == 1
    assert result["remote_missing"] == 1
    assert result["remote_coverage_percent"] == 50.0
    assert result["local_index_documents"] == 2
