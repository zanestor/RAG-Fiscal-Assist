from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .config import Settings
from .retrieval import RetrievedChunk

# BGE Reranker v2 (m3): a real, well-maintained multilingual cross-encoder with decent
# French coverage. Loaded lazily and cached at module level - loading it is the
# expensive part (a few seconds plus ~1GB of weights downloaded on first use), so it
# must happen once per process, never per query.
RERANK_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
SHORTLIST_TARGET = 20
CONTENT_PREVIEW_CHARACTERS = 900
PREVIEW_HEAD_CHARACTERS = 220
_PREVIEW_STOPWORDS = {
    "avec",
    "cette",
    "comment",
    "dans",
    "pour",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "selon",
    "article",
    "articles",
    "prevoit",
}

LLM_SHORTLIST_PROMPT = """Tu es un assistant de recherche juridique pour la RDC (Republique Democratique du Congo). Voici une question et une liste numerotee de passages candidats (titre, repere, apercu). Selectionne les {target} passages les PLUS PERTINENTS pour repondre a la question - privilegie la pertinence reelle au contenu de la question plutot que la simple presence de mots-cles partages.

Reponds UNIQUEMENT avec un tableau JSON des numeros selectionnes (entiers), du plus au moins pertinent, sans aucun autre texte ni balise markdown.

Question : {question}

Passages candidats :
{candidates}"""

_cross_encoder_model: Any = None
_cross_encoder_load_failed = False


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif hasattr(item, "text"):
                parts.append(str(item.text))
        return "\n".join(parts)
    return str(content or "")


def _parse_json_int_array(raw: str) -> list[int]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    numbers: list[int] = []
    for item in data:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            numbers.append(item)
        elif isinstance(item, float) and item.is_integer():
            numbers.append(int(item))
        elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
            numbers.append(int(item.strip()))
    return numbers


def _normalized_preview_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


def _normalized_preview_text_with_map(value: str) -> tuple[str, list[int]]:
    """Like _normalized_preview_text, but also returns a map from each output
    character's index back to the index of the original character it came from.

    NFKD decomposition and casefold() are not length-preserving (a ligature like
    "ﬁ" expands to two characters, "ß".casefold() becomes "ss"), so a position
    found in the normalized text does not line up with the same position in the
    original string. Without this map, callers that search the normalized text
    but slice the original one can silently read from the wrong offset."""
    normalized_characters: list[str] = []
    index_map: list[int] = []
    for original_index, character in enumerate(value):
        for piece in unicodedata.normalize("NFKD", character).casefold():
            if unicodedata.combining(piece):
                continue
            normalized_characters.append(piece)
            index_map.append(original_index)
    return "".join(normalized_characters), index_map


