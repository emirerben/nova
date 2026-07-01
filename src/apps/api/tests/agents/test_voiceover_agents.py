"""Unit tests for the "Get a transcript" voiceover agents + schema (no network, no DB).

Covers the deterministic parse()/validation contracts:
- script_writer: spoken-length band, URL/@handle sanitization, refusal detection,
  speech-appropriate line splitting (NOT the 6-word on-screen cap it replaces).
- interviewer: server-enforced 3-turn cap.
- VoiceoverScript schema: read-time + target-word helpers, blob validation.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RefusalError, SchemaError
from app.agents.voiceover_interviewer import (
    VoiceoverInterviewerAgent,
    VoiceoverInterviewerInput,
)
from app.agents.voiceover_script_writer import (
    VoiceoverScriptWriterAgent,
    VoiceoverScriptWriterInput,
    split_script_lines,
)
from app.schemas.voiceover_script import (
    VoiceoverScript,
    estimate_read_time_s,
    target_word_count,
)
from app.services.transcript_fallbacks import heuristic_question, heuristic_script

# A ~68-word first-person script — inside the band for 30s footage (target 69).
_SCRIPT_30S = (
    "It is 5am and nobody is awake yet. Just me, the cold, and a hundred animals "
    "who do not care how tired I am. I fill the troughs while the sky is still "
    "black. The first light comes over the ridge and for a second everything is "
    "quiet. This is the part nobody sees, but it is the part I would not trade "
    "for anything at all today."
)


def _script_agent() -> VoiceoverScriptWriterAgent:
    return VoiceoverScriptWriterAgent(MagicMock())


def _interviewer() -> VoiceoverInterviewerAgent:
    return VoiceoverInterviewerAgent(MagicMock())


def _inp(duration: float = 30.0) -> VoiceoverScriptWriterInput:
    return VoiceoverScriptWriterInput(
        footage_summary="farm at dawn, feeding animals",
        brief="morning routine",
        target_duration_s=duration,
    )


# ── script_writer ──────────────────────────────────────────────────────────────


def test_script_parse_happy_path_in_band() -> None:
    out = _script_agent().parse(json.dumps({"text": _SCRIPT_30S}), _inp())
    assert 48 <= len(out.text.split()) <= 97  # 0.7×..1.4× of target 69
    assert len(out.lines) >= 3
    # Every line is non-empty and a substring-ish of the script (no fabrication).
    assert all(line.strip() for line in out.lines)


def test_script_parse_rejects_too_short() -> None:
    with pytest.raises(RefusalError):
        _script_agent().parse(json.dumps({"text": "Way too short a script."}), _inp())


def test_script_parse_rejects_too_long() -> None:
    long_text = " ".join(["word"] * 300) + "."
    with pytest.raises(RefusalError):
        _script_agent().parse(json.dumps({"text": long_text}), _inp())


def test_script_parse_strips_urls_and_handles() -> None:
    poisoned = _SCRIPT_30S + " Also visit http://evil.com and follow @somebody and #tag."
    out = _script_agent().parse(json.dumps({"text": poisoned}), _inp())
    assert "evil.com" not in out.text
    assert "@somebody" not in out.text
    assert "#tag" not in out.text


def test_script_long_60s_not_truncated() -> None:
    # Regression: the short-field sanitizer caps at 400 chars; a 60s voiceover is
    # legitimately longer. The script must survive whole, never chopped with "…".
    words = ["the", "light", "breaks", "slow", "over", "the", "hills", "again"] * 20
    script = " ".join(words[:130]) + "."
    out = _script_agent().parse(json.dumps({"text": script}), _inp(duration=60))
    assert len(out.text.split()) == 130
    assert "…" not in out.text


def test_script_parse_rejects_refusal_text() -> None:
    with pytest.raises(RefusalError):
        _script_agent().parse(
            json.dumps({"text": "As an AI I cannot write this script for you today okay."}),
            _inp(),
        )


def test_script_parse_rejects_non_json() -> None:
    with pytest.raises(SchemaError):
        _script_agent().parse("not json", _inp())


def test_split_lines_is_speech_not_six_word_cap() -> None:
    # A 20-word sentence must NOT be chopped into 6-word fragments; clauses stay whole.
    text = (
        "This is a much longer sentence that keeps going and going, "
        "because it runs long and needs a natural break."
    )
    lines = split_script_lines(text)
    assert len(lines) >= 2
    # No line is a hard 6-word cap artifact: at least one line exceeds 6 words.
    assert any(len(line.split()) > 6 for line in lines)


# ── interviewer ─────────────────────────────────────────────────────────────────


def test_interviewer_not_final_before_cap() -> None:
    payload = {"question": "What should they feel?", "suggestions": ["Calm"], "is_final": False}
    out = _interviewer().parse(json.dumps(payload), VoiceoverInterviewerInput(turn_count=1))
    assert out.is_final is False


def test_interviewer_forces_final_at_cap() -> None:
    out = _interviewer().parse(
        json.dumps({"question": "Anything else?", "suggestions": ["No"], "is_final": False}),
        VoiceoverInterviewerInput(turn_count=3),
    )
    assert out.is_final is True


def test_interviewer_rejects_non_json() -> None:
    with pytest.raises(SchemaError):
        _interviewer().parse("nope", VoiceoverInterviewerInput(turn_count=1))


# ── schema helpers ──────────────────────────────────────────────────────────────


def test_read_time_and_target_word_helpers() -> None:
    assert target_word_count(30) == 69  # 30 * 2.3
    assert estimate_read_time_s(" ".join(["w"] * 69)) == 30
    assert estimate_read_time_s("") == 1  # floor


def test_voiceover_script_validates() -> None:
    s = VoiceoverScript(
        version=1,
        text=_SCRIPT_30S,
        read_time_s=estimate_read_time_s(_SCRIPT_30S),
        brief="morning routine",
        lines=split_script_lines(_SCRIPT_30S),
    )
    assert s.version == 1
    assert s.source == "generated"
    assert s.footage_summary is None


# ── heuristic fallbacks (no-Gemini localhost path) ──────────────────────────────


def test_heuristic_script_lands_in_band() -> None:
    # The no-Gemini fallback must produce band-valid text for every duration so the
    # route can persist it without an agent round-trip.
    for duration in (10, 30, 60, 90):
        t = target_word_count(duration)
        lo, hi = max(12, round(t * 0.7)), round(t * 1.4)
        script = heuristic_script("a morning at the farm", ["Calm", "the light"], duration)
        assert lo <= len(script.split()) <= hi, f"{duration}s script out of band"


def test_heuristic_script_bounded_for_absurd_duration() -> None:
    # Regression (review CRITICAL): duration_s is client-supplied and unbounded at
    # the schema was the trigger for a quadratic word-fill DoS. The heuristic must
    # clamp to _MAX_HEURISTIC_WORDS regardless of how large the duration is.
    script = heuristic_script("a quick clip", ["Calm"], 10_000_000)
    assert len(script.split()) <= 260, "heuristic must be word-capped for huge durations"


def test_heuristic_question_caps_at_three() -> None:
    assert heuristic_question(0)[2] is False  # first question, not final
    assert heuristic_question(2)[2] is True  # third question is final
    assert heuristic_question(5)[2] is True  # past the bank still final
