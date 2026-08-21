from pathlib import Path

from fiscal_rag.legal_graph import (
    LegalGraph,
    extract_article_relationship_matches,
    extract_relationships,
    find_instrument_mentions,
    normalize_article_number,
    normalize_instrument_number,
    parse_self_instrument,
)


def make_record(document_id: str, title: str, source: str = "leganews", superseded_note: str = "") -> dict[str, object]:
    return {
        "id": document_id,
        "title": title,
        "source": source,
        "source_label": "LegaNews",
        "category": "lois",
        "source_url": "https://example.test/doc",
        "superseded_note": superseded_note,
        "status": "ready",
        "present": True,
    }


def test_normalizes_equivalent_number_spellings() -> None:
    assert normalize_instrument_number("69/009") == normalize_instrument_number("69-009") == normalize_instrument_number("69.009")


def test_parses_self_instrument_type_number_and_date() -> None:
    mention = parse_self_instrument(
        "Ordonnance-loi n° 87-026 du 7 juillet 1987 instituant au sein de l'Office de gestion de la dette publique "
        "un fonds de couverture du risque de change sur les emprunts en devises à long terme."
    )
    assert mention is not None
    assert mention.type_label == "Ordonnance-loi"
    assert mention.number == "87-026"
    assert mention.date_iso == "1987-07-07"


def test_jurisprudence_and_forms_are_not_treated_as_instruments() -> None:
    assert parse_self_instrument("CCJA - Société Cambanis And Company C- Monsieur A B -Arrêt n°071-2023 du 06 avril 2023.") is None
    assert parse_self_instrument("MOD. AE - Demande d'affiliation de l'employeur.") is None


def test_type_matching_requires_a_word_boundary() -> None:
    # "loi" is a substring of "emploi"/"employeur"; without a \b guard this
    # would spuriously report a "Loi" mention inside such words.
    mentions = find_instrument_mentions("Arrêté relatif aux conditions d'emploi et à l'employeur.")
    assert all(mention.type_label != "Loi" for mention in mentions)


def test_extracts_modifies_relationship_without_a_no_marker_on_the_target() -> None:
    self_mention = parse_self_instrument(
        "Loi n° 008-03 du 13 mars 2003 portant modification de l'ordonnance-loi 69-058 du 5 décembre 1969 "
        "relative à l'impôt sur le chiffre d'affaires."
    )
    assert self_mention is not None
    relationships = extract_relationships(
        "Loi n° 008-03 du 13 mars 2003 portant modification de l'ordonnance-loi 69-058 du 5 décembre 1969 "
        "relative à l'impôt sur le chiffre d'affaires.",
        self_mention,
    )
    assert len(relationships) == 1
    assert relationships[0].relation == "MODIFIES"
    assert relationships[0].target.type_label == "Ordonnance-loi"
    assert relationships[0].target.number_key == normalize_instrument_number("69-058")


def test_extracts_repeals_relationship() -> None:
    title = "Décret n° 12/042 du 16 octobre 2012 abrogeant le Décret n° 055 du 12 avril 2002 portant création d'un service."
    self_mention = parse_self_instrument(title)
    relationships = extract_relationships(title, self_mention)
    assert [(r.relation, r.target.number, r.partial) for r in relationships] == [("REPEALS", "055", False)]


def test_repeal_scoped_to_one_article_is_flagged_partial_not_full() -> None:
    # A real corpus title: this repeals only Article 321 of Loi 73-021, not
    # the whole (still very much active) land-tenure law.
    title = "Ordonnance-loi n° 78-007 du 29 mars 1978 abrogeant l'article 321 de la loi n° 73-021 du 20 juillet 1973."
    self_mention = parse_self_instrument(title)
    relationships = extract_relationships(title, self_mention)
    assert len(relationships) == 1
    assert relationships[0].relation == "REPEALS"
    assert relationships[0].partial is True


