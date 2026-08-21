from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2

FRENCH_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
}

# Ordered longest phrase first (checked at match time) so "arrêté interministériel"
# is recognized before the generic "arrêté", and "loi de finances" before "loi".
INSTRUMENT_TYPES: tuple[tuple[str, str, int], ...] = (
    ("loi organique", "Loi organique", 95),
    ("loi de finances rectificative", "Loi de finances rectificative", 92),
    ("loi de finances", "Loi de finances", 92),
    ("loi-cadre", "Loi-cadre", 90),
    ("ordonnance-loi", "Ordonnance-loi", 90),
    ("décret-loi", "Décret-loi", 88),
    ("decret-loi", "Décret-loi", 88),
    ("arrêté interministériel", "Arrêté interministériel", 72),
    ("arrete interministeriel", "Arrêté interministériel", 72),
    ("arrêté ministériel", "Arrêté ministériel", 70),
    ("arrete ministeriel", "Arrêté ministériel", 70),
    ("arrêté départemental", "Arrêté départemental", 65),
    ("arrete departemental", "Arrêté départemental", 65),
    ("arrêté provincial", "Arrêté provincial", 65),
    ("arrete provincial", "Arrêté provincial", 65),
    ("arrêté urbain", "Arrêté urbain", 62),
    ("arrete urbain", "Arrêté urbain", 62),
    ("acte uniforme", "Acte uniforme", 88),
    ("règlement", "Règlement", 60),
    ("reglement", "Règlement", 60),
    ("circulaire ministérielle", "Circulaire ministérielle", 55),
    ("circulaire ministerielle", "Circulaire ministérielle", 55),
    ("note circulaire", "Note circulaire", 45),
    ("note de service", "Note de service", 35),
    ("constitution", "Constitution", 100),
    ("traité", "Traité", 95),
    ("traite", "Traité", 95),
    ("convention collective", "Convention collective", 70),
    ("convention", "Convention", 95),
    ("protocole", "Protocole", 90),
    ("décision", "Décision", 55),
    ("decision", "Décision", 55),
    ("instruction", "Instruction", 45),
    ("arrêté", "Arrêté", 65),
    ("arrete", "Arrêté", 65),
    ("ordonnance", "Ordonnance", 82),
    ("décret", "Décret", 80),
    ("decret", "Décret", 80),
    ("édit", "Édit", 75),
    ("edit", "Édit", 75),
    ("circulaire", "Circulaire", 50),
    ("loi", "Loi", 90),
)
DEFAULT_AUTHORITY_SCORE = 20

# Longest-first so a compound phrase (e.g. "modifiant et complétant") is not
# shadowed by a shorter one ("modifiant") that would truncate the match window.
RELATIONSHIP_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("modifiant et complétant", "MODIFIES"),
    ("modifiant et completant", "MODIFIES"),
    ("modifiant, complétant et abrogeant", "MODIFIES"),
    ("portant modification et complément de", "MODIFIES"),
    ("portant modification de", "MODIFIES"),
    ("modification de", "MODIFIES"),
    ("modifiant", "MODIFIES"),
    ("modifie", "MODIFIES"),
    ("complétant", "COMPLEMENTS"),
    ("completant", "COMPLEMENTS"),
    ("complément de", "COMPLEMENTS"),
    ("portant abrogation de", "REPEALS"),
    ("abrogeant", "REPEALS"),
    ("abrogation de", "REPEALS"),
    ("abroge", "REPEALS"),
    ("remplaçant", "REPLACES"),
    ("remplacant", "REPLACES"),
    ("remplacement de", "REPLACES"),
    ("remplace", "REPLACES"),
    ("portant ratification de", "RATIFIES"),
    ("ratifiant", "RATIFIES"),
    ("ratification de", "RATIFIES"),
    ("portant application de", "IMPLEMENTS"),
    ("rendant applicable", "IMPLEMENTS"),
    ("mesures d'exécution de", "IMPLEMENTS"),
    ("mesures d'execution de", "IMPLEMENTS"),
    ("portant prorogation de", "EXTENDS"),
    ("prorogeant", "EXTENDS"),
    ("prorogation de", "EXTENDS"),
    ("portant dérogation à", "DEROGATES"),
    ("portant derogation a", "DEROGATES"),
    ("dérogeant à", "DEROGATES"),
    ("derogeant a", "DEROGATES"),
)

RELATIONSHIP_LABELS = {
    "MODIFIES": "modifie",
    "COMPLEMENTS": "complète",
    "REPEALS": "abroge",
    "REPLACES": "remplace",
    "RATIFIES": "ratifie",
    "IMPLEMENTS": "met en application",
    "EXTENDS": "proroge",
    "DEROGATES": "déroge à",
    "INSERTS": "insère",
}
INVERSE_RELATIONSHIP_LABELS = {
    "MODIFIES": "modifié par",
    "COMPLEMENTS": "complété par",
    "REPEALS": "abrogé par",
    "REPLACES": "remplacé par",
    "RATIFIES": "ratifié par",
    "IMPLEMENTS": "mis en application par",
    "EXTENDS": "prorogé par",
    "DEROGATES": "faisant l'objet d'une dérogation par",
    "INSERTS": "inséré par",
}

_TYPE_TABLE = sorted(INSTRUMENT_TYPES, key=lambda item: len(item[0]), reverse=True)
_TYPE_ALTERNATION = "|".join(re.escape(phrase) for phrase, _label, _score in _TYPE_TABLE)
_TYPE_LOOKUP = {phrase: (label, score) for phrase, label, score in _TYPE_TABLE}
_MONTH_ALTERNATION = "|".join(sorted(FRENCH_MONTHS, key=len, reverse=True))

_NUMBER_GROUP = r"[0-9][0-9A-Za-zÀ-ÖØ-öø-ÿ&]*(?:[\/\-\.][0-9A-Za-zÀ-ÖØ-öø-ÿ&]+)*"
_DATE_GROUP = rf"(?P<day>[0-3]?\d)(?:er)?\s+(?P<month>{_MONTH_ALTERNATION})\s+(?P<year>\d{{4}})"

INSTRUMENT_REFERENCE_PATTERN = re.compile(
    rf"\b(?P<type>{_TYPE_ALTERNATION})\b"
    rf"(?:[\s,]*[°º]?\s*(?:n[o°º]?\s*[:.]?\s*)?(?P<number>{_NUMBER_GROUP}))?"
    rf"(?:\s+du\s+{_DATE_GROUP})?",
    flags=re.IGNORECASE | re.UNICODE,
)

# Keyword scan uses the same case-insensitive matching; longest phrases are
# tried first via the tuple order above combined with non-overlapping search.
_RELATIONSHIP_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase, _relation in RELATIONSHIP_KEYWORDS) + r")",
    flags=re.IGNORECASE | re.UNICODE,
)
_RELATIONSHIP_LOOKUP = {phrase.casefold(): relation for phrase, relation in RELATIONSHIP_KEYWORDS}

OBSOLETE_TITLE_MARKER = "[TEXTE OBSOLÈTE"
RELATIONSHIP_SEARCH_WINDOW = 220
SELF_MATCH_MAX_START = 6


def _strip_accents(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))


