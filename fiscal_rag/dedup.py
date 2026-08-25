from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .legal_graph import parse_self_instrument


# Lower index = higher priority = kept as the canonical copy when several sources
# scraped the same real-world instrument. Official gazette/registry sources and
# primary government bodies rank above general-purpose legal aggregators and
# mirror sites, which is where this corpus's heaviest duplication comes from.
# leganews ranks just behind the gazette-tier sources despite being a general
# aggregator: its catalog is scraped from native .txt files rather than OCR'd
# PDFs, so its extractions don't carry the column-scrambling/garbling artifacts
# that show up throughout this corpus's scanned-PDF sources - a real quality
# signal for which copy is safest to keep as canonical.
SOURCE_PRIORITY: tuple[str, ...] = (
    "budget_officiel",
    "scribd",
    "lextenso",
    "leganet",
    "leganews",
    "natlex",
    "faolex",
    "dgi",
    "dgrad",
    "finances",
    "bcc",
    "cnss",
    "onecrdc",
    "onem",
    "government_public_affairs",
    "awa",
    "leganews_attachments",
    "anicns",
    "congomines",
    "droitcongolais",
)

# Word-shingle (n-gram) Jaccard similarity, not character-sequence comparison:
# this corpus's PDFs include multi-column Journal Officiel layouts whose text
# extraction interleaves columns, scrambling word order throughout the document
# even when it IS a clean re-scrape of the same instrument - order-sensitive
# comparison (e.g. difflib's SequenceMatcher.ratio()) was empirically found to
# score a confirmed genuine duplicate at 0.037 for exactly this reason, while
# character-frequency comparison (quick_ratio) went the other way and scored an
# unrelated document pair at 0.99 just for sharing French legal vocabulary.
# Shingles of SHINGLE_SIZE consecutive words are a middle ground: order-insensitive
# at the paragraph level (tolerates column-scrambling and reordering) while still
# requiring genuine multi-word PHRASE overlap (unlike single-word bag-of-words,
# which would be as easily fooled by shared boilerplate as character frequency).
#
# A second, independent false positive was found beyond the "Vu ..." citation
# case this replaced: a SERIES of short, distinct decrees issued together (e.g.
# 5 arretes each creating land districts for a DIFFERENT province) share so much
# masthead/closing-formula template boilerplate, relative to their short bodies,
# that raw full-text shingle overlap alone cleared 0.6+ despite zero substantive
# overlap - confirmed by reading actual content (different provinces, cities,
# boundaries). _operative_text() strips this pipeline's own synthetic metadata
# header plus everything before a document's first "Article" marker (masthead,
# "Vu ..." preamble) so comparison focuses on the part that actually varies
# between distinct instruments.
#
# Both thresholds below are calibrated against 27 real document pairs verified
# by independent manual reading: 18 from workflow run wf_3b8ed44f-e3f, plus the
# 5-province false-positive series (10 pairs) discovered afterward. At n=4 with
# _operative_text(), every confirmed same-document pair scored >= 0.710 and every
# confirmed different-document pair (including the province series) scored <=
# 0.595 - a clean gap. MIN_CONTENT_SIMILARITY sits close to the middle but
# leans toward the DIFFERENT side deliberately: an incorrect merge permanently
# deletes a document from the OpenAI vector store, while an incorrect exclusion
# only leaves redundancy in place - the two error types are not equally costly,
# so ties are broken against merging. (One known, accepted gap: a document pair
# with severely column-scrambled OCR on one side scored 0.367 in this same
# calibration - correctly excluded rather than risking a lower threshold that
# would reopen the province-series false positive.)
SHINGLE_SIZE = 4
MIN_CONTENT_SIMILARITY = 0.65
# Pure performance pre-filter (not a correctness gate - Jaccard's own union-based
# denominator already penalizes size mismatches): skip full shingle comparison for
# pairs so size-mismatched that no realistic overlap could clear MIN_CONTENT_SIMILARITY.
MIN_LENGTH_RATIO = 0.15


def _source_rank(source_id: str) -> int:
    try:
        return SOURCE_PRIORITY.index(source_id)
    except ValueError:
        return len(SOURCE_PRIORITY)


def _extracted_size(record: dict[str, Any]) -> int:
    extracted_path = record.get("extracted_path")
    if not extracted_path:
        return 0
    try:
        return Path(extracted_path).stat().st_size
    except OSError:
        return 0


