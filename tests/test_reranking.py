from types import SimpleNamespace

import openai

import fiscal_rag.reranking as reranking_module
from fiscal_rag.reranking import (
    _parse_json_int_array,
    cross_encoder_rerank,
    llm_shortlist,
    rerank_chunks,
)
from fiscal_rag.retrieval import RetrievedChunk


def make_chunk(document_id: str, locator: str, content: str = "contenu") -> RetrievedChunk:
    return RetrievedChunk(
        document_id=document_id,
        title=f"Titre {document_id}",
        source="test",
        source_label="Test",
        category="",
        published_date="",
        source_url="",
        locator=locator,
        content=content,
        score=0.0,
    )


def test_parse_json_int_array_handles_various_formats() -> None:
    assert _parse_json_int_array("```json\n[3, 1, 2]\n```") == [3, 1, 2]
    assert _parse_json_int_array('[1, "2", 3.0, true]') == [1, 2, 3]
    assert _parse_json_int_array("not json") == []
    assert _parse_json_int_array('{"not": "a list"}') == []


def test_llm_shortlist_returns_pool_unchanged_when_not_larger_than_target() -> None:
    settings = SimpleNamespace(openrouter_api_key="key", api_key=None)
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    assert llm_shortlist("Question ?", candidates, settings, target=10) == candidates


def test_llm_shortlist_truncates_in_original_order_without_any_provider_configured() -> None:
    settings = SimpleNamespace(openrouter_api_key=None, api_key=None)
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    assert llm_shortlist("Question ?", candidates, settings, target=3) == candidates[:3]


def test_llm_shortlist_never_raises_on_request_failure(monkeypatch) -> None:
    settings = SimpleNamespace(
        openrouter_api_key="key",
        openrouter_base_url="https://example.test",
        openrouter_model="test-model",
        api_key=None,
    )
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    class FailingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(openai, "OpenAI", FailingClient)

    assert llm_shortlist("Question ?", candidates, settings, target=3) == candidates[:3]


def test_llm_shortlist_selects_and_orders_by_llm_response(monkeypatch) -> None:
    settings = SimpleNamespace(
        openrouter_api_key="key",
        openrouter_base_url="https://example.test",
        openrouter_model="test-model",
        api_key=None,
    )
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    class FakeMessage:
        content = "[4, 2]"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = FakeChat()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)

    result = llm_shortlist("Question ?", candidates, settings, target=3)

    assert [chunk.document_id for chunk in result] == ["3", "1"]


def test_cross_encoder_rerank_fails_open_when_model_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(reranking_module, "_get_cross_encoder", lambda: None)
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(3)]

    assert cross_encoder_rerank("Question ?", candidates) == candidates


def test_cross_encoder_rerank_orders_by_predicted_score(monkeypatch) -> None:
    class FakeModel:
        def predict(self, pairs):
            return [len(content) for _, content in pairs]

    monkeypatch.setattr(reranking_module, "_get_cross_encoder", lambda: FakeModel())
    candidates = [
        make_chunk("a", "Page 1", content="court"),
        make_chunk("b", "Page 2", content="un contenu beaucoup plus long"),
        make_chunk("c", "Page 3", content="moyen texte"),
    ]

    result = cross_encoder_rerank("Question ?", candidates)

    assert [chunk.document_id for chunk in result] == ["b", "c", "a"]


def test_rerank_chunks_respects_the_disabled_setting(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("reranking must be skipped entirely when disabled")

    monkeypatch.setattr(reranking_module, "llm_shortlist", explode)
    settings = SimpleNamespace(rerank_enabled=False)
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    assert rerank_chunks("Question ?", candidates, settings, final_limit=3) == candidates[:3]


def test_rerank_chunks_runs_the_full_funnel(monkeypatch) -> None:
    settings = SimpleNamespace(rerank_enabled=True, openrouter_api_key=None, api_key=None)
    candidates = [make_chunk(str(i), f"Page {i}") for i in range(5)]

    monkeypatch.setattr(reranking_module, "_get_cross_encoder", lambda: None)

    result = rerank_chunks("Question ?", candidates, settings, final_limit=2, shortlist_target=4)

    # No provider configured -> llm_shortlist fails open (truncates to shortlist_target);
    # no cross-encoder available -> cross_encoder_rerank fails open (order unchanged).
    assert result == candidates[:2]