def _candidate_preview(question: str, content: str, limit: int = CONTENT_PREVIEW_CHARACTERS) -> str:
    """Show the opening plus a window around substantive query terms.

    Legal chunks can be several thousand characters long. Sending only their first
    few hundred characters hides the operative sentence whenever an article starts
    with definitions or procedural boilerplate. Keeping a short opening preserves
    context while the query-centered window exposes the likely evidence.
    """
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= limit:
        return compact

    normalized_question = _normalized_preview_text(question)
    query_terms = {
        term
        for term in re.findall(r"[^\W_]{4,}", normalized_question, flags=re.UNICODE)
        if term not in _PREVIEW_STOPWORDS
    }
    normalized_content, content_index_map = _normalized_preview_text_with_map(compact)
    matches = [
        (normalized_content.count(term), normalized_content.find(term), term)
        for term in query_terms
        if normalized_content.find(term) >= PREVIEW_HEAD_CHARACTERS
    ]
    if not matches:
        return compact[:limit]

    head_size = min(PREVIEW_HEAD_CHARACTERS, max(0, limit // 3))
    separator = " … "
    if limit <= len(separator):
        return compact[:limit]
    window_size = max(0, limit - head_size - len(separator))
    # A rare query term is usually more discriminating than a generic word such
    # as "fiscal" that may occur throughout the passage.
    _, focus_normalized, _ = min(matches)
    # focus_normalized indexes into normalized_content; translate it back to the
    # matching offset in compact (the string actually being sliced below).
    focus = content_index_map[focus_normalized] if content_index_map else focus_normalized
    window_start = max(head_size, focus - window_size // 3)
    window_end = min(len(compact), window_start + window_size)
    window_start = max(head_size, window_end - window_size)
    return f"{compact[:head_size]}{separator}{compact[window_start:window_end]}"


def llm_shortlist(
    question: str,
    candidates: list[RetrievedChunk],
    settings: Settings,
    target: int = SHORTLIST_TARGET,
) -> list[RetrievedChunk]:
    """Cheaply narrow a wide BM25 candidate pool before the expensive cross-encoder
    reranks it, reusing the same chat-completion APIs already wired in for query
    optimization. A funnel stage, not a source of truth: any failure here (no API key,
    bad JSON, request error) fails open - the original candidates are returned
    truncated to `target` in their existing order, so a broken shortlist step only
    forfeits the funnel's narrowing benefit for that one query, never breaks
    retrieval."""
    if len(candidates) <= target:
        return candidates

    openrouter_key = getattr(settings, "openrouter_api_key", None)
    openai_key = getattr(settings, "api_key", None)
    if not openrouter_key and not openai_key:
        return candidates[:target]

    listing = "\n".join(
        f"{index}. [{chunk.title} - {chunk.locator}] {_candidate_preview(question, chunk.content)}"
        for index, chunk in enumerate(candidates, start=1)
    )
    prompt = LLM_SHORTLIST_PROMPT.format(question=question, target=target, candidates=listing)
    try:
        from openai import OpenAI

        if openrouter_key:
            client = OpenAI(api_key=openrouter_key, base_url=settings.openrouter_base_url)
            response = client.chat.completions.create(
                model=settings.openrouter_model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=400,
                extra_body={"reasoning": {"effort": "low"}},
            )
            raw = _message_text(response.choices[0].message.content)
        else:
            client = OpenAI(api_key=openai_key)
            response = client.responses.create(
                model=settings.openai_model,
                input=prompt,
                reasoning={"effort": "low"},
                max_output_tokens=400,
            )
            raw = response.output_text

        seen: set[int] = set()
        shortlisted: list[RetrievedChunk] = []
        for position in _parse_json_int_array(raw):
            if 1 <= position <= len(candidates) and position not in seen:
                seen.add(position)
                shortlisted.append(candidates[position - 1])
        return shortlisted[:target] if shortlisted else candidates[:target]
    except Exception:
        return candidates[:target]


def _get_cross_encoder() -> Any:
    """Load and cache the local cross-encoder once per process. Returns None (rather
    than raising) when the optional dependency is not installed or the model fails to
    load - reranking is an enhancement, not a requirement, so its absence must never
    break retrieval."""
    global _cross_encoder_model, _cross_encoder_load_failed
    if _cross_encoder_model is not None or _cross_encoder_load_failed:
        return _cross_encoder_model
    try:
        from sentence_transformers import CrossEncoder

        _cross_encoder_model = CrossEncoder(RERANK_MODEL_NAME)
    except Exception:
        _cross_encoder_load_failed = True
    return _cross_encoder_model


def cross_encoder_rerank(question: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Precisely reorder a (already narrowed) shortlist by actual query-passage
    relevance. Fails open to the input order on any load or inference error."""
    if not candidates:
        return candidates
    model = _get_cross_encoder()
    if model is None:
        return candidates
    try:
        scores = model.predict([(question, chunk.content) for chunk in candidates])
    except Exception:
        return candidates
    return [chunk for _, chunk in sorted(zip(scores, candidates), key=lambda pair: pair[0], reverse=True)]


def rerank_chunks(
    question: str,
    candidates: list[RetrievedChunk],
    settings: Settings,
    final_limit: int,
    shortlist_target: int = SHORTLIST_TARGET,
) -> list[RetrievedChunk]:
    """Funnel: a cheap LLM call narrows a wide BM25 candidate pool, then a precise
    local cross-encoder does the final ordering on that shortlist. Both stages fail
    open independently, so retrieval degrades to the original BM25 ranking rather than
    breaking when either is unavailable."""
    if not getattr(settings, "rerank_enabled", True):
        return candidates[:final_limit]
    shortlisted = llm_shortlist(question, candidates, settings, target=shortlist_target)
    reranked = cross_encoder_rerank(question, shortlisted)
    return reranked[:final_limit]
