"""What a continued turn's stored planner answers may reveal, and when they stop applying.

Failure modes, written before the code:

- a stored answer that no longer validates (schema drift after a deploy) is logged with
  Pydantic's message, which quotes the offending value: the Main Creator is sensitive_io, so
  creator words reach the logs. A drifted answer is a silent miss;
- the key omits something that changes the answer for the same input (thinking level, output
  mode, release): a continued run gets a plausible answer from the old behaviour;
- the key cannot be computed (an input that does not serialise): the turn must still run.

Stub Redis; no network.
"""

from __future__ import annotations

import json
import traceback
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from app.services import turn_checkpoint

PRIVATE = "Creator private words: meet me at 5 Calle Mayor"


class _Answer(BaseModel):
    reply: int


class _Redis:
    def __init__(self, stored: dict[str, bytes] | None = None) -> None:
        self.stored = stored or {}

    def hget(self, _key: str, field: str) -> bytes | None:
        return self.stored.get(field)


class _RecordingLog:
    """Renders what a real renderer prints: the message, and the traceback when asked for."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def warning(self, event: str, **kw: Any) -> None:
        text = f"{event} {kw}"
        if kw.get("exc_info"):
            text += traceback.format_exc()
        self.lines.append(text)

    info = debug = warning


def test_a_stored_answer_that_no_longer_validates_logs_no_creator_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = json.dumps({"v": 1, "output": json.dumps({"reply": PRIVATE})}).encode()
    monkeypatch.setattr(turn_checkpoint, "_redis", lambda: _Redis({"main_creator:x": envelope}))
    recorded = _RecordingLog()
    monkeypatch.setattr(turn_checkpoint, "log", recorded)

    stored = turn_checkpoint._load(uuid.uuid4(), "main_creator:x", _Answer)

    assert stored is None
    assert "Calle Mayor" not in "\n".join(recorded.lines)
