from __future__ import annotations

import json
import mimetypes
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fiscal_rag.assistant import FiscalAssistant
from fiscal_rag.catalog import source_summary
from fiscal_rag.config import get_settings
from fiscal_rag.history import ChatHistoryStore
from fiscal_rag.indexer import FiscalIndexer
from fiscal_rag.locking import IndexingLock
from fiscal_rag.state import load_state


SETTINGS = get_settings()
HISTORY = ChatHistoryStore(SETTINGS.history_path)
MAX_REQUEST_BYTES = 64 * 1024
CONVERSATION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")


def json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def current_status() -> dict[str, object]:
    indexer = FiscalIndexer(SETTINGS, progress=lambda _: None)
    summary = indexer.summary()
    state = load_state(SETTINGS.state_path)
    records = [record for record in state["documents"].values() if record.get("present", True)]
    discovered = list(records)
    openai_ready = bool(SETTINGS.api_key and summary["indexed"] and summary["vector_store_id"])
    openrouter_ready = bool(SETTINGS.openrouter_api_key and summary["locally_indexed"])
    if SETTINGS.provider == "openai":
        ready_for_questions = openai_ready
        active_provider = "openai"
        searchable = summary["indexed"]
        model = SETTINGS.openai_model
    elif SETTINGS.provider == "openrouter":
        ready_for_questions = openrouter_ready
        active_provider = "openrouter"
        searchable = summary["locally_indexed"]
        model = SETTINGS.openrouter_model
    else:
        ready_for_questions = openai_ready or (SETTINGS.fallback_enabled and openrouter_ready)
        active_provider = "openai" if openai_ready else "openrouter"
        searchable = summary["indexed"] if openai_ready else summary["locally_indexed"]
        model = SETTINGS.openai_model if openai_ready else SETTINGS.openrouter_model
    return {
        **summary,
        "api_key_configured": bool(SETTINGS.api_key or SETTINGS.openrouter_api_key),
        "openai_key_configured": bool(SETTINGS.api_key),
        "openrouter_key_configured": bool(SETTINGS.openrouter_api_key),
        "provider_mode": SETTINGS.provider,
        "active_provider": active_provider,
        "openai_ready": openai_ready,
        "openrouter_ready": openrouter_ready,
        "fallback_enabled": SETTINGS.fallback_enabled,
        "history_enabled": SETTINGS.history_enabled,
        "searchable": searchable,
        "model": model,
        "openai_model": SETTINGS.openai_model,
        "openrouter_model": SETTINGS.openrouter_model,
        "reasoning_effort": SETTINGS.reasoning_effort,
        "ready_for_questions": ready_for_questions,
        "repository_root": str(SETTINGS.repository_root),
        "sources": source_summary(SETTINGS, discovered),
    }


