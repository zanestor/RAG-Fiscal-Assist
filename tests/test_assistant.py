from types import SimpleNamespace

import openai

import fiscal_rag.assistant as assistant_module
from fiscal_rag.assistant import build_source_filter, collect_file_ids
from fiscal_rag.assistant import FiscalAssistant, _optimize_search_queries, _parse_json_string_array
from fiscal_rag.state import empty_state, save_state


def test_collects_unique_file_ids_from_annotations_and_results() -> None:
    response = SimpleNamespace(
        model_dump=lambda: {
            "output": [
                {"type": "message", "content": [{"annotations": [{"file_id": "file_a"}]}]},
                {"type": "file_search_call", "results": [{"file_id": "file_a"}, {"file_id": "file_b"}]},
            ]
        }
    )
    assert collect_file_ids(response) == ["file_a", "file_b"]


def test_builds_supported_source_filters() -> None:
    enabled = {"awa", "dgi", "dgrad"}
    assert build_source_filter(enabled, ["dgi"]) == {"type": "eq", "key": "source", "value": "dgi"}
    assert build_source_filter(enabled, ["dgi", "dgrad"]) == {
        "type": "or",
        "filters": [
            {"type": "eq", "key": "source", "value": "dgi"},
            {"type": "eq", "key": "source", "value": "dgrad"},
        ],
    }
    assert build_source_filter(enabled, ["awa", "dgi", "dgrad"]) is None


def test_auto_provider_falls_back_when_openai_request_fails(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    state = empty_state()
    state["vector_store_id"] = "vs_test"
    save_state(state_path, state)
    settings = SimpleNamespace(
        provider="auto",
        api_key="openai-test-key",
        state_path=state_path,
        fallback_enabled=True,
        openai_retry_seconds=300,
    )
    assistant = FiscalAssistant(settings)

    def fail_openai(*args, **kwargs):
        raise RuntimeError("quota unavailable")

    monkeypatch.setattr(assistant, "_ask_openai", fail_openai)
    monkeypatch.setattr(
        assistant,
        "_ask_openrouter",
        lambda *args, **kwargs: {"answer": "fallback", "provider": "openrouter"},
    )
    monkeypatch.setattr(assistant_module, "_openai_unavailable_until", 0.0)
    monkeypatch.setattr(assistant_module, "_openai_unavailable_reason", "")
    monkeypatch.setattr(assistant_module, "_optimize_search_queries", lambda *args, **kwargs: [])

    result = assistant.ask("Question fiscale")

    assert result["answer"] == "fallback"
    assert result["fallback_used"] is True
    assert result["fallback_from"] == "openai"
    assert result["fallback_reason"] == "RuntimeError"


def test_optimize_search_queries_returns_empty_without_any_provider_configured() -> None:
    settings = SimpleNamespace(openrouter_api_key=None, api_key=None)
    assert _optimize_search_queries("Une question ?", settings) == []


def test_optimize_search_queries_never_raises_on_request_failure(monkeypatch) -> None:
    settings = SimpleNamespace(
        openrouter_api_key="key",
        openrouter_base_url="https://example.test",
        openrouter_model="test-model",
        api_key=None,
    )

    class FailingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(openai, "OpenAI", FailingClient)

    assert _optimize_search_queries("Une question ?", settings) == []


def test_parse_json_string_array_handles_markdown_fenced_output() -> None:
    assert _parse_json_string_array('```json\n["a", "b"]\n```') == ["a", "b"]
    assert _parse_json_string_array('["a", "", "b", 3]') == ["a", "b"]
    assert _parse_json_string_array("not json") == []
    assert _parse_json_string_array('{"not": "a list"}') == []
