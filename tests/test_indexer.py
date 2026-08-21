from pathlib import Path

from fiscal_rag.config import Settings
from fiscal_rag.indexer import FiscalIndexer


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
