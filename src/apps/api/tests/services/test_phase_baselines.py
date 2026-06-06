"""Tests for app.services.phase_baselines — all three pipelines + unknown."""

from app.services.phase_baselines import get_baselines, scale_render_variants


def test_get_baselines_generative():
    b = get_baselines("generative")
    assert b is not None
    assert "analyze_clips" in b
    assert all(isinstance(v, int) and v > 0 for v in b.values())


def test_get_baselines_template():
    b = get_baselines("template")
    assert b is not None
    assert "download_clips" in b
    assert "finalize" in b
    # queued intentionally excluded — ETA suppressed while queued (D9)
    assert "queued" not in b
    assert all(isinstance(v, int) and v > 0 for v in b.values())


def test_get_baselines_template_returns_expected_phases():
    """All 8 template phases present."""
    b = get_baselines("template")
    assert b is not None
    expected = {
        "download_clips",
        "analyze_clips",
        "match_clips",
        "assemble",
        "mix_audio",
        "generate_copy",
        "upload",
        "finalize",
    }
    assert expected == set(b.keys())


def test_get_baselines_activation():
    b = get_baselines("content_plan_activation")
    assert b is not None
    assert "matching_clips" in b
    assert all(isinstance(v, int) and v > 0 for v in b.values())


def test_get_baselines_unknown_returns_none():
    assert get_baselines("nonexistent_pipeline") is None


def test_get_baselines_returns_copy_not_reference():
    """Mutations to the returned dict must not affect subsequent calls."""
    b1 = get_baselines("template")
    assert b1 is not None
    b1["download_clips"] = 999_999
    b2 = get_baselines("template")
    assert b2 is not None
    assert b2["download_clips"] != 999_999


def test_scale_render_variants_scales_by_count():
    baselines = {"render_variants": 90_000, "other": 5_000}
    scaled = scale_render_variants(baselines, pending_count=1)
    # 90_000 * 1 / 3 = 30_000
    assert scaled["render_variants"] == 30_000
    assert scaled["other"] == 5_000


def test_scale_render_variants_zero_count_no_change():
    baselines = {"render_variants": 90_000}
    scaled = scale_render_variants(baselines, pending_count=0)
    assert scaled["render_variants"] == 90_000
