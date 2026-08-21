from fiscal_rag.history import ChatHistoryStore, conversation_title


def test_conversation_title_is_compact_and_bounded() -> None:
    assert conversation_title("  Quel   est le taux de TVA ?  ") == "Quel est le taux de TVA ?"
    assert len(conversation_title("x" * 200)) == 100


def test_saves_lists_loads_and_deletes_local_history(tmp_path) -> None:
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    citation = {
        "document_id": "a" * 20,
        "title": "Loi TVA",
        "pdf_url": f"/documents/{'a' * 20}",
    }

    conversation_id = store.save_exchange(
        question="Quel est le taux de TVA ?",
        answer="Le document indique un taux.",
        citations=[citation],
        provider="openrouter",
        model="openai/gpt-5.6-terra",
    )
    same_id = store.save_exchange(
        question="Quel article ?",
        answer="Voir l'article cité.",
        citations=[citation],
        provider="openrouter",
        model="openai/gpt-5.6-terra",
        conversation_id=conversation_id,
    )

    assert same_id == conversation_id
    conversations = store.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["message_count"] == 4
    loaded = store.get_conversation(conversation_id)
    assert loaded is not None
    assert [message["role"] for message in loaded["messages"]] == ["user", "assistant", "user", "assistant"]
    assert loaded["messages"][1]["citations"] == [citation]
    assert store.delete_conversation(conversation_id) is True
    assert store.get_conversation(conversation_id) is None
