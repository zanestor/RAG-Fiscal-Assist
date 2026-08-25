from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from fiscal_rag.config import get_settings
from fiscal_rag.indexer import FiscalIndexer
from fiscal_rag.legal_graph import LegalGraph, find_instrument_mentions
from fiscal_rag.locking import IndexingLock
from fiscal_rag.state import load_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and index the multi-source RDC fiscal reference library.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="Discover PDFs and update the local inventory without extracting or uploading.")

    prepare = subparsers.add_parser("prepare", help="Extract page-aware text locally; no API calls are made.")
    prepare.add_argument("--limit", type=int, default=None, help="Only prepare this many changed documents.")
    prepare.add_argument("--ocr", action="store_true", help="Try Tesseract OCR on pages with little extractable text.")
    prepare.add_argument(
        "--force-ocr", action="store_true",
        help="Also OCR pages that already have 'enough' embedded text by length - use for a document known "
        "to have a garbled/duplicated text layer (e.g. a pre-OCR'd source file), which --ocr alone will never "
        "touch since it only OCRs pages that are too SHORT, not pages that are long but wrong. Implies --ocr; "
        "always scope this with --source to a specific known-bad document, not a whole corpus re-run.",
    )
    prepare.add_argument("--source", action="append", default=[], help="Only prepare a source ID; repeat for multiple sources.")
    prepare.add_argument("--include-review-required", action="store_true", help="Include files flagged for privacy/content review.")
    prepare.add_argument(
        "--max-pages", type=int, default=None,
        help="Skip documents already known (from a prior extraction) to have more than this many pages; "
        "lets a large backlog's many short documents finish before its few huge ones.",
    )
    prepare.add_argument(
        "--min-pages", type=int, default=None,
        help="Skip documents already known to have fewer than this many pages; pairs with --max-pages to "
        "run the deferred large-document batch separately afterward.",
    )
    prepare.add_argument(
        "--workers", type=int, default=1,
        help="Extract this many documents concurrently in separate processes (state.json is still written "
        "by only this main process, one result at a time). Useful for --ocr batches on a multi-core machine; "
        "leave a couple of cores free for everything else on the machine.",
    )

    index = subparsers.add_parser(
        "index",
        help="Build the local fallback index and, when configured, upload changed documents to OpenAI File Search.",
    )
    index.add_argument("--limit", type=int, default=None, help="Only index this many changed documents.")
    index.add_argument("--ocr", action="store_true", help="Try Tesseract OCR on pages with little extractable text.")
    index.add_argument(
        "--force-ocr", action="store_true",
        help="Also OCR pages that already have 'enough' embedded text by length - see 'prepare --force-ocr'.",
    )
    index.add_argument("--source", action="append", default=[], help="Only index a source ID; repeat for multiple sources.")
    index.add_argument("--include-review-required", action="store_true", help="Upload files flagged for privacy/content review.")
    index.add_argument("--max-pages", type=int, default=None, help="Skip documents already known to have more than this many pages.")
    index.add_argument("--min-pages", type=int, default=None, help="Skip documents already known to have fewer than this many pages.")
    index.add_argument("--workers", type=int, default=1, help="Extract this many documents concurrently in separate processes.")

    local_index = subparsers.add_parser(
        "index-local",
        help="Prepare changed documents and build only the local SQLite retrieval index used by OpenRouter.",
    )
    local_index.add_argument("--limit", type=int, default=None, help="Only index this many changed documents.")
    local_index.add_argument("--ocr", action="store_true", help="Try Tesseract OCR on pages with little extractable text.")
    local_index.add_argument("--source", action="append", default=[], help="Only index a source ID; repeat for multiple sources.")
    local_index.add_argument("--include-review-required", action="store_true", help="Include files flagged for privacy/content review.")
    local_index.add_argument("--max-pages", type=int, default=None, help="Skip documents already known to have more than this many pages.")
    local_index.add_argument("--min-pages", type=int, default=None, help="Skip documents already known to have fewer than this many pages.")
    local_index.add_argument("--workers", type=int, default=1, help="Extract this many documents concurrently in separate processes.")

    subparsers.add_parser("status", help="Show local extraction and remote indexing status.")

    dedupe = subparsers.add_parser(
        "dedupe",
        help="Group documents that are the same real-world legal instrument scraped by several sources "
        "and keep only one canonical copy, both locally and in the OpenAI vector store.",
    )
    dedupe.add_argument(
        "--apply", action="store_true",
        help="Actually mark duplicates and delete their OpenAI files/local index rows. Without this flag, "
        "dedupe only previews the groups it would act on (default; this permanently deletes OpenAI files).",
    )
    dedupe.add_argument(
        "--source", action="append", default=[],
        help="Only consider documents from this source ID when grouping; repeat for multiple sources.",
    )

    subparsers.add_parser(
        "legal-build",
        help="Rebuild the structured legal instrument/relationship graph from titles in the current inventory.",
    )

    subparsers.add_parser(
        "article-graph-build",
        help="Rebuild the article-level legal cross-reference graph from the local retrieval index's chunks. "
        "Run legal-build first so instrument rows are fresh.",
    )

    legal_status = subparsers.add_parser(
        "legal-status",
        help="Look up an instrument's deterministic status (in force / modified / repealed) by free-text reference.",
    )
    legal_status.add_argument("reference", help="e.g. 'ordonnance-loi 69/009' or 'loi n 73-021 du 20 juillet 1973'")

    learn = subparsers.add_parser(
        "learn",
        help="Record a reviewed correction from a conversation so it refines all future answers.",
    )
    learn.add_argument("note", help="The confirmed lesson or correction, in the language the assistant should apply.")
    learn.add_argument("--source", default="", help="Optional reference (law, article, conversation) backing the note.")
    return parser