def _normalize_text(text: str) -> str:
    stripped = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


# Every file this pipeline extracts starts with a synthetic "Fiscal reference
# source" metadata block (title/source/document id/repository path/category)
# that it added itself - not part of the original document - followed by this
# fixed marker before the actual page content begins.
_PIPELINE_HEADER_MARKER = "<!-- PAGE 1 -->"


def _operative_text(raw: str) -> str:
    """Strip this pipeline's own synthetic header, then skip to the document's
    first "Article" marker so comparison excludes shared masthead/"Vu ..."
    preamble boilerplate and focuses on the part that actually distinguishes
    one instrument from another. Falls back to the full (header-stripped) text
    when no "Article" marker is found, e.g. communiques or tables."""
    marker_index = raw.find(_PIPELINE_HEADER_MARKER)
    if marker_index != -1:
        raw = raw[marker_index:]
    normalized = _normalize_text(raw)
    article_index = normalized.find("article")
    return normalized[article_index:] if article_index != -1 else normalized


def _shingles(text: str, n: int = SHINGLE_SIZE) -> frozenset[str]:
    words = text.split()
    if len(words) < n:
        return frozenset({" ".join(words)}) if words else frozenset()
    return frozenset(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))


@dataclass(frozen=True)
class _ComparisonDoc:
    shingles: frozenset[str]
    word_count: int


