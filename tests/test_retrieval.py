import sqlite3
from pathlib import Path

from fiscal_rag.retrieval import (
    FULL_TEXT_CHARACTER_CAP,
    LocalRetrievalIndex,
    _informal_code_keyword,
    _requested_article_numbers,
    build_fts_query,
    chunk_extracted_text,
)


def make_record(tmp_path: Path, document_id: str, source: str, text: str) -> dict[str, object]:
    extracted = tmp_path / f"{document_id}.md"
    extracted.write_text(text, encoding="utf-8")
    return {
        "id": document_id,
        "title": "Loi relative à la TVA",
        "source": source,
        "source_label": "Direction Générale des Impôts",
        "category": "lois",
        "published_date": "2025-01-01",
        "source_url": "https://example.test/tva",
        "sha256": f"hash-{document_id}",
        "extracted_path": str(extracted),
    }


def test_chunks_keep_page_locators() -> None:
    chunks = chunk_extracted_text("Header\n\n## Page 3\n\nLa taxe sur la valeur ajoutée est exigible.")
    assert chunks == [("Page 3", "La taxe sur la valeur ajoutée est exigible.", None)]


def test_chunks_split_at_article_boundaries_and_tag_article_number() -> None:
    chunks = chunk_extracted_text(
        "## Page 10\n\n"
        "Article 32 : Le requérant invite les services du GUPEC. "
        "Article 33 : La bâtisse disposant du certificat peut relancer ses travaux."
    )
    assert chunks == [
        ("Page 10", "Article 32 : Le requérant invite les services du GUPEC.", "32"),
        ("Page 10", "Article 33 : La bâtisse disposant du certificat peut relancer ses travaux.", "33"),
    ]


def test_chunks_tag_preamble_before_first_article_as_unspecified() -> None:
    chunks = chunk_extracted_text(
        "## Page 1\n\nExposé des motifs sans article.\n\nArticle 1er : Le sol congolais appartient à l'Etat."
    )
    assert chunks == [
        ("Page 1", "Exposé des motifs sans article.", None),
        ("Page 1", "Article 1er : Le sol congolais appartient à l'Etat.", "1er"),
    ]


def test_chunks_carry_article_suffix_across_page_boundaries() -> None:
    chunks = chunk_extracted_text(
        "## Page 4\n\nArticle 12 BIS : La garantie commence sur cette page.\n\n"
        "## Page 5\n\nElle continue sans répéter son en-tête. "
        "Article 12-ter : Une garantie distincte commence ensuite."
    )

    assert chunks == [
        ("Page 4", "Article 12 BIS : La garantie commence sur cette page.", "12 bis"),
        ("Page 5", "Elle continue sans répéter son en-tête.", "12 bis"),
        ("Page 5", "Article 12-ter : Une garantie distincte commence ensuite.", "12 ter"),
    ]


def test_chunks_stop_carrying_last_article_into_annexes() -> None:
    chunks = chunk_extracted_text(
        "## Page 53\n\nArticle 88 : La loi entre en vigueur.\n\n"
        "## Page 54\n\nANNEXES\n\n"
        "## Page 55\n\nANNEXE I : SYNTHESE DU BUDGET 2026"
    )

    assert chunks == [
        ("Page 53", "Article 88 : La loi entre en vigueur.", "88"),
        ("Page 54", "ANNEXES", None),
        ("Page 55", "ANNEXE I : SYNTHESE DU BUDGET 2026", None),
    ]


def test_builds_safe_prefix_query() -> None:
    assert build_fts_query("Quel est le taux de TVA ?") == '"taux"* OR "tva"*'


def test_extracts_requested_article_numbers_and_ranges() -> None:
    assert _requested_article_numbers("Que prévoient les articles 31, 32 et 33 ?") == {"31", "32", "33"}
    assert _requested_article_numbers("Appliquer les articles 12 à 14") == {"12", "13", "14"}
    assert _requested_article_numbers("Comparer les articles 1er, 12 bis, 12-ter et 13 quater") == {
        "1er",
        "12 bis",
        "12 ter",
        "13 quater",
    }


def test_long_eastern_drc_question_keeps_tax_terms_and_expands_synonyms() -> None:
    query = build_fts_query(
        "En RDC, exclusivement dans la partie de l'est de la république, pour faciliter le développement "
        "immobilier, l'Etat avait donné des mesures incitatives à la construction des villes. Ainsi, sont "
        "exonérées les nouvelles constructions de moins de 5 ans pour certaines taxes comme l'impôt sur "
        "le revenu locatif et l'impôt foncier. Précisez la durée, l'abrogation et les bénéficiaires."
    )

    for expected in ('"orientale"*', '"nord-kivu"*', '"exempt"*', '"cinquième"*', '"locatif"*'):
        assert expected in query


