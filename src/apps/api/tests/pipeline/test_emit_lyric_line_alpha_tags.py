from app.pipeline.text_overlay import _emit_lyric_line_alpha_tags


def test_normal_case_emits_both_transforms() -> None:
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 150, 250)
    assert r"\t(0,150,0.5," in tags
    assert r"\t(2750,3000,2.0," in tags


def test_zero_fade_in_starts_fully_opaque() -> None:
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 0, 250)
    assert tags.startswith(r"{\alpha&H00&")
    assert r"\t(0," not in tags


def test_zero_fade_out_emits_no_fade_out_transform() -> None:
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 150, 0)
    assert tags.count(r"\t(") == 1


def test_fade_clamps_when_duration_shorter_than_fades() -> None:
    tags = _emit_lyric_line_alpha_tags(0.0, 0.2, 150, 250)
    assert r"\t(0,150,0.5," in tags
    assert r"\t(150,200,2.0," in tags


def test_both_fades_zero_emits_constant_alpha() -> None:
    assert _emit_lyric_line_alpha_tags(0.0, 3.0, 0, 0) == r"{\alpha&H00&}"


def test_duration_zero_does_not_crash() -> None:
    assert _emit_lyric_line_alpha_tags(1.0, 1.0, 150, 250) == r"{\alpha&H00&}"


def test_default_fade_out_curve_emits_accel_2_0() -> None:
    """Solo-line / sparse-pair / hard-cut / kill-switch-off paths leave
    `fade_out_curve` as None → libass tag uses accel=2.0 (`1−p²` lingering)."""
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 150, 250, fade_out_curve=None)
    assert r"\t(2750,3000,2.0," in tags
    assert r"\t(2750,3000,0.5," not in tags


def test_sqrt_fade_out_curve_emits_accel_0_5() -> None:
    """Dynamic-crossfade outgoing line tagged with fade_out_curve="sqrt" →
    libass tag uses accel=0.5 (`1−√p` mirror of the sqrt fade-in). Paired
    with the incoming line's fade-in over a matched window, this gives
    α_out + α_in = 1 at every t — no readable stacked text."""
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 150, 300, fade_out_curve="sqrt")
    assert r"\t(2700,3000,0.5," in tags
    assert r"\t(2700,3000,2.0," not in tags


def test_unknown_curve_value_falls_back_to_default_accel() -> None:
    """Defensive: anything other than "sqrt" must use the default 2.0
    accel, so an unfamiliar future curve name doesn't silently invent
    rendering behavior."""
    tags = _emit_lyric_line_alpha_tags(0.0, 3.0, 150, 250, fade_out_curve="cubic")
    assert r"\t(2750,3000,2.0," in tags