def normalize_instrument_number(raw: str) -> str:
    """Canonicalize a legal reference number so '69/009', '69-009', '69.009' compare equal."""
    if not raw:
        return ""
    cleaned = _strip_accents(raw).casefold()
    segments = [segment.lstrip("0") or "0" for segment in re.split(r"[\/\-\.\s]+", cleaned) if segment]
    return "/".join(segments)


@dataclass(frozen=True)
class InstrumentMention:
    type_label: str
    authority_score: int
    number: str
    number_key: str
    date_iso: str
    start: int
    end: int
    raw: str


def _date_iso(match: re.Match[str]) -> str:
    day, month, year = match.group("day"), match.group("month"), match.group("year")
    if not (day and month and year):
        return ""
    month_number = FRENCH_MONTHS.get(_strip_accents(month).casefold())
    if not month_number:
        return ""
    try:
        return f"{int(year):04d}-{month_number:02d}-{int(day):02d}"
    except ValueError:
        return ""


def find_instrument_mentions(text: str) -> list[InstrumentMention]:
    """Find every legal-instrument reference (type + number + optional date) in text.

    A mention with neither a number nor a date is too ambiguous to link
    across documents (e.g. a bare 'Loi' with no reference) and is dropped.
    """
    mentions: list[InstrumentMention] = []
    for match in INSTRUMENT_REFERENCE_PATTERN.finditer(text):
        type_phrase = match.group("type").casefold()
        label, score = _TYPE_LOOKUP.get(type_phrase, (None, 0))
        if not label:
            continue
        number = (match.group("number") or "").strip(" .,;:")
        date_iso = _date_iso(match)
        if not number and not date_iso:
            continue
        mentions.append(
            InstrumentMention(
                type_label=label,
                authority_score=score,
                number=number,
                number_key=normalize_instrument_number(number),
                date_iso=date_iso,
                start=match.start(),
                end=match.end(),
                raw=match.group(0).strip(),
            )
        )
    return mentions


def parse_self_instrument(title: str) -> InstrumentMention | None:
    """The instrument a document IS, not one it merely references.

    Requires the type phrase to appear at (or very near) the start of the
    title, matching this corpus's consistent 'Type n° X du DATE ...' naming.
    Case titles ('CCJA - ...'), forms, and descriptive titles correctly yield
    no match, keeping the graph limited to actual normative texts.
    """
    working = title.lstrip()
    if working.startswith(OBSOLETE_TITLE_MARKER):
        closing = working.find("]")
        if closing != -1:
            working = working[closing + 1 :].lstrip()
    mentions = find_instrument_mentions(working)
    for mention in mentions:
        if mention.start <= SELF_MATCH_MAX_START:
            return mention
    return None


@dataclass(frozen=True)
class RelationshipMention:
    relation: str
    keyword: str
    target: InstrumentMention
    partial: bool = False


# A relationship whose title names a specific article/paragraph, or hedges with
# "certain(e)s ..." (some provisions, not all), targets a PROVISION of the
# instrument, not the instrument as a whole. This matters most for REPEALS:
# "abrogeant l'article 321 de la loi 73-021" does not mean the whole law 73-021
# is repealed, and must never be reported as such.
_PARTIAL_SCOPE_PATTERN = re.compile(
    r"\barticles?\s+\d+|\balin[ée]as?\s+\d+|\bcertain(?:e|es|s)?\s+(?:article|disposition)",
    flags=re.IGNORECASE | re.UNICODE,
)


def extract_relationships(title: str, self_mention: InstrumentMention | None) -> list[RelationshipMention]:
    """Find '<self> MODIFIES/REPEALS/... <target>' relationships stated in a title."""
    mentions = find_instrument_mentions(title)
    if self_mention is not None:
        mentions = [mention for mention in mentions if mention.start != self_mention.start]
    if not mentions:
        return []

    relationships: list[RelationshipMention] = []
    seen_targets: set[tuple[str, str, str]] = set()
    for keyword_match in _RELATIONSHIP_PATTERN.finditer(title):
        relation = _RELATIONSHIP_LOOKUP.get(keyword_match.group(0).casefold())
        if not relation:
            continue
        window_end = keyword_match.end() + RELATIONSHIP_SEARCH_WINDOW
        candidate = next(
            (mention for mention in mentions if keyword_match.end() <= mention.start <= window_end),
            None,
        )
        if candidate is None:
            continue
        dedupe_key = (relation, candidate.type_label, candidate.number_key)
        if dedupe_key in seen_targets:
            continue
        seen_targets.add(dedupe_key)
        gap_text = title[keyword_match.end() : candidate.start]
        partial = bool(_PARTIAL_SCOPE_PATTERN.search(gap_text))
        relationships.append(
            RelationshipMention(relation=relation, keyword=keyword_match.group(0), target=candidate, partial=partial)
        )
    return relationships


## --------------------------------------------------------------------------------
## Article-level cross-reference extraction (gap #2, Family 2).
##
## Extends the whole-instrument graph above with a finer-grained layer that links
## one ARTICLE of one document to an article/instrument named in a citation
## sentence found inside that article's own chunked text. Scoped to three real,
## corpus-validated surface forms only (see legal_graph module docstring context
## in the design notes): passive dispositif ("L'article N de <X> est modifié"),
## parenthetical amendment notes ("(modifié par l'article N du <X>)"), and the
## insertion formula ("il est inséré ... les articles N, N+1, ...").
## --------------------------------------------------------------------------------

# Longest-first so a shared prefix ("ter" is a prefix of "terdecies", "quater" of
# "quaterdecies") never causes the shorter alternative to match and strand the
# rest of the word unconsumed.
_ARTICLE_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies",
            "nonies", "decies", "undecies", "duodecies", "terdecies", "quaterdecies",
        ),
        key=len,
        reverse=True,
    )
)
_ARTICLE_SUFFIX_ALTERNATION = "|".join(_ARTICLE_SUFFIXES)

# A single article-number token: "12", "1er", "premier", "68bis". Used both to
# recognize one number and, via findall, to fan a comma/"et"-joined list of these
# out into separate numbers.
_ARTICLE_NUM_TOKEN = rf"(?:1er|premier|\d+(?:\s*(?:{_ARTICLE_SUFFIX_ALTERNATION}))?)"
_ARTICLE_NUM_TOKEN_RE = re.compile(_ARTICLE_NUM_TOKEN, flags=re.IGNORECASE | re.UNICODE)
_ARTICLE_NUMLIST_PATTERN = rf"{_ARTICLE_NUM_TOKEN}(?:\s*(?:,|et)\s*{_ARTICLE_NUM_TOKEN})*"

_ARTICLE_NUMBER_ONE_ALIASES = {"1er", "premier"}


def normalize_article_number(raw: str) -> str:
    """Canonicalize an article number for matching: casefold, strip spaces/degree
    signs, and fold '1er'/'premier' (both mean article one) to '1'."""
    cleaned = re.sub(r"[°º\s]+", "", (raw or "")).casefold()
    if cleaned in _ARTICLE_NUMBER_ONE_ALIASES:
        return "1"
    return cleaned


# A connector between "l'article N" and the instrument/self-reference that follows.
# Each branch consumes its own trailing whitespace so "de l'Ordonnance..." (no
# space after the elided apostrophe) and "du Décret..." (a real space) both work.
_INSTRUMENT_CONNECTOR = r"(?:de\s+l['’]|de\s+la\s+|des\s+|du\s+|de\s+)"

