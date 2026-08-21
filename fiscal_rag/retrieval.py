from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
TARGET_CHUNK_CHARACTERS = 4_500
MAX_QUERY_TERMS = 64
DEFAULT_RESULT_LIMIT = 14
DEFAULT_CONTEXT_CHARACTERS = 60_000
MAX_CHUNKS_PER_DOCUMENT = 5
# When a question names a specific legal instrument, corpus-wide chunk ranking
# starves it: a long code's individual pages compete against every other
# document's chunks for the same top-N slots, and generic shared terms let
# short, less-relevant documents win. Reading the named instrument directly
# sidesteps that entirely.
# Even ranked in isolation (within-document only, no cross-corpus competition),
# BM25 can still bury the one article that actually answers a broad question:
# a criminal provision's specific vocabulary ('harcele', 'usurpe', 'fausse
# information') shares little with the question's own generic terms ('code',
# 'sanctions', 'liberte', 'constitution'). Confirmed on a real case: the
# article that precisely answered a free-expression/sanctions question ranked
# 118th of 179 pages within its own document. Only reading the whole document
# reliably avoids this class of miss - hence a cap generous enough to cover
# real DRC codes in full (~130k tokens for a 523k-character, 179-page code)
# rather than one that still forces a lossy chunk selection for them.
FULL_TEXT_CHARACTER_CAP = 900_000
MAX_NAMED_INSTRUMENT_DOCUMENTS = 2
DOCUMENT_SCOPED_CHUNK_LIMIT = 10
# A broad, multi-concept question against a very long single document (a
# 70-180 page code) dilutes BM25 scores: generic shared terms ("code",
# "constitution", "republique") let many short, less-relevant documents
# outrank the specific passage that actually answers the question. SQLite
# still scores every MATCHing row before truncating to LIMIT, so widening
# this candidate pool costs little (ranking, not the cutoff, dominates
# query cost) while giving relevant chunks buried deep in a long document a
# real chance to reach the per-document selection below.
CANDIDATE_POOL_SIZE = 500

STOP_WORDS = {
    "ai",
    "au",
    "aux",
    "afin",
    "ainsi",
    "avec",
    "avait",
    "ce",
    "ces",
    "cet",
    "cette",
    "citez",
    "comme",
    "dans",
    "date",
    "de",
    "document",
    "du",
    "des",
    "donc",
    "elle",
    "elles",
    "en",
    "est",
    "et",
    "était",
    "été",
    "être",
    "faire",
    "il",
    "ils",
    "les",
    "le",
    "la",
    "leur",
    "leurs",
    "mais",
    "ne",
    "nous",
    "ou",
    "page",
    "par",
    "pas",
    "plus",
    "pour",
    "précisez",
    "qu",
    "que",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "qui",
    "sa",
    "sans",
    "se",
    "ses",
    "son",
    "sont",
    "sur",
    "un",
    "une",
    "vous",
    "vrais",
    "y",
}

TERM_EXPANSIONS = {
    "5": ("cinq", "cinquième"),
    "construction": ("construit", "immeuble"),
    "constructions": ("construit", "immeuble"),
    "exonération": ("exempt",),
    "exonéré": ("exempt", "exemption"),
    "exonérée": ("exempt", "exemption"),
    "exonérés": ("exempt", "exemption"),
    "exonérées": ("exempt", "exemption"),
    "locatif": ("locatifs", "location"),
    "nouvelle": ("nouvellement",),
    "nouvelles": ("nouvellement",),
    "revenu": ("revenus",),
}

RDC_EAST_PATTERN = re.compile(
    r"(?:partie\s+de\s+l[’']est|est\s+(?:de|du)\s+(?:la\s+)?(?:rdc|république|congo))",
    flags=re.UNICODE,
)
RDC_EAST_TERMS = ("orientale", "nord-kivu", "sud-kivu", "maniema")

