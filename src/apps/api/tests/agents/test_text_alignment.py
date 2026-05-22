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


def test_missing_index_falls_back_to_ocr_not_dropped(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Phrase the LLM omits is kept with its OCR text, not dropped.

    Fix B (2026-05-21): the old behavior dropped omitted indices as
    "hallucinations" but in practice this discarded legitimate on-screen
    text whenever the transcript was incomplete (Whisper missed a word,
    music-bed mumble, etc.). The OCR text IS what's visible on screen;
    showing it is strictly better than nothing.
    """
    phrases = [
        _make_phrase(["real caption"], 1.0, 3.0),
        _make_phrase(["@WatermarkHandle"], 5.0, 10.0),  # speaker silent here
    ]
    transcript_words = [
        _make_word("real", 1.1, 1.3),
        _make_word("caption", 1.4, 1.6),
    ]
    # LLM returns only the matched phrase (omits index 1)
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["real caption"]},
                # index 1 is absent → OCR fallback under Fix B
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert out.dropped_count == 0
    assert len(out.phrases) == 2
    assert out.phrases[0].sample_text == "real caption"
    assert out.phrases[1].sample_text == "@WatermarkHandle"


def test_partial_output_missing_phrases_kept_via_ocr(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """LLM omits a middle index → that phrase is kept via OCR fallback."""
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
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["hook text"]},
                # index 1 missing → kept via OCR fallback
                {"index": 2, "lines": ["cta phrase"]},
            ]
        ),
    )
    out = agent.run(TextAlignmentInput(phrases=phrases, transcript_words=transcript_words))

    assert len(out.phrases) == 3
    assert out.dropped_count == 0
    assert [p.sample_text for p in out.phrases] == ["hook text", "body copy", "cta phrase"]


def test_ocr_fallback_runs_sanitizer(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """OCR fallback for omitted indices must strip ASS tags, debug markers,
    and escape sequences just like the aligned-output path does.
    """
    phrases = [
        _make_phrase([r"{\an5}garbage[overlap_truncated]"], 1.0, 2.0),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([]),  # LLM omits index 0
    )
    out = agent.run(
        TextAlignmentInput(phrases=phrases, transcript_words=[_make_word("something", 1.0, 1.5)])
    )
    assert len(out.phrases) == 1
    # ASS tag and debug marker stripped; "garbage" survives as the actual word.
    assert out.phrases[0].sample_text == "garbage"


def test_ocr_fallback_drops_when_sanitizer_empties(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """OCR fallback only kicks in when OCR has real content. If sanitisation
    reduces the OCR lines to nothing, the phrase is genuinely dropped.
    """
    phrases = [
        _make_phrase([r"{\an5}{\fs120}"], 1.0, 2.0),  # ASS tags only
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([]),
    )
    out = agent.run(
        TextAlignmentInput(phrases=phrases, transcript_words=[_make_word("something", 1.0, 1.5)])
    )
    assert len(out.phrases) == 0
    assert out.dropped_count == 1


def test_atomized_uniqueness_revert_overlapping_duplicates(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """LLM maps three distinct OCR phrases to the same transcript word.
    The first survives as the LLM's pick; the rest revert to OCR text.

    Reproduces the prod failure shape on template fdaf3bbc 2026-05-21
    where "allow", "anyone", and "diminish" at 9.5-10.0 were all relabeled
    to "allow" because the transcript only had "allow" in that window.
    """
    phrases = [
        _make_phrase(["allow"], 9.5, 10.0),
        _make_phrase(["anyone"], 9.5, 10.0),
        _make_phrase(["diminish"], 9.5, 10.0),
    ]
    transcript_words = [_make_word("allow", 9.5, 9.7)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["allow"]},
                {"index": 1, "lines": ["allow"]},  # collision — must revert to OCR
                {"index": 2, "lines": ["allow"]},  # collision — must revert to OCR
            ]
        ),
    )
    out = agent.run(
        TextAlignmentInput(
            phrases=phrases,
            transcript_words=transcript_words,
            atomize_mode=True,
        )
    )
    sample_texts = [p.sample_text for p in out.phrases]
    assert sample_texts == ["allow", "anyone", "diminish"], (
        "phrase 0 keeps the LLM's 'allow'; phrases 1 and 2 revert to OCR "
        f"because 'allow' was already assigned; got {sample_texts}"
    )
    assert out.dropped_count == 0


def test_atomized_uniqueness_preserves_legitimate_repeat_with_gap(
    agent: TextAlignmentAgent,
    mock_client: MockModelClient,
) -> None:
    """Two phrases at the same text with a gap above the dedup threshold
    survive — uniqueness defense only fires on overlapping or near-adjacent
    windows.
    """
    phrases = [
        _make_phrase(["rain"], 0.0, 1.0),
        _make_phrase(["rain"], 3.0, 4.0),  # 2s gap, far above 0.5s threshold
    ]
    transcript_words = [
        _make_word("rain", 0.1, 0.5),
        _make_word("rain", 3.1, 3.5),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response(
            [
                {"index": 0, "lines": ["rain"]},
                {"index": 1, "lines": ["rain"]},
            ]
        ),
    )
    out = agent.run(
        TextAlignmentInput(
            phrases=phrases,
            transcript_words=transcript_words,
            atomize_mode=True,
        )
    )
    assert [p.sample_text for p in out.phrases] == ["rain", "rain"]
    assert out.dropped_count == 0


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


# ── Sanitizer post-pass (regression: prod job 87b7292b) ───────────────────────
#
# These tests lock in the deterministic cleanup applied to LLM output before
# the corrected lines are accepted. The sanitizer strips STRUCTURAL forbidden
# content (literal \n, debug markers) but does NOT collapse adjacent identical
# tokens — that would corrupt legitimate transcript refrains like "rain rain
# go away" or "Whoa whoa whoa".


def test_sanitizer_strips_literal_newline_and_truncation_markers(
    agent: TextAlignmentAgent, mock_client: MockModelClient
):
    phrase = _make_phrase(["the work to get there"], start_t_s=4.5, end_t_s=5.4)
    transcript_words = [
        _make_word("the", 4.5, 4.6),
        _make_word("work", 4.6, 4.9),
        _make_word("to", 4.9, 5.0),
        _make_word("get", 5.0, 5.2),
        _make_word("there", 5.2, 5.4),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["the\\nwork to get there[overlap_truncated]"]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    assert len(out.phrases) == 1
    text = out.phrases[0].sample_text
    assert "\\n" not in text
    assert "[overlap_truncated]" not in text
    assert text == "the work to get there"


def test_sanitizer_emptied_llm_output_falls_back_to_ocr(
    agent: TextAlignmentAgent, mock_client: MockModelClient
):
    """LLM returns only forbidden content → sanitiser empties it → OCR
    fallback path runs (same code path as a fully-omitted index). Under
    Fix B (2026-05-21) the phrase is kept with its OCR text rather than
    dropped — same rationale as the omission case.
    """
    phrase = _make_phrase(["watermark"], start_t_s=0.0, end_t_s=1.0)
    transcript_words = [_make_word("hello", 0.0, 1.0)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["[overlap_truncated]"]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    assert len(out.phrases) == 1
    assert out.phrases[0].sample_text == "watermark"
    assert out.dropped_count == 0


def test_sanitizer_drops_when_both_llm_and_ocr_are_empty(
    agent: TextAlignmentAgent, mock_client: MockModelClient
):
    """Only when BOTH the LLM output AND the OCR fallback sanitise to
    nothing does the phrase actually drop. This is the only true-drop path
    after Fix B.
    """
    phrase = _make_phrase(["[overlap_truncated]"], start_t_s=0.0, end_t_s=1.0)
    transcript_words = [_make_word("hello", 0.0, 1.0)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["[overlap_truncated]"]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    assert out.phrases == []
    assert out.dropped_count == 1


def test_sanitizer_strips_ass_subtitle_tags(
    agent: TextAlignmentAgent, mock_client: MockModelClient
):
    """LLM-emitted ASS subtitle tags (`{\\an5}`, `{\\fs120}`) would reach the
    renderer and be interpreted as positioning/style overrides instead of
    text. The sanitizer must strip them defensively.
    """
    phrase = _make_phrase(["centered hook"], start_t_s=0.0, end_t_s=1.0)
    transcript_words = [_make_word("centered", 0.0, 0.3), _make_word("hook", 0.4, 1.0)]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["{\\an5}centered{\\fs120} hook"]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    text = out.phrases[0].sample_text
    assert "{" not in text and "}" not in text
    assert "\\an5" not in text and "\\fs120" not in text
    assert text == "centered hook"


def test_sanitizer_strips_unicode_controls(agent: TextAlignmentAgent, mock_client: MockModelClient):
    """Zero-width joiners, RTL overrides, and line/paragraph separators are
    stripped so they cannot flip rendering direction or break layout. Tab and
    newline survive — newlines are handled by the existing whitespace pass.
    """
    phrase = _make_phrase(["hello world"], start_t_s=0.0, end_t_s=1.0)
    transcript_words = [_make_word("hello", 0.0, 0.3), _make_word("world", 0.4, 1.0)]
    # ‮ is RTL override, ​ is zero-width space,   is line separator.
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["hello‮​ world "]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    text = out.phrases[0].sample_text
    assert "‮" not in text
    assert "​" not in text
    assert " " not in text
    assert text == "hello world"


def test_sanitizer_preserves_legitimate_repeated_tokens(
    agent: TextAlignmentAgent, mock_client: MockModelClient
):
    """Refrains like 'rain rain go away' and 'Whoa whoa whoa' must survive the
    sanitizer. Stage D and the LLM prompt already handle OCR-side dedup; doing
    it again here would corrupt legitimate content for music-template overlays.
    """
    phrase = _make_phrase(["rain rain go away"], start_t_s=1.0, end_t_s=2.5)
    transcript_words = [
        _make_word("rain", 1.0, 1.3),
        _make_word("rain", 1.4, 1.7),
        _make_word("go", 1.8, 2.0),
        _make_word("away", 2.1, 2.5),
    ]
    mock_client.queue(
        "gemini-2.5-flash",
        _aligned_response([{"index": 0, "lines": ["rain rain go away"]}]),
    )
    out = agent.run(TextAlignmentInput(phrases=[phrase], transcript_words=transcript_words))

    assert out.phrases[0].sample_text == "rain rain go away"


# ── atomize_mode prompt branching (regression: v0.4.34.0 multi-word stuffing) ─
#
# The v0.4.34.0 prompt told the LLM to "concatenate eligible transcript words"
# which produced multi-word output like "if you" 2-4s when the atomized OCR
# phrase represented a single word "if" at 2.0s. atomize_mode=True must tell
# the LLM to output ONE transcript word per phrase, never concatenate.


def test_render_prompt_atomize_mode_emits_atomized_directive(
    agent: TextAlignmentAgent,
) -> None:
    """When atomize_mode=True, the rendered prompt must contain the
    single-word-per-phrase directive. Locks the wiring through
    TextAlignmentInput → render_prompt → load_prompt's mode_directive slot.
    """
    phrase = _make_phrase(["if"], start_t_s=2.0, end_t_s=4.0)
    transcript_words = [_make_word("if", 2.0, 2.5)]
    inp = TextAlignmentInput(
        phrases=[phrase],
        transcript_words=transcript_words,
        atomize_mode=True,
    )
    rendered = agent.render_prompt(inp)
    assert "ATOMIZED INPUT" in rendered
    assert "exactly ONE transcript word" in rendered
    assert "NEVER concatenate" in rendered
    # Phrase-mode directive must NOT appear when atomize_mode=True.
    assert "PHRASE-MODE INPUT" not in rendered


def test_render_prompt_phrase_mode_emits_phrase_directive(
    agent: TextAlignmentAgent,
) -> None:
    """When atomize_mode=False (default), the rendered prompt instructs the
    LLM to concatenate eligible transcript words for multi-word OCR blocks.
    """
    phrase = _make_phrase(["multi word block"], start_t_s=0.0, end_t_s=2.0)
    transcript_words = [
        _make_word("multi", 0.0, 0.5),
        _make_word("word", 0.5, 1.0),
        _make_word("block", 1.0, 2.0),
    ]
    inp = TextAlignmentInput(
        phrases=[phrase],
        transcript_words=transcript_words,
        atomize_mode=False,
    )
    rendered = agent.render_prompt(inp)
    assert "PHRASE-MODE INPUT" in rendered
    assert "concatenated in transcript order" in rendered
    assert "ATOMIZED INPUT" not in rendered


def test_atomize_mode_defaults_to_false():
    """Existing callers that omit atomize_mode get the legacy phrase-mode
    directive. Backward-compatible default.
    """
    inp = TextAlignmentInput(phrases=[], transcript_words=[])
    assert inp.atomize_mode is False
