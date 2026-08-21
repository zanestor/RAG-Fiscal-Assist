from pathlib import Path

from fiscal_rag.catalog import discover_documents
from fiscal_rag.config import Settings, SourceConfig


def make_settings(tmp_path: Path) -> Settings:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        "full_name,pdf_filename,category,url\nLoi fiscale,loi.pdf,lois,https://example.test/loi.pdf\n",
        encoding="utf-8",
    )
    (source_dir / "loi.pdf").write_bytes(b"%PDF-test")
    source = SourceConfig("test", "Test source", "Description", (source_dir,), (catalog,), True)
    return Settings(tmp_path, tmp_path, tmp_path / "data", tmp_path / "static", (source,), "test-model", "low", 8010, None)


def test_discovers_and_enriches_pdf(tmp_path: Path) -> None:
    documents = discover_documents(make_settings(tmp_path))
    assert len(documents) == 1
    assert documents[0]["title"] == "Loi fiscale"
    assert documents[0]["category"] == "lois"
    assert documents[0]["source_url"] == "https://example.test/loi.pdf"
    assert len(documents[0]["id"]) == 20

