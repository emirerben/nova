"""Tests for the build_sound_effects_command FFmpeg filter graph."""

from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.pipeline.sound_effects import build_sound_effects_command


def _make_placement(
    at_s: float, gain: float = 1.0, trim_start=None, trim_end=None
) -> SoundEffectPlacement:
    return SoundEffectPlacement(
        id="testid",
        src_gcs_path="sound-effects/x/audio.mp3",
        at_s=at_s,
        gain=gain,
        trim_start_s=trim_start,
        trim_end_s=trim_end,
    )


def test_command_contains_normalize_zero():
    """normalize=0 is mandatory — default normalize=1 drops ~6 dB."""
    eff = _make_placement(3.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "normalize=0" in fc


def test_command_contains_aresample_48000():
    """aresample=48000 mandatory — avoids AAC 96kHz pitch bug."""
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "aresample=48000" in fc


def test_command_copies_video_stream():
    """-c:v copy — no video re-encode."""
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    assert "-c:v" in cmd
    cv_idx = cmd.index("-c:v")
    assert cmd[cv_idx + 1] == "copy"


def test_command_duration_first():
    """duration=first — pins output audio length to the base video."""
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "duration=first" in fc


def test_adelay_ms_equals_at_s_times_1000():
    """adelay delay in ms must equal round(at_s * 1000)."""
    at_s = 4.567
    expected_ms = round(at_s * 1000)  # 4567
    eff = _make_placement(at_s)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert f"adelay={expected_ms}|{expected_ms}" in fc


def test_gain_written_to_volume_filter():
    """Gain is applied via the volume= filter."""
    eff = _make_placement(1.0, gain=0.5)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "volume=0.5" in fc


def test_multiple_effects_amix_inputs_count():
    """K effects → amix=inputs=K+1 (1 base + K effects)."""
    effects = [_make_placement(1.0), _make_placement(3.0), _make_placement(5.0)]
    paths = ["/sfx1.mp3", "/sfx2.mp3", "/sfx3.mp3"]
    cmd = build_sound_effects_command("/base.mp4", effects, paths, "/out.mp4")
    fc = " ".join(cmd)
    assert "amix=inputs=4" in fc  # 3 effects + 1 base


def test_single_effect_amix_inputs_count():
    """1 effect → amix=inputs=2."""
    eff = _make_placement(2.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "amix=inputs=2" in fc


def test_trim_branch_present_when_trim_set():
    """atrim is included when trim_start_s or trim_end_s is set."""
    eff = _make_placement(1.0, trim_start=0.5, trim_end=2.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "atrim" in fc


def test_trim_branch_absent_when_no_trim():
    """atrim is NOT included when no trim bounds are set."""
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    fc = " ".join(cmd)
    assert "atrim" not in fc


def test_output_path_at_end():
    """output_path is the last argument."""
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    assert cmd[-1] == "/out.mp4"


def test_base_video_is_first_input():
    eff = _make_placement(1.0)
    cmd = build_sound_effects_command("/base.mp4", [eff], ["/sfx.mp3"], "/out.mp4")
    assert cmd[1] == "-i"
    assert cmd[2] == "/base.mp4"
