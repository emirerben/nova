"""Tests for scripts/overlay_forensics/diff.py."""
from __future__ import annotations

from scripts.overlay_forensics.diff import (
    DiffFinding,
    diff_beat_alignment,
    diff_cooccurrence_rule,
    diff_paired_event,
    diff_safe_crop,
    levenshtein,
    pair_events,
    plain_english_summary,
    sort_findings,
)
from scripts.overlay_forensics.events import FrameObservation, TextEvent
from scripts.overlay_forensics.masking import BBox


def _ev(text: str, color: str, bbox: BBox,
        t_start: float, t_end: float,
        pixels: int = 1000, entrance: tuple[str, float] = ("static", 0.0)) -> TextEvent:
    n_obs = max(2, int((t_end - t_start) / 0.05) + 1)
    obs = [
        FrameObservation(
            t_s=t_start + i * (t_end - t_start) / (n_obs - 1),
            color_key=color, bbox=bbox,
            mask_pixel_count=pixels, mean_brightness=0.8,
        )
        for i in range(n_obs)
    ]
    ev = TextEvent(color_key=color, observations=obs)
    ev.text = text
    ev._entrance = entrance  # type: ignore[attr-defined]
    return ev


def test_levenshtein_zero_for_identical():
    assert levenshtein("This", "This") == 0


def test_levenshtein_case_insensitive():
    assert levenshtein("This", "this") == 0


def test_levenshtein_one_substitution():
    assert levenshtein("This", "Thus") == 1


def test_levenshtein_completely_different():
    assert levenshtein("This", "Morocco") >= 4


def test_pair_events_matches_same_text_first():
    recipe = [
        _ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3),
        _ev("is", "white", BBox(700, 200, 800, 250), 1.3, 2.5),
    ]
    output = [
        _ev("is", "white", BBox(700, 200, 800, 250), 1.4, 2.6),
        _ev("This", "white", BBox(100, 200, 200, 250), 0.1, 1.2),
    ]
    pairs = pair_events(recipe, output)
    # Both recipe events must be paired; key matches recipe text.
    paired_keys = [p.pair_key for p in pairs if p.recipe and p.output]
    assert "This" in paired_keys
    assert "is" in paired_keys


def test_pair_events_unpaired_recipe_becomes_missing():
    recipe = [_ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3)]
    output: list[TextEvent] = []
    pairs = pair_events(recipe, output)
    assert len(pairs) == 1
    assert pairs[0].output is None


def test_pair_events_unpaired_output_becomes_extra():
    recipe: list[TextEvent] = []
    output = [_ev("Morocco", "maize", BBox(400, 800, 600, 1000), 0.0, 2.4)]
    pairs = pair_events(recipe, output)
    assert len(pairs) == 1
    assert pairs[0].recipe is None
    assert pairs[0].output is not None


def test_diff_paired_event_flags_text_mismatch_as_critical():
    r = _ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3)
    o = _ev("Morocco", "maize", BBox(400, 800, 600, 1000), 0.0, 2.4)
    findings = diff_paired_event(
        type("P", (), {"recipe": r, "output": o, "pair_key": "This"})(),
        recipe_frame_w=1024, recipe_frame_h=576,
        output_frame_w=1080, output_frame_h=1920,
    )
    severities = [f.severity for f in findings]
    assert "critical" in severities
    text_findings = [f for f in findings if "text" in f.property]
    assert text_findings
    assert text_findings[0].recipe == "This"
    assert text_findings[0].output == "Morocco"


def test_diff_paired_event_flags_missing_event_as_critical():
    r = _ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3)
    findings = diff_paired_event(
        type("P", (), {"recipe": r, "output": None, "pair_key": "This"})(),
        recipe_frame_w=1024, recipe_frame_h=576,
        output_frame_w=1080, output_frame_h=1920,
    )
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert "missing" in findings[0].reason.lower()


