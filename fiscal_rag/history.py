from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def conversation_title(question: str, limit: int = 100) -> str:
    compact = " ".join(question.split())
    return compact[:limit] or "Nouvelle conversation"


class ChatHistoryStore:
    """Persist chat messages locally without storing retrieved context blocks."""

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
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    response_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(conversation_id, message_id);
                """
            )
            version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and int(version[0]) != SCHEMA_VERSION:
                raise RuntimeError("The local chat-history database has an unsupported schema version.")
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def save_exchange(
        self,
        question: str,
        answer: str,
        citations: list[dict[str, Any]],
        provider: str,
        model: str,
        response_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        self.ensure_schema()
        now = utc_now()
        conversation_id = conversation_id or uuid.uuid4().hex
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT conversation_id FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not existing:
                connection.execute(
                    """
                    INSERT INTO conversations(conversation_id, title, created_at, updated_at, provider, model)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (conversation_id, conversation_title(question), now, now, provider, model),
                )
            connection.execute(
                """
                INSERT INTO messages(conversation_id, role, content, created_at)
                VALUES(?, 'user', ?, ?)
                """,
                (conversation_id, question, now),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    conversation_id, role, content, citations_json, provider, model, response_id, created_at
                ) VALUES(?, 'assistant', ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    answer,
                    json.dumps(citations, ensure_ascii=False),
                    provider,
                    model,
                    response_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE conversations
                SET updated_at=?, provider=?, model=?
                WHERE conversation_id=?
                """,
                (now, provider, model, conversation_id),
            )
        return conversation_id

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*, COUNT(m.message_id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id=c.conversation_id
                GROUP BY c.conversation_id
                ORDER BY c.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        self.ensure_schema()
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                return None
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY message_id",
                (conversation_id,),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = dict(row)
            try:
                message["citations"] = json.loads(message.pop("citations_json"))
            except json.JSONDecodeError:
                message["citations"] = []
                message.pop("citations_json", None)
            messages.append(message)
        return {**dict(conversation), "messages": messages}

    def recent_turns(self, conversation_id: str, max_exchanges: int = 4) -> list[dict[str, str]]:
        """Return the last few user/assistant turns for in-conversation memory.

        Answers are stored without their retrieved-context blocks, so replaying
        them keeps follow-up questions coherent without re-injecting stale
        evidence. Returns oldest-first, capped to the most recent exchanges.
        """
        if not self.path.is_file() or not conversation_id:
            return []
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM messages
                WHERE conversation_id=?
                ORDER BY message_id DESC
                LIMIT ?
                """,
                (conversation_id, max(0, max_exchanges) * 2),
            ).fetchall()
        turns = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
        return turns

    def delete_conversation(self, conversation_id: str) -> bool:
        if not self.path.is_file():
            return False
        self.ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            )
        return cursor.rowcount > 0
