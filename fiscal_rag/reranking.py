from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .retrieval import RetrievedChunk

# BGE Reranker v2 (m3): a real, well-maintained multilingual cross-encoder with decent
# French coverage. Loaded lazily and cached at module level - loading it is the
# expensive part (a few seconds plus ~1GB of weights downloaded on first use), so it
# must happen once per process, never per query.
RERANK_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
SHORTLIST_TARGET = 20
CONTENT_PREVIEW_CHARACTERS = 400

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
        f"{index}. [{chunk.title} - {chunk.locator}] {chunk.content[:CONTENT_PREVIEW_CHARACTERS]}"
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
