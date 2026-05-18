"""Unit tests for TextAlignmentAgent (stage E of the Layer-2 OCR pipeline).

All tests use MockModelClient from conftest so no network calls are made.
Each test documents the specific behaviour it asserts in its docstring.
"""

from __future__ import annotations

import pytest

from app.agents._runtime import TerminalError
from app.agents._schemas.text_alignment import TextAlignmentInput, TranscriptWord
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.agents.text_alignment import TextAlignmentAgent
from tests.agents.conftest import MockModelClient

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_phrase(
    lines: list[str],
    start_t_s: float = 0.0,
    end_t_s: float = 2.0,
) -> Phrase:
    return Phrase(
        lines=lines,
        start_t_s=start_t_s,
        end_t_s=end_t_s,
        aabb=(0.3, 0.6, 0.7, 0.75),
        mean_confidence=0.95,
    )


def _make_word(text: str, start_s: float, end_s: float) -> TranscriptWord:
    return TranscriptWord(text=text, start_s=start_s, end_s=end_s)


def _make_agent(mock_client: MockModelClient) -> TextAlignmentAgent:
    return TextAlignmentAgent(mock_client)


def _aligned_response(entries: list[dict]) -> dict:
    """Build the JSON shape the agent expects from Gemini."""
    return {"aligned_phrases": entries}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> MockModelClient:
    return MockModelClient()


@pytest.fixture
def agent(mock_client: MockModelClient) -> TextAlignmentAgent:
    return _make_agent(mock_client)


# ── Happy-path tests ──────────────────────────────────────────────────────────


def test_happy_path_all_phrases_aligned(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """3 OCR phrases with a transcript covering all of them.
    All phrases survive and their sample_text values are updated.
    """
    phrases = [
        _make_phrase(["It's", "not", "just luck"], 0.3, 2.0),
        _make_phrase(["if you", "put in"], 2.3, 4.5),
        _make_phrase(["The", "work", "to get", "there"], 4.8, 6.3),
    ]
    transcript_words = [
        _make_word("It's", 0.4, 0.6),
        _make_word("not", 0.7, 0.9),
        _make_word("just", 1.0, 1.2),
        _make_word("luck", 1.3, 1.5),
        _make_word("if", 2.4, 2.5),
        _make_word("you", 2.6, 2.7),
        _make_word("put", 2.8, 3.0),
        _make_word("in", 3.1, 3.2),
        _make_word("the", 4.9, 5.0),
        _make_word("work", 5.1, 5.3),
        _make_word("to", 5.4, 5.5),
        _make_word("get", 5.6, 5.7),
        _make_word("there", 5.8, 6.0),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["It's", "not", "just luck"]},
                {"index": 1, "lines": ["if you", "put in"]},
                {"index": 2, "lines": ["the", "work", "to get", "there"]},
            ]
        ),
    )
    inp = TextAlignmentInput(
        phrases=phrases,
        transcript_words=transcript_words,
        template_id="test-001",
    )
    out = agent.run(inp)

    assert out.dropped_count == 0
    assert len(out.phrases) == 3
    # Phrase 0 unchanged (already correct)
    assert out.phrases[0].sample_text == "It's\nnot\njust luck"
    # Phrase 1 unchanged
    assert out.phrases[1].sample_text == "if you\nput in"
    # Phrase 2: "The" → "the" corrected via transcript
    assert out.phrases[2].lines[0] == "the"
    assert out.phrases[2].sample_text == "the\nwork\nto get\nthere"


