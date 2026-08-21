from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any, Callable

from .catalog import discover_documents
from .config import Settings
from .extractor import extract_document, sha256_file, validate_ocr_configuration
from .legal_graph import LegalGraph
from .retrieval import LocalRetrievalIndex
from .state import load_state, save_state


ProgressCallback = Callable[[str], None]


def _ocr_already_attempted(record: dict[str, Any]) -> bool:
    return bool(
        record.get("ocr_attempted")
        or record.get("ocr_pages")
        or any(str(warning).startswith("OCR ") for warning in (record.get("warnings") or []))
    )


def _extract_worker(record: dict[str, Any], extracted_dir: Path, use_ocr: bool) -> dict[str, Any]:
    """Runs in a worker process for --workers > 1: the CPU-bound extraction step only.
    Deliberately touches no shared state - self.state / state.json are only ever
    written by the parent process, sequentially, as each worker's result comes back,
    so concurrent processes can never race on the same file. Must stay a top-level
    function (not a method or closure) so ProcessPoolExecutor can pickle it on
    Windows, which re-imports this module in each worker rather than forking."""
    path = Path(record["absolute_path"])
    try:
        content_hash = sha256_file(path)
        extraction = extract_document(record, extracted_dir, use_ocr=use_ocr)
        extracted_sha256 = sha256_file(Path(extraction["extracted_path"]))
        return {
            "id": record["id"],
            "ok": True,
            "extraction": extraction,
            "sha256": content_hash,
            "extracted_sha256": extracted_sha256,
            "size_bytes": record["size_bytes"],
            "mtime_ns": record["mtime_ns"],
            "ocr_attempted": bool(use_ocr and path.suffix.lower() == ".pdf"),
        }
    except Exception as error:
        return {"id": record["id"], "ok": False, "error": str(error)}


def _is_pending_ocr_candidate(record: dict[str, Any]) -> bool:
    path = Path(record.get("absolute_path", ""))
    return bool(
        path.suffix.lower() == ".pdf"
        and not _ocr_already_attempted(record)
        and (
            record.get("status") in {"needs_ocr", "insufficient_text"}
            or bool(record.get("empty_pages"))
        )
    )


