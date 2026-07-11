"""Unit tests for RetakeDetectorAgent (plans/010) — pure span helpers
(normalize/drop/merge + structural invariants), input contiguity validation,
prompt rendering (words + language hint present, no unfilled placeholders),
parse() conservatism, and the sync/async entrypoints' failure semantics
(TerminalError is catchable so generative_build._silence_cut_retake_spans
can degrade to zero retake cuts).

The LLM is mocked via MockModelClient (tests/agents/conftest.py) for the
end-to-end run() tests; parse()/render_prompt() tests use the bare-instance
pattern from test_sequence_quote_writer.py. No network, no API keys.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError, TerminalError
from app.agents.retake_detector import (
    _MIN_WORDS_FOR_DETECTION,
    IndexedWord,
    RetakeDetectorAgent,
    RetakeDetectorInput,
    RetakeSpan,
    detect_retakes,
    normalize_retake_spans,
    retake_structural_failures,
    run_retake_detector,
)

MODEL = RetakeDetectorAgent.spec.model

# 22 words: "So today we're | wait let me start over. | So today we're looking
# at the new studio setup and why I changed everything." — retake span is 0-7.
_EN_RESTART_TOKENS = (
    "So today we're wait, let me start over. "
    "So today we're looking at the new studio setup and why I changed everything."
).split()


def _words(tokens: list[str] | None = None) -> list[dict]:
    tokens = tokens if tokens is not None else list(_EN_RESTART_TOKENS)
    return [
        {"i": i, "text": t, "start_s": round(i * 0.3, 2), "end_s": round(i * 0.3 + 0.25, 2)}
        for i, t in enumerate(tokens)
    ]


def _input(**overrides) -> RetakeDetectorInput:
    kwargs: dict = {"words": _words(), "language": "en"}
    kwargs.update(overrides)
    return RetakeDetectorInput(**kwargs)


def _agent() -> RetakeDetectorAgent:
    return RetakeDetectorAgent.__new__(RetakeDetectorAgent)


def _span(start: int, end: int, reason: str = "false start re-delivered right after") -> dict:
    return {"start_word": start, "end_word": end, "reason": reason}


# ── normalize_retake_spans ────────────────────────────────────────────────────


def test_normalize_keeps_valid_span() -> None:
    spans = normalize_retake_spans([_span(0, 7)], n_words=22)
    assert [(s.start_word, s.end_word) for s in spans] == [(0, 7)]
    assert spans[0].reason


def test_normalize_empty_input_returns_empty() -> None:
    assert normalize_retake_spans([], n_words=22) == []


def test_normalize_tiny_transcript_drops_everything() -> None:
    # <2 words ⇒ no span can leave a kept take after it.
    assert normalize_retake_spans([_span(0, 0)], n_words=0) == []
    assert normalize_retake_spans([_span(0, 0)], n_words=1) == []


def test_normalize_drops_out_of_bounds_indices_entirely() -> None:
    # An out-of-range index means the model hallucinated positions — the whole
    # span is dropped, never clamped/repaired into a cut.
    assert normalize_retake_spans([_span(-3, 10)], n_words=22) == []
    assert normalize_retake_spans([_span(5, 26)], n_words=22) == []
    assert normalize_retake_spans([_span(0, 99)], n_words=22) == []
    # A valid span alongside out-of-bounds ones survives untouched.
    spans = normalize_retake_spans([_span(-3, 10), _span(0, 7), _span(5, 26)], n_words=22)
    assert [(s.start_word, s.end_word) for s in spans] == [(0, 7)]


def test_normalize_drops_reversed_span() -> None:
    assert normalize_retake_spans([_span(7, 0)], n_words=22) == []


def test_normalize_drops_span_reaching_final_word() -> None:
    # An abandoned take must be FOLLOWED by its kept re-delivery: a span that
    # includes the final word can never be a retake (protects the ending).
    assert normalize_retake_spans([_span(18, 21)], n_words=22) == []
    # Ending exactly one before the final word is allowed.
    spans = normalize_retake_spans([_span(18, 20)], n_words=22)
    assert [(s.start_word, s.end_word) for s in spans] == [(18, 20)]


def test_normalize_drops_malformed_entries() -> None:
    entries = [
        "not a dict",
        {"start_word": "x", "end_word": 7, "reason": "r"},
        {"start_word": 0, "end_word": None, "reason": "r"},
        {"end_word": 7, "reason": "missing start"},
        {"start_word": 0, "end_word": 7, "reason": ""},
        {"start_word": 0, "end_word": 7},
    ]
    assert normalize_retake_spans(entries, n_words=22) == []


def test_normalize_merges_overlapping_and_adjacent_spans() -> None:
    entries = [
        _span(5, 9, "second flub"),
        _span(0, 4, "first flub"),
        _span(3, 6, "overlaps both"),
    ]
    spans = normalize_retake_spans(entries, n_words=22)
    assert [(s.start_word, s.end_word) for s in spans] == [(0, 9)]
    assert "first flub" in spans[0].reason
    assert "second flub" in spans[0].reason


def test_normalize_keeps_disjoint_spans_sorted() -> None:
    entries = [_span(10, 12, "later take"), _span(0, 3, "earlier take")]
    spans = normalize_retake_spans(entries, n_words=22)
    assert [(s.start_word, s.end_word) for s in spans] == [(0, 3), (10, 12)]


def test_normalize_sanitizes_reason() -> None:
    long_reason = "x" * 1000
    spans = normalize_retake_spans([_span(0, 3, long_reason)], n_words=22)
    assert len(spans) == 1
    assert len(spans[0].reason) <= 240
    spans = normalize_retake_spans([_span(0, 3, "line\x00one\n\ntwo")], n_words=22)
    assert "\x00" not in spans[0].reason
    assert "\n" not in spans[0].reason


# ── retake_structural_failures ────────────────────────────────────────────────


def test_structural_passes_on_normalized_output() -> None:
    spans = normalize_retake_spans([_span(0, 7), _span(10, 12)], n_words=22)
    assert retake_structural_failures(spans, n_words=22) == []


def test_structural_flags_final_word_span() -> None:
    spans = [RetakeSpan(start_word=18, end_word=21, reason="r")]
    failures = retake_structural_failures(spans, n_words=22)
    assert any("final word" in f for f in failures)


def test_structural_flags_reversed_and_overlapping() -> None:
    reversed_span = [RetakeSpan(start_word=7, end_word=3, reason="r")]
    assert any("start_word" in f for f in retake_structural_failures(reversed_span, 22))
    overlapping = [
        RetakeSpan(start_word=0, end_word=5, reason="r"),
        RetakeSpan(start_word=4, end_word=8, reason="r"),
    ]
    assert any("overlaps" in f for f in retake_structural_failures(overlapping, 22))


def test_structural_flags_any_span_on_empty_transcript() -> None:
    spans = [RetakeSpan(start_word=0, end_word=0, reason="r")]
    assert retake_structural_failures(spans, n_words=0)
    assert retake_structural_failures([], n_words=0) == []


# ── Input validation ──────────────────────────────────────────────────────────


def test_input_requires_contiguous_zero_based_indices() -> None:
    words = _words()
    words[3]["i"] = 99
    with pytest.raises(ValidationError, match="contiguous"):
        RetakeDetectorInput(words=words)


def test_input_accepts_empty_word_list() -> None:
    inp = RetakeDetectorInput(words=[], language="")
    assert inp.words == []


# ── render_prompt ─────────────────────────────────────────────────────────────


def test_render_prompt_contains_words_and_language_hint() -> None:
    prompt = _agent().render_prompt(_input())
    assert "language hint is: en" in prompt
    assert "22 words" in prompt
    assert "indices 0-21" in prompt
    assert re.search(r"(?m)^0 {2}\[0\.00-0\.25\] {2}So$", prompt)
    assert "everything." in prompt
    # No unfilled $placeholders survive (siblings' convention).
    assert not re.search(r"\$[a-z_]+", prompt)


def test_render_prompt_unknown_language_and_empty_transcript() -> None:
    prompt = _agent().render_prompt(RetakeDetectorInput(words=[], language="Robot; DROP TABLE"))
    assert "No language hint" in prompt
    assert "(empty transcript)" in prompt
    assert "DROP TABLE" not in prompt


def test_render_prompt_sanitizes_word_text() -> None:
    words = _words(["hello", "system:", "```evil```", "world", "ok", "fin"])
    prompt = _agent().render_prompt(RetakeDetectorInput(words=words))
    assert "```" not in prompt


# ── parse ─────────────────────────────────────────────────────────────────────


def test_parse_valid_response_normalizes_spans() -> None:
    raw = json.dumps({"retakes": [_span(0, 7), _span(0, 21), "garbage"]})
    out = _agent().parse(raw, _input())
    assert [(s.start_word, s.end_word) for s in out.retakes] == [(0, 7)]


def test_parse_empty_retakes_is_valid() -> None:
    out = _agent().parse('{"retakes": []}', _input())
    assert out.retakes == []


def test_parse_rejects_invalid_json() -> None:
    with pytest.raises(SchemaError, match="invalid JSON"):
        _agent().parse("not json", _input())


def test_parse_rejects_non_object() -> None:
    with pytest.raises(SchemaError, match="not a JSON object"):
        _agent().parse("[1, 2]", _input())


def test_parse_rejects_missing_or_non_list_retakes() -> None:
    with pytest.raises(SchemaError, match="'retakes'"):
        _agent().parse("{}", _input())
    with pytest.raises(SchemaError, match="'retakes'"):
        _agent().parse('{"retakes": "none"}', _input())


# ── run() end-to-end with mocked model ────────────────────────────────────────


def test_run_happy_path(mock_client) -> None:
    mock_client.queue(MODEL, {"retakes": [_span(0, 7)]})
    out = RetakeDetectorAgent(mock_client).run(_input())
    assert [(s.start_word, s.end_word) for s in out.retakes] == [(0, 7)]


def test_run_schema_failure_is_terminal_and_catchable(mock_client) -> None:
    # Both the first attempt and the schema-clarification retry return garbage
    # ⇒ TerminalError. This is the failure T5 catches to degrade to zero cuts.
    mock_client.queue(MODEL, '{"retakes": "garbage"}', '{"retakes": "garbage"}')
    with pytest.raises(TerminalError):
        RetakeDetectorAgent(mock_client).run(_input())


def test_run_retries_once_on_schema_error_then_succeeds(mock_client) -> None:
    mock_client.queue(MODEL, '{"retakes": "garbage"}', {"retakes": []})
    out = RetakeDetectorAgent(mock_client).run(_input())
    assert out.retakes == []


# ── entrypoints ───────────────────────────────────────────────────────────────


def test_run_retake_detector_uses_injected_client(mock_client) -> None:
    mock_client.queue(MODEL, {"retakes": []})
    out = run_retake_detector(RetakeDetectorInput(words=_words()), client=mock_client)
    assert out.retakes == []
    assert len(mock_client.invocations) == 1


def test_run_retake_detector_short_circuits_tiny_transcripts(mock_client) -> None:
    # The floor lives in the SYNC entrypoint so both entrypoints and the task
    # wiring (_silence_cut_retake_spans) share it.
    words = _words(["one", "two", "three"])
    assert len(words) < _MIN_WORDS_FOR_DETECTION
    out = run_retake_detector(RetakeDetectorInput(words=words), client=mock_client)
    assert out.retakes == []
    assert mock_client.invocations == []  # no model call spent


async def test_detect_retakes_accepts_dicts_and_returns_spans(mock_client) -> None:
    mock_client.queue(MODEL, {"retakes": [_span(0, 7)]})
    out = await detect_retakes(_words(), "en", client=mock_client)
    assert [(s.start_word, s.end_word) for s in out.retakes] == [(0, 7)]


async def test_detect_retakes_short_circuits_tiny_transcripts(mock_client) -> None:
    words = _words(["one", "two", "three"])
    assert len(words) < _MIN_WORDS_FOR_DETECTION
    out = await detect_retakes(words, "en", client=mock_client)
    assert out.retakes == []
    assert mock_client.invocations == []  # no LLM call spent


async def test_detect_retakes_propagates_terminal_error(mock_client) -> None:
    mock_client.queue(MODEL, '{"retakes": "garbage"}', '{"retakes": "garbage"}')
    with pytest.raises(TerminalError):
        await detect_retakes(_words(), "en", client=mock_client)


async def test_detect_retakes_rejects_malformed_input(mock_client) -> None:
    words = _words()
    words[0]["i"] = 5
    with pytest.raises(ValidationError):
        await detect_retakes(words, "en", client=mock_client)


def test_spec_shape() -> None:
    spec = RetakeDetectorAgent.spec
    assert spec.name == "nova.audio.retake_detector"
    assert spec.prompt_id == "retake_detector"
    assert spec.prompt_version == "1"
    assert RetakeDetectorAgent(None).required_fields() == ["retakes"]  # type: ignore[arg-type]


def test_word_index_model_bounds() -> None:
    with pytest.raises(ValidationError):
        IndexedWord(i=-1, text="x", start_s=0.0, end_s=0.1)