LOCATOR_PATTERN = re.compile(r"(?m)^##\s+(Page\s+\d+|Sheet:\s*[^\r\n]+|Section\s+\d+)\s*$")

# Matches a French legal article header (e.g. "Article 6 :", "Article 1er", "ARTICLE 15
# bis."). Not anchored to line start: extracted text routinely runs one short article
# straight into the next on the same line ("...GUPEC. Article 33 : La batisse..."), so a
# newline requirement would miss most real headers. Requires a literal capitalized
# "Article"/"ARTICLE" (not the re.I-matched lowercase "article") specifically to tell a
# genuine header apart from an inline cross-reference like "conformement a l'article 12",
# which French legal drafting convention always writes lowercase.
ARTICLE_HEADER_PATTERN = re.compile(
    r"\b(?:Article|ARTICLE)[ \t]+(1er|premier|\d+(?:[ \t]*(?:bis|ter|quater|quinquies|sexies|septies))?)\b"
)

# Recency-aware ranking: newer legal texts outrank older ones at similar
# relevance, without burying old documents that are uniquely relevant.
YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20\d{2})\b")
OBSOLETE_TITLE_MARKER = "[TEXTE OBSOLÈTE"
RECENCY_PENALTY_PER_YEAR = 0.12
RECENCY_MAX_AGE_YEARS = 15
RECENCY_UNKNOWN_AGE_YEARS = 6
OBSOLETE_EXTRA_PENALTY = 2.0


def document_year(title: str, published_date: str = "") -> int | None:
    """Infer the legal year of a document, preferring years named in the title.

    The catalog's published_date is often only the scrape timestamp, so it is
    used strictly as a fallback when the title names no plausible year.
    """
    ceiling = date.today().year + 1
    years = [int(value) for value in YEAR_PATTERN.findall(title) if int(value) <= ceiling]
    if years:
        return max(years)
    match = YEAR_PATTERN.search(published_date or "")
    return int(match.group(0)) if match and int(match.group(0)) <= ceiling else None


def _recency_adjusted_score(score: float, title: str, published_date: str) -> float:
    year = document_year(title, published_date)
    age = min(max(date.today().year - year, 0), RECENCY_MAX_AGE_YEARS) if year else RECENCY_UNKNOWN_AGE_YEARS
    penalty = age * RECENCY_PENALTY_PER_YEAR
    if title.startswith(OBSOLETE_TITLE_MARKER):
        penalty += OBSOLETE_EXTRA_PENALTY
    return score + penalty


@dataclass(frozen=True)
class RetrievedChunk:
    document_id: str
    title: str
    source: str
    source_label: str
    category: str
    published_date: str
    source_url: str
    locator: str
    content: str
    score: float
    article_number: str | None = None

    def citation(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "source": self.source,
            "source_label": self.source_label,
            "category": self.category,
            "published_date": self.published_date,
            "source_url": self.source_url,
            "pdf_url": f"/documents/{self.document_id}",
            "locator": self.locator,
        }


