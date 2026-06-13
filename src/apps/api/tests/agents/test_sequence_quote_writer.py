"""Unit tests for SequenceQuoteWriterAgent — prompt rendering (real data appears;
no unfilled placeholders survive) and strict quote-shape validation (sentence
count band, per-sentence word cap, total-word band, terminal punctuation,
quoted-span balance, sanitization).

The LLM is mocked via MockModelClient (tests/agents/conftest.py) for the
end-to-end run() tests; parse()/render_prompt() tests use the bare-instance
pattern from test_sequence_emphasis.py.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from app.agents._runtime import SchemaError, TerminalError
from app.agents.music_matcher import ClipSummary
from app.agents.sequence_quote_writer import (
    SequenceQuoteInput,
    SequenceQuoteWriterAgent,
    expected_sentence_count,
    split_quote_sentences,
)

# The approved rhythm-mode demo quote (video_duration_s=10.4 → 9 scenes).
GOOD_QUOTE = (
    'It\'s not "just luck". If you put in the work. To get there. Luck is just. '
    "A combination of hard work. And good timing. So… don't allow anyone. "
    "To diminish your hard work."
)

HERO_CLIP = ClipSummary(
    clip_id="clip_001",
    media_type="video",
    duration_s=4.2,
    subject="person training alone in a dim gym at night",
    hook_text="",
    hook_score=6.5,
    energy=6.0,
    description="moody handheld footage, chalk dust, heavy lifts, empty gym",
)


def _input(**overrides) -> SequenceQuoteInput:
    kwargs: dict = {
        "hero_clip": HERO_CLIP,
        "hero_transcript": "let's get this last set in",
        "tone": "gritty, no-excuses",
        "video_duration_s": 10.4,
    }
    kwargs.update(overrides)
    return SequenceQuoteInput(**kwargs)


def _agent() -> SequenceQuoteWriterAgent:
    return SequenceQuoteWriterAgent.__new__(SequenceQuoteWriterAgent)


def _response(quote: str) -> str:
    return json.dumps({"quote": quote})


# -- helpers ----------------------------------------------------------------------


def test_expected_sentence_count_tracks_duration():
    assert expected_sentence_count(10.4) == 8  # round(10.4 / 1.3) = 8
    assert expected_sentence_count(5.2) == 4  # exactly 4
    assert expected_sentence_count(2.0) == 4  # clamped up to the floor
    assert expected_sentence_count(60.0) == 9  # clamped down to the ceiling


def test_split_quote_sentences_matches_demo_split():
    # The approved demo split the quote into 9 sentences — "So…" ends its own
    # 1-word sentence because "…" is terminal punctuation.
    sentences = split_quote_sentences(GOOD_QUOTE)
    assert len(sentences) == 9
    assert sentences[6] == "So"
    assert sentences[7] == "don't allow anyone"
    assert sentences[-1] == "To diminish your hard work"


def test_split_quote_sentences_punctuation_runs_are_one_boundary():
    assert split_quote_sentences("what?! no way. really…") == ["what", "no way", "really"]


# -- render_prompt ----------------------------------------------------------------


def test_render_prompt_includes_real_data():
    rendered = _agent().render_prompt(_input())
    assert "person training alone in a dim gym at night" in rendered
    assert "let's get this last set in" in rendered
    assert "gritty, no-excuses" in rendered
    assert "10.4" in rendered  # video duration
    assert "about 8 sentences" in rendered  # duration-derived target


def test_render_prompt_includes_persona_context():
    rendered = _agent().render_prompt(
        _input(
            content_pillars=["discipline over motivation"],
            theme="the work nobody sees",
            idea="late-night solo session montage",
        )
    )
    assert "discipline over motivation" in rendered
    assert "the work nobody sees" in rendered
    assert "late-night solo session montage" in rendered


def test_render_prompt_language_instruction_turkish():
    rendered = _agent().render_prompt(_input(language="tr"))
    assert "TURKISH" in rendered
    assert "Output Turkish only" in rendered


def test_render_prompt_no_placeholder_survives_unfilled():
    rendered = _agent().render_prompt(_input())
    # string.Template ($var / ${var}) — an unfilled placeholder survives verbatim.
    for name in (
        "language_instruction",
        "tone",
        "persona_context",
        "preferences",
        "filming_guide",
        "hero_subject",
        "hero_hook",
        "hero_description",
        "hero_transcript",
        "video_duration_s",
        "target_sentences",
        "min_sentences",
        "max_sentences",
        "max_sentence_words",
        "min_total_words",
        "max_total_words",
    ):
        assert f"${name}" not in rendered
        assert f"${{{name}}}" not in rendered
        # {var}-style placeholders silently pass through safe_substitute (known
        # prod incident) — the prompt must never use them.
        assert f"{{{name}}}" not in rendered
    # No bare $-placeholder of ANY name survives (the prompt contains no other
    # literal '$' characters by construction).
    assert not re.search(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", rendered)


# -- input validation ---------------------------------------------------------------


def test_input_rejects_non_positive_duration():
    with pytest.raises(ValidationError):
        _input(video_duration_s=0.0)
    with pytest.raises(ValidationError):
        _input(video_duration_s=-3.0)


# -- parse / validation ---------------------------------------------------------------


def test_valid_quote_accepted():
    out = _agent().parse(_response(GOOD_QUOTE), _input())
    assert out.quote == GOOD_QUOTE


def test_too_few_sentences_raises_schema_error():
    # 3 sentences, but word totals + per-sentence caps all pass — the ONLY
    # violation is the sentence-count floor.
    quote = "This is the first sentence. Here comes the second one. And a third one lands."
    with pytest.raises(SchemaError, match="sentences"):
        _agent().parse(_response(quote), _input())


def test_too_many_sentences_raises_schema_error():
    quote = " ".join(["Short one here."] * 10)  # 10 sentences > 9
    with pytest.raises(SchemaError, match="sentences"):
        _agent().parse(_response(quote), _input())


def test_eight_word_sentence_raises_schema_error():
    quote = (
        "I wake before the sun. Nobody claps for the quiet hours here. "
        "It still counts. Watch me build."
    )
    # Sentence 1 has 8 words; everything else passes.
    with pytest.raises(SchemaError, match="words"):
        _agent().parse(_response(quote), _input())


def test_too_few_total_words_raises_schema_error():
    quote = "One. Two. Three. Four words now."  # 4 sentences but only 7 words
    with pytest.raises(SchemaError, match="total words"):
        _agent().parse(_response(quote), _input())


def test_too_many_total_words_raises_schema_error():
    quote = " ".join(["Six words live inside this sentence."] * 8)  # 48 words > 40
    with pytest.raises(SchemaError, match="total words"):
        _agent().parse(_response(quote), _input())


def test_empty_quote_raises_schema_error():
    with pytest.raises(SchemaError, match="empty"):
        _agent().parse(_response(""), _input())
    with pytest.raises(SchemaError, match="empty"):
        _agent().parse(_response("   "), _input())


def test_missing_terminal_punctuation_raises_schema_error():
    quote = "It's not luck. If you put in work. To get there. Luck is timing"
    with pytest.raises(SchemaError, match="terminal punctuation"):
        _agent().parse(_response(quote), _input())


def test_unbalanced_double_quotes_raise_schema_error():
    quote = "It's not \"just luck. If you put in the work. To get there. Luck is just good timing."
    with pytest.raises(SchemaError, match="double-quote"):
        _agent().parse(_response(quote), _input())


def test_curly_quotes_normalized_and_balanced_pair_accepted():
    quote = "It’s not “just luck”. If you put in the work. To get there. Luck is just good timing."
    out = _agent().parse(_response(quote), _input())
    assert '"just luck"' in out.quote  # curly pair normalized to straight quotes


def test_injected_handle_is_stripped():
    tainted = GOOD_QUOTE.replace("And good timing.", "And good timing @evil.")
    out = _agent().parse(_response(tainted), _input())
    assert "@" not in out.quote
    assert "evil" not in out.quote


def test_quote_missing_or_not_string_raises_schema_error():
    with pytest.raises(SchemaError, match="missing or not a string"):
        _agent().parse(json.dumps({}), _input())
    with pytest.raises(SchemaError, match="missing or not a string"):
        _agent().parse(json.dumps({"quote": 42}), _input())


def test_invalid_json_raises_schema_error():
    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_non_object_response_raises_schema_error():
    with pytest.raises(SchemaError):
        _agent().parse(json.dumps(["a quote"]), _input())


# -- end-to-end through the runtime (mocked LLM) --------------------------------------


def test_run_with_mocked_llm(mock_client):
    agent = SequenceQuoteWriterAgent(mock_client)
    mock_client.queue("gemini-2.5-flash", {"quote": GOOD_QUOTE})
    out = agent.run(_input())
    assert out.quote == GOOD_QUOTE
    assert len(mock_client.invocations) == 1
    prompt = mock_client.invocations[0]["prompt"]
    assert "person training alone in a dim gym at night" in prompt


def test_run_retries_with_clarification_on_schema_violation(mock_client):
    agent = SequenceQuoteWriterAgent(mock_client)
    bad = {"quote": "Way too short. Not enough."}
    good = {"quote": GOOD_QUOTE}
    mock_client.queue("gemini-2.5-flash", bad, good)
    out = agent.run(_input())
    assert out.quote == GOOD_QUOTE
    assert len(mock_client.invocations) == 2
    # The retry prompt carries the schema clarification suffix.
    assert "words or fewer" in mock_client.invocations[1]["prompt"]


def test_run_exhausted_schema_retries_is_terminal(mock_client):
    agent = SequenceQuoteWriterAgent(mock_client)
    bad = {"quote": "Way too short. Not enough."}
    mock_client.queue("gemini-2.5-flash", bad, bad)
    with pytest.raises(TerminalError):
        agent.run(_input())