def test_eastern_drc_expansion_retrieves_article_twelve(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    target = make_record(
        tmp_path,
        "c" * 20,
        "government_public_affairs",
        "## Page 69\n\nArticle 12 : Sont exemptés de l'impôt sur les revenus locatifs les immeubles "
        "nouvellement construits dans les Provinces Orientale, du Nord-Kivu, du Sud-Kivu et du Maniema, "
        "jusqu'au 31 décembre de la cinquième année qui suit celle de l'achèvement de la construction.",
    )
    target["title"] = "CODE DES IMPÔTS 2023"
    distractor = make_record(
        tmp_path,
        "d" * 20,
        "awa",
        "## Page 1\n\nLe programme de développement immobilier prévoit des mesures incitatives à la construction des villes.",
    )
    distractor["title"] = "Programme immobilier"
    index.index_document(target)
    index.index_document(distractor)

    results = index.search(
        "En RDC, dans la partie de l'est de la république, les nouvelles constructions de moins de 5 ans "
        "sont-elles exonérées de l'impôt sur le revenu locatif ?"
    )

    assert results[0].document_id == "c" * 20
    assert results[0].locator == "Page 69"
    assert results[0].article_number == "12"


def test_indexes_searches_and_filters_local_documents(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    first = make_record(
        tmp_path,
        "a" * 20,
        "dgi",
        "## Page 7\n\nLe taux de la taxe sur la valeur ajoutée est établi par la loi fiscale.",
    )
    second = make_record(
        tmp_path,
        "b" * 20,
        "bcc",
        "## Page 2\n\nLa politique monétaire concerne la banque centrale.",
    )
    assert index.index_document(first) == 1
    assert index.index_document(second) == 1

    results = index.search("taux TVA", sources=["dgi"])

    assert len(results) == 1
    assert results[0].document_id == "a" * 20
    assert results[0].locator == "Page 7"
    assert index.stats() == {"documents": 2, "chunks": 2}


def test_index_records_extracted_content_hash(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_record(
        tmp_path,
        "e" * 20,
        "dgi",
        "## Page 10\n\nArticle 31 : Le certificat de conformité est obligatoire.",
    )
    record["extracted_sha256"] = "hash-of-ocr-text"

    index.index_document(record)

    import sqlite3

    with sqlite3.connect(index.path) as connection:
        stored_hash = connection.execute(
            "SELECT content_hash FROM documents WHERE document_id=?", (record["id"],)
        ).fetchone()[0]
    assert stored_hash == "hash-of-ocr-text"


def test_document_content_hashes_reflect_actual_index_membership(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    assert index.document_content_hashes() == {}

    record = make_record(
        tmp_path,
        "h" * 20,
        "dgi",
        "## Page 3\n\nArticle 7 : La déclaration est obligatoire.",
    )
    record["extracted_sha256"] = "current-extracted-hash"
    index.index_document(record)

    assert index.document_content_hashes() == {record["id"]: "current-extracted-hash"}

    with sqlite3.connect(index.path) as connection:
        index_names = {str(row[1]) for row in connection.execute("PRAGMA index_list('chunks')")}
    assert "idx_chunks_document_id" in index_names


def test_reindexing_empty_extraction_removes_stale_document_and_chunks(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_record(
        tmp_path,
        "i" * 20,
        "dgi",
        "## Page 3\n\nArticle 7 : La déclaration est obligatoire.",
    )
    assert index.index_document(record) == 1

    Path(str(record["extracted_path"])).write_text(" \n\n", encoding="utf-8")
    assert index.index_document(record) == 0

    assert index.stats() == {"documents": 0, "chunks": 0}
    assert index.document_content_hashes() == {}


def test_search_keeps_distinct_article_chunks_on_the_same_page(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_record(
        tmp_path,
        "j" * 20,
        "awa",
        "## Page 10\n\nArticle 32 : Le certificat fiscal confirme la conformité. "
        "Article 33 : Le certificat fiscal autorise la reprise.",
    )
    index.index_document(record)

    results = index.search("certificat fiscal", limit=10)
    scoped_results = index._search_within_document("certificat fiscal", str(record["id"]), limit=10)

    assert {result.article_number for result in results} == {"32", "33"}
    assert {result.article_number for result in scoped_results} == {"32", "33"}
    assert {result.locator for result in results} == {"Page 10"}


def test_structured_article_tag_outranks_incidental_cross_reference(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_record(
        tmp_path,
        "k" * 20,
        "awa",
        "## Page 4\n\nArticle 12 bis : La règle commence ici.\n\n"
        "## Page 5\n\nLa modalité ciblée poursuit cette règle. "
        "Article 99 : La modalité ciblée renvoie à l'article 12 bis.",
    )
    index.index_document(record)

    results = index.search("Quelle modalité prévoit l'article 12 bis ?", limit=10)

    assert results[0].article_number == "12 bis"
    assert any(result.article_number == "99" for result in results)


def test_explicit_article_numbers_prioritize_matching_page(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_record(
        tmp_path,
        "f" * 20,
        "awa",
        "## Page 1\n\nArrêté ministériel relatif au Guichet Unique de délivrance du permis de construire.\n\n"
        "## Page 10\n\nArticle 32 : Le requérant invite les services du GUPEC. "
        "Article 33 : La bâtisse disposant du certificat peut relancer ses travaux.",
    )
    record["title"] = "Arrêté ministériel 0058 relatif au Guichet Unique"
    index.index_document(record)

    results = index.search(
        "Selon l'Arrêté ministériel 0058 relatif au Guichet Unique, que prévoient les articles 31, 32 et 33 ?"
    )

    assert results[0].locator == "Page 10"
    assert results[0].article_number in {"32", "33"}


def make_state_record(tmp_path: Path, document_id: str, title: str, text: str, **overrides: object) -> dict[str, object]:
    record = make_record(tmp_path, document_id, "awa", text)
    record.update(
        {
            "title": title,
            "status": "ready",
            "present": True,
            "character_count": len(text),
            "superseded_note": "",
        }
    )
    record.update(overrides)
    return record


def test_resolve_named_instrument_reads_full_text_when_cited_formally(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_state_record(
        tmp_path,
        "a" * 20,
        "Loi n° 73/021 du 20 juillet 1973 portant régime général des biens.",
        "## Page 1\n\nArticle 1er : Le sol congolais appartient à l'Etat.",
    )
    state_documents = {record["id"]: record}

    entries = index.resolve_named_instrument_text(
        "Que prévoit la loi n°73/021 du 20 juillet 1973 sur le régime foncier ?", state_documents
    )

    assert len(entries) == 1
    assert entries[0]["mode"] == "full_text"
    assert "Le sol congolais appartient" in entries[0]["content"]


def test_resolve_named_instrument_uses_scoped_chunks_for_long_documents(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    long_text = "## Page 1\n\nDispositions générales.\n\n" + "\n\n".join(
        f"## Page {n}\n\nDisposition administrative diverse sans rapport avec la question." for n in range(2, 60)
    )
    long_text += "\n\n## Page 60\n\nArticle 200 : Les sanctions pénales applicables sont fixées par le présent code."

    record = make_state_record(
        tmp_path,
        "b" * 20,
        "Ordonnance-loi n° 23/010 du 13 mars 2023 portant Code du numérique.",
        long_text,
        character_count=FULL_TEXT_CHARACTER_CAP + 1,  # force the scoped-chunks path regardless of fixture size
    )
    # A shorter, differently-worded competing document that would otherwise win a
    # corpus-wide ranking on generic terms but must not crowd out the named instrument.
    distractor = make_state_record(
        tmp_path,
        "c" * 20,
        "Communiqué relatif au code du numérique.",
        "## Section 1\n\nLe conseil des ministres a adopté le projet de code du numérique.",
    )
    index.index_document(record)
    index.index_document(distractor)
    state_documents = {record["id"]: record, distractor["id"]: distractor}

    entries = index.resolve_named_instrument_text(
        "L'ordonnance-loi n°23/010 du 13 mars 2023 portant code du numérique prévoit-elle des sanctions ?",
        state_documents,
    )

    assert len(entries) == 1
    assert entries[0]["mode"] == "scoped_chunks"
    assert any("sanctions pénales" in chunk.content for chunk in entries[0]["chunks"])
    assert all(chunk.document_id == record["id"] for chunk in entries[0]["chunks"])


def test_resolve_named_instrument_falls_back_to_informal_code_name(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    record = make_state_record(
        tmp_path,
        "d" * 20,
        "Ordonnance-loi n°23/010 du 13 mars 2023 portant Code du numérique.",
        "## Page 1\n\nLe present code fixe les regles applicables au numerique.",
    )
    state_documents = {record["id"]: record}

    entries = index.resolve_named_instrument_text(
        "Le code du numérique prévoit-il des sanctions administratives ?", state_documents
    )

    assert len(entries) == 1
    assert "CODE DU NUMÉRIQUE" in entries[0]["title"].upper()


def test_resolve_named_instrument_skips_superseded_documents(tmp_path: Path) -> None:
    index = LocalRetrievalIndex(tmp_path / "retrieval.sqlite3")
    draft = make_state_record(
        tmp_path,
        "e" * 20,
        "PROPOSITION DE LOI PORTANT CODE DU NUMÉRIQUE.",
        "## Page 1\n\nProjet de texte non encore adopté.",
        superseded_note="proposition de loi (avant-projet); remplacée par le texte adopté",
    )
    state_documents = {draft["id"]: draft}

    entries = index.resolve_named_instrument_text("Le code du numérique prévoit-il des sanctions ?", state_documents)

    assert entries == []


def test_informal_code_keyword_extraction() -> None:
    assert _informal_code_keyword("Le code du numérique prévoit-il des sanctions ?") == "numerique"
    assert _informal_code_keyword("Que dit le code des impôts sur la TVA ?") == "impots"
    assert _informal_code_keyword("Quel est le taux de TVA ?") is None
