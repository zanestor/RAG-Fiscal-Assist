"""Golden-question regression eval for the fiscal RAG assistant.

Sends a small, hand-verified set of real questions (grounded in rows actually
present in the production legal_graph.sqlite3 / local_retrieval.sqlite3 at the
time this file was written - see the `note` field on each case for where each
fact comes from) to a running `server.py` instance and checks the answer text
for required substrings. This is not a full evaluation framework (see the
NotebookLM research this was scoped from for what a scaled version - Ragas-style
metrics, mined adversarial traps, atomic-fact entailment - would look like);
it is a fast, concrete regression check for the specific failure mode that
motivated it: confirming the article-level legal graph (gap #2) and the
article-precision citations (gap #1) actually surface in real answers, not
just in unit tests against synthetic fixtures.

Usage:
    .venv/Scripts/python.exe eval_golden.py
    .venv/Scripts/python.exe eval_golden.py --base-url http://127.0.0.1:8010 --workers 6
"""

from __future__ import annotations

import argparse
import json
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GoldenCase:
    id: str
    question: str
    note: str
    must_contain: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()


# Every fact below was pulled directly from the production databases
# (%LOCALAPPDATA%\RAF_Fiscal_Assistant\legal_graph.sqlite3 and
# local_retrieval.sqlite3) and, for the graph-derived cases, independently
# confirmed via LegalGraph.article_brief() before being written here - none
# of this is paraphrased from memory or from an LLM's own claims.
GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        id="vat-art44-repealed",
        question=(
            "L'article 44 de l'Ordonnance-loi n° 10/001 du 20 août 2010 portant institution de la "
            "taxe sur la valeur ajoutée est-il toujours en vigueur ?"
        ),
        note="REPEALS edge: Ordonnance-loi 001/2012 art.2 repeals (supprime) Ordonnance-loi 10/001 art.44.",
        must_contain=("001/2012", "44"),
        any_of=("abrog", "supprim"),
    ),
    GoldenCase(
        id="fsrdc-art8-modified-target-side",
        question=(
            "L'article 8 du Décret n° 009/2002 du 5 février 2002 portant création et statuts du Fonds "
            "social de la RDC a-t-il été modifié ?"
        ),
        note=(
            "MODIFIES edge, TARGET side (the article being asked about has no legal_article row of its "
            "own - this is exactly the lookup bug fixed this session): Décret 009/2002 art.8 is itself "
            "the SOURCE modifying Décret 05/063 art.1, so this question asks about the source article, "
            "which does have its own node - a companion/contrast to the vat-art44 case above, which asks "
            "about a pure target."
        ),
        must_contain=("05/063", "modifi"),
    ),
    GoldenCase(
        id="fiscal-procedures-art101-modified",
        question=(
            "L'article 101 de la Loi n° 004/2003 du 13 mars 2003 portant réforme des procédures fiscales "
            "a-t-il été modifié, et par quel texte ?"
        ),
        note="MODIFIES edge, pure TARGET side: Loi 11/011 (2011-07-13) art.57 modifies Loi 004/2003 art.101.",
        must_contain=("101",),
        any_of=("11/011", "11-011"),
    ),
    GoldenCase(
        id="petit-commerce-art12-repealed",
        question=(
            "L'article 12 de l'Ordonnance-loi n° 90-046 du 8 août 1990 portant réglementation du Petit "
            "commerce est-il encore applicable ?"
        ),
        note="REPEALS edge, pure TARGET side: an Ordonnance-loi n°002 art.2 repeals Ordonnance-loi 90-046 art.12.",
        must_contain=("12",),
        any_of=("abrog", "supprim"),
    ),
    GoldenCase(
        id="douanes-art137-modified-cross-type",
        question=(
            "L'article 137 de l'Ordonnance-loi n° 10/002 du 20 août 2010 portant Code des douanes a-t-il "
            "été modifié ?"
        ),
        note="MODIFIES edge, cross instrument-type (Loi modifying an Ordonnance-loi): Loi 11/011 art.15 -> Ordonnance-loi 10/002 art.137.",
        must_contain=("137",),
        any_of=("11/011", "11-011"),
    ),
    GoldenCase(
        id="mandataires-publics-inserts-source-side",
        question=(
            "Le Décret n° 13/056 du 13 décembre 2013 portant statut des mandataires publics a-t-il été "
            "complété par de nouveaux articles, par exemple un article 11 quater ?"
        ),
        note="INSERTS edges, SOURCE side: Décret 13/056 art.3bis inserts several new articles into the same decree, including 11 quater.",
        must_contain=("11 quater",),
        any_of=("insér", "ajout", "nouvel"),
    ),
    GoldenCase(
        id="irpp-entry-into-force",
        question=(
            "À quelle date la Loi n° 23/053 du 30 novembre 2023 relative à l'impôt sur les sociétés et à "
            "l'impôt sur le revenu des personnes physiques entre-t-elle en vigueur, et quel article le "
            "prévoit ?"
        ),
        note=(
            "Not a gap #2 case - regression check for gap #1 (article-precision citations / focused "
            "reading), live-verified earlier this session: entry into force is article 153, a 24-month "
            "period from 2023-12-31 that different (correct) phrasings state as ending '31 décembre "
            "2025' or as taking effect '1er janvier 2026' - both are valid readings of the same rule, "
            "so accept either year rather than requiring one exact phrasing."
        ),
        must_contain=("153",),
        any_of=("2025", "2026"),
    ),
    GoldenCase(
        id="negative-control-unrelated-article",
        question="L'article 250 de la Loi n° 004/2003 du 13 mars 2003 portant réforme des procédures fiscales a-t-il été modifié ou abrogé par un autre texte ?",
        note=(
            "Negative control: article 250 of Loi 004/2003 has no row in legal_article_relationship "
            "(confirmed absent from the production graph before writing this case). The system must not "
            "fabricate a specific amending law/article number for it."
        ),
        must_not_contain=("11/011", "11-011", "13/003"),
    ),
)


