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
    assert "freezeFrame" not in recipe.required_capabilities
    # KRI-828: a longer main-video hold is the creator's freeze frame, its own capability
    # (never editor media).
    clip["hold_duration"] = 2 / frame_rate
    held = EditRecipeV2.model_validate(raw)
    assert "freezeFrame" in held.required_capabilities
    assert "editorMedia" not in held.required_capabilities
    clip["hold_duration"] = 1 / frame_rate
    clip["overlay_above_text"] = True
    with pytest.raises(ValueError, match="V2 overlay"):
        EditRecipeV2.model_validate(raw)


# --- KRI-689: a chat speed ask at creation reaches the phone recipe through the montage plan ---
# docs/reviews/kri-689-chat-speed-failure-modes.md, section C: C1 (no drift), C2 (footage runs
# out), C3 (approved windows keep their edges), C4/C5/C6 (cut -> moment -> recipe math at every
# native rate), C9 (`variableSpeed` capability).


def _montage_clips(count: int = 3, duration_s: float = 6.0):
    from app.pipeline.unified_montage import UnifiedClip

    return [
        UnifiedClip(
            media_id=f"clip-{index}",
            proxy_path=f"users/u/analysis-proxy-clip-{index}.mp4",
            generation="7",
            duration_s=duration_s,
            width=1920,
            height=1080,
            analysis={
                "best_moments": [
                    {
                        "start_s": 1.0,
                        "end_s": 3.0,
                        "energy": 0.9,
                        "description": "a boat crossing the river" if index == 1 else "a street",
                    }
                ]
            },
        )
        for index in range(count)
    ]


