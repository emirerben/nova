"""Selected footage must survive phone speed compilation unchanged."""

import pytest

from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from tests.pipeline.test_phone_guided_plan import fixture


@pytest.mark.parametrize("rate", [0.25, 0.5, 1, 2, 4])
def test_selected_window_compiles_at_requested_rate(rate):
    plan, bindings = fixture()
    moment = plan.story_timeline[0]
    moment.playback_rate = rate
    moment.duration_s = 3 / rate
    moment.output_end_s = moment.duration_s
    plan.resolved_duration_s = moment.duration_s
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_start == 2
    assert clip.source_duration == 3
    assert clip.rate == rate
    assert recipe.duration == pytest.approx(3 / rate)
    assert ("variableSpeed" in recipe.required_capabilities) == (rate != 1)


@pytest.mark.parametrize("rate", [0.5, 1, 2])
def test_retimed_window_does_not_silently_move_when_device_source_is_shorter(rate):
    plan, bindings = fixture()
    moment = plan.story_timeline[0]
    moment.playback_rate = rate  # explicit 1x is also a reset with selected edges
    moment.duration_s = moment.output_end_s = 3 / rate
    plan.resolved_duration_s = 3 / rate
    bindings[0].original.duration_s = 4
    with pytest.raises(UnsupportedPhonePlan, match="source window"):
        compile_phone_guided_plan(plan, bindings)


def test_lipsync_retime_is_explicitly_rejected():
    from tests.pipeline.test_phone_song_lane import lipsync_plan
    from tests.pipeline.user_song_helpers import bindings_for, compiled_plan, song_bed

    result = lipsync_plan()
    plan = compiled_plan(result)
    footage, visuals = bindings_for(result)
    plan.story_timeline[0].playback_rate = 2
    with pytest.raises(UnsupportedPhonePlan, match="lip-sync clip cannot change speed"):
        compile_phone_guided_plan(plan, footage, visuals, song=song_bed())


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf"), 5])
def test_invalid_rate_is_rejected_even_for_unvalidated_plan_mutation(rate):
    plan, bindings = fixture()
    plan.story_timeline[0].playback_rate = rate
    with pytest.raises(UnsupportedPhonePlan, match="playback rate"):
        compile_phone_guided_plan(plan, bindings)


def test_pool_video_uses_same_speed_contract():
    from tests.pipeline.test_phone_guided_plan import pool_only_video_fixture

    plan, visuals = pool_only_video_fixture(10)
    moment = plan.story_timeline[0]
    moment.playback_rate = 2
    moment.duration_s = moment.output_end_s = plan.resolved_duration_s = 1.5
    recipe = compile_phone_guided_plan(plan, (), visuals)
    assert recipe.tracks[0].clips[0].rate == 2
    assert recipe.tracks[0].clips[0].source_duration == 3