def test_alignment_fix_character_error(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """OCR phrase has 'angone' (should be 'anyone') and 'The' (should be 'the').
    After alignment the corrected spellings appear in the output.
    """
    phrases = [
        _make_phrase(["don't", "allow angone", "to diminish"], 9.3, 10.0),
    ]
    transcript_words = [
        _make_word("don't", 9.3, 9.5),
        _make_word("allow", 9.5, 9.7),
        _make_word("anyone", 9.7, 9.9),
        _make_word("to", 9.9, 10.0),
        _make_word("diminish", 10.0, 10.2),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["don't", "allow anyone", "to diminish"]},
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert out.dropped_count == 0
    assert len(out.phrases) == 1
    assert out.phrases[0].lines[1] == "allow anyone"  # "angone" → "anyone"


def test_drop_hallucination_no_transcript_match(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Phrase with no plausible transcript match is dropped.
    `dropped_count` reflects the omission.
    """
    phrases = [
        _make_phrase(["real caption"], 1.0, 3.0),
        _make_phrase(["@WatermarkHandle"], 0.0, 10.0),  # never spoken — hallucination
    ]
    transcript_words = [
        _make_word("real", 1.1, 1.3),
        _make_word("caption", 1.4, 1.6),
    ]
    # LLM returns only the matched phrase (omits the hallucination)
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["real caption"]},
                # index 1 is absent → dropped
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert out.dropped_count == 1
    assert len(out.phrases) == 1
    assert out.phrases[0].sample_text == "real caption"


def test_partial_output_missing_phrases(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Gemini returns valid JSON but for only some input phrases.
    The agent does not crash; missing phrases are silently dropped.
    """
    phrases = [
        _make_phrase(["hook text"], 0.0, 1.5),
        _make_phrase(["body copy"], 2.0, 4.0),
        _make_phrase(["cta phrase"], 5.0, 6.0),
    ]
    transcript_words = [
        _make_word("hook", 0.1, 0.3),
        _make_word("text", 0.4, 0.6),
        _make_word("body", 2.1, 2.3),
        _make_word("copy", 2.4, 2.6),
        _make_word("cta", 5.1, 5.3),
        _make_word("phrase", 5.4, 5.6),
    ]
    # Only first and last phrases returned
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["hook text"]},
                # index 1 missing
                {"index": 2, "lines": ["cta phrase"]},
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert len(out.phrases) == 2
    assert out.dropped_count == 1
    assert out.phrases[0].sample_text == "hook text"
    assert out.phrases[1].sample_text == "cta phrase"


def test_malformed_json_raises_schema_error(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Gemini returns broken JSON → SchemaError (wrapped into TerminalError by runtime)."""
    phrases = [_make_phrase(["some text"])]
    transcript_words = [_make_word("some", 0.0, 0.2), _make_word("text", 0.3, 0.5)]
    mock_client.queue("gemini-2.5-flash", "not valid json{{{")
    inp = TextAlignmentInput(phrases=phrases, transcript_words=transcript_words)
    with pytest.raises(TerminalError):
        agent.run(inp)


def test_empty_phrase_list_no_llm_call(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Input has 0 phrases → output has 0 phrases, no LLM call made."""
    out = agent.run(TextAlignmentInput(phrases=[], transcript_words=[]))

    assert out.phrases == []
    assert out.dropped_count == 0
    # Confirm the mock was never invoked
    assert len(mock_client.invocations) == 0


def test_preserve_line_breaks_in_buildup_caption(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """A build-up phrase with multiple lines has its line breaks preserved.
    Even though the transcript has no line breaks, the aligned output keeps
    the same number of lines with the same structure.
    """
    # OCR error: "s0..." → "so..." in last line; line structure must stay intact
    phrases = [
        _make_phrase(["and", "good timing", "s0..."], 8.3, 9.0),
    ]
    transcript_words = [
        _make_word("and", 8.4, 8.5),
        _make_word("good", 8.5, 8.7),
        _make_word("timing", 8.7, 8.9),
        _make_word("so...", 8.9, 9.0),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["and", "good timing", "so..."]},
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert len(out.phrases) == 1
    phrase = out.phrases[0]
    # Three separate lines — line breaks preserved
    assert len(phrase.lines) == 3
    assert phrase.lines[0] == "and"
    assert phrase.lines[1] == "good timing"
    assert phrase.lines[2] == "so..."
    # sample_text joins them with \n
    assert phrase.sample_text == "and\ngood timing\nso..."


def test_line_count_mismatch_falls_back_to_ocr(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """If LLM returns a different number of lines than the input phrase, the agent
    falls back to the original OCR lines and still keeps the phrase (no crash, no drop).
    """
    phrases = [
        _make_phrase(["line one", "line two"], 0.0, 2.0),
    ]
    transcript_words = [
        _make_word("line", 0.1, 0.3),
        _make_word("one", 0.4, 0.5),
        _make_word("line", 0.6, 0.7),
        _make_word("two", 0.8, 0.9),
    ]
    # LLM incorrectly collapses the two lines into one
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["line one line two"]},  # wrong: 1 line instead of 2
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    # Phrase is kept (not dropped) — falls back to original OCR lines
    assert len(out.phrases) == 1
    assert out.dropped_count == 0
    assert out.phrases[0].lines == ["line one", "line two"]


# ── Empty-transcript passthrough tests (music-only / caption-only templates) ──


def test_empty_transcript_passthrough_non_empty_phrases(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Input has phrases but transcript_words is empty (music-only template).
    All phrases are returned unchanged, no LLM call is made, dropped_count=0.
    This is the canary-template scenario: fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102.
    """
    phrases = [
        _make_phrase(["it's", "not", "just luck"], 0.3, 2.0),
        _make_phrase(["it's", "not", "just luck\nit's", "not", "just luck"], 2.3, 4.5),
    ]
    out = agent.run(
        TextAlignmentInput(
            phrases=phrases,
            transcript_words=[],  # empty — music-only, no speech
            template_id="fdaf3bbc-2f4f-43bc-ba7c-e5cd819de102",
        )
    )

    # All phrases pass through unchanged
    assert out.dropped_count == 0
    assert len(out.phrases) == 2
    assert out.phrases[0].lines == ["it's", "not", "just luck"]
    assert out.phrases[1].lines == ["it's", "not", "just luck\nit's", "not", "just luck"]
    # Timing and bbox unchanged
    assert out.phrases[0].start_t_s == 0.3
    assert out.phrases[0].end_t_s == 2.0
    # No LLM call was made
    assert len(mock_client.invocations) == 0


def test_empty_transcript_and_empty_phrases_returns_empty(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Both phrases and transcript_words are empty.
    Output is empty and no LLM call is made (existing behaviour unchanged).
    """
    out = agent.run(TextAlignmentInput(phrases=[], transcript_words=[]))

    assert out.phrases == []
    assert out.dropped_count == 0
    assert len(mock_client.invocations) == 0


def test_non_empty_transcript_still_calls_llm(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """When transcript_words is non-empty the LLM call path is taken as before."""
    phrases = [_make_phrase(["hello world"])]
    transcript_words = [_make_word("hello", 0.1, 0.3), _make_word("world", 0.4, 0.6)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["hello world"]}]),
    )
    out = agent.run(
        TextAlignmentInput(
            phrases=phrases,
            transcript_words=transcript_words,
            template_id="test-llm-path",
        )
    )

    assert len(out.phrases) == 1
    assert out.dropped_count == 0
    # LLM was called exactly once
    assert len(mock_client.invocations) == 1
    assert mock_client.invocations[0]["model"] == "gemini-2.5-flash"


def test_registry_registration() -> None:
    """TextAlignmentAgent is reachable via the registry under the expected name."""
    from app.agents._registry import get_agent
    from app.agents.text_alignment import TextAlignmentAgent

    cls = get_agent("nova.compose.text_alignment")
    assert cls is TextAlignmentAgent


def test_timing_and_bbox_preserved(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """After alignment, the phrase retains its original timing and bbox from OCR.
    The agent only updates `lines`; spatial / temporal metadata is unchanged.
    """
    phrase_with_custom_bbox = Phrase(
        lines=["hello world"],
        start_t_s=1.5,
        end_t_s=3.5,
        aabb=(0.1, 0.2, 0.9, 0.4),
        mean_confidence=0.88,
    )
    transcript_words = [_make_word("hello", 1.6, 1.8), _make_word("world", 1.9, 2.1)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["hello world"]}]),
    )
    out = agent.run(
        TextAlignmentInput(phrases=[phrase_with_custom_bbox], transcript_words=transcript_words)
    )

    assert len(out.phrases) == 1
    result = out.phrases[0]
    assert result.start_t_s == 1.5
    assert result.end_t_s == 3.5
    assert result.aabb == (0.1, 0.2, 0.9, 0.4)
    assert result.mean_confidence == 0.88