def _montage_bindings(clips):
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.services.phone_sources import PhoneSourceBinding

    return tuple(
        PhoneSourceBinding(
            media_id=clip.media_id,
            proxy_path=clip.proxy_path,
            generation=clip.generation,
            original=OriginalMediaDescriptor(
                sha256=f"{index:x}".rjust(64, "a"),
                byte_count=1000 + index,
                duration_s=clip.duration_s,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for index, clip in enumerate(clips)
    )


def _montage_plan(description: str | None, clips, *, clip_speed: bool = True):
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.pipeline.unified_montage import brief_view, plan_unified_montage

    requirements = [BriefRequirement(id="r1", kind="text", scope="title", literal="River day")]
    if description is not None:
        requirements.append(
            BriefRequirement(id="r5", kind="style", scope="global", description=description)
        )
    brief = CreativeBrief(version=1, requirements=requirements)
    return plan_unified_montage(clips, brief_view(brief), strategy={}, clip_speed=clip_speed)


def _compiled(plan, clips):
    from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan

    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    return execution, compile_phone_guided_plan(execution, _montage_bindings(clips))


@pytest.mark.parametrize(
    ("description", "rate"),
    [
        ("slow the clips down to 0.25x", 0.25),
        ("slow motion on everything", 0.5),
        ("speed up the footage 2x", 2.0),
        ("speed it up 4x", 4.0),
    ],
)
def test_a_whole_edit_speed_ask_reaches_every_recipe_clip_without_moving_the_timeline(
    description, rate
):
    clips = _montage_clips(duration_s=12.0)  # room for 4x on a 2 s cut
    baseline, _ = _compiled(_montage_plan(None, clips), clips)
    plan = _montage_plan(description, clips)
    execution, recipe = _compiled(plan, clips)

    # C1: the output timeline is byte-identical to the plan without the ask.
    assert [(m.output_start_s, m.output_end_s) for m in execution.story_timeline] == [
        (m.output_start_s, m.output_end_s) for m in baseline.story_timeline
    ]
    assert execution.resolved_duration_s == baseline.resolved_duration_s
    for moment, base in zip(execution.story_timeline, baseline.story_timeline, strict=True):
        assert moment.playback_rate == rate
        # The source window is the slot's length times the rate, anchored on the same start.
        assert moment.source_start_s == base.source_start_s
        assert moment.source_end_s - moment.source_start_s == pytest.approx(
            moment.duration_s * rate, abs=0.002 * max(1.0, rate)
        )
    clips_by_id = {clip.id: clip for clip in recipe.tracks[0].clips}
    for moment in execution.story_timeline:
        clip = clips_by_id[moment.moment_id]
        assert clip.rate == rate
        assert clip.source_duration / rate + (clip.hold_duration or 0) == pytest.approx(
            moment.duration_s, abs=1e-6
        )
    assert "variableSpeed" in recipe.required_capabilities
    (row,) = plan.record()["clip_speeds"]
    assert row["status"] == "applied" and row["rate"] == rate
    assert row["cut_positions"] == [1, 2, 3] and row["grounding"] == "all_clips"


def test_a_named_moment_grounds_the_rate_on_that_clip_only():
    clips = _montage_clips()
    baseline, _ = _compiled(_montage_plan(None, clips), clips)
    plan = _montage_plan("Speed the river shots up 2x", clips)
    execution, recipe = _compiled(plan, clips)

    rates = {m.media_id: m.playback_rate for m in execution.story_timeline}
    assert rates == {"clip-0": None, "clip-1": 2.0, "clip-2": None}
    # B1 negative control: the other clips keep their exact 1x windows.
    for moment, base in zip(execution.story_timeline, baseline.story_timeline, strict=True):
        if moment.media_id != "clip-1":
            assert (moment.source_start_s, moment.source_end_s) == (
                base.source_start_s,
                base.source_end_s,
            )
    clips_by_id = {clip.id: clip for clip in recipe.tracks[0].clips}
    assert {m.media_id: clips_by_id[m.moment_id].rate for m in execution.story_timeline} == {
        "clip-0": 1,
        "clip-1": 2.0,
        "clip-2": 1,
    }
    (row,) = plan.record()["clip_speeds"]
    assert row["status"] == "applied"
    assert row["media_ids"] == ["clip-1"] and row["cut_positions"] == [2]
    assert row["grounding"] == "analysed_moment"


def test_words_that_name_no_clip_apply_the_rate_nowhere():
    clips = _montage_clips()
    plan = _montage_plan("speed up the fireworks 2x", clips)
    execution, recipe = _compiled(plan, clips)
    assert all(m.playback_rate is None for m in execution.story_timeline)
    assert "variableSpeed" not in recipe.required_capabilities
    (row,) = plan.record()["clip_speeds"]
    assert row["status"] == "not_grounded"


def test_the_last_clip_is_the_final_cut():
    clips = _montage_clips()
    plan = _montage_plan("Slow down the last clip", clips)
    execution, _ = _compiled(plan, clips)
    assert [m.playback_rate for m in execution.story_timeline] == [None, None, 0.5]
    (row,) = plan.record()["clip_speeds"]
    assert row["grounding"] == "cut_position" and row["rate_stated"] is False


def test_footage_that_runs_out_lowers_the_rate_and_keeps_the_cut_length():
    # C2: 4x on a 2 s cut needs 8 s of footage; a 3 s clip has 2.95 s to give. The cut keeps
    # its length (no drift) and plays as fast as the footage allows; the record says so.
    clips = _montage_clips(duration_s=3.0)
    baseline, _ = _compiled(_montage_plan(None, clips), clips)
    plan = _montage_plan("speed it up 4x", clips)
    execution, recipe = _compiled(plan, clips)
    assert execution.resolved_duration_s == baseline.resolved_duration_s
    for moment in execution.story_timeline:
        assert moment.playback_rate is not None and 1.05 <= moment.playback_rate < 4.0
        assert moment.source_end_s <= 3.0 - 0.05 + 1e-6
        assert moment.source_end_s - moment.source_start_s == pytest.approx(
            moment.duration_s * moment.playback_rate, abs=0.002 * moment.playback_rate
        )
    assert recipe.duration == pytest.approx(execution.resolved_duration_s, abs=1e-6)
    (row,) = plan.record()["clip_speeds"]
    assert row["status"] == "applied" and row["footage_limited"] is True
    assert row["rate"] == min(m.playback_rate for m in execution.story_timeline)
    assert row["rate_requested"] == 4.0


def test_without_a_verified_speed_renderer_no_rate_is_planned():
    clips = _montage_clips()
    plan = _montage_plan("speed up the footage 2x", clips, clip_speed=False)
    execution, recipe = _compiled(plan, clips)
    assert all(m.playback_rate is None for m in execution.story_timeline)
    assert "variableSpeed" not in recipe.required_capabilities
    (row,) = plan.record()["clip_speeds"]
    assert (row["status"], row["reason"]) == ("locked", "renderer")


def test_a_plan_without_a_speed_ask_records_nothing_new():
    clips = _montage_clips()
    assert "clip_speeds" not in _montage_plan(None, clips).record()


def test_an_approved_window_keeps_its_edges_and_the_slot_follows_the_rate():
    """C3: a creator-picked source window is never widened; its output length changes."""
    from app.kria.brief import BriefRequirement
    from app.kria.speed_ask import speed_spec
    from app.pipeline.unified_montage import _ground_speeds

    clips = _montage_clips()
    spans = [("clip-0", 0.0, 1.2, False), ("clip-1", 1.0, 3.0, True), ("clip-2", 0.0, 1.2, False)]
    spec = speed_spec(
        BriefRequirement(
            id="r5", kind="style", scope="global", description="slow motion on the river"
        )
    )
    spans, rates, rows = _ground_speeds([spec], spans, clips, pinned=(), freeze_holds={})
    assert spans[1] == ("clip-1", 1.0, 3.0, True)
    assert rates == {1: 0.5}
    assert rows[0]["status"] == "applied" and rows[0]["grounding"] == "analysed_moment"


def test_a_fast_montage_cut_validates_its_output_against_the_rate():
    from pydantic import ValidationError

    from app.schemas.edit_proposal import FastMontageCut

    cut = FastMontageCut(
        cut_id="c",
        media_id="m",
        source_start_s=1.0,
        source_end_s=3.4,
        output_duration_s=1.2,
        role="hook",
        playback_rate=2.0,
    )
    assert cut.model_dump(mode="json")["playback_rate"] == 2.0
    # The legacy dump is untouched: no rate, no key.
    plain = FastMontageCut(
        cut_id="c", media_id="m", source_start_s=1.0, source_end_s=2.2, output_duration_s=1.2,
        role="hook",
    )  # fmt: skip
    assert "playback_rate" not in plain.model_dump(mode="json")
    with pytest.raises(ValidationError, match="output duration"):
        FastMontageCut(
            cut_id="c", media_id="m", source_start_s=1.0, source_end_s=3.4,
            output_duration_s=2.4, role="hook", playback_rate=2.0,
        )  # fmt: skip
    with pytest.raises(ValidationError, match="freeze"):
        FastMontageCut(
            cut_id="c", media_id="m", source_start_s=1.0, source_end_s=3.4,
            output_duration_s=2.2, role="hook", playback_rate=2.0, freeze_hold_s=1.0,
        )  # fmt: skip