# Self-reference words ("le présent décret", "ladite loi", "l'ordonnance susdite")
# mean the target is the citing document's own instrument, not a separate lookup.
_SELF_REFERENCE_PATTERN = re.compile(
    r"\b(?:presente?s?|ledit|ladite|lesdits?|lesdites?|susdite?s?)\b",
    flags=re.IGNORECASE | re.UNICODE,
)

# Family (a): passive dispositif, e.g. "L'article 12 de l'Ordonnance-loi n° 90-046
# du 8 août 1990 ... est modifié et complété". A capital "L'article"/"Les articles"
# is required (mirrors ARTICLE_HEADER_PATTERN's rationale in retrieval.py): a bare
# lowercase "l'article" mid-sentence is an ordinary cross-reference, not a fresh
# amending clause, in standard French legal drafting. The "L'alinéa N de l'article"
# variant is the one case where "l'article" is legitimately lowercase because an
# alinéa qualifier precedes it - that variant is recognized explicitly and flagged
# as alinéa-granularity.
_PASSIVE_ANCHOR_PATTERN = re.compile(
    r"(?:"
    r"(?P<alinea_marker>L['’]alin[ée]a\s+\d+\s+de\s+l['’]article)"
    r"|(?P<plain_marker>L['’]article|Les\s+articles)"
    r")\s+"
    rf"(?P<numlist>{_ARTICLE_NUMLIST_PATTERN})\s+"
    rf"{_INSTRUMENT_CONNECTOR}"
)
_PASSIVE_VERB_PATTERN = re.compile(
    r"\b(?:est|sont)\s+"
    r"(modifi[ée][es]?|complét[ée][es]?|abrog[ée][es]?|remplac[ée][es]?|supprim[ée][es]?)\b",
    flags=re.IGNORECASE | re.UNICODE,
)
PASSIVE_SEARCH_WINDOW = 400

# Family (b): parenthetical amendment note, e.g. "(modifié et complété par
# l'article 1° du Décret n° 05/063 du 22 juillet 2005)". The leading "(" is
# optional, not required: real corpus data uses the identical "VERB par
# l'article N <connector> <instrument>" phrasing bare, without parentheses,
# just as often (e.g. "Article 8 : modifié et complété par l'article 1° du
# Décret n° 05/063 du 22 juillet 2005."). _ORDINAL_NOISE absorbs the stray
# OCR punctuation ("”", "′", a stand-alone quote mark, ...) real scans
# routinely substitute for the "°" ordinal marker between the article number
# and the connector that follows it.
_PAREN_VERB_GROUP = r"(?:modifi[ée]e?s?|complét[ée]e?s?|abrog[ée]e?s?|remplac[ée]e?s?|supprim[ée]e?s?)"
_ORDINAL_NOISE = r"[°ºª\"'’”′″]*"
_PAREN_AMENDMENT_ANCHOR = re.compile(
    r"(?P<paren>\(\s*)?" + _PAREN_VERB_GROUP + r"(?:\s+et\s+" + _PAREN_VERB_GROUP + r")?"
    rf"\s+par\s+l['’]article\s+(?P<num>{_ARTICLE_NUM_TOKEN}){_ORDINAL_NOISE}\s+{_INSTRUMENT_CONNECTOR}",
    flags=re.IGNORECASE | re.UNICODE,
)
PAREN_SEARCH_WINDOW = 200

# "tel(le)(s) que <verb> par l'article N de X" describes X's own amendment
# history as background for some OTHER noun the sentence is really about (an
# ordinary cross-reference, or the real target of a family-(a) passive
# dispositif elsewhere in the same sentence) - it is not a declarative note
# that the CURRENT article was amended. A real corpus example: "... et aux
# dispositions de l'article 5 du Décret n°18/019 ..., tel que modifié et
# complété par l'article 1° du Décret n°20/025 ..." - the citing article
# itself was never touched; it is the ALREADY-CITED Décret n°18/019 whose
# amendment history is being recounted. Reject on this immediately-preceding
# cue to avoid mis-attributing that history to the wrong source article.
_NESTED_REFERENCE_PATTERN = re.compile(r"\btel(?:le)?s?\s+que\s*$", flags=re.IGNORECASE | re.UNICODE)

# Family (c): insertion formula, e.g. "Il est inséré au décret du 30 janvier 1940
# portant Code pénal congolais les articles 68bis, 68ter, ... libellés comme suit".
# The gap between "inséré" and the "article(s)" keyword is the instrument being
# amended (type + number + date, sometimes with its own trailing amendment
# note - "..., tel que modifié et complété par Décret n°23/14B du 12 avril
# 2023, l'article 3 bis"), which real corpus citations routinely run past 150
# characters; 80 was too tight to ever reach it and left this family with zero
# working matches in production. The gap stays bounded (not unlimited) so an
# unrelated, much later "article N" mention elsewhere in the same chunk is
# never mistaken for the one actually being inserted.
_INSERTION_ANCHOR = re.compile(
    r"il est\s+ins[ée]r[ée]s?\b(?P<gap>.{0,300}?)\barticles?\s+(?P<numlist>" + _ARTICLE_NUMLIST_PATTERN + r")",
    flags=re.IGNORECASE | re.UNICODE | re.DOTALL,
)


def _relation_for_verb(word: str) -> str:
    stem = _strip_accents(word).casefold()
    if stem.startswith("modifi"):
        return "MODIFIES"
    if stem.startswith("complet"):
        return "COMPLEMENTS"
    if stem.startswith("abrog"):
        return "REPEALS"
    if stem.startswith("remplac"):
        return "REPLACES"
    if stem.startswith("supprim"):
        return "REPEALS"
    return "MODIFIES"


def _quoted_replacement_spans(content: str) -> list[tuple[int, int]]:
    """«...» spans hold NEW replacement text introduced by a dispositif/insertion
    match, not fresh citations - re-scanning inside one for further matches finds
    only spurious 'article N' mentions that belong to the quoted content."""
    spans: list[tuple[int, int]] = []
    pos = 0
    length = len(content)
    while True:
        start = content.find("«", pos)
        if start == -1:
            break
        end = content.find("»", start + 1)
        if end == -1:
            spans.append((start, length))
            break
        spans.append((start, end + 1))
        pos = end + 1
    return spans


@dataclass(frozen=True)
class ArticleRelationshipMatch:
    relation: str
    numbers: tuple[str, ...]
    target_mention: InstrumentMention | None
    target_is_self: bool
    granularity: str
    confidence: str
    raw_text: str