@pytest.mark.parametrize("rate", [0.5, 2])
@pytest.mark.parametrize("has_audio", [True, False])
def test_real_cloud_export_matches_phone_selected_window_and_preserves_pitch(
    tmp_path, rate, has_audio
):
    """Measured media, not command arguments: shared source window and a 440 Hz tone."""
    import json
    import shutil
    import subprocess

    import numpy as np

    from app.pipeline.canvas import Canvas
    from app.pipeline.probe import probe_video
    from app.pipeline.reframe import reframe_and_export

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            # Blue outside the selection; red, green and yellow identify the
            # three selected seconds. Check actual output pixels below so a
            # correct duration cannot conceal the wrong source window.
            "color=blue:size=160x90:rate=30,"
            "drawbox=color=red:t=fill:enable='gte(t,2)*lt(t,3)',"
            "drawbox=color=lime:t=fill:enable='gte(t,3)*lt(t,4)',"
            "drawbox=color=yellow:t=fill:enable='gte(t,4)*lt(t,5)'",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-y",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    plan, bindings = fixture()
    moment = plan.story_timeline[0]
    moment.playback_rate = rate
    moment.duration_s = moment.output_end_s = plan.resolved_duration_s = 3 / rate
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    reframe_and_export(
        str(source),
        clip.source_start,
        clip.source_start + clip.source_duration,
        "16:9",
        None,
        str(output),
        canvas=Canvas(160, 90),
        color_trc="bt709",
        has_audio=has_audio,
        speed_factor=clip.rate,
        exact_duration=True,
    )
    duration = probe_video(str(output)).duration_s
    assert duration == pytest.approx(recipe.duration, abs=1 / 30)
    stream = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    )["streams"][0]
    frames = int(stream["nb_read_frames"])
    assert frames == round(recipe.duration * 30)

    def assert_selected_markers(path):
        observed = []
        for source_offset, expected in [
            (0.3, (255, 0, 0)),
            (1.3, (0, 255, 0)),
            (2.3, (255, 255, 0)),
        ]:
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(source_offset / rate),
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            actual = pixels.mean(axis=0)
            # Classify the deliberately distinct markers, allowing normal
            # YUV/BT.709 conversion without testing colour grading here.
            assert all(
                value > 180 if target else value < 70 for value, target in zip(actual, expected)
            )
            observed.append(actual.tolist())
        return observed

    marker_colors = assert_selected_markers(output)
    if has_audio and rate == 2:
        # Render a real wrong-window control. Its duration is correct but its
        # footage is not; the pixel oracle must reject it.
        wrong = tmp_path / "wrong-window-control.mp4"
        reframe_and_export(
            str(source),
            0,
            3,
            "16:9",
            None,
            str(wrong),
            canvas=Canvas(160, 90),
            color_trc="bt709",
            has_audio=True,
            speed_factor=rate,
            exact_duration=True,
        )
        assert probe_video(str(wrong)).duration_s == pytest.approx(recipe.duration, abs=1 / 30)
        with pytest.raises(AssertionError):
            assert_selected_markers(wrong)
    pcm = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    samples = np.frombuffer(pcm, dtype="<f4")[4800:-4800]
    peak = np.argmax(np.abs(np.fft.rfft(samples * np.hanning(len(samples)))))
    frequency = peak * 48000 / len(samples)
    if has_audio:
        assert frequency == pytest.approx(440, abs=3)
    else:
        assert np.max(np.abs(samples)) < 1e-6
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "rate": rate,
                "has_audio": has_audio,
                "source_start_s": clip.source_start,
                "source_end_s": clip.source_start + clip.source_duration,
                "expected_duration_s": recipe.duration,
                "duration_s": duration,
                "frames": frames,
                "audio_peak_hz": frequency,
                "selected_marker_rgb": marker_colors,
            },
            indent=2,
        )
    )
    # A default-rate export would fail the duration oracle by seconds.
    assert abs(clip.source_duration - recipe.duration) > 1


@pytest.mark.parametrize("rate,span", [(4, 3), (0.5, 3.01), (1, 3.01), (1, 3.02)])
def test_phone_frame_rounding_never_extends_selection_or_overlaps_next_slot(rate, span):
    plan, bindings = fixture()
    moment = plan.story_timeline[0]
    moment.source_end_s = moment.source_start_s + span
    moment.playback_rate = rate
    output = round(round(span / rate * 30) / 30, 6)
    moment.duration_s = moment.output_end_s = plan.resolved_duration_s = output
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_duration <= span + 1e-6
    assert clip.source_duration / rate + (clip.hold_duration or 0) == pytest.approx(
        output, abs=1e-6
    )
    assert recipe.duration == pytest.approx(output, abs=1e-6)


@pytest.mark.parametrize("frame_rate", [30, 60])
def test_main_video_padding_is_limited_to_one_recipe_frame(frame_rate):
    from app.kria.recipes_v2 import EditRecipeV2

    plan, bindings = fixture()
    raw = compile_phone_guided_plan(plan, bindings).model_dump()
    raw["frame_rate"] = frame_rate
    clip = raw["tracks"][0]["clips"][0]
    clip["hold_duration"] = 1 / frame_rate
    recipe = EditRecipeV2.model_validate(raw)
    assert "editorMedia" not in recipe.required_capabilities
    clip["hold_duration"] = 2 / frame_rate
    with pytest.raises(ValueError, match="V2 overlay"):
        EditRecipeV2.model_validate(raw)
    clip["hold_duration"] = 1 / frame_rate
    clip["overlay_above_text"] = True
    with pytest.raises(ValueError, match="V2 overlay"):
        EditRecipeV2.model_validate(raw)
