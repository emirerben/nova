"""KRI-952: a failed clip vision check records WHY it failed, never the creator's words.

Prod thread 545F53A9 (Don Julio walk-in M2), round 7 (turn 452064c5): the chat's first vision
check (``order_specific_clips`` on the one video) failed 3 ms after the Gemini file went
active. The creator got "I couldn't reliably match that request to your clips. Please try
again shortly."; the turn said only ``reason=vision_provider_error`` with
``failed_error_types=[]``, and no ``agent_run`` row was written, so nothing says what broke.

Real here: the resolver's parser, the vision candidate batching, ``query_clip_vision`` (its
download and the Gemini upload are faked, as in the other resolution tests) and the
ClipQuestionAgent runtime. The failure is forced at each place it can happen: before the
agent runs (the request built after the upload), inside the provider transport, and in the
upload itself.

Ways this goes wrong, each pinned below:
1. A failed vision check leaves no exception class in the turn diagnostics or the logs.
2. The record leaks private words: an exception message can quote the creator's request or
   the vision question built from it, and media ids are file names. Class names and an
   HTTP status only.
3. A wrapped error hides the real one: the runtime turns an unclassified SDK error into a
   ``TerminalError``; the cause must be named.
4. The fix changes behaviour: the status, reason code, deferral and the creator's reply
   must stay as they are.
5. Many failing candidates grow the record without bound.
6. An error class nobody has seen yet still lands (the generic path).
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from app.agents._runtime import ModelClient, ModelInvocation, RunContext
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn

VIDEO = "analysis-proxy-ios-155481DC-92B9-431A-BD9B-6D69972E2894.mp4"
PRIVATE = "Ojo de bife"  # creator words that must never reach the record
REQUEST = f'Show the ribeye photo with the text "{PRIVATE} 🥩", then the six moments in order.'


class ClientError(Exception):
    """Shaped like google.genai.errors.ClientError: an int ``code`` and a quoting message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


_MOMENTS = (
    "the window sign",
    "the queue",
    "the butcher",
    "the cook",
    "the dried flowers",
    "the plaques",
    "the ribeye",
    "the dining room",
    "the grill",
    "the waiter",
    "the street",
    "the closing line",
)


def _intent(n: int = 1) -> list[ClipIntent]:
    """``n`` order rows, each its own vision check (a check is asked about its own words)."""
    return [
        ClipIntent(intent_id=f"order_specific_clips_{i}", op="order", attribute=_MOMENTS[i])
        for i in range(n)
    ]


def _wire(monkeypatch: pytest.MonkeyPatch, intents: list[ClipIntent], *, upload=None) -> None:
    """The resolver sends every intent to vision on the one video, as in prod round 7."""

    def _resolver(self, input, *, ctx=None):  # noqa: A002, ANN001
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=i.intent_id,
                    needs_vision=[
                        ResolverVisionQuestion(
                            media="m001", question=f"Is {PRIVATE} on screen in this clip ({n})?"
                        )
                    ],
                )
                for n, i in enumerate(intents)
            ]
        )

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolver)

    def _download(_path, local_path, **_kwargs):  # noqa: ANN001, ANN003
        with open(local_path, "wb") as fh:
            fh.write(b"fake")

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr("app.storage.download_generation_to_file", _download)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        upload or (lambda _path, timeout=120: SimpleNamespace(uri="files/7vme4z4px9px")),
    )


class _Transport(ModelClient):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def invoke(self, **_: Any) -> ModelInvocation:
        raise self.error


async def _turn(intents: list[ClipIntent], *, background: bool = False):
    clip = IntentClip(
        media_id=VIDEO,
        kind="video",
        analysis={"subject": "a walk into a steakhouse", "description": "one long take"},
        gcs_path=f"users/u/clips/{VIDEO}",
    )
    with capture_logs() as logs:
        result = await resolve_clip_intents_for_turn(
            intents=intents,
            creator_request=REQUEST,
            clips=[clip],
            run_context=RunContext(),
            background=background,
        )
    return result, logs


_RECORD_EVENTS = {"clip_intent_vision_failed", "clip_intent_resolution_degraded"}