def _load_comparison_doc(record: dict[str, Any]) -> _ComparisonDoc:
    extracted_path = record.get("extracted_path")
    if not extracted_path:
        return _ComparisonDoc(frozenset(), 0)
    try:
        raw = Path(extracted_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _ComparisonDoc(frozenset(), 0)
    operative = _operative_text(raw)
    return _ComparisonDoc(_shingles(operative), len(operative.split()))


def _is_same_document(doc_a: _ComparisonDoc, doc_b: _ComparisonDoc) -> bool:
    """True only when two documents' extracted text is substantially the same
    piece of writing, not merely two documents that reference the same law.
    See the SHINGLE_SIZE/MIN_CONTENT_SIMILARITY comments above for why this is
    a word-shingle Jaccard comparison rather than a character-sequence one."""
    if not doc_a.word_count or not doc_b.word_count:
        return False
    shorter, longer = sorted((doc_a.word_count, doc_b.word_count))
    if shorter / longer < MIN_LENGTH_RATIO:
        return False
    union = len(doc_a.shingles | doc_b.shingles)
    if not union:
        return False
    intersection = len(doc_a.shingles & doc_b.shingles)
    return intersection / union >= MIN_CONTENT_SIMILARITY


def find_exact_duplicate_groups(documents: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Group document_ids whose raw file content is byte-identical (same
    sha256) - the same PDF saved under a different name or path. This is a
    zero-ambiguity signal, unlike the title+content-similarity path below: no
    title parsing and no similarity threshold needed, because an exact hash
    match already proves identity. It exists because that title-based path
    structurally can't catch this case - a document whose title doesn't
    follow the corpus's "Type n DATE ..." citation convention (an informally
    named upload, training material, a manually-saved copy) never enters that
    candidate pool at all, no matter how identical its content is to another
    document's."""
    groups: dict[str, list[str]] = {}
    for document_id, record in documents.items():
        if not record.get("present", True) or record.get("duplicate_of"):
            continue
        sha = record.get("sha256")
        if not sha:
            continue
        groups.setdefault(sha, []).append(document_id)
    return {sha: ids for sha, ids in groups.items() if len(ids) > 1}


def find_exact_extracted_text_groups(documents: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Group different binaries that yield exactly the same operative evidence.

    This catches re-encoded or rescanned copies with different PDF hashes and
    non-parseable generic titles (for example, three copies named only "Code des
    impôts"). Unlike the fuzzy title pass, exact normalized text cannot merge two
    merely similar editions. Documents without usable extracted text are excluded.
    """
    candidates: list[tuple[str, dict[str, Any]]] = []
    for document_id, record in documents.items():
        if (
            not record.get("present", True)
            or record.get("duplicate_of")
            or record.get("status") != "ready"
            or not record.get("extracted_path")
        ):
            continue
        candidates.append((document_id, record))

    # Deliberately not pre-filtered by page_count: it's ingestion metadata, not
    # part of the hashed operative text, so it can legitimately differ between
    # two extractions of byte-identical text (a blank/cover page counted in one
    # but not the other, missing/zero page_count). Pre-filtering on it would
    # silently skip real duplicates, contradicting this pass's "exact text, no
    # fuzzy threshold" guarantee.
    groups: dict[str, list[str]] = {}
    for document_id, record in candidates:
        try:
            raw = Path(str(record["extracted_path"])).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        operative = _operative_text(raw)
        if len(operative) < 200:
            continue
        fingerprint = hashlib.sha256(operative.encode("utf-8")).hexdigest()
        groups.setdefault(fingerprint, []).append(document_id)
    return {fingerprint: ids for fingerprint, ids in groups.items() if len(ids) > 1}


def find_duplicate_groups(documents: dict[str, dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """Group document_ids that share the same title-anchored legal instrument
    identity, using legal_graph's title parser so a document only joins a group
    when its title confidently states the instrument it IS (not one it merely
    references). This is a candidate pool, not a final grouping: many corpus
    titles for an IMPLEMENTING text open with the base law's own reference
    (e.g. "Loi n 015/2002 ... Code du Travail, specialement en son article 169
    ; Vu ..."), so title agreement alone is not sufficient - plan_dedupe()
    additionally requires content agreement before treating any of these as
    true duplicates of one another."""
    groups: dict[tuple[str, str], list[str]] = {}
    for document_id, record in documents.items():
        if not record.get("present", True) or record.get("duplicate_of"):
            continue
        mention = parse_self_instrument(record.get("title") or "")
        if mention is None:
            continue
        key = (mention.type_label, mention.number_key)
        groups.setdefault(key, []).append(document_id)
    return {key: ids for key, ids in groups.items() if len(ids) > 1}


def select_canonical(document_ids: list[str], documents: dict[str, dict[str, Any]]) -> str:
    def rank(document_id: str) -> tuple[int, int, int]:
        record = documents[document_id]
        status_rank = 0 if record.get("status") == "ready" else 1
        return (_source_rank(record.get("source") or ""), status_rank, -_extracted_size(record))

    return min(document_ids, key=rank)


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_a] = root_b


def _cluster_by_content(document_ids: list[str], doc_of: Callable[[str], _ComparisonDoc]) -> list[list[str]]:
    """Full pairwise clustering (not "compare everyone to one pre-picked reference"):
    a title match alone tells us candidates MIGHT be the same instrument, but the
    highest-source-priority candidate is sometimes itself a short excerpt (e.g. a
    Journal Officiel bulletin citing one article) rather than the actual full text,
    which would wrongly exclude every genuine duplicate if used as the sole point of
    comparison. Clustering first finds which candidates truly agree with each other;
    select_canonical() then only has to choose among documents already confirmed to
    share the same content."""
    union_find = _UnionFind(document_ids)
    for i, doc_a in enumerate(document_ids):
        comparison_a = doc_of(doc_a)
        for doc_b in document_ids[i + 1 :]:
            if union_find.find(doc_a) == union_find.find(doc_b):
                continue
            if _is_same_document(comparison_a, doc_of(doc_b)):
                union_find.union(doc_a, doc_b)
    clusters: dict[str, list[str]] = {}
    for document_id in document_ids:
        clusters.setdefault(union_find.find(document_id), []).append(document_id)
    return [members for members in clusters.values() if len(members) > 1]


@dataclass
class DedupeGroupPreview:
    type_label: str
    number_key: str
    canonical_id: str
    canonical_title: str
    canonical_source: str
    duplicate_ids: list[str]
    excluded_ids: list[str] = field(default_factory=list)


@dataclass
class DedupeReport:
    groups_found: int
    documents_marked: int
    documents_excluded_by_content_check: int
    previews: list[DedupeGroupPreview] = field(default_factory=list)


def plan_dedupe(documents: dict[str, dict[str, Any]]) -> DedupeReport:
    previews: list[DedupeGroupPreview] = []
    documents_marked = 0
    documents_excluded = 0

    # Pass 1: exact byte-for-byte duplicates need no verification - an exact
    # hash match already proves identity, so these are resolved before (and
    # independently of) the title-based candidate pool below.
    already_resolved: set[str] = set()
    for sha, document_ids in find_exact_duplicate_groups(documents).items():
        canonical_id = select_canonical(document_ids, documents)
        duplicate_ids = [doc_id for doc_id in document_ids if doc_id != canonical_id]
        documents_marked += len(duplicate_ids)
        already_resolved.update(document_ids)
        previews.append(
            DedupeGroupPreview(
                type_label="Exact match (sha256)",
                number_key=sha[:12],
                canonical_id=canonical_id,
                canonical_title=documents[canonical_id].get("title", ""),
                canonical_source=documents[canonical_id].get("source", ""),
                duplicate_ids=duplicate_ids,
                excluded_ids=[],
            )
        )

    # Pass 2: exact normalized operative text. This remains a zero-ambiguity
    # merge signal while catching different PDF encodings and generic titles.
    unresolved_documents = {
        document_id: record for document_id, record in documents.items() if document_id not in already_resolved
    }
    for fingerprint, document_ids in find_exact_extracted_text_groups(unresolved_documents).items():
        canonical_id = select_canonical(document_ids, documents)
        duplicate_ids = [doc_id for doc_id in document_ids if doc_id != canonical_id]
        documents_marked += len(duplicate_ids)
        already_resolved.update(document_ids)
        previews.append(
            DedupeGroupPreview(
                type_label="Exact extracted text",
                number_key=fingerprint[:12],
                canonical_id=canonical_id,
                canonical_title=documents[canonical_id].get("title", ""),
                canonical_source=documents[canonical_id].get("source", ""),
                duplicate_ids=duplicate_ids,
                excluded_ids=[],
            )
        )

    # Pass 3: title-anchored candidates + content verification, for anything
    # not already resolved above.
    title_groups = {
        key: [doc_id for doc_id in ids if doc_id not in already_resolved]
        for key, ids in find_duplicate_groups(documents).items()
    }
    title_groups = {key: ids for key, ids in title_groups.items() if len(ids) > 1}
    doc_cache: dict[str, _ComparisonDoc] = {}

    def comparison_doc(document_id: str) -> _ComparisonDoc:
        if document_id not in doc_cache:
            doc_cache[document_id] = _load_comparison_doc(documents[document_id])
        return doc_cache[document_id]

    for (type_label, number_key), document_ids in title_groups.items():
        # A title group occasionally contains more than one genuine cluster (e.g.
        # two different sources both duplicating a short bulletin about article X,
        # unrelated to a full-text copy elsewhere in the same title group) - resolve
        # every cluster before deciding what counts as excluded for the group as a
        # whole, so a document merged in one cluster is never also reported excluded.
        resolved: list[tuple[str, list[str]]] = []
        for cluster in _cluster_by_content(document_ids, comparison_doc):
            # select_canonical() only ranks within a cluster whose members are
            # already pairwise-confirmed as the same content, so a short excerpt
            # that merely outranks everyone else by source priority can no longer
            # be chosen as canonical over the real full text of the instrument.
            canonical_id = select_canonical(cluster, documents)
            canonical_doc = comparison_doc(canonical_id)
            duplicate_ids: list[str] = []
            for document_id in cluster:
                if document_id == canonical_id:
                    continue
                # Re-check directly against the FINAL canonical, not just against
                # whichever cluster member the transitive union-find pass happened
                # to link it through - guards against similarity drifting across a
                # chain (A~B, B~C) where A and C alone would not have matched.
                if _is_same_document(canonical_doc, comparison_doc(document_id)):
                    duplicate_ids.append(document_id)
            if duplicate_ids:
                resolved.append((canonical_id, duplicate_ids))

        if not resolved:
            documents_excluded += len(document_ids)
            continue

        kept_ids = {canonical_id for canonical_id, _ in resolved}
        for _, duplicate_ids in resolved:
            kept_ids.update(duplicate_ids)
        excluded_ids = [doc_id for doc_id in document_ids if doc_id not in kept_ids]
        documents_marked += len(kept_ids) - len(resolved)
        documents_excluded += len(excluded_ids)
        for canonical_id, duplicate_ids in resolved:
            previews.append(
                DedupeGroupPreview(
                    type_label=type_label,
                    number_key=number_key,
                    canonical_id=canonical_id,
                    canonical_title=documents[canonical_id].get("title", ""),
                    canonical_source=documents[canonical_id].get("source", ""),
                    duplicate_ids=duplicate_ids,
                    excluded_ids=excluded_ids,
                )
            )
    previews.sort(key=lambda preview: len(preview.duplicate_ids), reverse=True)
    return DedupeReport(
        groups_found=len(previews),
        documents_marked=documents_marked,
        documents_excluded_by_content_check=documents_excluded,
        previews=previews,
    )
