import http.client
import json
import threading
from types import SimpleNamespace

import server
from fiscal_rag.history import ChatHistoryStore


def request(port: int, method: str, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_history_http_lifecycle(tmp_path, monkeypatch) -> None:
    history = ChatHistoryStore(tmp_path / "history.sqlite3")
    conversation_id = history.save_exchange(
        "Question locale",
        "Réponse locale",
        [],
        "openrouter",
        "openai/gpt-5.6-terra",
    )
    monkeypatch.setattr(server, "HISTORY", history)
    monkeypatch.setattr(server, "SETTINGS", SimpleNamespace(history_enabled=True))

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.FiscalRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, listing = request(httpd.server_port, "GET", "/api/history")
        assert status == 200
        assert listing["conversations"][0]["conversation_id"] == conversation_id

        status, conversation = request(httpd.server_port, "GET", f"/api/history/{conversation_id}")
        assert status == 200
        assert len(conversation["messages"]) == 2

        status, deleted = request(httpd.server_port, "DELETE", f"/api/history/{conversation_id}")
        assert status == 200
        assert deleted == {"deleted": True}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
