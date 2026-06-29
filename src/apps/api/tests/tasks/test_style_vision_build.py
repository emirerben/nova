"""Unit tests for style_vision_build — deterministic aggregator only.

No Celery, no Gemini, no yt-dlp. The task body is integration-tested via
the local E2E playbook (TIKTOK_STYLE_VISION_ENABLED=true + worker + real handle).
"""

from __future__ import annotations

import pytest

from app.tasks.style_vision_build import _aggregate_observations, _mode_or_none

# ── _mode_or_none ─────────────────────────────────────────────────────────────


class TestModeOrNone:
    def test_empty_returns_none(self):
        assert _mode_or_none([]) is None

    def test_single_value(self):
        assert _mode_or_none(["serif_editorial"]) == "serif_editorial"

    def test_majority_wins(self):
        assert _mode_or_none(["a", "b", "a", "c", "a"]) == "a"

    def test_tie_returns_first_most_common(self):
        # Counter.most_common() is stable across ties in insertion order on 3.7+
        result = _mode_or_none(["a", "b"])
        assert result in ("a", "b")


# ── _aggregate_observations ───────────────────────────────────────────────────


def _obs(
    *,
    has_text: bool = True,
    font_feel: str = "bold_display",
    position: str = "center",
    size_class: str = "large",
    layout: str = "linear",
    stroke: str = "none",
    text_anchor: str = "center",
    text_color: str | None = "#ffffff",
    highlight: str | None = "#f6d895",
    confidence: float = 0.9,
) -> dict:
    return {
        "has_on_screen_text": has_text,
        "font_feel": font_feel,
        "position": position,
        "size_class": size_class,
        "layout": layout,
        "stroke": stroke,
        "text_anchor": text_anchor,
        "text_color_hex": text_color,
        "highlight_color_hex": highlight,
        "confidence": confidence,
    }


class TestAggregateObservations:
    def test_empty_returns_empty(self):
        assert _aggregate_observations([]) == {}

    def test_single_observation_all_fields_pass(self):
        agg = _aggregate_observations([_obs()])
        assert agg["has_on_screen_text"] is True
        assert agg["font_feel"] == "bold_display"
        assert agg["position"] == "center"
        assert agg["size_class"] == "large"
        assert agg["layout"] == "linear"
        assert agg["text_color_hex"] == "#ffffff"
        assert agg["highlight_color_hex"] == "#f6d895"
        assert agg["videos_seen"] == 1
        assert agg["mean_confidence"] == pytest.approx(0.9)

    def test_unanimous_agreement_passes(self):
        obs = [_obs(font_feel="serif_editorial") for _ in range(5)]
        agg = _aggregate_observations(obs)
        assert agg["font_feel"] == "serif_editorial"
        assert agg["confidence_per_field"]["font_feel"] == pytest.approx(1.0)

    def test_low_agreement_yields_none(self):
        """5 different font_feels → mode appears once (20%) → below 0.5 threshold."""
        obs = [
            _obs(font_feel="serif_editorial"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="clean_sans"),
            _obs(font_feel="handwritten"),
            _obs(font_feel="mono"),
        ]
        agg = _aggregate_observations(obs)
        assert agg["font_feel"] is None

    def test_majority_passes(self):
        """3/5 agree on 'center' → 60% > 50% threshold."""
        obs = [
            _obs(position="center"),
            _obs(position="center"),
            _obs(position="center"),
            _obs(position="top"),
            _obs(position="bottom"),
        ]
        agg = _aggregate_observations(obs)
        assert agg["position"] == "center"
        assert agg["confidence_per_field"]["position"] == pytest.approx(0.6)

    def test_exactly_half_passes(self):
        """agreement >= 0.5 (not >), so exactly half qualifies."""
        obs = [
            _obs(font_feel="bold_display"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="clean_sans"),
            _obs(font_feel="clean_sans"),
        ]
        agg = _aggregate_observations(obs)
        # Either wins on Counter.most_common stability, but one must pass
        assert agg["font_feel"] is not None

    def test_no_text_videos_categorical_fields_none(self):
        """When all observations have has_text=False, categorical fields are None."""
        obs = [_obs(has_text=False, font_feel="bold_display") for _ in range(3)]
        agg = _aggregate_observations(obs)
        assert agg["has_on_screen_text"] is False
        assert agg["font_feel"] is None

    def test_font_feel_none_excluded_from_mode_result(self):
        """'none' is excluded from the candidate pool so it can't win the mode.
        4/5 agree on 'bold_display'; 'none' is dropped from values → 80% agreement passes.
        """
        obs = [
            _obs(font_feel="bold_display"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="none"),
        ]
        agg = _aggregate_observations(obs)
        # 4/5 = 0.8 ≥ 0.5 threshold; "none" is excluded from values so can't become mode
        assert agg["font_feel"] == "bold_display"
        assert agg["confidence_per_field"]["font_feel"] == pytest.approx(0.8)

    def test_font_feel_sparse_detection_below_threshold(self):
        """2 bold_display + 3 none → 2/5 = 40% < threshold → None (safe, not over-fitted)."""
        obs = [
            _obs(font_feel="bold_display"),
            _obs(font_feel="bold_display"),
            _obs(font_feel="none"),
            _obs(font_feel="none"),
            _obs(font_feel="none"),
        ]
        agg = _aggregate_observations(obs)
        assert agg["font_feel"] is None

    def test_has_on_screen_text_majority_vote(self):
        obs = [
            _obs(has_text=True),
            _obs(has_text=True),
            _obs(has_text=False),
        ]
        agg = _aggregate_observations(obs)
        assert agg["has_on_screen_text"] is True

    def test_has_on_screen_text_tie_rounds_up(self):
        """At exactly 50% (2/4), has_text_count >= n/2 → True."""
        obs = [
            _obs(has_text=True),
            _obs(has_text=True),
            _obs(has_text=False),
            _obs(has_text=False),
        ]
        agg = _aggregate_observations(obs)
        assert agg["has_on_screen_text"] is True

    def test_color_low_agreement_yields_none(self):
        """4 different text colors → none clears 50%."""
        obs = [
            _obs(text_color="#ffffff"),
            _obs(text_color="#000000"),
            _obs(text_color="#ff0000"),
            _obs(text_color="#00ff00"),
        ]
        agg = _aggregate_observations(obs)
        assert agg["text_color_hex"] is None

    def test_mean_confidence_computed(self):
        obs = [_obs(confidence=0.8), _obs(confidence=0.6), _obs(confidence=1.0)]
        agg = _aggregate_observations(obs)
        assert agg["mean_confidence"] == pytest.approx(0.8, rel=1e-3)

    def test_videos_seen_count(self):
        obs = [_obs() for _ in range(7)]
        agg = _aggregate_observations(obs)
        assert agg["videos_seen"] == 7

    def test_qbuilder_profile(self):
        """Mirrors the @qbuilder live fixture: bold_display, #ffffff, #f6d895, center."""
        obs = [_obs(
            font_feel="bold_display",
            text_color="#ffffff",
            highlight="#f6d895",
            position="center",
            confidence=0.9,
        )] * 22 + [_obs(has_text=False)] * 5
        agg = _aggregate_observations(obs)
        assert agg["font_feel"] == "bold_display"
        assert agg["text_color_hex"] == "#ffffff"
        assert agg["highlight_color_hex"] == "#f6d895"
        assert agg["position"] == "center"
        assert agg["has_on_screen_text"] is True
