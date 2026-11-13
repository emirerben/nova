"""A local stand-in for api.openai.com that the real ``openai`` SDK talks to over HTTP.

Point a client at it with ``base_url=server.url`` (or ``OPENAI_BASE_URL``). Each request takes
the next queued behavior: ``("ok", ...)`` answers, ``("status", 503)`` fails with that HTTP
status, ``("drop",)`` closes the connection without an answer. An empty queue answers ``ok``.
``requests`` keeps every request body the server saw, so a test can prove a refused
reservation never reached the provider.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAI:
    def __init__(self, *, prompt_tokens: int = 1_000, completion_tokens: int = 200) -> None:
        self.behaviors: deque[tuple[Any, ...]] = deque()
        self.requests: list[dict[str, Any]] = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.audio_duration_s = 125.0
        self.resolved_model = "gpt-4o-2024-08-06"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.requests.append({"path": self.path, "body": raw})
                behavior = outer.behaviors.popleft() if outer.behaviors else ("ok",)
                if behavior[0] == "drop":
                    self.connection.close()
                    return
                if behavior[0] == "status":
                    self._send(behavior[1], {"error": {"message": "forced", "type": "forced"}})
                    return
                if self.path.endswith("/audio/transcriptions"):
                    self._send(200, outer.transcription())
                else:
                    self._send(200, outer.completion(json.loads(raw)))

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def completion(self, request: dict[str, Any]) -> dict[str, Any]:
        """Answer a correction or translation request: the lines back, one word changed."""
        user = json.loads(request["messages"][-1]["content"])
        lines = [re.sub(r"\bteh\b", "the", str(line)) for line in user.get("lines", [])]
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": self.resolved_model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({"lines": lines})},
                }
            ],
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }

    def transcription(self) -> dict[str, Any]:
        return {
            "task": "transcribe",
            "language": "english",
            "duration": self.audio_duration_s,
            "text": "hello there",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.5},
                {"word": "there", "start": 0.6, "end": 1.0},
            ],
        }

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