def _extract_passive_dispositif(content: str, in_quote: Any) -> list[ArticleRelationshipMatch]:
    results: list[ArticleRelationshipMatch] = []
    for anchor in _PASSIVE_ANCHOR_PATTERN.finditer(content):
        if in_quote(anchor.start()):
            continue
        numbers = _ARTICLE_NUM_TOKEN_RE.findall(anchor.group("numlist"))
        if not numbers:
            continue
        is_alinea = anchor.group("alinea_marker") is not None

        window_start = anchor.end()
        window_end = min(len(content), window_start + PASSIVE_SEARCH_WINDOW)
        window = content[window_start:window_end]

        stripped = _strip_accents(window).casefold()
        self_match = _SELF_REFERENCE_PATTERN.match(stripped)

        target_mention: InstrumentMention | None = None
        target_is_self = False
        if self_match:
            target_is_self = True
            target_end = self_match.end()
        else:
            candidates = find_instrument_mentions(window)
            if not candidates:
                continue
            target_mention = candidates[0]
            target_end = target_mention.end

        verb_match = _PASSIVE_VERB_PATTERN.search(window, target_end)
        if not verb_match:
            continue

        gap = window[target_end : verb_match.start()]
        if find_instrument_mentions(gap):
            # Another, free-standing instrument mention with its own verb sits
            # between our target and the verb: the verb binds to THAT instrument,
            # not to the article/self-reference we anchored on. Reject.
            continue

        if not is_alinea and re.search(r"alin[ée]a", _strip_accents(gap), flags=re.IGNORECASE):
            is_alinea = True

        relation = _relation_for_verb(verb_match.group(1))
        raw_text = (anchor.group(0) + window[: verb_match.end()]).strip()
        results.append(
            ArticleRelationshipMatch(
                relation=relation,
                numbers=tuple(numbers),
                target_mention=target_mention,
                target_is_self=target_is_self,
                granularity="alinea" if is_alinea else "article",
                confidence="high",
                raw_text=raw_text,
            )
        )
    return results


def _extract_paren_amendment(content: str, in_quote: Any) -> list[ArticleRelationshipMatch]:
    results: list[ArticleRelationshipMatch] = []
    for anchor in _PAREN_AMENDMENT_ANCHOR.finditer(content):
        if in_quote(anchor.start()):
            continue
        preceding = content[max(0, anchor.start() - 24) : anchor.start()]
        if _NESTED_REFERENCE_PATTERN.search(preceding):
            continue
        num = anchor.group("num")
        window_start = anchor.end()
        window_end = min(len(content), window_start + PAREN_SEARCH_WINDOW)
        window = content[window_start:window_end]

        candidates = find_instrument_mentions(window)
        if not candidates:
            continue
        target_mention = candidates[0]

        has_paren = anchor.group("paren") is not None
        if has_paren:
            closing = window.find(")", target_mention.end)
            if closing == -1:
                continue
            raw_text = (anchor.group(0) + window[: closing + 1]).strip()
        else:
            raw_text = (anchor.group(0) + window[: target_mention.end]).strip()

        verb_search = re.search(_PAREN_VERB_GROUP, anchor.group(0), flags=re.IGNORECASE)
        relation = _relation_for_verb(verb_search.group(0)) if verb_search else "MODIFIES"
        results.append(
            ArticleRelationshipMatch(
                relation=relation,
                numbers=(num,),
                target_mention=target_mention,
                target_is_self=False,
                granularity="article",
                confidence="high",
                raw_text=raw_text,
            )
        )
    return results


def _extract_insertion(content: str, in_quote: Any) -> list[ArticleRelationshipMatch]:
    results: list[ArticleRelationshipMatch] = []
    for anchor in _INSERTION_ANCHOR.finditer(content):
        if in_quote(anchor.start()):
            continue
        numbers = _ARTICLE_NUM_TOKEN_RE.findall(anchor.group("numlist"))
        if not numbers:
            continue
        gap = anchor.group("gap")
        stripped_gap = _strip_accents(gap).casefold()

        target_mention: InstrumentMention | None = None
        target_is_self = False
        if _SELF_REFERENCE_PATTERN.search(stripped_gap):
            target_is_self = True
        else:
            candidates = find_instrument_mentions(gap)
            if not candidates:
                continue
            target_mention = candidates[0]

        results.append(
            ArticleRelationshipMatch(
                relation="INSERTS",
                numbers=tuple(numbers),
                target_mention=target_mention,
                target_is_self=target_is_self,
                granularity="article",
                confidence="high",
                raw_text=anchor.group(0).strip(),
            )
        )
    return results


def extract_article_relationship_matches(content: str) -> list[ArticleRelationshipMatch]:
    """Run all three article-level extraction families over one chunk's text,
    skipping any match that starts inside a «...» quoted-replacement span."""
    quote_spans = _quoted_replacement_spans(content)

    def _in_quote(pos: int) -> bool:
        return any(start <= pos < end for start, end in quote_spans)

    matches: list[ArticleRelationshipMatch] = []
    matches.extend(_extract_passive_dispositif(content, _in_quote))
    matches.extend(_extract_paren_amendment(content, _in_quote))
    matches.extend(_extract_insertion(content, _in_quote))
    return matches


# Article numbers named in a free-text question, e.g. "l'article 68bis" or
# "articles 6, 8 et 14" - the article-level counterpart of retrieval.py's
# _requested_article_numbers, but matching the broader Latin-suffix vocabulary
# used by this module (kept local rather than cross-imported to keep the two
# modules independently testable).
_QUESTION_ARTICLE_PATTERN = re.compile(
    rf"\barticles?\s+({_ARTICLE_NUM_TOKEN}(?:\s*(?:,|;|et|à|a|-)\s*{_ARTICLE_NUM_TOKEN})*)",
    flags=re.IGNORECASE | re.UNICODE,
)


