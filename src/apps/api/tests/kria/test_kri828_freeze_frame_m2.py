"""KRI-828: "Freeze the frame for one second on Atakan's bite" renders a real one-frame hold.

Merope café M2 (KRI-775 fixture): six ~2 s moments of one 68 s take, the Cotopaxi photo for
1 s after the hello wave, "Freeze the frame for one second on Atakan's bite", original sound,
about 14 seconds. KRI-777 made the freeze an honest "Couldn't". This replays the same approved
draft end to end through the real gate, the real phone worker, the strict guided compiler, the
phone recipe compiler and the phone rollout validator, and renders the cloud path's held moment
to an MP4, so the hold is proven on the media, not on command arguments.

Ways this can fail (written before the fix), each pinned below:
1. Wrong frame held: the hold lands on the window's start/end instead of the grounded bite
   (the analysed "man takes first bite of dessert" moment, 57.04-59.84 s of the take).
2. Fake slow motion: a playback rate below 1 instead of a true one-frame hold.
3. Hold missing: the plan carries a freeze but the compiler / recipe validator / rollout
   gate drops or refuses it; or the receipt says "met" while the recipe has no hold.
4. The freeze's 1 s becomes the whole video's length (KRI-777 regression).
5. Timing breaks: the hold squeezes the six moments or the photo, or the total misses
   "about 14 seconds".
6. Source audio loops or repeats across the hold (cloud: the held seconds must be silent
   for the take's own sound, and the next moment's sound must start on its own cut).
7. Text after the hold is not shifted (the photo label must sit on the photo's own window).
8. An old app build / an unverified feature gets a hold recipe: without ``freezeFrame``
   verified the plan carries no hold and the receipt stays the honest "Couldn't".
9. Model output alone becomes a freeze: a ``freeze_frame`` fact on words that are not a
   freeze ask ("hold on the sunset for 2 seconds") never holds a frame.
10. Cloud and phone differ: the cloud render must clone the same source frame for the same
    seconds the phone recipe holds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.pipeline import unified_montage
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.kria.test_kri775_merope_m2_replay import (
    RENDERS,
    TAKE,
    gate_media,
    gate_strategy,
    m2_brief_with_moment_ask,
    prod_flags,
    run_gate,
    run_worker,
)
from tests.pipeline.user_song_helpers import bindings_for

# Set KRI828_KEEP_DIR to keep the evidence (recipe JSON, cloud MP4, report.json).
KEEP_DIR = os.environ.get("KRI828_KEEP_DIR")


def _keep(name: str, payload: object = None, *, source: Path | None = None) -> None:
    if not KEEP_DIR:
        return
    directory = Path(KEEP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    if source is not None:
        shutil.copyfile(source, directory / name)
    else:
        (directory / name).write_text(json.dumps(payload, indent=2, default=str) + "\n")


# The take's analysed moment for "Atakan's bite" (clip record, best_moments).
BITE_START_S, BITE_END_S = 57.04, 59.84
BITE_FRAME_S = round((BITE_START_S + BITE_END_S) / 2, 3)


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


def _verify_freeze(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    features = [f for f in settings.phone_render_verified_features if f != "freezeFrame"]
    monkeypatch.setattr(
        settings, "phone_render_verified_features", [*features, "freezeFrame"] if on else features
    )


async def _plan_m2(monkeypatch: pytest.MonkeyPatch, *, brief=None):  # noqa: ANN001, ANN202
    """Gate + phone worker on the approved M2 draft; returns (plan, worker outcome)."""
    render = RENDERS[0]
    brief = brief if brief is not None else m2_brief_with_moment_ask(render)
    result = await run_gate(monkeypatch, render, brief=brief)
    captured: dict = {}
    real = unified_montage.plan_unified_montage

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        captured["plan"] = real(*args, **kwargs)
        return captured["plan"]

    monkeypatch.setattr(unified_montage, "plan_unified_montage", spy)
    outcome = run_worker(
        monkeypatch,
        render,
        brief=brief,
        media=gate_media(result),
        strategy=gate_strategy(result),
    )
    return captured["plan"], outcome


def _phone_recipe(monkeypatch: pytest.MonkeyPatch, plan):  # noqa: ANN001, ANN202
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    footage, visuals = bindings_for(plan, original_duration={TAKE: 68.37})
    recipe = compile_phone_guided_plan(execution, footage, visuals)
    # The prod allowlist verifies the ordinary montage lanes; `freezeFrame` stays as the
    # test set it.
    verified = set(settings.phone_render_verified_features)
    ordinary = recipe.required_capabilities - {"freezeFrame"}
    monkeypatch.setattr(settings, "phone_render_verified_features", sorted(verified | ordinary))
    validate_phone_pilot_recipe(recipe)
    return execution, recipe


def _receipt(outcome, requirement_id: str) -> dict:  # noqa: ANN001
    return next(row for row in outcome.receipts if row["requirement_id"] == requirement_id)


# -- 1, 2, 3, 4, 5, 7: the approved M2 draft carries the hold end to end ----------------------


async def test_m2_bite_is_held_for_one_second_on_the_phone_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verify_freeze(monkeypatch, on=False)
    baseline_plan, _baseline = await _plan_m2(monkeypatch)
    baseline, _recipe = _phone_recipe(monkeypatch, baseline_plan)
    _verify_freeze(monkeypatch)
    plan, outcome = await _plan_m2(monkeypatch)
    assert outcome.refusal is None, outcome.refusal

    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1, [m.model_dump() for m in execution.story_timeline]
    bite = held[0]
    # 1. the frozen frame is the grounded bite, and the window ends on it
    assert bite.media_id == TAKE
    assert bite.source_end_s == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert BITE_START_S <= bite.source_end_s <= BITE_END_S
    assert bite.freeze_hold_s == pytest.approx(1.0, abs=0.001)
    # 5. the moment itself keeps its ~2 s of motion; the hold is on top of it
    assert bite.source_end_s - bite.source_start_s == pytest.approx(2.0, abs=0.05)
    assert bite.duration_s == pytest.approx(3.0, abs=0.05)

    clips = {clip.id: clip for track in recipe.tracks for clip in track.clips}
    clip = clips[bite.moment_id]
    # 2. a true hold at normal speed, never slow motion
    assert clip.rate == 1
    assert clip.hold_duration == pytest.approx(1.0, abs=1 / 30 + 0.001)
    assert clip.source_start + clip.source_duration == pytest.approx(BITE_FRAME_S, abs=0.02)
    assert "freezeFrame" in recipe.required_capabilities
    assert "editorMedia" not in recipe.required_capabilities
    # every other moment is a plain cut: one hold, on one clip
    assert [c.id for c in clips.values() if (c.hold_duration or 0) > 0.05] == [bite.moment_id]

    # 4, 5. the hold adds its second to the approved draft and squeezes nothing: the same
    # six ~2 s moments in the same order, the photo unchanged (its own length is KRI-825's)
    take_moments = [m for m in execution.story_timeline if m.media_id == TAKE]
    assert len(take_moments) == 6
    for moment in take_moments:
        assert moment.source_end_s - moment.source_start_s == pytest.approx(2.0, abs=0.05)
    assert [m.media_id for m in execution.story_timeline] == [
        m.media_id for m in baseline.story_timeline
    ]
    photo = next(m for m in execution.story_timeline if m.kind == "image")
    base_photo = next(m for m in baseline.story_timeline if m.kind == "image")
    assert photo.duration_s == pytest.approx(base_photo.duration_s, abs=0.001)
    unchanged = [
        (a.source_start_s, a.source_end_s)
        for a, b in zip(execution.story_timeline, baseline.story_timeline, strict=True)
        if a.moment_id != bite.moment_id and a.kind == "video"
    ]
    assert unchanged == [
        (b.source_start_s, b.source_end_s)
        for b in baseline.story_timeline
        if b.moment_id != bite.moment_id and b.kind == "video"
    ]
    assert recipe.duration == pytest.approx(baseline.resolved_duration_s + 1.0, abs=0.01)
    assert execution.resolved_duration_s == pytest.approx(recipe.duration, abs=0.01)
    assert 13.5 <= recipe.duration <= 15.0  # "About 14 seconds"

    _keep("m2_recipe.json", recipe.model_dump(mode="json"))
    _keep(
        "m2_plan.json",
        {
            "story_timeline": [m.model_dump(mode="json") for m in execution.story_timeline],
            "resolved_duration_s": execution.resolved_duration_s,
            "receipts": outcome.receipts,
        },
    )
    # 3. the receipt is "met" because the plan carries the hold, and names the frame
    freeze = _receipt(outcome, "r3")
    assert freeze["status"] == "met", freeze
    assert "freez" in freeze["reason"].lower()
    length = _receipt(outcome, "r7")
    assert length["status"] == "met", length
    for row in outcome.receipts:
        assert "you asked for 1s" not in str(row.get("reason")), row

    # 7. the photo's label stays on the photo's own window (text shifted with the hold)
    # (M2's held bite is the last moment, so nothing plays after the hold here; the editor
    # test in test_kri828_freeze_review_fixes moves it before the photo.)
    label = next(
        e
        for e in [*execution.text_elements, *execution.context_label_text_elements]
        if "Ecuador" in e.text and e.start_s > 0
    )
    assert label.start_s == pytest.approx(photo.output_start_s, abs=0.05)
    assert label.end_s == pytest.approx(photo.output_end_s, abs=0.05)


# -- 8: without the verified feature the hold is never shipped, the receipt stays honest ------


async def test_without_verified_freeze_frame_the_plan_has_no_hold_and_says_couldnt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verify_freeze(monkeypatch, on=False)
    plan, outcome = await _plan_m2(monkeypatch)
    _execution, recipe = _phone_recipe(monkeypatch, plan)
    assert all(not (c.hold_duration or 0) > 0.05 for t in recipe.tracks for c in t.clips)
    assert "freezeFrame" not in recipe.required_capabilities
    freeze = _receipt(outcome, "r3")
    assert freeze["status"] == "not_possible", freeze


# -- 3: a recipe carrying a hold is refused when the feature is not verified ---------------------


async def test_rollout_refuses_a_hold_recipe_when_freeze_frame_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.phone_rollout import PhoneCapabilityUnavailable  # noqa: PLC0415

    _verify_freeze(monkeypatch)
    plan, _outcome = await _plan_m2(monkeypatch)
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    footage, visuals = bindings_for(plan, original_duration={TAKE: 68.37})
    recipe = compile_phone_guided_plan(execution, footage, visuals)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted(recipe.required_capabilities - {"freezeFrame"}),
    )
    with pytest.raises(PhoneCapabilityUnavailable) as caught:
        validate_phone_pilot_recipe(recipe)
    assert caught.value.capability == "freezeFrame"


# -- 9: model output alone never freezes ------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Hold on the sunset for 2 seconds",
        "The frozen lake at the start",
        "Dondurma yerken çekilen kısım",
        "About 14 seconds",
    ],
)
def test_a_freeze_fact_without_a_freeze_ask_never_holds(description: str) -> None:
    from app.kria.freeze_ask import freeze_spec  # noqa: PLC0415

    req = BriefRequirement(
        id="r9",
        kind="timing",
        scope="global",
        description=description,
        facts={"duration_s": 2, "freeze_frame": True},
    )
    assert freeze_spec(req) is None


@pytest.mark.parametrize(
    ("description", "facts", "hold_s", "words"),
    [
        (
            "Freeze the frame for one second on Atakan's bite",
            {"duration_s": 1, "freeze_frame": True},
            1.0,
            ("atakan", "bite"),
        ),
        ("Freeze frame on the goal for 2 seconds", {"freeze_frame": True}, 2.0, ("goal",)),
        ("Freeze on the jump", {}, 1.0, ("jump",)),
        (
            "Pastayı keserken kareyi 2 saniye dondur",
            {"duration_s": 2},
            2.0,
            ("pastayi", "keserken"),
        ),
        # The creator's number wins over a model-typed duration_s that disagrees.
        (
            "Freeze the frame on the bite for 1 second",
            {"duration_s": 2.5, "freeze_frame": True},
            1.0,
            ("bite",),
        ),
        # Seconds that are not the hold (video length, a position) never set it.
        (
            "Make a 15 second video and freeze when he bites",
            {"duration_s": 15, "freeze_frame": True},
            1.0,
            ("bites",),
        ),
        ("Freeze the frame at 14 seconds for 2 seconds", {"freeze_frame": True}, 2.0, ()),
        ("Freeze it for half a second on the jump", {"freeze_frame": True}, 0.5, ("jump",)),
    ],
)
def test_the_creators_freeze_words_name_the_moment_and_the_seconds(
    description: str, facts: dict, hold_s: float, words: tuple[str, ...]
) -> None:
    from app.kria.freeze_ask import freeze_spec  # noqa: PLC0415

    spec = freeze_spec(
        BriefRequirement(
            id="r3", kind="timing", scope="global", description=description, facts=facts
        )
    )
    assert spec is not None
    assert spec.hold_s == pytest.approx(hold_s)
    assert set(words) <= set(spec.target_words), spec


# -- 1, 2, 6, 10: the cloud render clones the same frame, silent under the hold ---------------


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=60
    )


def _frame_luma(path: Path, at_s: float) -> float:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{at_s:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=8:8,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return sum(raw) / max(1, len(raw))


def _rms(path: Path, start_s: float, end_s: float) -> float:
    import numpy as np  # noqa: PLC0415

    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{start_s:.3f}",
            "-to",
            f"{end_s:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0


def test_cloud_render_holds_the_same_source_frame_silently(tmp_path: Path) -> None:
    from app.pipeline import guided_story  # noqa: PLC0415

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    source = tmp_path / "take.mp4"
    # Brightness ramps every frame (luma == time), and a steady tone plays throughout:
    # a held frame is constant luma at the frozen time; any looped sound is audible.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=180x320:rate=30,geq=lum='min(255,T*40)':cb=128:cr=128",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(source),
    )
    plan = {
        "output_orientation": "portrait",
        "resolved_duration_s": 4.0,
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "story_timeline": [
            {
                "moment_id": "held",
                "beat_id": "held",
                "media_id": "take",
                "generation": "1",
                "kind": "video",
                "layout": "fullscreen",
                "source_start_s": 1.0,
                "source_end_s": 3.0,
                "freeze_hold_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 3.0,
                "duration_s": 3.0,
            },
            {
                "moment_id": "next",
                "beat_id": "next",
                "media_id": "take",
                "generation": "1",
                "kind": "video",
                "layout": "fullscreen",
                "source_start_s": 4.0,
                "source_end_s": 5.0,
                "output_start_s": 3.0,
                "output_end_s": 4.0,
                "duration_s": 1.0,
            },
        ],
    }
    outputs, receipts = guided_story._render_moments(plan, {"take": str(source)}, str(tmp_path))
    held = Path(outputs[0])
    assert receipts[0]["output_duration_s"] == pytest.approx(3.0, abs=0.1)
    assert receipts[0]["freeze_hold_s"] == pytest.approx(1.0)
    # 2. motion before the hold plays at normal speed: one output second spans one source
    # second of the brightness ramp (slow motion would span half of it)
    moved = _frame_luma(held, 1.5) - _frame_luma(held, 0.5)
    assert moved == pytest.approx(_frame_luma(source, 2.5) - _frame_luma(source, 1.5), rel=0.25)
    # 1, 10. the hold is the source frame at source_end_s, unchanged for the whole hold
    frozen = [_frame_luma(held, t) for t in (2.1, 2.5, 2.9)]
    assert max(frozen) - min(frozen) < 1.5, frozen
    last_moving = _frame_luma(held, 1.97)
    assert frozen[0] == pytest.approx(last_moving, abs=2)
    # ... and that frame is the source's frame at source_end_s (3.0 s), not the window start
    assert frozen[0] == pytest.approx(_frame_luma(source, 2.96), abs=3)
    assert abs(frozen[0] - _frame_luma(source, 1.0)) > 30

    joined = tmp_path / "joined.mp4"
    listing = tmp_path / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in outputs))
    _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-an", "-c", "copy", str(joined))
    final = Path(
        guided_story._mux_guided_source_audio(
            str(joined), plan, {"take": str(source)}, str(tmp_path / "final.mp4")
        )
    )
    # 6. the take's own sound plays under the moving part, is silent under the hold, and
    # the next moment's sound starts on its own cut (never a loop of the held moment)
    assert _rms(final, 0.5, 1.8) > 1000
    assert _rms(final, 2.1, 2.9) < 50
    assert _rms(final, 3.1, 3.9) > 1000
    _keep("cloud_freeze.mp4", source=final)
    _keep("cloud_source.mp4", source=source)
    _keep(
        "cloud_report.json",
        {
            "plan": plan,
            "moment_receipts": receipts,
            "luma_moving_0.5_1.5": [_frame_luma(held, 0.5), _frame_luma(held, 1.5)],
            "luma_hold_2.1_2.5_2.9": frozen,
            "luma_source_at_2.96": _frame_luma(source, 2.96),
            "rms_moving_0.5_1.8": _rms(final, 0.5, 1.8),
            "rms_hold_2.1_2.9": _rms(final, 2.1, 2.9),
            "rms_next_3.1_3.9": _rms(final, 3.1, 3.9),
        },
    )


# -- editor round trip: Save / trim another moment keeps the hold, at 1x ----------------------


def _saved(revision: dict, slots: list[dict]) -> dict:
    from app.routes.generative_jobs import TimelineSlotEdit  # noqa: PLC0415
    from app.services.guided_timeline import build_guided_segments  # noqa: PLC0415

    segments = build_guided_segments(
        revision,
        [TimelineSlotEdit.model_validate(row) for row in slots],
        transitions_enabled=False,
        max_slots=40,
        max_total_s=180,
    )
    return {**revision, "segments": segments}


async def test_editor_save_and_a_trim_elsewhere_keep_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.guided_story import compile_guided_runtime_plan  # noqa: PLC0415
    from app.routes.generative_jobs import _guided_v2_slot_rows  # noqa: PLC0415
    from app.schemas.guided_edit_revision import (  # noqa: PLC0415
        guided_editor_revision_from_approval,
    )

    _verify_freeze(monkeypatch)
    plan, _outcome = await _plan_m2(monkeypatch)
    guided = plan.guided_edit()
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["state_hash"] = ""
    slots = _guided_v2_slot_rows(revision, lambda segment: "fullscreen", include_source_path=False)
    held_slot = next(row for row in slots if row.get("freeze_hold_s"))
    assert held_slot["freeze_hold_s"] == pytest.approx(1.0)
    editable = [
        {key: row[key] for key in ("slot_id", "clip_index", "in_s", "duration_s")} for row in slots
    ]

    def recipe_for(saved: dict):  # noqa: ANN202
        runtime = GuidedStoryExecutionPlan.model_validate(
            compile_guided_runtime_plan(canonical, guided, saved)
        )
        footage, visuals = bindings_for(plan, original_duration={TAKE: 68.37})
        return runtime, compile_phone_guided_plan(runtime, footage, visuals)

    # A plain Save (an older client sends no freeze field) keeps the hold on the same frame.
    runtime, recipe = recipe_for(_saved(revision, editable))
    clip = next(c for t in recipe.tracks for c in t.clips if c.id == held_slot["slot_id"])
    assert clip.rate == 1
    assert clip.hold_duration == pytest.approx(1.0, abs=1 / 30 + 0.001)
    assert clip.source_start + clip.source_duration == pytest.approx(BITE_FRAME_S, abs=0.02)

    # An app build below the freeze protocol is told to update instead of receiving a hold
    # recipe it cannot decode (the worker plans with no app build in hand).
    from app.services.client_protocol import (  # noqa: PLC0415
        clear_request_context,
        set_client_protocol,
    )
    from app.services.phone_rollout import (  # noqa: PLC0415
        freeze_frame_client_supported,
        recipe_has_freeze_frame,
    )

    assert recipe_has_freeze_frame(recipe)
    try:
        set_client_protocol(settings.freeze_frame_min_client_protocol - 1)
        assert not freeze_frame_client_supported()
        set_client_protocol(settings.freeze_frame_min_client_protocol)
        assert freeze_frame_client_supported()
    finally:
        clear_request_context()

    # Splitting the held moment keeps the freeze only on the half that still ends on the
    # chosen frame; the leading half plays through (no second freeze on a frame never chosen).
    left = next(row for row in editable if row["slot_id"] == held_slot["slot_id"])
    cut_s = round(left["duration_s"] / 2, 3)
    split = []
    for row in editable:
        if row is left:
            split.append({**row, "duration_s": cut_s})
            split.append(
                {
                    "slot_id": None,
                    "parent_segment_id": row["slot_id"],
                    "clip_index": row["clip_index"],
                    "in_s": round(row["in_s"] + cut_s, 3),
                    "duration_s": round(row["duration_s"] - cut_s, 3),
                }
            )
        else:
            split.append(row)
    split_saved = _saved(revision, split)
    held_rows = [seg for seg in split_saved["segments"] if seg.get("freeze_hold_s")]
    assert len(held_rows) == 1, held_rows
    assert held_rows[0]["segment_id"] != held_slot["slot_id"]
    assert held_rows[0]["source_end_s"] == pytest.approx(BITE_FRAME_S, abs=0.02)
    lead = next(seg for seg in split_saved["segments"] if seg["segment_id"] == held_slot["slot_id"])
    assert lead["source_end_s"] == pytest.approx(held_rows[0]["source_start_s"], abs=0.001)
    _runtime3, recipe3 = recipe_for(split_saved)
    held_clips = [c for t in recipe3.tracks for c in t.clips if c.hold_duration]
    assert len(held_clips) == 1
    assert held_clips[0].source_start + held_clips[0].source_duration == pytest.approx(
        BITE_FRAME_S, abs=0.02
    )

    # Trimming ANOTHER moment leaves the held one byte-identical.
    other = next(row for row in editable if row["slot_id"] != held_slot["slot_id"])
    trimmed = [
        {**row, "duration_s": round(row["duration_s"] - 0.5, 3)} if row is other else row
        for row in editable
    ]
    runtime2, recipe2 = recipe_for(_saved(revision, trimmed))
    held2 = next(c for t in recipe2.tracks for c in t.clips if c.id == held_slot["slot_id"])
    assert (held2.source_start, held2.source_duration, held2.rate, held2.hold_duration) == (
        clip.source_start,
        clip.source_duration,
        clip.rate,
        clip.hold_duration,
    )
    assert runtime2.resolved_duration_s == pytest.approx(
        runtime.resolved_duration_s - 0.5, abs=0.05
    )


# -- quick mixed-media frame budgeting never moves the frozen frame ---------------------------


def test_quick_mixed_timing_keeps_the_held_frame_and_puts_rounding_in_the_hold() -> None:
    """A held video moment in photo+video quick timing keeps its source window ending ON the
    frozen frame; the frame budget's rounding lands in the hold, never in the footage."""
    from app.pipeline.guided_story import _quantize_quick_mixed_timeline  # noqa: PLC0415

    moments = [
        {
            "beat_id": "a",
            "kind": "image",
            "source_start_s": 0.0,
            "source_end_s": 0.8,
            "duration_s": 0.8,
        },
        {
            "beat_id": "b",
            "kind": "video",
            "source_start_s": 56.0,
            "source_end_s": 58.44,
            "duration_s": 3.44,
            "freeze_hold_s": 1.0,
        },
        {
            "beat_id": "c",
            "kind": "video",
            "source_start_s": 10.0,
            "source_end_s": 12.0,
            "duration_s": 2.0,
        },
    ]
    windows = [{"beat_id": m["beat_id"]} for m in moments]
    total = _quantize_quick_mixed_timeline(moments, windows, target_s=6.24)
    held = moments[1]
    assert held["source_end_s"] == pytest.approx(58.44)
    assert held["source_start_s"] == pytest.approx(56.0)
    assert held["freeze_hold_s"] == pytest.approx(1.0, abs=1 / 30 + 1e-6)
    assert held["duration_s"] == pytest.approx(
        held["source_end_s"] - held["source_start_s"] + held["freeze_hold_s"], abs=1e-5
    )
    assert total == pytest.approx(6.24, abs=1 / 30)