def _split_large_section(text: str, target: int = TARGET_CHUNK_CHARACTERS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    blocks = [block.strip() for block in re.split(r"\n\s*\n|(?<=\.)\s+(?=[A-ZÀ-ÖØ-Þ])", text) if block.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        if len(block) > target:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_size = 0
            for start in range(0, len(block), target):
                chunks.append(block[start : start + target])
            continue
        additional = len(block) + (2 if current else 0)
        if current and current_size + additional > target:
            chunks.append("\n\n".join(current))
            current = []
            current_size = 0
        current.append(block)
        current_size += additional
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_into_articles(text: str) -> list[tuple[str | None, str]]:
    """Split a section's text at 'Article N' headers into (article_number, content)
    segments, so each resulting chunk can be tagged with the one article it actually
    belongs to. Text before the first header (preamble, definitions, table of contents)
    is tagged None. Text with no headers at all is returned as a single None segment."""
    matches = list(ARTICLE_HEADER_PATTERN.finditer(text))
    if not matches:
        return [(None, text)]

    segments: list[tuple[str | None, str]] = []
    if matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            segments.append((None, preamble))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        article_number = match.group(1).strip()
        segment_text = text[start:end].strip()
        if segment_text:
            segments.append((article_number, segment_text))
    return segments


def chunk_extracted_text(text: str) -> list[tuple[str, str, str | None]]:
    """Split extracted Markdown while retaining page/sheet/section locators and, when
    the text names one, the specific article each chunk belongs to. Citations can then
    read the article number from this structured field instead of asking the model to
    re-read it out of raw (sometimes OCR-noisy) chunk text."""
    matches = list(LOCATOR_PATTERN.finditer(text))
    sections: list[tuple[str, str]] = (
        [
            (
                match.group(1).strip(),
                text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)],
            )
            for index, match in enumerate(matches)
        ]
        if matches
        else [("Document", text)]
    )

    chunks: list[tuple[str, str, str | None]] = []
    for locator, section_text in sections:
        for article_number, article_text in _split_into_articles(section_text):
            parts = _split_large_section(article_text)
            for part, content in enumerate(parts, start=1):
                part_locator = locator if len(parts) == 1 else f"{locator}, part {part}"
                chunks.append((part_locator, content, article_number))
    return chunks


def _search_terms(question: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    candidates = re.findall(r"[^\W_]{2,}|\d+(?:[.,]\d+)?", normalized, flags=re.UNICODE)
    selected: list[str] = []

    if RDC_EAST_PATTERN.search(normalized):
        selected.extend(RDC_EAST_TERMS)

    for candidate in candidates:
        if candidate in STOP_WORDS:
            continue
        for term in (candidate, *TERM_EXPANSIONS.get(candidate, ())):
            if term not in selected:
                selected.append(term)
                if len(selected) >= MAX_QUERY_TERMS:
                    break
        if len(selected) >= MAX_QUERY_TERMS:
            break
    if not selected:
        selected = list(dict.fromkeys(candidates))[:MAX_QUERY_TERMS]
    return selected


def build_fts_query(question: str) -> str:
    terms = _search_terms(question)
    return " OR ".join(f'"{term.replace(chr(34), "")}"*' for term in terms)


def _requested_article_numbers(question: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    numbers: set[str] = set()
    pattern = re.compile(r"\barticles?\s+(\d+(?:\s*(?:,|;|et|à|a|-)\s*\d+)*)", flags=re.UNICODE)
    for match in pattern.finditer(normalized):
        found = re.findall(r"\d+", match.group(1))
        numbers.update(found)
        if len(found) == 2 and re.search(r"(?:à|\ba\b|-)", match.group(1)):
            start, end = (int(value) for value in found)
            if 0 <= end - start <= 20:
                numbers.update(str(value) for value in range(start, end + 1))
    return numbers


def _article_reference_hits(content: str, requested: set[str]) -> int:
    if not requested:
        return 0
    found = set(re.findall(r"\barti\w{0,10}\s*[.:°º-]*\s*(\d+)\b", content.casefold(), flags=re.UNICODE))
    return len(found & requested)


_INFORMAL_CODE_STOPWORDS = {"du", "de", "des", "la", "le", "les", "l", "d", "un", "une", "et"}
_INFORMAL_CODE_PATTERN = re.compile(r"\bcode\b\s+(.+)", flags=re.IGNORECASE | re.UNICODE)


def _informal_code_keyword(question: str) -> str | None:
    """Most real questions name a law informally ('le code du numérique', 'le code des
    impots') rather than by formal citation number. Pull the single head noun after
    'code' and its leading article(s) - not more, or a following verb ('prevoit') gets
    swept in too - so it can be matched against document titles directly."""
    normalized = unicodedata.normalize("NFKD", question)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char)).casefold()
    match = _INFORMAL_CODE_PATTERN.search(normalized)
    if not match:
        return None
    for word in re.findall(r"[a-z0-9]+", match.group(1)):
        if word in _INFORMAL_CODE_STOPWORDS:
            continue
        return word
    return None


