"""Tests for the build_sound_effects_command FFmpeg filter graph."""

from app.agents._schemas.sound_effect import (
    SoundEffectPlacement,
    normalize_generated_sound_effects,
)
from app.pipeline.sound_effects import MusicBedTreatment, build_sound_effects_command


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


def test_fx_label_attaches_without_comma():
    """REGRESSION: the per-effect output pad label must attach DIRECTLY to the
    last filter ("volume=1.0[fx0]"), never comma-separated ("volume=1.0,[fx0]").
    A comma makes ffmpeg parse "[fx0]" as an empty filter → "Filter not found"
    (rc=8) and the whole SFX mix fails. The old code comma-joined the label as a
    chain element. This path was never exercised in prod because the SFX render
    was Download-gated and never triggered, so the bug stayed latent."""
    fc = " ".join(
        build_sound_effects_command("/base.mp4", [_make_placement(5.0)], ["/sfx.mp3"], "/out.mp4")
    )
    assert ",[fx0]" not in fc, f"malformed comma before output label: {fc}"
    assert "[fx0]" in fc
    # Two effects: both labels must attach cleanly, and amix must consume them.
    fc2 = " ".join(
        build_sound_effects_command(
            "/base.mp4",
            [_make_placement(1.0), _make_placement(2.0)],
            ["/a.mp3", "/b.mp3"],
            "/out.mp4",
        )
    )
    assert ",[fx0]" not in fc2 and ",[fx1]" not in fc2, fc2
    assert "[0:a][fx0][fx1]amix=inputs=3" in fc2


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


def test_repeated_asset_uses_one_decoder_input_and_asplit():
    effects = [_make_placement(1.0), _make_placement(2.0), _make_placement(3.0)]
    cmd = build_sound_effects_command(
        "/base.mp4",
        effects,
        ["/same-pop.wav", "/same-pop.wav", "/same-pop.wav"],
        "/out.mp4",
    )
    assert cmd.count("/same-pop.wav") == 1
    fc = " ".join(cmd)
    assert "[1:a]asplit=3" in fc
    assert "[sfxsrc0_0]" in fc
    assert "[sfxsrc0_2]" in fc


def test_music_graph_preserves_sfx_mute_intervals():
    cmd = build_sound_effects_command(
        "/base.mp4",
        [_make_placement(1.0)],
        ["/sfx.mp3"],
        "/out.mp4",
        mute_intervals=[(2.0, 4.0)],
        music_bed=MusicBedTreatment(
            track_id="track",
            src_gcs_path="music/track/audio.mp3",
            section_start_s=0.0,
            section_end_s=10.0,
        ),
        music_local_path="/music.mp3",
    )

    assert "volume=0:enable='between(t,2.000000,4.000000)'" in " ".join(cmd)


def test_music_graph_enforces_compiled_ducking_and_loudness_policy():
    common = dict(
        track_id="track",
        src_gcs_path="music/track/audio.mp3",
        section_start_s=0.0,
        section_end_s=10.0,
    )
    gentle = build_sound_effects_command(
        "/base.mp4",
        [_make_placement(1.0)],
        ["/sfx.mp3"],
        "/gentle.mp4",
        music_bed=MusicBedTreatment(**common, speech_duck_db=-6.0, final_lufs=-16.0),
        music_local_path="/music.mp3",
    )
    strong = build_sound_effects_command(
        "/base.mp4",
        [_make_placement(1.0)],
        ["/sfx.mp3"],
        "/strong.mp4",
        music_bed=MusicBedTreatment(**common, speech_duck_db=-18.0, final_lufs=-12.0),
        music_local_path="/music.mp3",
    )

    gentle_graph = " ".join(gentle)
    strong_graph = " ".join(strong)
    assert "loudnorm=I=-16:TP=-1.5" in gentle_graph
    assert "loudnorm=I=-12:TP=-1.5" in strong_graph
    assert gentle_graph != strong_graph
    gentle_ratio = float(gentle_graph.split("ratio=")[1].split(":")[0])
    strong_ratio = float(strong_graph.split("ratio=")[1].split(":")[0])
    assert 1.0 < gentle_ratio < strong_ratio <= 20.0