def _normalize(text: str) -> str:
    stripped = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return stripped.casefold()


def ask(base_url: str, question: str, timeout: int = 280) -> dict:
    payload = json.dumps({"question": question}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"error": f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')}"}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"error": f"Request failed: {error}"}


def grade(case: GoldenCase, answer: str) -> dict:
    normalized_answer = _normalize(answer)
    missing = [term for term in case.must_contain if _normalize(term) not in normalized_answer]
    any_of_ok = not case.any_of or any(_normalize(term) in normalized_answer for term in case.any_of)
    forbidden_found = [term for term in case.must_not_contain if _normalize(term) in normalized_answer]
    passed = not missing and any_of_ok and not forbidden_found
    return {
        "passed": passed,
        "missing_required": missing,
        "any_of_satisfied": any_of_ok,
        "forbidden_found": forbidden_found,
    }


def run_case(base_url: str, case: GoldenCase) -> dict:
    try:
        response = ask(base_url, case.question)
        if "error" in response:
            return {"id": case.id, "note": case.note, "question": case.question, "passed": False, "error": response["error"]}
        answer = str(response.get("answer", ""))
        verdict = grade(case, answer)
        return {
            "id": case.id,
            "note": case.note,
            "question": case.question,
            "answer": answer,
            "provider": response.get("provider"),
            **verdict,
        }
    except Exception as error:  # a single bad case must never take down the whole run
        return {"id": case.id, "note": case.note, "question": case.question, "passed": False, "error": f"{type(error).__name__}: {error}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--output", default=str(Path(__file__).parent / "golden_eval_report.json"))
    args = parser.parse_args()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, args.base_url, case): case for case in GOLDEN_CASES}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: [case.id for case in GOLDEN_CASES].index(item["id"]))

    passed = sum(1 for result in results if result["passed"])
    print(f"\n{passed}/{len(results)} passed\n")
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['id']}")
        if not result["passed"]:
            print(f"    note: {result['note']}")
            if result.get("error"):
                print(f"    error: {result['error']}")
            if result.get("missing_required"):
                print(f"    missing required terms: {result['missing_required']}")
            if not result.get("any_of_satisfied", True):
                print("    none of the acceptable alternative terms were found")
            if result.get("forbidden_found"):
                print(f"    fabricated/forbidden terms found: {result['forbidden_found']}")

    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nFull report (including every answer text): {args.output}")


if __name__ == "__main__":
    main()