def _assert_private(result, logs) -> None:
    """Failure 2: no creator words, vision question or file name in the failure record (the
    diagnostics and the two failure log lines; ``agent_run`` keeps its own projection)."""
    record = json.dumps(
        [result.diagnostics, [e for e in logs if e["event"] in _RECORD_EVENTS]], default=str
    )
    for secret in (PRIVATE, "on screen in this clip", VIDEO, "155481DC", "7vme4z4px9px"):
        assert secret not in record, secret


def _assert_unchanged(result) -> None:
    """Failure 4: what the creator sees and what the turn does next are as before."""
    assert result.status == "provider_unavailable"
    assert result.error_code == "vision_provider_error"
    assert result.diagnostics["reason"] == "vision_provider_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        pytest.param(
            lambda: ClientError(400, f"INVALID_ARGUMENT: '{PRIVATE}' file {VIDEO}"),
            "TerminalError<-ClientError:400",
            id="provider-400",
        ),
        pytest.param(
            lambda: ClientError(503, "UNAVAILABLE"),
            "TerminalError<-ClientError:503",
            id="provider-503",
        ),
        pytest.param(
            lambda: RuntimeError(f"unexpected {PRIVATE}"),
            "TerminalError<-RuntimeError",
            id="never-seen-class",
        ),
    ],
)
async def test_a_provider_failure_records_its_class_and_cause(
    monkeypatch, make_error, expected
) -> None:
    """Failures 1, 3, 6: inside the provider transport (the runtime wraps it)."""
    intents = _intent()
    _wire(monkeypatch, intents)
    monkeypatch.setattr(resolution, "default_client", lambda: _Transport(make_error()))
    result, logs = await _turn(intents)
    _assert_unchanged(result)
    assert result.diagnostics["vision_error_types"] == [expected]
    degraded = next(e for e in logs if e["event"] == "clip_intent_resolution_degraded")
    assert degraded["vision_error_types"] == [expected]
    _assert_private(result, logs)


@pytest.mark.asyncio
async def test_a_bad_request_built_after_the_upload_is_named(monkeypatch) -> None:
    """Failure 1, prod's shape: the file goes active, then the request fails before any
    model call (no ``agent_run`` row). Here the upload returns no file URI."""
    intents = _intent()
    _wire(monkeypatch, intents, upload=lambda _path, timeout=120: SimpleNamespace(uri=None))
    monkeypatch.setattr(resolution, "default_client", lambda: _Transport(AssertionError("x")))
    result, logs = await _turn(intents)
    _assert_unchanged(result)
    assert result.diagnostics["vision_error_types"] == ["ValidationError"]
    assert not any(e["event"] == "agent_run" for e in logs)  # as in prod
    _assert_private(result, logs)


@pytest.mark.asyncio
async def test_an_upload_failure_is_named(monkeypatch) -> None:
    """Failure 1: the Gemini upload itself (before the file is active)."""
    intents = _intent()

    def _upload(_path, timeout=120):  # noqa: ANN001, ANN202
        raise TimeoutError(f"upload of {VIDEO} timed out")

    _wire(monkeypatch, intents, upload=_upload)
    result, logs = await _turn(intents)
    _assert_unchanged(result)
    assert result.diagnostics["vision_error_types"] == ["TimeoutError"]
    _assert_private(result, logs)


@pytest.mark.asyncio
async def test_the_record_is_bounded(monkeypatch) -> None:
    """Failure 5: twelve distinct checks fail (the background lane runs them all), at most
    eight entries are kept."""
    intents = _intent(12)
    _wire(monkeypatch, intents)
    monkeypatch.setattr(
        resolution, "default_client", lambda: _Transport(ClientError(429, "RESOURCE_EXHAUSTED"))
    )
    result, logs = await _turn(intents, background=True)
    assert sum(e["event"] == "clip_intent_vision_failed" for e in logs) == 12
    types = result.diagnostics["vision_error_types"]
    assert len(types) == 8
    assert set(types) == {"TerminalError<-ClientError:429"}


def test_the_class_chain_is_names_and_status_only() -> None:
    """Failures 2 + 3, the formatter on its own: a message never survives."""
    try:
        try:
            raise ClientError(400, f"bad request for {PRIVATE}")
        except ClientError as inner:
            raise ValueError(f"wrapped {PRIVATE}") from inner
    except ValueError as exc:
        assert resolution._error_type(exc) == "ValueError<-ClientError:400"
    assert re.fullmatch(r"[A-Za-z_]+", resolution._error_type(KeyError(PRIVATE)))
