"""End-to-end Stage D→E→G replay on the captured prod fixture for template
89cde014.

This is the integration test the PR #286 / #288 work should have had. Instead
of hand-built synthetic overlays, it drives the REAL stages on the VERBATIM
prod data:

  - Stage D OCR phrases (input_phrases) captured from the prod agent_run.
  - Stage E (`TextAlignmentAgent`) with the VERBATIM prod LLM output mocked —
    exercises the mis-mapped-duplicate revert defense.
  - Stage G grouping (`build_line_groups`) + cumulative emit
    (`_classified_phrases_to_output`) — exercises de-clustering, sub-group y
    stacking, the min-duration floor, and the quote sanitizer.

Asserts the FINAL overlay set satisfies all three product goals for this
template:
  1. Text fits — no cumulative line exceeds the renderer's 90%-canvas budget
     at the uniform 120 px render size (split into stacked sub-groups instead).
  2. No jumping — every cumulative reveal adds exactly one word (no multi-word
     pops), reveals are monotonic and >= the min reveal step apart.
  3. Proper timing — no zero/negative-duration overlays; every overlay clears
     the render floor.

Plus the correctness invariants: words appear in transcript order, no jumbled
sub-groups ("and combination"), no dangling OCR quote artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agents._schemas.text_alignment import TextAlignmentInput, TranscriptWord
from app.agents._schemas.text_classification import ClassifiedPhrase
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.agents.text_alignment import TextAlignmentAgent
from app.pipeline.text_overlay import measure_text_width
from app.pipeline.text_overlay_v2.constants import (
    LAYER2_RENDER_TEXT_SIZE,
    LAYER2_RENDER_TEXT_SIZE_PX,
)
from app.pipeline.text_overlay_v2.line_grouping import build_line_groups
from app.pipeline.text_overlay_v2.pipeline import (
    _CANVAS_W_PX,
    _CUMULATIVE_LINE_MAX_W_FRAC,
    _MIN_WORD_REVEAL_STEP_S,
    _classified_phrases_to_output,
)
from tests.agents.conftest import MockModelClient

_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "fixtures"
    / "text_overlay_v2"
    / "89cde014_stage_e.json"
)

# The spoken transcript for this template (drives Stage G grouping + per-word
# reveal timing). Timings are deliberately clustered the way Whisper/OCR
# produce them for this fast word-by-word build-up, so the test exercises the
# de-clustering pass.
_TRANSCRIPT = [
    ("it's", 0.0), ("not", 0.9), ("just", 1.5), ("luck", 1.7),
    ("if", 2.0), ("you", 3.0), ("put", 3.5), ("in", 4.0),
    ("the", 4.5), ("work", 5.0), ("to", 5.5), ("get", 5.6),
    ("just", 6.5), ("luck", 6.6), ("is", 6.7), ("a", 6.8),
    ("combination", 7.5), ("of", 7.6), ("and", 8.0), ("good", 8.5),
    ("timing", 8.6), ("don't", 9.0), ("to", 9.5), ("allow", 9.6),
    ("anyone", 9.7), ("diminish", 9.8), ("your", 10.0), ("hard", 10.1),
    ("work", 10.2),
]


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text())


def _aligned_phrases_via_stage_e() -> list[Phrase]:
    fixture = _load_fixture()
    phrases = [
        Phrase(
            lines=p["lines"],
            start_t_s=p["start_t_s"],
            end_t_s=p["end_t_s"],
            aabb=tuple(p["aabb"]),
            mean_confidence=p["mean_confidence"],
        )
        for p in fixture["input_phrases"]
    ]
    transcript_words = [
        TranscriptWord(text=t, start_s=s, end_s=s + 0.3) for t, s in _TRANSCRIPT
    ]
    client = MockModelClient()
    client.queue(
        "gemini-2.5-flash",
        json.dumps({"aligned_phrases": fixture["raw_llm_aligned_phrases"]}),
    )
    agent = TextAlignmentAgent(client)
    out = agent.run(
        TextAlignmentInput(
            phrases=phrases,
            transcript_words=transcript_words,
            template_id="89cde014",
            atomize_mode=True,
        )
    )
    return out.phrases


def _run_stage_g(aligned: list[Phrase]):
    transcript_words = [
        TranscriptWord(text=t, start_s=s, end_s=s + 0.3) for t, s in _TRANSCRIPT
    ]
    # The bridge forces every Layer-2 overlay to the uniform render size, so
    # the classifier's per-phrase size_class is irrelevant here — use the
    # default. Cumulative emit reads pop animation off effect, so set pop-in.
    classified = [
        ClassifiedPhrase(phrase=p, effect="pop-in", role="label", size_class="large")
        for p in aligned
    ]
    line_groups = build_line_groups(aligned, transcript_words)
    # One slot spanning the whole template (the real recipe has 1 slot here).
    return _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 12.0)], line_groups=line_groups
    )


def test_e2e_no_jumbled_words_after_stage_e_defense():
    """Stage E mis-mapped duplicates revert to OCR, so the second sentence
    reads in order — no "and combination" jumble."""
    aligned = _aligned_phrases_via_stage_e()
    texts = [p.sample_text for p in aligned]
    # The two prod mis-maps reverted to OCR.
    assert "timing" in texts
    assert "don't" in texts
    # Each appears exactly once (the real occurrence only).
    assert texts.count("combination") == 1
    assert texts.count("and") == 1


def test_e2e_no_dangling_quote_artifact():
    """The OCR `luck"` artifact is stripped before reaching overlays."""
    aligned = _aligned_phrases_via_stage_e()
    for p in aligned:
        assert '"' not in p.sample_text, f"dangling quote survived: {p.sample_text!r}"


def test_e2e_overlays_fit_no_jump_proper_timing():
    """The three product goals on the real prod data:
    text fits, words reveal one-by-one, timing is renderable."""
    aligned = _aligned_phrases_via_stage_e()
    out = _run_stage_g(aligned)
    overlays = out.overlays
    assert overlays, "expected overlays"

    max_w_px = int(_CANVAS_W_PX * _CUMULATIVE_LINE_MAX_W_FRAC)

    # GOAL 1 — text fits. No emitted cumulative line exceeds 90% canvas at the
    # uniform render size (over-long lines must have been split into sub-groups).
    for ov in overlays:
        w = measure_text_width(
            ov.sample_text,
            text_size=LAYER2_RENDER_TEXT_SIZE,
            text_size_px=LAYER2_RENDER_TEXT_SIZE_PX,
        )
        assert w <= max_w_px, (
            f"overlay {ov.sample_text!r} measures {w}px > {max_w_px}px budget"
        )

    # GOAL 3 — proper timing. No zero/negative-duration overlays; all clear floor.
    for ov in overlays:
        dur = ov.end_s - ov.start_s
        assert dur >= 0.2 - 1e-6, (
            f"overlay {ov.sample_text!r} duration {dur:.3f}s below floor "
            f"({ov.start_s}-{ov.end_s})"
        )

    # GOAL 2 — no jumping. Within each y-band (one cumulative reveal line),
    # consecutive stages add exactly one word and are >= the min reveal step
    # apart.
    by_y: dict[float, list] = {}
    for ov in overlays:
        by_y.setdefault(round(ov.bbox.y_norm, 3), []).append(ov)
    for y, band in by_y.items():
        band = sorted(band, key=lambda o: o.start_s)
        prev_wordcount = 0
        prev_start = None
        for ov in band:
            wc = len(ov.sample_text.split())
            if prev_wordcount and wc > prev_wordcount:
                assert wc - prev_wordcount == 1, (
                    f"y={y}: reveal jumped {prev_wordcount}->{wc} words at "
                    f"{ov.sample_text!r}"
                )
            prev_wordcount = wc
            if prev_start is not None:
                assert ov.start_s - prev_start >= _MIN_WORD_REVEAL_STEP_S - 1e-6, (
                    f"y={y}: reveals {prev_start} and {ov.start_s} too close"
                )
            prev_start = ov.start_s


def test_e2e_no_known_jumble_fragments():
    """Regression lock on the EXACT prod symptom. The v0.4.42.4 render of this
    template emitted these jumbled fragments (words from different transcript
    positions glued in the wrong order) because Stage E mis-mapped duplicates:
        "and combination"  (and@19 before combination@17 — reversed)
        "good don't"       (good@20 jumped to don't@22, skipping timing@21)
        "good don't allow"
        "work your hard"   (work@29 before your@27/hard@28 — reversed)
    After the Stage E dupe-revert + de-clustering, none of these can appear.
    """
    aligned = _aligned_phrases_via_stage_e()
    out = _run_stage_g(aligned)
    texts = [o.sample_text.lower() for o in out.overlays]
    forbidden = ["and combination", "good don't", "work your hard", "good don't allow"]
    for frag in forbidden:
        for t in texts:
            assert frag not in t, (
                f"jumbled fragment {frag!r} reappeared in overlay {t!r}"
            )

    # Stronger invariant: within every overlay, the words appear in the same
    # order they appear in the aligned phrase list (no scrambling). Build a
    # cursor over aligned words and confirm each overlay is an in-order run.
    aligned_words = [p.sample_text.lower() for p in aligned]
    for t in texts:
        words = t.split()
        # Find a monotonic-increasing index assignment in aligned_words.
        cursor = -1
        for w in words:
            try:
                nxt = aligned_words.index(w, cursor + 1)
            except ValueError:
                # Word repeats earlier in the list; allow any later occurrence.
                nxt = next(
                    (i for i in range(cursor + 1, len(aligned_words)) if aligned_words[i] == w),
                    None,
                )
            assert nxt is not None and nxt > cursor, (
                f"overlay {t!r} has words out of aligned order at {w!r}"
            )
            cursor = nxt
