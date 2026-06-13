"""Unit tests for SequenceEmphasisAgent — prompt rendering (no unfilled
placeholders survive) and strict per-phrase role validation (1:1 alignment,
known vocabulary, closer-final-only, at least one hero).

The LLM is mocked via MockModelClient (tests/agents/conftest.py) for the
end-to-end run() tests; parse()/render_prompt() tests use the bare-instance
pattern from test_intro_writer.py.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError, TerminalError
from app.agents.sequence_emphasis import (
    SequenceEmphasisAgent,
    SequenceEmphasisInput,
)

PHRASES = [
    ["it's", "not", "just", "luck"],
    ["don't", "allow", "anyone"],
]
GOOD_ROLES = {
    0: ["connector", "connector", "hero", "hero"],
    1: ["connector", "hero", "hero"],
}


def _input(
    phrases: list[list[str]] | None = None, language_hint: str = "en"
) -> SequenceEmphasisInput:
    return SequenceEmphasisInput(phrases=phrases or PHRASES, language_hint=language_hint)


def _agent() -> SequenceEmphasisAgent:
    return SequenceEmphasisAgent.__new__(SequenceEmphasisAgent)


def _response(roles_by_index: dict[int, list[str]]) -> str:
    return json.dumps(
        {"phrases": [{"index": i, "word_roles": r} for i, r in roles_by_index.items()]}
    )


# -- render_prompt ----------------------------------------------------------------


def test_render_prompt_includes_real_phrase_data():
    rendered = _agent().render_prompt(_input())
    assert '"it\'s"' in rendered
    assert '"luck"' in rendered
    assert '"allow"' in rendered
    assert '"index": 0' in rendered
    assert '"index": 1' in rendered


def test_render_prompt_includes_language_hint():
    rendered = _agent().render_prompt(_input(language_hint="tr"))
    assert "language: tr" in rendered


def test_render_prompt_no_placeholder_survives_unfilled():
    rendered = _agent().render_prompt(_input())
    # string.Template ($var / ${var}) — an unfilled placeholder survives verbatim.
    assert "$phrases_json" not in rendered
    assert "$language_hint" not in rendered
    assert "${phrases_json}" not in rendered
    assert "${language_hint}" not in rendered
    # {var}-style placeholders silently pass through safe_substitute (known prod
    # incident) — the prompt must never use them.
    assert "{phrases_json}" not in rendered
    assert "{language_hint}" not in rendered
    # No bare $-placeholder of ANY name survives (the prompt contains no other
    # literal '$' characters by construction).
    assert not re.search(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", rendered)


def test_render_prompt_turkish_words_preserved():
    rendered = _agent().render_prompt(
        _input(phrases=[["bu", "sadece", "şans", "değil"]], language_hint="tr")
    )
    # ensure_ascii=False keeps diacritics as real codepoints, not \\u escapes.
    assert "şans" in rendered
    assert "değil" in rendered


# -- input validation ---------------------------------------------------------------


def test_input_rejects_empty_phrase():
    with pytest.raises(ValidationError):
        SequenceEmphasisInput(phrases=[["fine"], []])


def test_input_rejects_blank_word():
    with pytest.raises(ValidationError):
        SequenceEmphasisInput(phrases=[["ok", "  "]])


def test_input_rejects_overlong_phrase():
    with pytest.raises(ValidationError):
        SequenceEmphasisInput(phrases=[["one", "two", "three", "four", "five", "six", "seven"]])


# -- parse / validation ---------------------------------------------------------------


def test_valid_annotation_accepted():
    out = _agent().parse(_response(GOOD_ROLES), _input())
    assert [p.index for p in out.phrases] == [0, 1]
    assert out.phrases[0].word_roles == ["connector", "connector", "hero", "hero"]
    assert out.phrases[1].word_roles == ["connector", "hero", "hero"]


def test_closer_as_final_word_accepted():
    roles = {0: ["connector", "connector", "hero", "closer"], 1: GOOD_ROLES[1]}
    out = _agent().parse(_response(roles), _input())
    assert out.phrases[0].word_roles[-1] == "closer"


def test_misaligned_length_raises_schema_error_naming_phrase():
    roles = {0: ["hero"], 1: GOOD_ROLES[1]}
    with pytest.raises(SchemaError, match="phrase 0"):
        _agent().parse(_response(roles), _input())


def test_unknown_role_raises_schema_error():
    roles = {0: ["connector", "connector", "big", "hero"], 1: GOOD_ROLES[1]}
    with pytest.raises(SchemaError, match="phrase 0"):
        _agent().parse(_response(roles), _input())


def test_mid_phrase_closer_raises_schema_error():
    roles = {0: ["closer", "connector", "hero", "hero"], 1: GOOD_ROLES[1]}
    with pytest.raises(SchemaError, match="phrase 0"):
        _agent().parse(_response(roles), _input())


def test_zero_hero_phrase_raises_schema_error():
    roles = {0: GOOD_ROLES[0], 1: ["connector", "connector", "connector"]}
    with pytest.raises(SchemaError, match="phrase 1"):
        _agent().parse(_response(roles), _input())


def test_missing_phrase_annotation_raises_schema_error():
    raw = json.dumps({"phrases": [{"index": 0, "word_roles": GOOD_ROLES[0]}]})
    with pytest.raises(SchemaError, match="phrase 1"):
        _agent().parse(raw, _input())


def test_out_of_range_index_raises_schema_error():
    roles = dict(GOOD_ROLES)
    roles[7] = ["hero"]
    with pytest.raises(SchemaError, match="phrase 7"):
        _agent().parse(_response(roles), _input())


def test_duplicate_index_raises_schema_error():
    raw = json.dumps(
        {
            "phrases": [
                {"index": 0, "word_roles": GOOD_ROLES[0]},
                {"index": 0, "word_roles": GOOD_ROLES[0]},
                {"index": 1, "word_roles": GOOD_ROLES[1]},
            ]
        }
    )
    with pytest.raises(SchemaError, match="phrase 0"):
        _agent().parse(raw, _input())


def test_invalid_json_raises_schema_error():
    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_non_object_response_raises_schema_error():
    with pytest.raises(SchemaError):
        _agent().parse(json.dumps(["hero"]), _input())


# -- end-to-end through the runtime (mocked LLM) --------------------------------------


def test_run_with_mocked_llm_single_call_covers_all_phrases(mock_client):
    agent = SequenceEmphasisAgent(mock_client)
    mock_client.queue(
        "gemini-2.5-flash",
        {"phrases": [{"index": i, "word_roles": r} for i, r in GOOD_ROLES.items()]},
    )
    out = agent.run(_input())
    assert len(out.phrases) == 2
    assert len(mock_client.invocations) == 1  # ONE call for ALL phrases
    prompt = mock_client.invocations[0]["prompt"]
    assert '"luck"' in prompt


def test_run_retries_with_clarification_on_schema_violation(mock_client):
    agent = SequenceEmphasisAgent(mock_client)
    bad = {"phrases": [{"index": 0, "word_roles": ["hero"]}, {"index": 1, "word_roles": ["hero"]}]}
    good = {"phrases": [{"index": i, "word_roles": r} for i, r in GOOD_ROLES.items()]}
    mock_client.queue("gemini-2.5-flash", bad, good)
    out = agent.run(_input())
    assert [p.word_roles for p in out.phrases] == [GOOD_ROLES[0], GOOD_ROLES[1]]
    assert len(mock_client.invocations) == 2
    # The retry prompt carries the schema clarification suffix.
    assert "aligned 1:1" in mock_client.invocations[1]["prompt"]


def test_run_exhausted_schema_retries_is_terminal(mock_client):
    agent = SequenceEmphasisAgent(mock_client)
    bad = {"phrases": [{"index": 0, "word_roles": ["hero"]}, {"index": 1, "word_roles": ["hero"]}]}
    mock_client.queue("gemini-2.5-flash", bad, bad)
    with pytest.raises(TerminalError):
        agent.run(_input())
