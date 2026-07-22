"""prepare_smart_caption_cues + digit-word layout rule (plan 011, Feature B).

measure_caption's own scoring is pinned in tests/pipeline/test_render_geometry.py.
These tests pin the RENDER SEAM: that persisted emphasis keep-together pairs and
the deterministic digit+word rule actually reach measure_caption, gated by
SMART_CAPTION_LAYOUT_BALANCE_ENABLED, and that the flag-off path is a no-op.
"""

from __future__ import annotations

import app.pipeline.render_geometry as render_geometry
from app.config import settings
from app.pipeline.captions import (
    _cue_keep_together_pairs,
    digit_word_keep_together,
    prepare_smart_caption_cues,
)

_POLICY = {
    "font_family": "Montserrat Bold",
    "font_size_px": 64,
    "y_frac": 0.705,
    "width_frac": 0.88,
    "max_lines": 2,
    "color": "&H00FFFFFF",
    "stroke_color": "&H00000000",
    "stroke_width": 6,
}


def _spy_measure(monkeypatch):
    """Wrap the real measure_caption, recording the keep_together/penalize args."""

    real = render_geometry.measure_caption
    calls: list[dict] = []

    def spy(text, **kwargs):
        calls.append(
            {
                "text": text,
                "keep_together": kwargs.get("keep_together"),
                "penalize_widows": kwargs.get("penalize_widows"),
            }
        )
        return real(text, **kwargs)

    monkeypatch.setattr(render_geometry, "measure_caption", spy)
    return calls


# ── digit_word_keep_together (deterministic rule) ─────────────────────────────


def test_digit_word_pairs_number_with_the_word_it_labels() -> None:
    assert digit_word_keep_together(["1", "Messi", "and", "3", "million"]) == [(0, 1), (3, 4)]
    assert digit_word_keep_together(["1.", "Messi"]) == [(0, 1)]  # ordinal punctuation ok


def test_digit_word_no_pair_between_two_numbers_or_on_long_digits() -> None:
    assert digit_word_keep_together(["1", "2"]) == []  # next token is a number
    assert digit_word_keep_together(["1000", "goals"]) == []  # 4 digits not a marker
    assert digit_word_keep_together(["Messi"]) == []  # single token, no pair


# ── _cue_keep_together_pairs (persisted read, malformed-safe) ──────────────────


def test_cue_keep_together_pairs_drops_malformed() -> None:
    cue = {"smart_keep_together": [["x", 1], [0], [0, 1], "junk"]}
    assert _cue_keep_together_pairs(cue) == [(0, 1)]
    assert _cue_keep_together_pairs({}) == []


# ── render seam: pairs reach measure_caption, gated by the flag ───────────────


def test_flag_off_no_pairs_passes_nothing_to_measure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_caption_layout_balance_enabled", False, raising=False)
    calls = _spy_measure(monkeypatch)
    prepare_smart_caption_cues([{"text": "1 Messi is great"}], _POLICY)
    assert calls[0]["keep_together"] is None
    assert calls[0]["penalize_widows"] is False


def test_persisted_emphasis_pair_reaches_measure_even_with_layout_off(monkeypatch) -> None:
    # A job planned with emphasis on persists smart_keep_together; the pair must
    # still be honored on reburn regardless of the layout flag.
    monkeypatch.setattr(settings, "smart_caption_layout_balance_enabled", False, raising=False)
    calls = _spy_measure(monkeypatch)
    prepare_smart_caption_cues(
        [{"text": "number one is Messi", "smart_keep_together": [[0, 1]]}], _POLICY
    )
    assert (0, 1) in calls[0]["keep_together"]
    assert calls[0]["penalize_widows"] is False  # widow penalty stays layout-gated


def test_layout_on_adds_digit_pairs_and_widow_penalty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_caption_layout_balance_enabled", True, raising=False)
    calls = _spy_measure(monkeypatch)
    prepare_smart_caption_cues([{"text": "1 Messi is the goat"}], _POLICY)
    assert (0, 1) in calls[0]["keep_together"]  # digit rule fired
    assert calls[0]["penalize_widows"] is True


def test_persisted_pair_influences_the_actual_line_split(monkeypatch) -> None:
    # End-to-end: a cue whose natural split would strand the number gets its
    # persisted pair honored — the number stays glued to its word.
    monkeypatch.setattr(settings, "smart_caption_layout_balance_enabled", False, raising=False)
    cue = {"text": "3 Messi dribbles past every defender now", "smart_keep_together": [[0, 1]]}
    prepared = prepare_smart_caption_cues([cue], _POLICY)[0]
    lines = prepared["smart_render_lines"]
    if len(lines) == 2:
        # "3" must never be the whole first line (stranded) — it stays with "Messi".
        assert lines[0].split() != ["3"]
        assert "3 Messi" in lines[0] or "3 Messi" in lines[1]


def test_flag_off_no_persisted_is_byte_identical_to_bare_measure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smart_caption_layout_balance_enabled", False, raising=False)
    text = "number one is Messi the greatest of all time"
    prepared = prepare_smart_caption_cues([{"text": text}], _POLICY)[0]
    bare = render_geometry.measure_caption(
        text,
        font_family="Montserrat Bold",
        font_size_px=64,
        width_frac=0.88,
        y_frac=0.705,
        max_lines=2,
    )
    assert prepared["smart_render_lines"] == list(bare.lines)
    assert prepared["smart_render_font_size_px"] == bare.font_size_px
    assert prepared["smart_render_box"] == bare.box.as_dict()