def test_music_bed_only_graph_mixes_voice_and_bed_without_sfx():
    """The prod-default v2 shape: SFX lane flag off → effects=[] but the music
    bed still mixes against the voice, with video stream-copied."""
    cmd = build_sound_effects_command(
        "/base.mp4",
        [],
        [],
        "/out.mp4",
        music_bed=MusicBedTreatment(
            track_id="t",
            src_gcs_path="music/t/a.mp3",
            section_start_s=0.0,
            section_end_s=10.0,
        ),
        music_local_path="/music.mp3",
    )

    graph = " ".join(cmd)
    assert "amix=inputs=2" in graph
    assert "[voice]" in graph and "[bed]" in graph
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_music_bed_gain_is_clamped_from_persisted_values():
    treatment = MusicBedTreatment.from_value(
        {
            "track_id": "t",
            "src_gcs_path": "music/t/a.mp3",
            "section_start_s": 0.0,
            "section_end_s": 10.0,
            "gain_db": 60.0,
        }
    )
    assert treatment.gain_db == 0.0
    assert (
        MusicBedTreatment.from_value(
            {
                "track_id": "t",
                "src_gcs_path": "music/t/a.mp3",
                "section_start_s": 0.0,
                "section_end_s": 10.0,
                "gain_db": -99.0,
            }
        ).gain_db
        == -40.0
    )


def test_music_treatment_defaults_keep_cached_rows_backward_compatible():
    treatment = MusicBedTreatment.from_value(
        {
            "track_id": "legacy",
            "src_gcs_path": "music/legacy/audio.mp3",
            "section_start_s": 0.0,
            "section_end_s": 10.0,
        }
    )

    assert treatment.speech_duck_db == -12.0
    assert treatment.final_lufs == -14.0


def test_normalize_generated_sound_effects_collapses_near_duplicates():
    placements = [
        {
            "id": "a",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 1.0,
            "gain": 1.0,
            "smart_role": "caption_punch",
        },
        {
            "id": "b",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 1.09,
            "gain": 0.8,
            "smart_role": "caption_punch",
        },
        {
            "id": "c",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 1.3,
            "gain": 1.0,
            "smart_role": "caption_punch",
        },
    ]

    normalized = normalize_generated_sound_effects(placements, threshold_s=0.15)

    assert [placement["id"] for placement in normalized] == ["a", "c"]


def test_normalize_generated_sound_effects_collapses_generated_without_role():
    placements = [
        {
            "id": "a",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 1.0,
            "gain": 1.0,
            "smart_event_id": "cue-1",
        },
        {
            "id": "b",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 1.08,
            "gain": 1.0,
            "smart_event_id": "cue-1",
        },
    ]

    assert [p["id"] for p in normalize_generated_sound_effects(placements)] == ["a"]


def test_normalize_generated_sound_effects_preserves_manual_and_layered_effects():
    placements = [
        {
            "id": "manual",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 2.0,
            "gain": 1.0,
            "source": "user",
        },
        {
            "id": "manual-layer",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 2.04,
            "gain": 0.6,
            "source": "user",
        },
        {
            "id": "impact",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 3.0,
            "gain": 1.0,
            "smart_role": "caption_punch",
        },
        {
            "id": "whoosh",
            "src_gcs_path": "sound-effects/whoosh/audio.mp3",
            "at_s": 3.04,
            "gain": 1.0,
            "smart_role": "caption_punch",
        },
        {
            "id": "different-role",
            "src_gcs_path": "sound-effects/pop/audio.mp3",
            "at_s": 3.05,
            "gain": 1.0,
            "smart_role": "scene_transition",
        },
    ]

    normalized = normalize_generated_sound_effects(placements, threshold_s=0.15)

    assert [placement["id"] for placement in normalized] == [
        "manual",
        "manual-layer",
        "impact",
        "whoosh",
        "different-role",
    ]