def _requested_article_number_keys(question: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", question)
    keys: set[str] = set()
    for match in _QUESTION_ARTICLE_PATTERN.finditer(normalized):
        for token in _ARTICLE_NUM_TOKEN_RE.findall(match.group(1)):
            keys.add(normalize_article_number(token))
    return keys


class LegalGraph:
    """SQLite-backed store of legal instruments and their MODIFIES/REPEALS/REPLACES relationships.

    Parsed deterministically from document titles (no LLM calls); a document
    with a recognizable 'Type n° X du DATE' title becomes an instrument row,
    and relationship phrases in that same title ('modifiant', 'abrogeant', ...)
    become edges to the instrument(s) they name. This is the authoritative,
    non-generative layer the assistant consults before answering questions
    about whether a text is still in force.
    """

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
                CREATE TABLE IF NOT EXISTS legal_instrument (
                    instrument_id INTEGER PRIMARY KEY,
                    instrument_type TEXT NOT NULL,
                    instrument_number TEXT NOT NULL DEFAULT '',
                    number_key TEXT NOT NULL DEFAULT '',
                    instrument_date TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    authority_score INTEGER NOT NULL DEFAULT 0,
                    superseded_note TEXT NOT NULL DEFAULT '',
                    in_corpus INTEGER NOT NULL DEFAULT 1,
                    document_count INTEGER NOT NULL DEFAULT 0,
                    sources TEXT NOT NULL DEFAULT '',
                    UNIQUE(instrument_type, number_key)
                );
                CREATE INDEX IF NOT EXISTS idx_legal_instrument_number_key ON legal_instrument(number_key);
                CREATE INDEX IF NOT EXISTS idx_legal_instrument_type ON legal_instrument(instrument_type);
                CREATE TABLE IF NOT EXISTS legal_relationship (
                    relationship_id INTEGER PRIMARY KEY,
                    source_instrument_id INTEGER NOT NULL REFERENCES legal_instrument(instrument_id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL,
                    target_instrument_id INTEGER REFERENCES legal_instrument(instrument_id) ON DELETE SET NULL,
                    target_reference_text TEXT NOT NULL DEFAULT '',
                    target_type TEXT NOT NULL DEFAULT '',
                    target_number TEXT NOT NULL DEFAULT '',
                    target_number_key TEXT NOT NULL DEFAULT '',
                    target_date TEXT NOT NULL DEFAULT '',
                    detected_from TEXT NOT NULL DEFAULT 'title',
                    partial INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_instrument_id, relationship, target_type, target_number_key)
                );
                CREATE INDEX IF NOT EXISTS idx_legal_relationship_source ON legal_relationship(source_instrument_id);
                CREATE INDEX IF NOT EXISTS idx_legal_relationship_target ON legal_relationship(target_instrument_id);
                CREATE INDEX IF NOT EXISTS idx_legal_relationship_target_number_key ON legal_relationship(target_number_key);
                CREATE TABLE IF NOT EXISTS legal_article (
                    article_id INTEGER PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    instrument_id INTEGER REFERENCES legal_instrument(instrument_id) ON DELETE SET NULL,
                    article_number TEXT NOT NULL,
                    article_number_key TEXT NOT NULL,
                    in_corpus INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(document_id, article_number_key)
                );
                CREATE INDEX IF NOT EXISTS idx_legal_article_instrument ON legal_article(instrument_id, article_number_key);
                CREATE TABLE IF NOT EXISTS legal_article_relationship (
                    relationship_id INTEGER PRIMARY KEY,
                    source_article_id INTEGER NOT NULL REFERENCES legal_article(article_id) ON DELETE CASCADE,
                    relationship TEXT NOT NULL,
                    target_article_id INTEGER REFERENCES legal_article(article_id) ON DELETE SET NULL,
                    target_instrument_id INTEGER REFERENCES legal_instrument(instrument_id) ON DELETE SET NULL,
                    target_reference_text TEXT NOT NULL DEFAULT '',
                    target_article_number TEXT NOT NULL DEFAULT '',
                    target_article_number_key TEXT NOT NULL DEFAULT '',
                    granularity TEXT NOT NULL DEFAULT 'article',
                    confidence TEXT NOT NULL DEFAULT 'high',
                    source_chunk_locator TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_article_id, relationship, target_instrument_id, target_article_number_key)
                );
                CREATE INDEX IF NOT EXISTS idx_legal_article_rel_source ON legal_article_relationship(source_article_id);
                CREATE INDEX IF NOT EXISTS idx_legal_article_rel_target ON legal_article_relationship(target_article_id);
                """
            )
            version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and int(version[0]) != SCHEMA_VERSION:
                raise RuntimeError("The legal graph index has an unsupported schema. Rebuild it with cli.py legal-build.")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def rebuild(self, documents: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Repopulate the graph from scratch. Cheap enough to always run in full: a
        title-regex pass over ~12k documents takes well under a second.

        The same real-world instrument is frequently scraped by several
        sources (awa, leganet, droitcongolais, natlex, ...); those copies are
        grouped by (type, number_key) into a single canonical row so that a
        relationship stated by any one copy's title is visible regardless of
        which copy a lookup happens to find.
        """
        self.ensure_schema()

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        pending_relationships: list[tuple[tuple[str, str], list[RelationshipMention]]] = []
        unresolved_targets: dict[tuple[str, str], InstrumentMention] = {}

        for record in documents:
            if record.get("status") == "error" or not record.get("present", True):
                continue
            title = str(record.get("title") or "")
            self_mention = parse_self_instrument(title)
            if self_mention is None:
                continue
            key = (self_mention.type_label, self_mention.number_key)
            entry = grouped.get(key)
            if entry is None:
                entry = {
                    "instrument_type": self_mention.type_label,
                    "instrument_number": self_mention.number,
                    "number_key": self_mention.number_key,
                    "instrument_date": self_mention.date_iso,
                    "title": title,
                    "authority_score": self_mention.authority_score,
                    "superseded_note": "",
                    "document_count": 0,
                    "sources": set(),
                }
                grouped[key] = entry
            entry["document_count"] += 1
            if not entry["instrument_date"] and self_mention.date_iso:
                entry["instrument_date"] = self_mention.date_iso
            source_label = str(record.get("source_label") or "")
            if source_label:
                entry["sources"].add(source_label)
            note = str(record.get("superseded_note") or "")
            if note and note not in entry["superseded_note"]:
                entry["superseded_note"] = f"{entry['superseded_note']}; {note}".strip("; ")

            relationship_mentions = extract_relationships(title, self_mention)
            if relationship_mentions:
                pending_relationships.append((key, relationship_mentions))
                for relationship_mention in relationship_mentions:
                    target_key = (relationship_mention.target.type_label, relationship_mention.target.number_key)
                    unresolved_targets.setdefault(target_key, relationship_mention.target)

        with self._connect() as connection:
            connection.execute("DELETE FROM legal_relationship")
            connection.execute("DELETE FROM legal_instrument")

            key_to_instrument_id: dict[tuple[str, str], int] = {}
            for key, entry in grouped.items():
                cursor = connection.execute(
                    """
                    INSERT INTO legal_instrument(
                        instrument_type, instrument_number, number_key, instrument_date,
                        title, authority_score, superseded_note, in_corpus, document_count, sources
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        entry["instrument_type"],
                        entry["instrument_number"],
                        entry["number_key"],
                        entry["instrument_date"],
                        entry["title"],
                        entry["authority_score"],
                        entry["superseded_note"],
                        entry["document_count"],
                        ", ".join(sorted(entry["sources"])),
                    ),
                )
                key_to_instrument_id[key] = cursor.lastrowid

            # Targets referenced by other titles but not themselves found in the
            # corpus become stub rows (in_corpus=0), so the graph still names them.
            stub_instruments = 0
            for key, mention in unresolved_targets.items():
                if key in key_to_instrument_id:
                    continue
                stub_instruments += 1
                cursor = connection.execute(
                    """
                    INSERT INTO legal_instrument(
                        instrument_type, instrument_number, number_key, instrument_date,
                        title, authority_score, in_corpus, document_count, sources
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, '')
                    """,
                    (
                        mention.type_label,
                        mention.number,
                        mention.number_key,
                        mention.date_iso,
                        f"{mention.type_label} n° {mention.number}".strip(),
                        mention.authority_score,
                    ),
                )
                key_to_instrument_id[key] = cursor.lastrowid

            relationships = 0
            for source_key, relationship_mentions in pending_relationships:
                source_instrument_id = key_to_instrument_id.get(source_key)
                if source_instrument_id is None:
                    continue
                for relationship_mention in relationship_mentions:
                    target = relationship_mention.target
                    target_key = (target.type_label, target.number_key)
                    if target_key == source_key:
                        continue
                    target_instrument_id = key_to_instrument_id.get(target_key)
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO legal_relationship(
                            source_instrument_id, relationship, target_instrument_id, target_reference_text,
                            target_type, target_number, target_number_key, target_date, detected_from, partial
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'title', ?)
                        """,
                        (
                            source_instrument_id,
                            relationship_mention.relation,
                            target_instrument_id,
                            target.raw,
                            target.type_label,
                            target.number,
                            target.number_key,
                            target.date_iso,
                            int(relationship_mention.partial),
                        ),
                    )
                    relationships += cursor.rowcount

        return {
            "instruments": len(grouped),
            "stub_instruments": stub_instruments,
            "relationships": relationships,
        }

    def rebuild_articles(
        self, documents: dict[str, Any], chunk_rows: Iterable[sqlite3.Row]
    ) -> dict[str, Any]:
        """Repopulate the article-level cross-reference graph from scratch.

        Iterates chunks from the local retrieval index (local_retrieval.sqlite3),
        using each chunk's own `article_number` (populated at chunking time) as the
        SOURCE article for any citation sentence matched inside that chunk's text.
        `documents` is the same {document_id: record} mapping rebuild() consumes -
        it supplies each chunk's document title, from which the source's own
        instrument identity is resolved via parse_self_instrument(), exactly as
        rebuild() already does for the title-level graph.

        Like rebuild(), this always does a full DELETE + repopulate: the regex
        pass is cheap relative to the chunk read itself, and a full rebuild avoids
        ever having to reconcile a partial/incremental update.

        NOTE ON ORDERING: legal_instrument rows are deleted and reinserted by
        rebuild() (legal-build). Since legal_article.instrument_id and
        legal_article_relationship.target_instrument_id are declared ON DELETE SET
        NULL against legal_instrument, running legal-build again after this method
        will null out those links until this method is re-run. Always run
        legal-build before article-graph-build.
        """
        self.ensure_schema()

        with self._connect() as connection:
            connection.execute("DELETE FROM legal_article_relationship")
            connection.execute("DELETE FROM legal_article")

            instrument_by_key: dict[tuple[str, str], int] = {
                (row["instrument_type"], row["number_key"]): row["instrument_id"]
                for row in connection.execute("SELECT instrument_id, instrument_type, number_key FROM legal_instrument")
            }
            article_by_key: dict[tuple[str, str], int] = {}

            def _get_or_create_instrument(mention: InstrumentMention) -> int:
                key = (mention.type_label, mention.number_key)
                existing = instrument_by_key.get(key)
                if existing is not None:
                    return existing
                cursor = connection.execute(
                    """
                    INSERT INTO legal_instrument(
                        instrument_type, instrument_number, number_key, instrument_date,
                        title, authority_score, in_corpus, document_count, sources
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, '')
                    """,
                    (
                        mention.type_label,
                        mention.number,
                        mention.number_key,
                        mention.date_iso,
                        f"{mention.type_label} n° {mention.number}".strip(),
                        mention.authority_score,
                    ),
                )
                instrument_by_key[key] = cursor.lastrowid
                return cursor.lastrowid

            def _get_or_create_article(document_id: str, instrument_id: int | None, article_number: str) -> int:
                number_key = normalize_article_number(article_number)
                key = (document_id, number_key)
                existing = article_by_key.get(key)
                if existing is not None:
                    return existing
                connection.execute(
                    """
                    INSERT INTO legal_article(document_id, instrument_id, article_number, article_number_key, in_corpus)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(document_id, article_number_key) DO NOTHING
                    """,
                    (document_id, instrument_id, article_number, number_key),
                )
                row = connection.execute(
                    "SELECT article_id FROM legal_article WHERE document_id = ? AND article_number_key = ?",
                    (document_id, number_key),
                ).fetchone()
                article_by_key[key] = row["article_id"]
                return row["article_id"]

            relationships = 0
            by_relationship: dict[str, int] = {}
            self_instrument_cache: dict[str, int | None] = {}

            for chunk in chunk_rows:
                article_number = str(chunk["article_number"] or "").strip()
                if not article_number:
                    continue
                document_id = chunk["document_id"]
                record = documents.get(document_id)
                if not record:
                    continue

                if document_id in self_instrument_cache:
                    self_instrument_id = self_instrument_cache[document_id]
                else:
                    self_mention = parse_self_instrument(str(record.get("title") or ""))
                    self_instrument_id = _get_or_create_instrument(self_mention) if self_mention is not None else None
                    self_instrument_cache[document_id] = self_instrument_id

                content = str(chunk["content"] or "")
                matches = extract_article_relationship_matches(content)
                if not matches:
                    continue

                source_article_id = _get_or_create_article(document_id, self_instrument_id, article_number)
                locator = str(chunk["locator"] or "")

                for match in matches:
                    if match.target_is_self:
                        target_instrument_id = self_instrument_id
                    elif match.target_mention is not None:
                        target_instrument_id = _get_or_create_instrument(match.target_mention)
                    else:
                        continue

                    for raw_number in match.numbers:
                        number_key = normalize_article_number(raw_number)
                        target_article_id = None
                        if match.relation == "INSERTS" and target_instrument_id is not None:
                            # The inserted article is brand new; its text (if quoted
                            # in this same sentence) is the only copy of it we have,
                            # so it is recorded under the citing document's own id.
                            target_article_id = _get_or_create_article(document_id, target_instrument_id, raw_number)

                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO legal_article_relationship(
                                source_article_id, relationship, target_article_id, target_instrument_id,
                                target_reference_text, target_article_number, target_article_number_key,
                                granularity, confidence, source_chunk_locator
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_article_id,
                                match.relation,
                                target_article_id,
                                target_instrument_id,
                                match.raw_text,
                                raw_number,
                                number_key,
                                match.granularity,
                                match.confidence,
                                locator,
                            ),
                        )
                        if cursor.rowcount:
                            relationships += 1
                            by_relationship[match.relation] = by_relationship.get(match.relation, 0) + 1

            # Second pass: now that every chunk-derived article exists, backfill
            # target_article_id for cross-document edges where the target's own
            # article turned out to also be chunked somewhere in this corpus.
            connection.execute(
                """
                UPDATE legal_article_relationship
                SET target_article_id = (
                    SELECT la.article_id FROM legal_article la
                    WHERE la.instrument_id = legal_article_relationship.target_instrument_id
                      AND la.article_number_key = legal_article_relationship.target_article_number_key
                    LIMIT 1
                )
                WHERE target_article_id IS NULL
                  AND target_instrument_id IS NOT NULL
                  AND target_article_number_key != ''
                  AND EXISTS (
                    SELECT 1 FROM legal_article la
                    WHERE la.instrument_id = legal_article_relationship.target_instrument_id
                      AND la.article_number_key = legal_article_relationship.target_article_number_key
                  )
                """
            )

            article_total = int(connection.execute("SELECT COUNT(*) FROM legal_article").fetchone()[0])

        # Keyed "article_relationships" (not "relationships") so callers that do
        # result.update(graph.stats()) - as cli.py's legal-build/article-graph-build
        # branches both do - don't have this count silently overwritten by stats()'s
        # own "relationships" key, which counts the unrelated instrument-level table.
        return {
            "articles": article_total,
            "article_relationships": relationships,
            "by_relationship": by_relationship,
        }

    def rebuild_articles_from_index(self, documents: dict[str, Any], local_index_path: Path) -> dict[str, Any]:
        """Convenience wrapper used by every rebuild() call site (indexer.py's scan,
        cli.py's legal-build) so the two tables never drift apart: legal_article.instrument_id
        and legal_article_relationship.target_instrument_id are ON DELETE SET NULL against
        legal_instrument, so any rebuild() alone (a full delete+reinsert of legal_instrument
        with fresh row ids) silently nulls out every article-level link unless this runs
        again immediately after. No-ops (zero counts) if the local retrieval index has not
        been built yet - article-level linkage has nothing to build from until it exists,
        and this must stay safe to call from server startup before a first index-local."""
        if not local_index_path.is_file():
            return {"articles": 0, "article_relationships": 0, "by_relationship": {}}
        connection = sqlite3.connect(f"file:{local_index_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            chunk_rows = connection.execute(
                "SELECT c.document_id, c.locator, c.content, c.article_number FROM chunks c"
            ).fetchall()
        finally:
            connection.close()
        return self.rebuild_articles(documents, chunk_rows)

    def find_instruments(self, type_label: str | None, number: str) -> list[sqlite3.Row]:
        self.ensure_schema()
        number_key = normalize_instrument_number(number)
        if not number_key:
            return []
        query = "SELECT * FROM legal_instrument WHERE number_key = ?"
        parameters: list[Any] = [number_key]
        if type_label:
            query += " AND instrument_type = ?"
            parameters.append(type_label)
        with self._connect() as connection:
            return connection.execute(query, parameters).fetchall()

    def status_for(self, instrument_id: int) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as connection:
            instrument = connection.execute(
                "SELECT * FROM legal_instrument WHERE instrument_id = ?", (instrument_id,)
            ).fetchone()
            if instrument is None:
                return {"status": "indeterminate", "label": "⚪ Indéterminé"}
            incoming = connection.execute(
                """
                SELECT r.*, s.instrument_type AS source_type, s.instrument_number AS source_number,
                       s.instrument_date AS source_date, s.title AS source_title
                FROM legal_relationship r
                JOIN legal_instrument s ON s.instrument_id = r.source_instrument_id
                WHERE r.target_instrument_id = ?
                ORDER BY s.instrument_date DESC
                """,
                (instrument_id,),
            ).fetchall()
            outgoing = connection.execute(
                """
                SELECT r.*, t.instrument_type AS target_type_resolved, t.instrument_date AS target_date_resolved,
                       t.title AS target_title, t.in_corpus AS target_in_corpus
                FROM legal_relationship r
                LEFT JOIN legal_instrument t ON t.instrument_id = r.target_instrument_id
                WHERE r.source_instrument_id = ?
                """,
                (instrument_id,),
            ).fetchall()

        # A REPEALS relationship scoped to one article ("abrogeant l'article 321
        # de ...") does not repeal the instrument as a whole; only a whole-
        # instrument repeal may flip the top-level status to "repealed".
        repealed_by = [row for row in incoming if row["relationship"] == "REPEALS" and not row["partial"]]
        partially_repealed_by = [row for row in incoming if row["relationship"] == "REPEALS" and row["partial"]]
        modified_by = [row for row in incoming if row["relationship"] in {"MODIFIES", "COMPLEMENTS"}]

        if instrument["superseded_note"]:
            status, label = "flagged_superseded", "🔴 Signalé obsolète par le mainteneur de la bibliothèque"
        elif repealed_by:
            status, label = "repealed", "🔴 Abrogé (selon un texte indexé)"
        elif modified_by or partially_repealed_by:
            status, label = "modified", "🔵 Modifié (des textes ultérieurs indexés le modifient, le complètent, ou en abrogent une partie)"
        elif not instrument["in_corpus"]:
            status, label = "referenced_not_indexed", "⚪ Référencé mais non disponible dans le corpus indexé"
        else:
            status, label = "presumed_in_force_in_corpus", "🟡 Présumé en vigueur dans le corpus indexé (aucune abrogation trouvée)"

        return {
            "status": status,
            "label": label,
            "instrument": dict(instrument),
            "repealed_by": [dict(row) for row in repealed_by],
            "partially_repealed_by": [dict(row) for row in partially_repealed_by],
            "modified_by": [dict(row) for row in modified_by],
            "modifies_or_repeals": [dict(row) for row in outgoing],
        }

    def stats(self) -> dict[str, int]:
        empty = {
            "instruments": 0,
            "instruments_in_corpus": 0,
            "relationships": 0,
            "articles": 0,
            "article_relationships": 0,
        }
        if not self.path.is_file():
            return empty
        try:
            with self._connect() as connection:
                instruments = int(connection.execute("SELECT COUNT(*) FROM legal_instrument").fetchone()[0])
                in_corpus = int(
                    connection.execute("SELECT COUNT(*) FROM legal_instrument WHERE in_corpus = 1").fetchone()[0]
                )
                relationships = int(connection.execute("SELECT COUNT(*) FROM legal_relationship").fetchone()[0])
        except sqlite3.DatabaseError:
            return empty
        # The legal_article* tables are queried separately, and defaulted to 0 on
        # failure, so a DB still on the pre-article schema (not yet rebuilt with
        # article-graph-build) still reports its working instrument/relationship
        # counts instead of falling back to all-zero.
        articles = 0
        article_relationships = 0
        try:
            with self._connect() as connection:
                articles = int(connection.execute("SELECT COUNT(*) FROM legal_article").fetchone()[0])
                article_relationships = int(
                    connection.execute("SELECT COUNT(*) FROM legal_article_relationship").fetchone()[0]
                )
        except sqlite3.DatabaseError:
            pass
        return {
            "instruments": instruments,
            "instruments_in_corpus": in_corpus,
            "relationships": relationships,
            "articles": articles,
            "article_relationships": article_relationships,
        }

    def status_brief(self, question: str, max_instruments: int = 4) -> str:
        """Format a structured-index block for injection into the assistant's prompt.

        Scans the question for legal-instrument references and, for each match
        found in the graph, reports its deterministic status plus every known
        modifying/repealing text - the 'legal validity check' the assistant
        must consult before answering, per the maintainer's design notes.
        """
        mentions = find_instrument_mentions(question)
        seen: set[tuple[str, str]] = set()
        blocks: list[str] = []
        for mention in mentions:
            key = (mention.type_label, mention.number_key)
            if key in seen or not mention.number_key:
                continue
            seen.add(key)
            rows = self.find_instruments(mention.type_label, mention.number)
            for row in rows:
                info = self.status_for(row["instrument_id"])
                blocks.append(self._format_status_block(info))
            if len(blocks) >= max_instruments:
                break
        if not blocks:
            return ""
        return (
            "Structured legal index lookup (deterministic, parsed from instrument titles - "
            "not model-generated; treat as authoritative for the status signal only, and still "
            "cite the underlying retrieved text for substantive content):\n\n" + "\n\n".join(blocks)
        )

    @staticmethod
    def _format_status_block(info: dict[str, Any]) -> str:
        instrument = info["instrument"]
        header = f"{instrument['instrument_type']} n° {instrument['instrument_number']}".strip()
        if instrument["instrument_date"]:
            header += f" du {instrument['instrument_date']}"
        lines = [f"[{header}] Status: {info['label']}"]
        if instrument["document_count"] > 1:
            lines.append(f"  Found consistently in {instrument['document_count']} indexed copies ({instrument['sources']}).")
        if instrument["superseded_note"]:
            lines.append(f"  Maintainer note: {instrument['superseded_note']}")
        for row in info["modified_by"]:
            source = f"{row['source_type']} n° {row['source_number']}".strip()
            if row["source_date"]:
                source += f" du {row['source_date']}"
            lines.append(f"  - {RELATIONSHIP_LABELS.get(row['relationship'], row['relationship'])} par: {source}")
        for row in info["repealed_by"]:
            source = f"{row['source_type']} n° {row['source_number']}".strip()
            if row["source_date"]:
                source += f" du {row['source_date']}"
            lines.append(f"  - abrogé (intégralement) par: {source}")
        for row in info["partially_repealed_by"]:
            source = f"{row['source_type']} n° {row['source_number']}".strip()
            if row["source_date"]:
                source += f" du {row['source_date']}"
            lines.append(f"  - une disposition (article/alinéa) abrogée par: {source} — le texte reste en vigueur pour le reste")
        if (
            not info["modified_by"]
            and not info["repealed_by"]
            and not info["partially_repealed_by"]
            and info["status"] == "presumed_in_force_in_corpus"
        ):
            lines.append("  - No amending or repealing text was found among the indexed titles.")
        return "\n".join(lines)

    def article_brief(self, question: str, max_articles: int = 4) -> str:
        """Format an article-level structured-index block, the finer-grained
        counterpart of status_brief(): for each (instrument, article number) pair
        named in the question that matches a row in the article graph, reports
        every known MODIFIES/COMPLEMENTS/REPEALS/REPLACES/INSERTS edge in both
        directions. Returns '' (never raises) when nothing in the graph matches,
        exactly like status_brief.

        The relevance check (does the question even name an instrument and an
        article number?) runs BEFORE any DB access - including ensure_schema()
        - exactly like status_brief() only touches the DB lazily, inside its
        per-mention loop, for questions that actually name a recognized
        instrument. An irrelevant question must never touch the DB at all,
        and ensure_schema()'s schema-version-mismatch RuntimeError is caught
        alongside sqlite3.DatabaseError below so a stale-code process talking
        to an already-rebuilt DB (or vice versa) fails soft here instead of
        propagating out as an unhandled 503 for every chat request.
        """
        instrument_mentions = find_instrument_mentions(question)
        article_keys = _requested_article_number_keys(question)
        if not instrument_mentions or not article_keys:
            return ""

        seen: set[tuple[int, str]] = set()
        blocks: list[str] = []
        try:
            self.ensure_schema()
            with self._connect() as connection:
                for mention in instrument_mentions:
                    if not mention.number_key:
                        continue
                    instrument_rows = connection.execute(
                        "SELECT instrument_id, instrument_type, instrument_number, instrument_date "
                        "FROM legal_instrument WHERE number_key = ? AND instrument_type = ?",
                        (mention.number_key, mention.type_label),
                    ).fetchall()
                    for instrument_row in instrument_rows:
                        for article_key in article_keys:
                            dedupe_key = (instrument_row["instrument_id"], article_key)
                            if dedupe_key in seen:
                                continue
                            article_row = connection.execute(
                                "SELECT * FROM legal_article WHERE instrument_id = ? AND article_number_key = ?",
                                (instrument_row["instrument_id"], article_key),
                            ).fetchone()
                            # Incoming edges are looked up directly by (target_instrument_id,
                            # target_article_number_key) rather than via a legal_article row's
                            # article_id: an article that is only ever a TARGET (e.g. the
                            # provision a later law repeals) never becomes its own legal_article
                            # source node - nothing in ITS OWN text cites anything - so requiring
                            # article_row to exist here would silently hide exactly the "was this
                            # article repealed/modified" case a user is most likely to ask about.
                            incoming = connection.execute(
                                """
                                SELECT r.*, sa.article_number AS source_article_number,
                                       si.instrument_type AS source_instrument_type,
                                       si.instrument_number AS source_instrument_number,
                                       si.instrument_date AS source_instrument_date
                                FROM legal_article_relationship r
                                JOIN legal_article sa ON sa.article_id = r.source_article_id
                                LEFT JOIN legal_instrument si ON si.instrument_id = sa.instrument_id
                                WHERE r.target_instrument_id = ? AND r.target_article_number_key = ?
                                """,
                                (instrument_row["instrument_id"], article_key),
                            ).fetchall()
                            if article_row is None and not incoming:
                                continue
                            seen.add(dedupe_key)
                            outgoing = []
                            if article_row is not None:
                                outgoing = connection.execute(
                                    """
                                    SELECT r.*, ti.instrument_type AS target_instrument_type,
                                           ti.instrument_number AS target_instrument_number,
                                           ti.instrument_date AS target_instrument_date
                                    FROM legal_article_relationship r
                                    LEFT JOIN legal_instrument ti ON ti.instrument_id = r.target_instrument_id
                                    WHERE r.source_article_id = ?
                                    """,
                                    (article_row["article_id"],),
                                ).fetchall()
                            article_dict = (
                                dict(article_row)
                                if article_row is not None
                                else {"article_number": incoming[0]["target_article_number"] or article_key}
                            )
                            blocks.append(
                                self._format_article_block(
                                    dict(instrument_row),
                                    article_dict,
                                    [dict(row) for row in outgoing],
                                    [dict(row) for row in incoming],
                                )
                            )
                            if len(blocks) >= max_articles:
                                break
                        if len(blocks) >= max_articles:
                            break
                    if len(blocks) >= max_articles:
                        break
        except (sqlite3.DatabaseError, RuntimeError):
            return ""

        if not blocks:
            return ""
        return (
            "Structured legal index lookup - article level (deterministic, parsed from citation "
            "sentences found in the indexed text - not model-generated; treat as authoritative for "
            "the relationship signal only, and still cite the underlying retrieved text for "
            "substantive content):\n\n" + "\n\n".join(blocks)
        )

    @staticmethod
    def _format_article_block(
        instrument: dict[str, Any], article: dict[str, Any], outgoing: list[dict[str, Any]], incoming: list[dict[str, Any]]
    ) -> str:
        header = f"{instrument['instrument_type']} n° {instrument['instrument_number']}".strip()
        if instrument.get("instrument_date"):
            header += f" du {instrument['instrument_date']}"
        lines = [f"[{header}, art. {article['article_number']}]"]
        for row in outgoing:
            label = RELATIONSHIP_LABELS.get(row["relationship"], row["relationship"])
            target = f"{row.get('target_instrument_type') or ''} n° {row.get('target_instrument_number') or ''}".strip()
            if row.get("target_instrument_date"):
                target += f" du {row['target_instrument_date']}"
            if not target or target == "n°":
                target = row["target_reference_text"] or "instrument non résolu"
            article_part = f", art. {row['target_article_number']}" if row["target_article_number"] else ""
            note = " (alinéa)" if row["granularity"] == "alinea" else ""
            lines.append(f"  - {label}{note}: {target}{article_part}")
        for row in incoming:
            label = INVERSE_RELATIONSHIP_LABELS.get(row["relationship"], row["relationship"])
            source = f"{row.get('source_instrument_type') or ''} n° {row.get('source_instrument_number') or ''}".strip()
            if row.get("source_instrument_date"):
                source += f" du {row['source_instrument_date']}"
            note = " (alinéa)" if row["granularity"] == "alinea" else ""
            lines.append(f"  - {label}{note}: {source}, art. {row['source_article_number']}")
        if not outgoing and not incoming:
            lines.append("  - No article-level amendment relationship was found among the indexed texts.")
        return "\n".join(lines)
