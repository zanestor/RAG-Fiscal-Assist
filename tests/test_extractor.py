from pathlib import Path

import fitz

from fiscal_rag.extractor import extract_pdf, validate_ocr_configuration


def test_extracts_page_markers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, 520, 300),
        "Article 1. La taxe est due selon les conditions prevues par la loi. " * 6,
    )
    pdf.save(pdf_path)
    pdf.close()

    document = {
        "id": "a" * 20,
        "title": "Texte fiscal",
        "source": "test",
        "source_label": "Test",
        "filename": pdf_path.name,
        "relative_path": "source/sample.pdf",
        "absolute_path": str(pdf_path),
        "category": "lois",
        "published_date": "2026-01-01",
        "source_url": "https://example.test/sample.pdf",
    }
    result = extract_pdf(document, tmp_path / "extracted")
    content = Path(result["extracted_path"]).read_text(encoding="utf-8")
    assert result["status"] == "ready"
    assert "<!-- PAGE 1 -->" in content
    assert "## Page 1" in content
    assert "Article 1" in content


def test_blank_pdf_requires_ocr_even_with_multiple_page_markers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    pdf = fitz.open()
    for _ in range(4):
        pdf.new_page()
    pdf.save(pdf_path)
    pdf.close()
    document = {
        "id": "b" * 20,
        "title": "Scanned document",
        "source": "test",
        "source_label": "Test",
        "filename": pdf_path.name,
        "relative_path": "source/blank.pdf",
        "absolute_path": str(pdf_path),
        "category": "",
        "published_date": "",
        "source_url": "",
    }

    result = extract_pdf(document, tmp_path / "extracted")

    assert result["status"] == "needs_ocr"
    assert result["character_count"] == 0
    assert result["empty_pages"] == [1, 2, 3, 4]


def test_validates_application_tessdata_directory(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "fra.traineddata").write_bytes(b"test")
    (tmp_path / "eng.traineddata").write_bytes(b"test")
    monkeypatch.setenv("FISCAL_RAG_TESSDATA_DIR", str(tmp_path))
    monkeypatch.setenv("FISCAL_RAG_OCR_LANGUAGES", "fra+eng")

    languages, directory = validate_ocr_configuration()

    assert languages == "fra+eng"
    assert directory == tmp_path
