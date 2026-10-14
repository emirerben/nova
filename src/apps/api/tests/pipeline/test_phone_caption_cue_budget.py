"""KRI-632 guard: phone Talking caption cues fit the screen.

Real word timings from the Tigre T1 run (2026-10-12, thread 0DD3FF4E, Turkish): one
84-character cue wrapped to four rows over the speaker's chest, and its last two
words became their own cue. Failure modes this locks:

- a long sentence stays ONE cue (word cap, not size cap);
- a split leaves a 1-2 word orphan ("abi biz.");
- a cue stays on screen far longer than it takes to read;
- the compiled block (after the watermark re-wrap) grows past two rows or reaches the
  speaker's face band;
- a split drops, reorders or duplicates a word, or merges two sentences;
- the cloud burn (no budget) changes at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues
from app.pipeline.phone_captions import caption_ink_box, compile_caption_layers
from app.pipeline.transcribe import Word

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kri632_t1_talking_words.json"
CANVAS = (1080, 1920)
# `closing_title_placement.face_band` of the T1 job (top, bottom as canvas fractions).
FACE_BAND_BOTTOM_FRAC = 0.736
_MAX_ROWS = 2


def _words() -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in json.loads(FIXTURE.read_text())
    ]


def _cues() -> list[dict]:
    return build_plain_cues(_words(), attach_words=True, budget=PHONE_CUE_BUDGET)


def test_the_t1_long_sentence_is_split_into_short_balanced_cues() -> None:
    cues = _cues()
    texts = [c["text"] for c in cues]
    assert "Ben her videoda, işte abone olun," in texts  # clause break, not word 14
    assert not any("abi biz." == t for t in texts), "orphan tail"
    for cue in cues:
        words = cue["text"].split()
        assert len(cue["text"]) <= PHONE_CUE_BUDGET.max_chars, cue["text"]
        assert len(words) <= PHONE_CUE_BUDGET.max_words, cue["text"]
        if len(words) < 3:
            # only a whole short sentence may be this small
            assert cue["text"].rstrip()[-1] in ".?!…", f"orphan cue: {cue['text']!r}"
        assert cue["end_s"] - cue["start_s"] <= PHONE_CUE_BUDGET.max_s + 1e-6, cue["text"]


def test_a_split_keeps_every_word_in_order_and_never_merges_sentences() -> None:
    words = _words()
    cues = _cues()
    flat = [w["text"] for c in cues for w in c["words"]]
    assert flat == [w.text for w in words]
    for cue in cues[:-1]:
        # a sentence-ending word is always the last word of its cue
        assert not any(w["text"].endswith((".", "?", "!")) for w in cue["words"][:-1])


def test_compiled_t1_captions_are_at_most_two_rows_and_clear_of_the_face() -> None:
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.phone_captions import _DEFAULT_CAPTION_LOOK, _base_overlay

    cues = _cues()
    layers = compile_caption_layers(
        cues, style="word", canvas_width=CANVAS[0], canvas_height=CANVAS[1]
    )
    assert len(layers) == len(cues)
    typeface = cloud._resolve_typeface_for_overlay(
        _base_overlay(cues[0], _DEFAULT_CAPTION_LOOK)
    ).typeface
    face_bottom = FACE_BAND_BOTTOM_FRAC * CANVAS[1]
    for cue, layer in zip(cues, layers, strict=True):
        rows = len({run.baseline_y for run in layer.runs})
        assert rows <= _MAX_ROWS, f"{rows} rows: {cue['text']!r}"
        top = min(ink[1] for run in layer.runs if (ink := caption_ink_box(run, typeface)))
        assert top >= face_bottom, f"cue ink at y={top:.0f} reaches the face: {cue['text']!r}"


@pytest.mark.parametrize("sentence_words", [30])
def test_unpunctuated_speech_degrades_to_budgeted_chunks(sentence_words: int) -> None:
    words = [
        Word(text="kelime", start_s=i * 0.3, end_s=i * 0.3 + 0.25, confidence=1.0)
        for i in range(sentence_words)
    ]
    cues = build_plain_cues(words, budget=PHONE_CUE_BUDGET)
    assert sum(len(c["text"].split()) for c in cues) == sentence_words
    assert all(len(c["text"]) <= PHONE_CUE_BUDGET.max_chars for c in cues)
    assert all(len(c["text"].split()) >= 3 for c in cues)


def test_without_a_budget_the_cloud_split_is_unchanged() -> None:
    cues = build_plain_cues(_words(), attach_words=True)
    long_cue = next(c for c in cues if c["text"].startswith("Ben her videoda"))
    assert len(long_cue["text"].split()) == 14  # the legacy word cap