def test_partial_repeal_does_not_flip_instrument_status_to_repealed(tmp_path: Path) -> None:
    documents = [
        make_record("doc-land-law", "Loi n° 73-021 du 20 juillet 1973 portant régime général des biens."),
        make_record(
            "doc-partial-repeal",
            "Ordonnance-loi n° 78-007 du 29 mars 1978 abrogeant l'article 321 de la loi n° 73-021 du 20 juillet 1973.",
        ),
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    graph.rebuild(documents)

    row = graph.find_instruments("Loi", "73-021")[0]
    status = graph.status_for(row["instrument_id"])
    assert status["status"] != "repealed"
    assert status["status"] == "modified"
    assert len(status["partially_repealed_by"]) == 1
    assert status["repealed_by"] == []


def test_rebuild_links_relationships_and_resolves_status(tmp_path: Path) -> None:
    documents = [
        make_record("doc-original", "Ordonnance-loi n° 69/009 du 10 février 1969 relative aux impôts cédulaires sur les revenus."),
        make_record(
            "doc-amendment",
            "Loi n° 18/025 du 13 décembre 2018 modifiant et complétant l'ordonnance-loi n° 69/009 du 10 février 1969.",
        ),
        make_record(
            "doc-repeal",
            "Décret n° 05/184 du 31 décembre 2005 abrogeant les dispositions du Décret n° 068 du 2 avril 1998 portant régime.",
        ),
        make_record("doc-old-decree", "Décret n° 068 du 2 avril 1998 portant régime des sociétés."),
        make_record(
            "doc-flagged",
            "Code des impôts (version 2009).",
            superseded_note="version antérieure à 2012; consulter le CODE DES IMPÔTS 2023",
        ),
    ]
    # give the flagged document a recognizable self-instrument so it lands in the graph
    documents[-1]["title"] = "Loi n° 04/2009 du 1 janvier 2009 portant Code des impôts."

    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild(documents)
    assert stats["instruments"] == 5
    assert stats["relationships"] == 2

    original_row = graph.find_instruments("Ordonnance-loi", "69/009")[0]
    status = graph.status_for(original_row["instrument_id"])
    assert status["status"] == "modified"
    assert len(status["modified_by"]) == 1
    assert status["modified_by"][0]["source_number"] == "18/025"

    old_decree_row = graph.find_instruments("Décret", "068")[0]
    old_decree_status = graph.status_for(old_decree_row["instrument_id"])
    assert old_decree_status["status"] == "repealed"

    flagged_row = graph.find_instruments("Loi", "04/2009")[0]
    flagged_status = graph.status_for(flagged_row["instrument_id"])
    assert flagged_status["status"] == "flagged_superseded"


def test_status_brief_matches_a_question_naming_the_instrument(tmp_path: Path) -> None:
    documents = [
        make_record("doc-original", "Ordonnance-loi n° 69/009 du 10 février 1969 relative aux impôts cédulaires sur les revenus."),
        make_record(
            "doc-amendment",
            "Loi n° 18/025 du 13 décembre 2018 modifiant et complétant l'ordonnance-loi n° 69/009 du 10 février 1969.",
        ),
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    graph.rebuild(documents)

    brief = graph.status_brief("Est-ce que l'ordonnance-loi n°69/009 du 10 février 1969 est toujours en vigueur ?")
    assert "Ordonnance-loi n° 69/009" in brief
    assert "Modifié" in brief
    assert "18/025" in brief


def test_status_brief_is_empty_when_question_names_no_instrument(tmp_path: Path) -> None:
    graph = LegalGraph(tmp_path / "unused.sqlite3")
    assert graph.status_brief("Quel est le taux de TVA applicable en RDC ?") == ""


## --------------------------------------------------------------------------------
## Article-level graph (gap #2, Family 2) - real corpus sentences as fixtures.
## --------------------------------------------------------------------------------


def make_chunk(document_id: str, article_number: str, content: str, locator: str = "Page 1") -> dict[str, object]:
    return {"document_id": document_id, "locator": locator, "content": content, "article_number": article_number}


def _fetch_article_relationships(graph: LegalGraph) -> list[dict[str, object]]:
    with graph._connect() as connection:  # noqa: SLF001 - test-only introspection
        rows = connection.execute("SELECT * FROM legal_article_relationship").fetchall()
        return [dict(row) for row in rows]


def test_passive_dispositif_single_target_creates_one_modifies_edge(tmp_path: Path) -> None:
    documents = {
        "doc-amend": {
            "title": "Loi n° 08/010 du 31 juillet 2008 modifiant l'Ordonnance-loi n° 90-046 du 8 août 1990 "
            "portant réglementation du Petit commerce."
        },
    }
    chunks = [
        make_chunk(
            "doc-amend",
            "3",
            "Article 3 : L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 portant réglementation "
            "du Petit commerce est modifié et complété comme suit : «Nouveau texte de l'article.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["article_relationships"] == 1
    assert stats["by_relationship"] == {"MODIFIES": 1}

    rows = _fetch_article_relationships(graph)
    assert len(rows) == 1
    assert rows[0]["relationship"] == "MODIFIES"
    assert rows[0]["target_article_number_key"] == normalize_article_number("1er") == "1"
    assert rows[0]["granularity"] == "article"


def test_passive_dispositif_numlist_fans_out_into_separate_edges(tmp_path: Path) -> None:
    # Real corpus sentence (VAT-instituting ordonnance-loi amendment). The quoted
    # comma/"et"-joined list names 15 distinct article numbers - each must become
    # its own relationship row, not one edge carrying the whole list as a string.
    documents = {"doc-amend": {"title": "Loi n° 14/003 du 30 juin 2014 modifiant l'Ordonnance-loi n°10/001."}}
    numlist = "6, 8, 14, 15, 17, 18, 19, 38, 39, 41, 42, 45, 61, 64 et 69"
    expected_numbers = ["6", "8", "14", "15", "17", "18", "19", "38", "39", "41", "42", "45", "61", "64", "69"]
    chunks = [
        make_chunk(
            "doc-amend",
            "1",
            f"Article 1er : Les articles {numlist} de l'Ordonnance-loi n°10/001 du 20 août 2010 portant "
            "institution de la taxe sur la valeur ajoutée sont modifiés et complétés comme suit : «Texte.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["article_relationships"] == len(expected_numbers)
    rows = _fetch_article_relationships(graph)
    assert sorted(row["target_article_number"] for row in rows) == sorted(expected_numbers)
    assert all(row["relationship"] == "MODIFIES" for row in rows)


def test_insertion_formula_creates_one_inserts_edge_per_suffix(tmp_path: Path) -> None:
    documents = {"doc-amend": {"title": "Loi n° 15/022 du 31 décembre 2015 modifiant le Code pénal congolais."}}
    suffixes = [
        "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies",
        "nonies", "decies", "undecies", "duodecies", "terdecies", "quaterdecies",
    ]
    numlist = ", ".join(f"68{suffix}" for suffix in suffixes)
    chunks = [
        make_chunk(
            "doc-amend",
            "1",
            f"Article 1er : Il est inséré au décret du 30 janvier 1940 portant Code pénal congolais les "
            f"articles {numlist} libellés comme suit : «Texte des nouveaux articles.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["by_relationship"] == {"INSERTS": len(suffixes)}
    rows = _fetch_article_relationships(graph)
    assert {row["target_article_number"] for row in rows} == {f"68{suffix}" for suffix in suffixes}
    # Each inserted article is itself a new legal_article row (it is a genuinely
    # new provision of the target instrument), not just a text reference.
    assert all(row["target_article_id"] is not None for row in rows)


def test_parenthetical_amendment_note_creates_modifies_edge(tmp_path: Path) -> None:
    documents = {"doc-amend": {"title": "Arrêté ministériel n° 044 du 12 mars 2010 portant mesures d'application."}}
    chunks = [
        make_chunk(
            "doc-amend",
            "3",
            "Article 3: (modifié et complété par l'article 1° du Décret n° 05/063 du 22 juillet 2005)",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["by_relationship"] == {"MODIFIES": 1}
    rows = _fetch_article_relationships(graph)
    assert rows[0]["target_article_number"] == "1"


def test_whole_instrument_anchored_verb_produces_zero_article_edges(tmp_path: Path) -> None:
    # "l'article 82" is only a qualifier ("sous réserve de ..."); the "est
    # abrogée" verb grammatically binds to "l'Ordonnance-loi n°69/054", a
    # separate free-standing instrument mention - not to article 82. That is a
    # whole-instrument repeal already covered by the title-level graph, and must
    # NOT be reported as an article-level edge here.
    documents = {"doc-loi": {"title": "Loi n° 10/010 du 27 avril 2010 portant mesures de sauvegarde."}}
    chunks = [
        make_chunk(
            "doc-loi",
            "84",
            "Article 84 : Sous réserve des dispositions de l'article 82 de la présente loi, l'Ordonnance-loi "
            "n° 69/054 du 5 décembre 1969 portant mesures de sauvegarde en matière économique et relative à "
            "la protection de l'industrie sucrière nationale contre la concurrence anarchique et déloyale, "
            "est abrogée.",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["article_relationships"] == 0
    assert _fetch_article_relationships(graph) == []


def test_alinea_qualifier_sets_alinea_granularity(tmp_path: Path) -> None:
    documents = {"doc-amend": {"title": "Loi n° 90-100 du 5 janvier 1990 modifiant la loi n° 71/012."}}
    chunks = [
        make_chunk(
            "doc-amend",
            "5",
            "Article 5 : L'alinéa 2 de l'article 10 de la loi n° 71/012 du 31 décembre 1971 est abrogé et "
            "remplacé par de nouvelles dispositions.",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)

    assert stats["article_relationships"] == 1
    rows = _fetch_article_relationships(graph)
    assert rows[0]["granularity"] == "alinea"
    assert rows[0]["target_article_number"] == "10"


def test_negative_case_also_yields_no_matches_from_extraction_directly() -> None:
    # Same guard as above, exercised at the pure regex-extraction level (no DB).
    sentence = (
        "Sous réserve des dispositions de l'article 82 de la présente loi, l'Ordonnance-loi n° 69/054 du "
        "5 décembre 1969 portant mesures de sauvegarde en matière économique et relative à la protection "
        "de l'industrie sucrière nationale contre la concurrence anarchique et déloyale, est abrogée."
    )
    assert extract_article_relationship_matches(sentence) == []


def test_article_brief_formats_known_article_and_empty_for_unknown(tmp_path: Path) -> None:
    documents = {
        "doc-amend": {
            "title": "Loi n° 08/010 du 31 juillet 2008 modifiant l'Ordonnance-loi n° 90-046 du 8 août 1990 "
            "portant réglementation du Petit commerce."
        },
    }
    chunks = [
        make_chunk(
            "doc-amend",
            "3",
            "Article 3 : L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 portant réglementation "
            "du Petit commerce est modifié et complété comme suit : «Nouveau texte de l'article.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    graph.rebuild(documents.values())
    graph.rebuild_articles(documents, chunks)

    # Ask about the SOURCE side (article 3 of the amending law itself), which is
    # backed by a chunked legal_article row.
    brief = graph.article_brief("L'article 3 de la loi n°08/010 du 31 juillet 2008 a-t-il modifié un autre texte ?")
    assert "Loi n° 08/010" in brief
    assert "art. 3" in brief
    assert "modifie" in brief
    assert "Ordonnance-loi" in brief

    # Ask about the TARGET side ("article 1er of Ordonnance-loi 90-046" - the
    # provision that got modified). It has no legal_article row of its own (nothing
    # in ITS text cites anything), but must still be found via the relationship's
    # target_instrument_id/target_article_number_key - this is the far more common
    # real-world question shape ("was this article modified?") and was silently
    # broken (returned "") before article_brief()'s lookup was fixed to not require
    # a pre-existing legal_article row on the target side.
    target_brief = graph.article_brief(
        "L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 est-il toujours en vigueur ?"
    )
    assert target_brief != ""
    assert "art. 1er" in target_brief
    assert "Loi n° 08/010" in target_brief
    assert "modifié par" in target_brief

    assert graph.article_brief("Quel est le taux de TVA applicable en RDC ?") == ""
    assert graph.article_brief("L'article 999 de la loi n° 00/000 est-il en vigueur ?") == ""


def test_stats_includes_article_level_counts(tmp_path: Path) -> None:
    documents = {
        "doc-amend": {
            "title": "Loi n° 08/010 du 31 juillet 2008 modifiant l'Ordonnance-loi n° 90-046 du 8 août 1990."
        },
    }
    chunks = [
        make_chunk(
            "doc-amend",
            "3",
            "Article 3 : L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 est modifié comme "
            "suit : «Texte.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    graph.rebuild_articles(documents, chunks)

    stats = graph.stats()
    assert stats["articles"] == 1
    assert stats["article_relationships"] == 1


def test_chunk_without_article_number_is_skipped(tmp_path: Path) -> None:
    documents = {"doc-amend": {"title": "Loi n° 08/010 du 31 juillet 2008 modifiant l'Ordonnance-loi n° 90-046."}}
    chunks = [
        make_chunk(
            "doc-amend",
            "",
            "L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 est modifié comme suit : «Texte.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    stats = graph.rebuild_articles(documents, chunks)
    assert stats["articles"] == 0
    assert stats["article_relationships"] == 0


def test_article_instrument_links_survive_a_repeat_instrument_rebuild(tmp_path: Path) -> None:
    # Reproduces a real production bug: legal_article.instrument_id and
    # legal_article_relationship.target_instrument_id are declared ON DELETE SET NULL
    # against legal_instrument. rebuild() always does a full DELETE + reinsert of
    # legal_instrument (fresh row ids), so calling it again after rebuild_articles()
    # has already run silently nulled out every article-level instrument link - which
    # happened on every real server restart (indexer.py's scan() calls rebuild() on
    # every startup) until callers were fixed to always re-run rebuild_articles()
    # immediately after rebuild(), exactly as this test does.
    documents = {
        "doc-amend": {
            "title": "Loi n° 08/010 du 31 juillet 2008 modifiant l'Ordonnance-loi n° 90-046 du 8 août 1990."
        },
    }
    chunks = [
        make_chunk(
            "doc-amend",
            "3",
            "Article 3 : L'article 1er de l'Ordonnance-loi n° 90-046 du 8 août 1990 est modifié comme "
            "suit : «Texte.»",
        )
    ]
    graph = LegalGraph(tmp_path / "legal_graph.sqlite3")
    graph.rebuild(documents.values())
    graph.rebuild_articles(documents, chunks)

    def _instrument_link_counts() -> tuple[int, int]:
        with graph._connect() as connection:  # noqa: SLF001 - test-only introspection
            articles = connection.execute(
                "SELECT COUNT(*) FROM legal_article WHERE instrument_id IS NOT NULL"
            ).fetchone()[0]
            relationships = connection.execute(
                "SELECT COUNT(*) FROM legal_article_relationship WHERE target_instrument_id IS NOT NULL"
            ).fetchone()[0]
        return articles, relationships

    assert _instrument_link_counts() == (1, 1)

    # Simulate a second scan()/legal-build - e.g. a server restart.
    graph.rebuild(documents.values())
    graph.rebuild_articles(documents, chunks)

    assert _instrument_link_counts() == (1, 1)