class FiscalRequestHandler(BaseHTTPRequestHandler):
    server_version = "FiscalRAG/1.0"

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path == "/api/status":
            self.send_json(HTTPStatus.OK, current_status())
            return
        if path == "/api/history":
            conversations = HISTORY.list_conversations() if SETTINGS.history_enabled else []
            self.send_json(
                HTTPStatus.OK,
                {"enabled": SETTINGS.history_enabled, "conversations": conversations},
            )
            return
        history_match = re.fullmatch(r"/api/history/([a-f0-9]{32})", path)
        if history_match:
            if not SETTINGS.history_enabled:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Local chat history is disabled."})
                return
            conversation = HISTORY.get_conversation(history_match.group(1))
            if not conversation:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Conversation not found."})
                return
            self.send_json(HTTPStatus.OK, conversation)
            return
        if path.startswith("/documents/"):
            self.serve_document(path.removeprefix("/documents/"))
            return
        self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/chat":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length."})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body is empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
            return

        question = str(payload.get("question", "")).strip()
        if not question:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Please enter a question."})
            return
        if len(question) > 8000:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "The question is too long."})
            return
        available_sources = {source.id for source in SETTINGS.sources if source.enabled}
        requested_sources = payload.get("sources") or []
        sources = [str(value) for value in requested_sources if str(value) in available_sources]
        previous_response_id = payload.get("previous_response_id")
        if previous_response_id is not None and not re.fullmatch(r"resp_[A-Za-z0-9_-]+", str(previous_response_id)):
            previous_response_id = None
        conversation_id = str(payload.get("conversation_id") or "")
        if not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
            conversation_id = ""

        history_messages: list[dict[str, str]] = []
        recent_citations: list[dict[str, Any]] = []
        if SETTINGS.history_enabled and conversation_id:
            try:
                history_messages = HISTORY.recent_turns(conversation_id)
            except Exception:
                history_messages = []
            try:
                recent_citations = HISTORY.recent_citations(conversation_id)
            except Exception:
                recent_citations = []

        try:
            result = FiscalAssistant(SETTINGS).ask(
                question=question,
                sources=sources,
                previous_response_id=previous_response_id,
                history_messages=history_messages,
                recent_citations=recent_citations,
            )
            if SETTINGS.history_enabled:
                try:
                    conversation_id = HISTORY.save_exchange(
                        question=question,
                        answer=str(result.get("answer", "")),
                        citations=list(result.get("citations") or []),
                        provider=str(result.get("provider", "")),
                        model=str(result.get("model", "")),
                        response_id=str(result.get("response_id") or "") or None,
                        conversation_id=conversation_id or None,
                    )
                    result["conversation_id"] = conversation_id
                except Exception as history_error:
                    result["history_warning"] = f"The answer was not saved locally: {history_error}"
            self.send_json(HTTPStatus.OK, result)
        except Exception as error:
            message = str(error)
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if "configured" in message or "index" in message or "passages" in message
                else HTTPStatus.BAD_GATEWAY
            )
            self.send_json(status, {"error": message})

    def do_DELETE(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        match = re.fullmatch(r"/api/history/([a-f0-9]{32})", path)
        if not match:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        if not SETTINGS.history_enabled:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Local chat history is disabled."})
            return
        if not HISTORY.delete_conversation(match.group(1)):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Conversation not found."})
            return
        self.send_json(HTTPStatus.OK, {"deleted": True})

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json_bytes(payload)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely cancel duplicate status/history requests during reloads.
            return

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        requested = (SETTINGS.static_dir / relative).resolve()
        try:
            requested.relative_to(SETTINGS.static_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not requested.is_file():
            if CONVERSATION_ID_PATTERN.fullmatch(request_path.lstrip("/")):
                requested = (SETTINGS.static_dir / "index.html").resolve()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        content = requested.read_bytes()
        media_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{media_type}; charset=utf-8" if media_type.startswith("text/") or media_type == "application/javascript" else media_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def serve_document(self, document_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{20}", document_id):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        record = load_state(SETTINGS.state_path)["documents"].get(document_id)
        if not record or not record.get("present", True):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = Path(record["absolute_path"])
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resolved_path = path.resolve()
        allowed = False
        for source in SETTINGS.sources:
            for source_path in source.paths:
                allowed_root = source_path if source_path.is_dir() else source_path.parent
                try:
                    resolved_path.relative_to(allowed_root.resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if allowed:
                break
        if not allowed:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            elif match.group(2):
                length = int(match.group(2))
                start = max(0, size - length)
            if start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = "inline" if path.suffix.lower() in {".pdf", ".txt", ".csv"} else "attachment"
        # Source files on disk are UTF-8 (see extractor.py / cli.py's read_text_file).
        # Without an explicit charset, browsers guess - typically the OS locale's
        # legacy codepage (e.g. windows-1252) - and silently mangle every accented
        # character, since the underlying bytes are correct UTF-8 either way.
        content_type = f"{media_type}; charset=utf-8" if media_type.startswith("text/") else media_type
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        encoded_filename = quote(record["filename"], safe="")
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{encoded_filename}")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
                remaining -= len(chunk)


def main() -> None:
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    print("Scanning repository inventory...")
    with IndexingLock(SETTINGS.indexing_lock_path):
        summary = FiscalIndexer(SETTINGS, progress=lambda _: None).scan(include_review_required=False)
    print(f"Discovered {summary['discovered']} documents; {summary['searchable'] if 'searchable' in summary else summary['locally_indexed']} locally searchable.")
    address = ("127.0.0.1", SETTINGS.port)
    server = ThreadingHTTPServer(address, FiscalRequestHandler)
    print(f"RDC Fiscal Reference Assistant: http://{address[0]}:{address[1]}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping fiscal assistant.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