def record_learned_note(settings, note: str, reference: str = "") -> str:
    from datetime import date

    note = " ".join(note.split()).strip()
    if not note:
        raise ValueError("The note is empty.")
    path = settings.learned_notes_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(
            "# Learned notes (maintainer-reviewed)\n\n"
            "Human-confirmed corrections drawn from earlier conversations. Each line is "
            "injected into the assistant's instructions to refine future answers. Review "
            "before adding; remove anything superseded.\n\n",
            encoding="utf-8",
        )
    suffix = f" (réf. {reference.strip()})" if reference.strip() else ""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- [{date.today().isoformat()}] {note}{suffix}\n")
    return str(path)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    settings = get_settings()
    if args.command == "learn":
        path = record_learned_note(settings, args.note, args.source)
        print(json.dumps({"recorded": True, "learned_notes_path": path}, ensure_ascii=False, indent=2))
        return
    if args.command == "legal-build":
        state = load_state(settings.state_path)
        graph = LegalGraph(settings.legal_graph_path)
        with IndexingLock(settings.indexing_lock_path):
            result = graph.rebuild(state["documents"].values())
            # Must run in the same pass: rebuild()'s delete+reinsert of legal_instrument
            # rows (fresh row ids) silently nulls out every legal_article.instrument_id
            # link (ON DELETE SET NULL) unless this re-links them immediately after.
            article_result = graph.rebuild_articles_from_index(state["documents"], settings.local_index_path)
        result.update(article_result)
        result.update(graph.stats())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "article-graph-build":
        state = load_state(settings.state_path)
        graph = LegalGraph(settings.legal_graph_path)
        with IndexingLock(settings.indexing_lock_path):
            local_connection = sqlite3.connect(f"file:{settings.local_index_path}?mode=ro", uri=True)
            local_connection.row_factory = sqlite3.Row
            try:
                chunk_rows = local_connection.execute(
                    "SELECT c.document_id, c.locator, c.content, c.article_number FROM chunks c"
                ).fetchall()
            finally:
                local_connection.close()
            result = graph.rebuild_articles(state["documents"], chunk_rows)
        result.update(graph.stats())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "legal-status":
        graph = LegalGraph(settings.legal_graph_path)
        mentions = find_instrument_mentions(args.reference)
        if not mentions:
            print(json.dumps({"error": "No instrument type/number recognized in the reference text."}, ensure_ascii=False, indent=2))
            return
        results = []
        for mention in mentions:
            for row in graph.find_instruments(mention.type_label, mention.number):
                results.append(graph.status_for(row["instrument_id"]))
        print(json.dumps(results or {"error": "No match in the legal graph."}, ensure_ascii=False, indent=2, default=str))
        return
    indexer = FiscalIndexer(settings)
    if args.command == "status":
        result = indexer.summary()
    else:
        with IndexingLock(settings.indexing_lock_path):
            if args.command == "scan":
                result = indexer.scan()
            elif args.command == "dedupe":
                result = indexer.dedupe(dry_run=not args.apply, source_ids=set(args.source) or None)
            elif args.command == "prepare":
                result = indexer.prepare(
                    limit=args.limit,
                    use_ocr=args.ocr or args.force_ocr,
                    force_ocr=args.force_ocr,
                    source_ids=set(args.source) or None,
                    include_review_required=args.include_review_required,
                    max_pages=args.max_pages,
                    min_pages=args.min_pages,
                    workers=args.workers,
                )
            elif args.command == "index":
                result = indexer.index(
                    limit=args.limit,
                    use_ocr=args.ocr or args.force_ocr,
                    force_ocr=args.force_ocr,
                    source_ids=set(args.source) or None,
                    include_review_required=args.include_review_required,
                    max_pages=args.max_pages,
                    min_pages=args.min_pages,
                    workers=args.workers,
                )
            else:
                indexed = indexer.index_local(
                    limit=args.limit,
                    use_ocr=args.ocr,
                    source_ids=set(args.source) or None,
                    include_review_required=args.include_review_required,
                    max_pages=args.max_pages,
                    min_pages=args.min_pages,
                    workers=args.workers,
                )
                result = indexer.summary()
                result["locally_indexed_this_run"] = indexed
                for key in ("ocr_processed_this_run", "ocr_queue_at_start", "ocr_pending_in_scope"):
                    if key in indexer.last_prepare_result:
                        result[key] = indexer.last_prepare_result[key]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