def test_diff_paired_event_flags_extra_event_as_critical():
    o = _ev("Morocco", "maize", BBox(400, 800, 600, 1000), 0.0, 2.4)
    findings = diff_paired_event(
        type("P", (), {"recipe": None, "output": o, "pair_key": "Morocco"})(),
        recipe_frame_w=1024, recipe_frame_h=576,
        output_frame_w=1080, output_frame_h=1920,
    )
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert "unexpected" in findings[0].reason.lower()


def test_diff_paired_event_position_delta_classified_correctly():
    # Recipe cx/cy at relative (0.25, 0.30); output at (0.50, 0.30).
    # Δ = 0.25 — over POSITION_MAJOR_FRAC (0.15) → major.
    # Recipe cx ~ 256 / 1024 = 0.25 ; output cx ~ 540 / 1080 = 0.50.
    r = _ev("This", "white", BBox(256 - 50, 576, 256 + 50, 676), 0.0, 1.3)
    o = _ev("This", "white", BBox(540 - 50, 576, 540 + 50, 676), 0.0, 1.3)
    findings = diff_paired_event(
        type("P", (), {"recipe": r, "output": o, "pair_key": "This"})(),
        recipe_frame_w=1024, recipe_frame_h=1920,
        output_frame_w=1080, output_frame_h=1920,
    )
    pos = [f for f in findings if "position" in f.property]
    assert pos, "expected a position finding"
    assert pos[0].severity == "major"


def test_diff_paired_event_animation_family_mismatch_is_major():
    r = _ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3, entrance=("slide-up", 0.2))
    o = _ev("This", "white", BBox(100, 200, 200, 250), 0.0, 1.3, entrance=("static", 0.0))
    findings = diff_paired_event(
        type("P", (), {"recipe": r, "output": o, "pair_key": "This"})(),
        recipe_frame_w=1024, recipe_frame_h=576,
        output_frame_w=1024, output_frame_h=576,
    )
    anim = [f for f in findings if "animation" in f.property]
    assert anim
    assert anim[0].severity == "major"


def test_diff_cooccurrence_rule_flags_recipe_yes_output_no():
    finding = diff_cooccurrence_rule(
        "africa_starts_while_this_and_is_visible",
        recipe_satisfied=True, output_satisfied=False,
        description="x",
    )
    assert finding is not None
    assert finding.severity == "critical"


def test_diff_cooccurrence_rule_no_finding_when_both_satisfied():
    finding = diff_cooccurrence_rule(
        "rule", recipe_satisfied=True, output_satisfied=True, description="x",
    )
    assert finding is None


def test_diff_cooccurrence_rule_no_finding_when_recipe_unknown():
    finding = diff_cooccurrence_rule(
        "rule", recipe_satisfied=None, output_satisfied=False, description="x",
    )
    assert finding is None


def test_diff_beat_alignment_critical_when_recipe_aligned_output_off():
    finding = diff_beat_alignment("is", recipe_offset_s=0.02, output_offset_s=0.45)
    assert finding is not None
    assert finding.severity == "critical"


def test_diff_beat_alignment_none_when_both_aligned():
    finding = diff_beat_alignment("is", recipe_offset_s=0.02, output_offset_s=0.08)
    assert finding is None


def test_diff_safe_crop_major_when_recipe_doesnt_survive():
    finding = diff_safe_crop("is", "centroid outside crop band", survives=False)
    assert finding is not None
    assert finding.severity == "major"


def test_diff_safe_crop_none_when_survives():
    assert diff_safe_crop("This", "ok", survives=True) is None


def test_sort_findings_critical_first():
    findings = [
        DiffFinding("minor", "a", 1, 2, "x"),
        DiffFinding("critical", "b", 1, 2, "x"),
        DiffFinding("major", "c", 1, 2, "x"),
    ]
    sorted_f = sort_findings(findings)
    assert [f.severity for f in sorted_f] == ["critical", "major", "minor"]


def test_plain_english_summary_caps_at_top_n():
    findings = [DiffFinding("critical", f"p{i}", 1, 2, f"reason {i}") for i in range(10)]
    summary = plain_english_summary(findings, top_n=3)
    assert len(summary) == 3
    assert all("CRITICAL" in line for line in summary)