class FiscalIndexer:
    def __init__(self, settings: Settings, progress: ProgressCallback | None = None) -> None:
        self.settings = settings
        self.progress = progress or print
        self.state = load_state(settings.state_path)
        self.local_index = LocalRetrievalIndex(settings.local_index_path)
        self.last_prepare_result: dict[str, Any] = {}

    def _eligible_records(
        self,
        source_ids: set[str] | None,
        include_review_required: bool,
    ) -> list[dict[str, Any]]:
        return [
            record
            for record in self.state["documents"].values()
            if record.get("present", True)
            and not record.get("duplicate_of")
            and (not source_ids or record.get("source") in source_ids)
            and (include_review_required or not record.get("requires_review"))
        ]

    def scan(
        self,
        source_ids: set[str] | None = None,
        include_review_required: bool = True,
        limit: int | None = None,
    ) -> dict[str, Any]:
        selected_source_ids = {
            source.id
            for source in self.settings.sources
            if source.enabled
            and (not source_ids or source.id in source_ids)
            and (include_review_required or not source.review_all)
        }
        documents = discover_documents(
            self.settings,
            source_ids=selected_source_ids,
            include_review_required=include_review_required,
            limit=limit,
        )
        known = self.state["documents"]
        for document in documents:
            current = known.get(document["id"], {})
            known[document["id"]] = {**current, **document}
        discovered_ids = {document["id"] for document in documents}
        if limit is None:
            for document_id, record in known.items():
                if record.get("source") in selected_source_ids:
                    record["present"] = document_id in discovered_ids
        save_state(self.settings.state_path, self.state)
        legal_graph = LegalGraph(self.settings.legal_graph_path)
        legal_graph.rebuild(known.values())
        # Must run immediately after rebuild(): legal_article's links to legal_instrument
        # are ON DELETE SET NULL, so rebuild()'s delete+reinsert of legal_instrument rows
        # (fresh row ids) silently nulls out every article-level link otherwise - and scan()
        # runs on every server startup (see server.py's main()), so this is not a rare path.
        legal_graph.rebuild_articles_from_index(known, self.settings.local_index_path)
        return self.summary()

    def prepare(
        self,
        limit: int | None = None,
        use_ocr: bool = False,
        source_ids: set[str] | None = None,
        include_review_required: bool = False,
        max_pages: int | None = None,
        min_pages: int | None = None,
        workers: int = 1,
    ) -> dict[str, Any]:
        if use_ocr:
            validate_ocr_configuration()
        self.scan(source_ids=source_ids, include_review_required=include_review_required, limit=limit)
        processed = 0
        records = self._eligible_records(source_ids, include_review_required)
        pending: list[tuple[int, dict[str, Any], Path]] = []
        skipped_by_page_filter = 0
        for position, record in enumerate(records, start=1):
            path = Path(record["absolute_path"])
            ocr_already_attempted = _ocr_already_attempted(record)
            unchanged = (
                record.get("prepared_size") == record.get("size_bytes")
                and record.get("prepared_mtime_ns") == record.get("mtime_ns")
                and record.get("extracted_path")
                and Path(record["extracted_path"]).is_file()
                and not (
                    use_ocr
                    and not ocr_already_attempted
                    and (
                        record.get("status") in {"needs_ocr", "insufficient_text"}
                        or bool(record.get("empty_pages"))
                    )
                )
            )
            if unchanged:
                continue
            # A page count is only known once a document has been extracted at least
            # once (e.g. the very run that first found it needs_ocr); documents never
            # yet extracted have no count to filter on and are never skipped by this -
            # this is for re-running a known needs_ocr backlog by size tier, not for
            # blindly guessing at unextracted documents.
            known_pages = record.get("page_count") or 0
            if known_pages:
                if max_pages is not None and known_pages > max_pages:
                    skipped_by_page_filter += 1
                    continue
                if min_pages is not None and known_pages < min_pages:
                    skipped_by_page_filter += 1
                    continue
            pending.append((position, record, path))
        if skipped_by_page_filter:
            self.progress(
                f"Page-count filter (max_pages={max_pages}, min_pages={min_pages}) deferred "
                f"{skipped_by_page_filter} document(s) to a later run."
            )

        selected = pending[:limit] if limit is not None else pending
        ocr_queue = [item for item in pending if item[2].suffix.lower() == ".pdf"] if use_ocr else []
        ocr_queue_positions = {record["id"]: position for position, (_, record, _) in enumerate(ocr_queue, start=1)}
        selected_ocr_count = sum(1 for _, _, path in selected if use_ocr and path.suffix.lower() == ".pdf")
        if use_ocr:
            selected_non_ocr_count = len(selected) - selected_ocr_count
            non_ocr_note = (
                f"; {selected_non_ocr_count} additional non-PDF document(s) are also selected"
                if selected_non_ocr_count
                else ""
            )
            self.progress(
                f"OCR queue: {selected_ocr_count}/{len(ocr_queue)} pending OCR documents will be processed "
                f"in this batch{non_ocr_note}."
            )
        if workers > 1 and len(selected) > 1:
            # Only the CPU-bound extraction itself runs in worker processes; every
            # record mutation and save_state() call below still happens here, in this
            # one process, strictly after each future resolves - so state.json only
            # ever has a single writer, exactly as in the sequential path.
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_extract_worker, record, self.settings.extracted_dir, use_ocr): (
                        library_position,
                        record,
                    )
                    for library_position, record, _path in selected
                }
                for future in concurrent.futures.as_completed(futures):
                    library_position, record = futures[future]
                    result_payload = future.result()
                    if result_payload["ok"]:
                        record.update(result_payload["extraction"])
                        record["sha256"] = result_payload["sha256"]
                        record["extracted_sha256"] = result_payload["extracted_sha256"]
                        record["prepared_size"] = result_payload["size_bytes"]
                        record["prepared_mtime_ns"] = result_payload["mtime_ns"]
                        record["indexed_content_hash"] = record.get("indexed_content_hash")
                        record["ocr_attempted"] = result_payload["ocr_attempted"]
                    else:
                        record.update({"status": "error", "error": result_payload["error"]})
                    processed += 1
                    ocr_progress = ""
                    if use_ocr and record["id"] in ocr_queue_positions:
                        ocr_progress = f"; OCR queue {ocr_queue_positions[record['id']]}/{len(ocr_queue)}"
                    self.progress(
                        f"[{processed}/{len(selected)}] Extracted {record['source']}: {record['filename']} "
                        f"(library {library_position}/{len(records)}{ocr_progress}; {workers} workers)"
                    )
                    save_state(self.settings.state_path, self.state)
        else:
            for batch_position, (library_position, record, path) in enumerate(selected, start=1):
                ocr_progress = ""
                if use_ocr and path.suffix.lower() == ".pdf":
                    ocr_progress = f"; OCR queue {ocr_queue_positions[record['id']]}/{len(ocr_queue)}"
                self.progress(
                    f"[{batch_position}/{len(selected)}] Extracting {record['source']}: {record['filename']} "
                    f"(library {library_position}/{len(records)}{ocr_progress})"
                )
                try:
                    content_hash = sha256_file(path)
                    extraction = extract_document(record, self.settings.extracted_dir, use_ocr=use_ocr)
                    record.update(extraction)
                    record["sha256"] = content_hash
                    record["extracted_sha256"] = sha256_file(Path(record["extracted_path"]))
                    record["prepared_size"] = record["size_bytes"]
                    record["prepared_mtime_ns"] = record["mtime_ns"]
                    record["indexed_content_hash"] = record.get("indexed_content_hash")
                    record["ocr_attempted"] = bool(use_ocr and path.suffix.lower() == ".pdf")
                except Exception as error:
                    record.update({"status": "error", "error": str(error)})
                processed += 1
                save_state(self.settings.state_path, self.state)
        result = self.summary()
        result["processed_this_run"] = processed
        if use_ocr:
            scoped_records = self._eligible_records(source_ids, include_review_required)
            scoped_ocr_pending = sum(1 for record in scoped_records if _is_pending_ocr_candidate(record))
            result["ocr_processed_this_run"] = selected_ocr_count
            result["ocr_queue_at_start"] = len(ocr_queue)
            result["ocr_pending_in_scope"] = scoped_ocr_pending
            self.progress(
                f"OCR progress: {selected_ocr_count}/{len(ocr_queue)} processed in this batch; "
                f"{scoped_ocr_pending} remain unattempted in the selected source(s)."
            )
        self.last_prepare_result = result
        return result

    def index(
        self,
        limit: int | None = None,
        use_ocr: bool = False,
        source_ids: set[str] | None = None,
        include_review_required: bool = False,
        max_pages: int | None = None,
        min_pages: int | None = None,
        workers: int = 1,
    ) -> dict[str, Any]:
        self.prepare(
            limit=limit,
            use_ocr=use_ocr,
            source_ids=source_ids,
            include_review_required=include_review_required,
            max_pages=max_pages,
            min_pages=min_pages,
            workers=workers,
        )
        local_indexed = self.index_local(
            limit=limit,
            source_ids=source_ids,
            include_review_required=include_review_required,
            prepare_first=False,
        )

        remote_indexed = 0
        remote_error = ""
        should_index_openai = self.settings.provider in {"auto", "openai"}
        if should_index_openai and self.settings.api_key:
            try:
                remote_indexed = self._index_openai(
                    limit=limit,
                    source_ids=source_ids,
                    include_review_required=include_review_required,
                )
            except Exception as error:
                if self.settings.provider == "openai" or not self.settings.fallback_enabled:
                    raise
                remote_error = str(error)
                self.progress(f"OpenAI indexing unavailable; local fallback remains ready: {error}")
        elif self.settings.provider == "openai":
            raise RuntimeError("OPENAI_API_KEY is not configured. Set it in .env before OpenAI indexing.")

        result = self.summary()
        result["locally_indexed_this_run"] = local_indexed
        result["indexed_this_run"] = remote_indexed
        for key in ("ocr_processed_this_run", "ocr_queue_at_start", "ocr_pending_in_scope"):
            if key in self.last_prepare_result:
                result[key] = self.last_prepare_result[key]
        if remote_error:
            result["remote_index_error"] = remote_error
        return result

    def index_local(
        self,
        limit: int | None = None,
        use_ocr: bool = False,
        source_ids: set[str] | None = None,
        include_review_required: bool = False,
        prepare_first: bool = True,
        max_pages: int | None = None,
        min_pages: int | None = None,
        workers: int = 1,
    ) -> int:
        if prepare_first:
            self.prepare(
                limit=limit,
                use_ocr=use_ocr,
                source_ids=source_ids,
                include_review_required=include_review_required,
                max_pages=max_pages,
                min_pages=min_pages,
                workers=workers,
            )

        missing_ids = [
            document_id
            for document_id, record in self.state["documents"].items()
            if not record.get("present", True)
        ]
        if missing_ids:
            self.local_index.remove_documents(missing_ids)

        indexed = 0
        records = self._eligible_records(source_ids, include_review_required)
        pending: list[tuple[int, dict[str, Any], Path, str]] = []
        for position, record in enumerate(records, start=1):
            if record.get("status") != "ready":
                continue
            extracted_path = Path(record.get("extracted_path", ""))
            if not extracted_path.is_file():
                continue
            extracted_content_hash = record.get("extracted_sha256") or record.get("sha256", "")
            if (
                record.get("local_indexed_content_hash") == extracted_content_hash
                and record.get("local_indexed_superseded_note", "") == record.get("superseded_note", "")
                and self.settings.local_index_path.is_file()
            ):
                continue
            pending.append((position, record, extracted_path, extracted_content_hash))

        selected = pending[:limit] if limit is not None else pending
        for batch_position, (library_position, record, extracted_path, extracted_content_hash) in enumerate(
            selected, start=1
        ):
            self.progress(
                f"[{batch_position}/{len(selected)}] Local indexing {record['source']}: {record['filename']} "
                f"(library {library_position}/{len(records)})"
            )
            chunk_count = self.local_index.index_document(record)
            record["local_indexed_content_hash"] = extracted_content_hash
            record["local_indexed_superseded_note"] = record.get("superseded_note", "")
            record["local_index_status"] = "indexed"
            record["local_chunk_count"] = chunk_count
            save_state(self.settings.state_path, self.state)
            indexed += 1
        return indexed

    def _index_openai(
        self,
        limit: int | None,
        source_ids: set[str] | None,
        include_review_required: bool,
    ) -> int:
        if not self.settings.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        from openai import APIConnectionError, AuthenticationError, OpenAI, PermissionDeniedError

        # max_retries gives the SDK's own exponential backoff a chance to ride
        # out a blip before it ever reaches the per-document handling below.
        client = OpenAI(api_key=self.settings.api_key, max_retries=5)
        if not self.state.get("vector_store_id"):
            vector_store = client.vector_stores.create(
                name=self.state.get("vector_store_name", "RDC Fiscal Reference"),
                description="Page-aware fiscal, legal, accounting, and financial reference documents for the DRC.",
            )
            self.state["vector_store_id"] = vector_store.id
            save_state(self.settings.state_path, self.state)

        indexed = 0
        consecutive_connection_failures = 0
        records = self._eligible_records(source_ids, include_review_required)
        for position, record in enumerate(records, start=1):
            if record.get("status") != "ready":
                continue
            extracted_content_hash = record.get("extracted_sha256") or record.get("sha256")
            if record.get("indexed_content_hash") == extracted_content_hash and record.get("openai_file_id"):
                continue
            if limit is not None and indexed >= limit:
                break

            extracted_path = Path(record["extracted_path"])
            self.progress(f"[{position}/{len(records)}] Uploading {record['source']}: {record['filename']}")
            previous_file_id = record.get("openai_file_id")
            try:
                with extracted_path.open("rb") as handle:
                    uploaded = client.files.create(file=handle, purpose="assistants")
                attributes: dict[str, str | int | float | bool] = {
                    "document_id": record["id"],
                    "source": record["source"],
                    "category": (record.get("category") or "uncategorized")[:512],
                }
                try:
                    client.vector_stores.files.create_and_poll(
                        uploaded.id,
                        vector_store_id=self.state["vector_store_id"],
                        attributes=attributes,
                    )
                except Exception:
                    try:
                        client.files.delete(uploaded.id)
                    except Exception:
                        pass
                    raise
            except (AuthenticationError, PermissionDeniedError):
                # Not worth continuing: every subsequent upload would fail the same way.
                raise
            except APIConnectionError as error:
                # A single dropped connection should not abort an entire multi-hour
                # batch. Skip this document - it stays un-indexed and is retried on
                # the next 'index' run - but stop if failures keep piling up, since
                # that likely means the network is down outright, not just flaky.
                consecutive_connection_failures += 1
                record.setdefault("warnings", []).append(f"Upload skipped after a connection error: {error}")
                save_state(self.settings.state_path, self.state)
                if consecutive_connection_failures >= 8:
                    raise RuntimeError(
                        f"Stopping after {consecutive_connection_failures} consecutive connection errors "
                        f"({indexed} document(s) indexed this run); the rest will resume on the next 'index' run."
                    ) from error
                continue

            consecutive_connection_failures = 0
            record["openai_file_id"] = uploaded.id
            record["indexed_content_hash"] = extracted_content_hash
            record["index_status"] = "indexed"
            save_state(self.settings.state_path, self.state)
            indexed += 1

            if previous_file_id and previous_file_id != uploaded.id:
                try:
                    client.files.delete(previous_file_id)
                except Exception:
                    record.setdefault("warnings", []).append(f"Old remote file was not deleted: {previous_file_id}")
                    save_state(self.settings.state_path, self.state)

        return indexed

    def dedupe(
        self,
        dry_run: bool = True,
        source_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Group documents that are the same real-world legal instrument scraped by
        several sources, keep one canonical copy, and (unless dry_run) remove the
        rest from the local index and the OpenAI vector store. Marks non-canonical
        records with duplicate_of instead of touching 'present', because scan()
        recomputes 'present' from disk discovery on every run and would otherwise
        silently resurrect a marked duplicate the next time it runs."""
        from dataclasses import asdict

        from .dedup import plan_dedupe

        documents = self.state["documents"]
        scoped = (
            {doc_id: record for doc_id, record in documents.items() if record.get("source") in source_ids}
            if source_ids
            else documents
        )
        report = plan_dedupe(scoped)
        result = {
            "dry_run": dry_run,
            "groups_found": report.groups_found,
            "documents_marked": report.documents_marked,
            "documents_excluded_by_content_check": report.documents_excluded_by_content_check,
            "previews": [asdict(preview) for preview in report.previews],
        }
        if dry_run or not report.previews:
            return result

        duplicate_to_canonical = {
            document_id: preview.canonical_id
            for preview in report.previews
            for document_id in preview.duplicate_ids
        }

        client = None
        if self.settings.api_key:
            from openai import OpenAI

            client = OpenAI(api_key=self.settings.api_key, max_retries=5)

        for position, (document_id, canonical_id) in enumerate(duplicate_to_canonical.items(), start=1):
            record = documents[document_id]
            self.progress(
                f"[{position}/{len(duplicate_to_canonical)}] Marking duplicate {record.get('source')}: "
                f"{record.get('filename')} (canonical: {canonical_id})"
            )
            # Record where this instrument can still be consulted even after its
            # copy stops being searchable, so citations for the canonical can note
            # it: "also available via LegaNews, AWA-Afrika, ...".
            canonical_record = documents[canonical_id]
            mirrors = canonical_record.setdefault("mirrors", [])
            mirror_entry = {
                "source": record.get("source"),
                "source_label": record.get("source_label"),
                "source_url": record.get("source_url"),
            }
            if mirror_entry["source_url"] and not any(m.get("source_url") == mirror_entry["source_url"] for m in mirrors):
                mirrors.append(mirror_entry)
            record["duplicate_of"] = canonical_id
            file_id = record.get("openai_file_id")
            if client and file_id:
                try:
                    client.files.delete(file_id)
                except Exception:
                    record.setdefault("warnings", []).append(f"Duplicate remote file was not deleted: {file_id}")
            record["openai_file_id"] = None
            record["index_status"] = None
            record["indexed_content_hash"] = None
            record["local_index_status"] = None
            record["local_indexed_content_hash"] = None
            save_state(self.settings.state_path, self.state)

        self.local_index.remove_documents(duplicate_to_canonical.keys())
        return result

    def summary(self) -> dict[str, Any]:
        records = [record for record in self.state["documents"].values() if record.get("present", True)]
        default_ocr_records = [record for record in records if not record.get("requires_review")]
        by_source: dict[str, int] = {}
        for record in records:
            by_source[record.get("source", "unknown")] = by_source.get(record.get("source", "unknown"), 0) + 1
        local_stats = self.local_index.stats()
        return {
            "discovered": len(records),
            "prepared": sum(1 for record in records if record.get("status") in {"ready", "needs_ocr", "insufficient_text"}),
            "ready": sum(1 for record in records if record.get("status") == "ready"),
            "needs_ocr": sum(1 for record in records if record.get("status") == "needs_ocr"),
            "ocr_pending": sum(1 for record in default_ocr_records if _is_pending_ocr_candidate(record)),
            "ocr_pending_review_required": sum(
                1 for record in records if record.get("requires_review") and _is_pending_ocr_candidate(record)
            ),
            "ocr_attempted_unresolved": sum(
                1
                for record in default_ocr_records
                if _ocr_already_attempted(record)
                and record.get("status") in {"needs_ocr", "insufficient_text"}
            ),
            "insufficient_text": sum(1 for record in records if record.get("status") == "insufficient_text"),
            "review_required": sum(1 for record in records if record.get("requires_review")),
            "errors": sum(1 for record in records if record.get("status") == "error"),
            "indexed": sum(1 for record in records if record.get("index_status") == "indexed"),
            "locally_indexed": local_stats["documents"],
            "local_chunks": local_stats["chunks"],
            "local_index_path": str(self.settings.local_index_path),
            "vector_store_id": self.state.get("vector_store_id"),
            "by_source": by_source,
            "legal_graph": LegalGraph(self.settings.legal_graph_path).stats(),
        }