def _match_informal_code_reference(question: str, state_documents: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    keyword = _informal_code_keyword(question)
    if not keyword:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for record in state_documents.values():
        if record.get("status") != "ready" or not record.get("present", True):
            continue
        if record.get("superseded_note"):
            continue
        title = str(record.get("title") or "")
        normalized_title = unicodedata.normalize("NFKD", title)
        normalized_title = "".join(char for char in normalized_title if not unicodedata.combining(char)).casefold()
        if "code" not in normalized_title or keyword not in normalized_title:
            continue
        score = 2.0 if "portant code" in normalized_title else 0.0
        score += min(float(record.get("character_count", 0)) / 1_000_000, 1.0)
        if best is None or score > best[0]:
            best = (score, record)
    return best[1] if best else None


# Foundational laws that answer a whole category of common questions even
# though the question never names the law itself (e.g. "obligations fiscales
# d'une ASBL" never says "Loi 004/2001"), so neither find_instrument_mentions()
# nor _match_informal_code_reference() has anything to match against - a
# real, confirmed case where corpus-wide file_search ranking consistently
# failed to surface the one instrument that actually answers the question.
# content_marker/min_mentions guards against this corpus's title-extraction
# bug where an unrelated document's title gets mis-parsed as this very law
# (from a "Vu ..." citation) - a genuine copy discusses the topic dozens of
# times in its own body, a citing document once or not at all (verified: 0-1
# vs. 48-97 occurrences across this instrument's actual corpus candidates).
_TOPIC_INSTRUMENTS: tuple[dict[str, Any], ...] = (
    {
        "triggers": ("asbl", "association sans but lucratif", "associations sans but lucratif"),
        "instrument_key": ("Loi", "4/2001"),
        "content_marker": "association",
        "min_mentions": 10,
    },
)


def _match_topic_instrument(question: str, state_documents: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    from .dedup import _operative_text
    from .dedup import select_canonical as _select_canonical_by_priority
    from .legal_graph import parse_self_instrument

    normalized_question = unicodedata.normalize("NFKD", question)
    normalized_question = "".join(char for char in normalized_question if not unicodedata.combining(char)).casefold()

    for topic in _TOPIC_INSTRUMENTS:
        if not any(trigger in normalized_question for trigger in topic["triggers"]):
            continue
        candidate_ids: list[str] = []
        for document_id, record in state_documents.items():
            if record.get("status") != "ready" or not record.get("present", True):
                continue
            if record.get("superseded_note") or record.get("duplicate_of"):
                continue
            mention = parse_self_instrument(str(record.get("title") or ""))
            if mention is None or (mention.type_label, mention.number_key) != topic["instrument_key"]:
                continue
            extracted_path = record.get("extracted_path")
            if not extracted_path:
                continue
            try:
                raw = Path(extracted_path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _operative_text(raw).count(topic["content_marker"]) < topic["min_mentions"]:
                continue
            candidate_ids.append(document_id)
        if candidate_ids:
            best_id = _select_canonical_by_priority(candidate_ids, state_documents)
            return state_documents[best_id]
    return None


class LocalRetrievalIndex:
    """A portable SQLite FTS5 index used when hosted File Search is unavailable."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    published_date TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    locator TEXT NOT NULL,
                    content TEXT NOT NULL,
                    article_number TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    title,
                    content,
                    document_id UNINDEXED,
                    source UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and int(version[0]) != SCHEMA_VERSION:
                raise RuntimeError("The local retrieval index has an unsupported schema. Rebuild it with cli.py index-local.")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def index_document(self, record: dict[str, Any]) -> int:
        extracted_path = Path(record["extracted_path"])
        text = extracted_path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_extracted_text(text)
        if not chunks:
            return 0

        self.ensure_schema()
        with self._connect() as connection:
            old_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT chunk_id FROM chunks WHERE document_id=?",
                    (record["id"],),
                )
            ]
            if old_ids:
                connection.executemany("DELETE FROM chunks_fts WHERE rowid=?", ((chunk_id,) for chunk_id in old_ids))
            connection.execute("DELETE FROM chunks WHERE document_id=?", (record["id"],))
            connection.execute(
                """
                INSERT INTO documents(document_id, title, source, source_label, category, published_date, source_url, content_hash)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    title=excluded.title,
                    source=excluded.source,
                    source_label=excluded.source_label,
                    category=excluded.category,
                    published_date=excluded.published_date,
                    source_url=excluded.source_url,
                    content_hash=excluded.content_hash
                """,
                (
                    record["id"],
                    record["title"],
                    record["source"],
                    record["source_label"],
                    record.get("category", ""),
                    record.get("published_date", ""),
                    record.get("source_url", ""),
                    record.get("extracted_sha256") or record.get("sha256", ""),
                ),
            )
            for locator, content, article_number in chunks:
                cursor = connection.execute(
                    "INSERT INTO chunks(document_id, locator, content, article_number) VALUES(?, ?, ?, ?)",
                    (record["id"], locator, content, article_number),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, title, content, document_id, source) VALUES(?, ?, ?, ?, ?)",
                    (cursor.lastrowid, record["title"], content, record["id"], record["source"]),
                )
        return len(chunks)

    def remove_documents(self, document_ids: Iterable[str]) -> int:
        self.ensure_schema()
        removed = 0
        with self._connect() as connection:
            for document_id in document_ids:
                old_ids = [
                    row[0]
                    for row in connection.execute("SELECT chunk_id FROM chunks WHERE document_id=?", (document_id,))
                ]
                if old_ids:
                    connection.executemany("DELETE FROM chunks_fts WHERE rowid=?", ((chunk_id,) for chunk_id in old_ids))
                    removed += 1
                connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
                connection.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
        return removed

    def _fetch_candidates(
        self,
        connection: sqlite3.Connection,
        question: str,
        sources: list[str] | None,
        limit: int,
        requested_articles: set[str],
    ) -> list[sqlite3.Row]:
        query = build_fts_query(question)
        if not query:
            return []
        filters = ""
        parameters: list[Any] = [query]
        if sources:
            placeholders = ",".join("?" for _ in sources)
            filters = f" AND d.source IN ({placeholders})"
            parameters.extend(sources)
        parameters.append(
            max(
                CANDIDATE_POOL_SIZE,
                limit * (8 if requested_articles else 3),
                100 if requested_articles else limit,
            )
        )
        sql = f"""
            SELECT
                c.document_id, d.title, d.source, d.source_label, d.category,
                d.published_date, d.source_url, c.locator, c.content, c.article_number,
                bm25(chunks_fts, 5.0, 1.0, 0.0, 0.0) AS score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.rowid
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ?{filters}
            ORDER BY score
            LIMIT ?
        """
        return connection.execute(sql, parameters).fetchall()

    def search(
        self,
        question: str,
        sources: list[str] | None = None,
        limit: int = DEFAULT_RESULT_LIMIT,
        max_context_characters: int = DEFAULT_CONTEXT_CHARACTERS,
        extra_queries: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """extra_queries (e.g. LLM-rewritten phrasings using statutory vocabulary the
        raw question doesn't share) are searched independently and merged in: a chunk
        that only the rewritten query finds is included exactly as if the original
        question had found it. bm25 scores aren't comparable across different queries
        (different term sets produce different score scales), so cross-query ranking
        uses each row's rank position within its own query, not raw score - otherwise
        one query's naturally larger-magnitude scores could dominate every slot."""
        if not self.path.is_file():
            raise RuntimeError("No local retrieval index is configured. Run: python cli.py index-local")
        requested_articles = _requested_article_numbers(question)

        with self._connect() as connection:
            best_rank: dict[tuple[str, str], int] = {}
            candidates: dict[tuple[str, str], sqlite3.Row] = {}
            for variant in [question, *(extra_queries or [])]:
                variant_rows = self._fetch_candidates(connection, variant, sources, limit, requested_articles)
                for rank, row in enumerate(variant_rows):
                    key = (str(row["document_id"]), str(row["locator"]))
                    if key not in best_rank or rank < best_rank[key]:
                        best_rank[key] = rank
                        candidates[key] = row
            rows = list(candidates.values())

        rows = sorted(
            rows,
            key=lambda row: (
                -_article_reference_hits(str(row["content"]), requested_articles) if requested_articles else 0,
                best_rank[(str(row["document_id"]), str(row["locator"]))],
                _recency_adjusted_score(float(row["score"]), str(row["title"]), str(row["published_date"])),
            ),
        )

        selected: list[RetrievedChunk] = []
        per_document: dict[str, int] = {}
        used_characters = 0
        for row in rows:
            document_id = str(row["document_id"])
            if per_document.get(document_id, 0) >= MAX_CHUNKS_PER_DOCUMENT:
                continue
            content = str(row["content"])
            if selected and used_characters + len(content) > max_context_characters:
                continue
            selected.append(
                RetrievedChunk(
                    document_id=document_id,
                    title=str(row["title"]),
                    source=str(row["source"]),
                    source_label=str(row["source_label"]),
                    category=str(row["category"]),
                    published_date=str(row["published_date"]),
                    source_url=str(row["source_url"]),
                    locator=str(row["locator"]),
                    content=content,
                    score=float(row["score"]),
                    article_number=row["article_number"],
                )
            )
            per_document[document_id] = per_document.get(document_id, 0) + 1
            used_characters += len(content)
            if len(selected) >= limit:
                break
        return selected

    def _search_within_document(
        self, question: str, document_id: str, limit: int, extra_queries: list[str] | None = None
    ) -> list[RetrievedChunk]:
        """Rank one document's own chunks against the question, isolated from the rest
        of the corpus. Used for instruments too long to read in full: this still finds
        its most relevant passages, without competing against unrelated documents for
        the same result slots. extra_queries (statutory-vocabulary rewrites) matter even
        more here than in corpus-wide search - a specific criminal article's own words
        ('harcelement', 'usurpation d'identite') rarely match a broad question's generic
        terms, even with the rest of the corpus out of the way."""
        sql = """
            SELECT
                c.document_id, d.title, d.source, d.source_label, d.category,
                d.published_date, d.source_url, c.locator, c.content, c.article_number,
                bm25(chunks_fts, 5.0, 1.0, 0.0, 0.0) AS score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.rowid
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ? AND c.document_id = ?
            ORDER BY score
            LIMIT ?
        """
        # bm25 scores are not comparable across different queries (different term sets
        # produce different score scales), so merging by raw score would let one
        # query's results systematically crowd out another's - defeating the point of
        # asking several differently-worded questions. Interleave instead: each
        # variant's #1 pick is taken before anyone's #2, guaranteeing every
        # reformulated concept gets a fair shot at a slot.
        per_variant_rows: list[list[sqlite3.Row]] = []
        with self._connect() as connection:
            for variant in [question, *(extra_queries or [])]:
                query = build_fts_query(variant)
                per_variant_rows.append(
                    connection.execute(sql, (query, document_id, limit)).fetchall() if query else []
                )
        seen: set[str] = set()
        rows: list[sqlite3.Row] = []
        position = 0
        while len(rows) < limit and any(position < len(variant_rows) for variant_rows in per_variant_rows):
            for variant_rows in per_variant_rows:
                if position < len(variant_rows):
                    row = variant_rows[position]
                    locator = str(row["locator"])
                    if locator not in seen:
                        seen.add(locator)
                        rows.append(row)
                        if len(rows) >= limit:
                            break
            position += 1
        return [
            RetrievedChunk(
                document_id=str(row["document_id"]),
                title=str(row["title"]),
                source=str(row["source"]),
                source_label=str(row["source_label"]),
                category=str(row["category"]),
                published_date=str(row["published_date"]),
                source_url=str(row["source_url"]),
                locator=str(row["locator"]),
                content=str(row["content"]),
                score=float(row["score"]),
                article_number=row["article_number"],
            )
            for row in rows
        ]

    def _read_entry_for_record(
        self, question: str, record: dict[str, Any], extra_queries: list[str] | None = None
    ) -> dict[str, Any] | None:
        extracted_path = Path(str(record.get("extracted_path", "")))
        if not extracted_path.is_file():
            return None
        # Trust the already-known extraction size first, so a document known to be huge
        # is never fully read into memory just to discover that. Recorded character
        # counts absent or wrong are still caught below via the actual file length.
        known_size = int(record.get("character_count") or 0)
        if not known_size or known_size <= FULL_TEXT_CHARACTER_CAP:
            text = extracted_path.read_text(encoding="utf-8", errors="replace")
            if len(text) <= FULL_TEXT_CHARACTER_CAP:
                return {
                    "mode": "full_text",
                    "title": record["title"],
                    "source_label": record["source_label"],
                    "content": text,
                }
        chunks = self._search_within_document(
            question, str(record["id"]), DOCUMENT_SCOPED_CHUNK_LIMIT, extra_queries=extra_queries
        )
        if not chunks:
            return None
        return {
            "mode": "scoped_chunks",
            "title": record["title"],
            "source_label": record["source_label"],
            "chunks": chunks,
        }

    def resolve_named_instrument_text(
        self,
        question: str,
        state_documents: dict[str, dict[str, Any]],
        max_documents: int = MAX_NAMED_INSTRUMENT_DOCUMENTS,
        extra_queries: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """When the question names a specific legal instrument, read it directly instead
        of relying solely on corpus-wide chunk ranking. Small enough documents are read
        in full; longer ones get a document-scoped best-passages search instead (see
        _search_within_document, which extra_queries also improves). Tries a formal
        citation first ('ordonnance-loi n°23-010'); if the question only uses an
        informal name ('le code du numérique', the far more common way people actually
        ask), falls back to matching that against document titles."""
        from .legal_graph import find_instrument_mentions, parse_self_instrument

        mentions = find_instrument_mentions(question)
        seen_keys: set[tuple[str, str]] = set()
        results: list[dict[str, Any]] = []
        for mention in mentions:
            key = (mention.type_label, mention.number_key)
            if key in seen_keys or not mention.number_key:
                continue
            seen_keys.add(key)

            candidates = []
            for record in state_documents.values():
                if record.get("status") != "ready" or not record.get("present", True):
                    continue
                if record.get("superseded_note"):
                    continue
                self_mention = parse_self_instrument(str(record.get("title") or ""))
                if self_mention is not None and (self_mention.type_label, self_mention.number_key) == key:
                    candidates.append(record)
            if not candidates:
                continue
            candidates.sort(key=lambda record: record.get("character_count", 0), reverse=True)
            entry = self._read_entry_for_record(question, candidates[0], extra_queries=extra_queries)
            if entry is not None:
                results.append(entry)
            if len(results) >= max_documents:
                return results

        if not results:
            record = _match_informal_code_reference(question, state_documents)
            if record is not None:
                entry = self._read_entry_for_record(question, record, extra_queries=extra_queries)
                if entry is not None:
                    results.append(entry)
        if not results:
            record = _match_topic_instrument(question, state_documents)
            if record is not None:
                entry = self._read_entry_for_record(question, record, extra_queries=extra_queries)
                if entry is not None:
                    results.append(entry)
        return results

    def stats(self) -> dict[str, int]:
        if not self.path.is_file():
            return {"documents": 0, "chunks": 0}
        try:
            with self._connect() as connection:
                documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            return {"documents": documents, "chunks": chunks}
        except sqlite3.DatabaseError:
            return {"documents": 0, "chunks": 0}
