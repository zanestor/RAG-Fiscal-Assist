from pathlib import Path

from fiscal_rag.dedup import find_exact_extracted_text_groups, plan_dedupe


def make_record(
    tmp_path: Path,
    document_id: str,
    source: str,
    title: str,
    raw_sha256: str,
    body: str,
) -> dict[str, object]:
    extracted_path = tmp_path / f"{document_id}.md"
    extracted_path.write_text(
        f"# Fiscal reference source\n\nTitle: {title}\n\n"
        "Do not infer legal force, effective date, amendment, or repeal status from the catalog date alone.\n\n"
        f"<!-- PAGE 1 -->\n## Page 1\n\n{body}",
        encoding="utf-8",
    )
    return {
        "id": document_id,
        "source": source,
        "title": title,
        "sha256": raw_sha256,
        "status": "ready",
        "present": True,
        "page_count": 592,
        "extracted_path": str(extracted_path),
    }


def test_exact_extracted_text_catches_different_binaries_with_generic_titles(tmp_path: Path) -> None:
    body = "Article 1 : Le présent Code des impôts fixe les obligations fiscales. " * 20
    documents = {
        "dgi": make_record(tmp_path, "dgi", "dgi", "Code des impôts — 2021", "a" * 64, body),
        "finances": make_record(tmp_path, "finances", "finances", "Code des impôts", "b" * 64, body),
        "ong": make_record(tmp_path, "ong", "ong_ressources", "code des impots", "c" * 64, body),
    }

    groups = find_exact_extracted_text_groups(documents)
    assert list(groups.values()) == [["dgi", "finances", "ong"]]

    report = plan_dedupe(documents)
    exact_text_group = next(preview for preview in report.previews if preview.type_label == "Exact extracted text")
    assert exact_text_group.canonical_id == "dgi"
    assert exact_text_group.duplicate_ids == ["finances", "ong"]


def test_exact_extracted_text_does_not_merge_similar_but_different_editions(tmp_path: Path) -> None:
    common = "Article 1 : Le présent Code des impôts fixe les obligations fiscales. " * 20
    documents = {
        "edition-2021": make_record(tmp_path, "edition-2021", "dgi", "Code 2021", "a" * 64, common),
        "edition-2023": make_record(
            tmp_path,
            "edition-2023",
            "dgi",
            "Code 2023",
            "b" * 64,
            common + "Article 2 : Une obligation nouvelle est applicable.",
        ),
    }

    assert find_exact_extracted_text_groups(documents) == {}
    assert plan_dedupe(documents).groups_found == 0
