"""KRI-219: a copilot-compiled guided timeline edit commits cleanly on the PHONE.

Drives the exact chat-staged path for a device-rendered ``guided_story`` variant:
``compile_editor_ops`` (flag ``kria_guided_timeline_ops``) -> the payload the draft
sends (``timeline_slots`` + rebased ``text_elements`` + fractional durations) ->
``prepare_editor_commit`` -> ``prepare_phone_editor_commit`` (re-clocks to 1/30 s and
raises ``unsupported_phone_edit`` on any mismatch). Behaviour under test is NOT
changed here; a failing scenario is pinned as a strict xfail with its exact error.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    ClipLabel,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

FRAME_S = 1.0 / 30.0
# `authoredText` (per-clip label bars) and `crossfade` (set by the transition ops) are the
# device-verified capabilities a guided timeline edit needs beyond the basic text set; the
# flag flip must be paired with both being in PHONE_RENDER_VERIFIED_FEATURES in prod.
_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "authoredText",
    "crossfade",
]
CLIP_COUNT = 4
CLIP_S = 7.5
SOURCE_S = 10.0


def _guided_phone_job(monkeypatch, *, count=CLIP_COUNT, clip_s=CLIP_S):
    from app.config import settings

    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        _VERIFIED,
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(settings, "edit_transitions_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)

    job_id = uuid.uuid4()
    media = [
        MediaRef(
            lane="clip",
            media_id=f"m{i}",
            gcs_path=f"generative-jobs/{job_id}/sources/analysis-proxy-m{i}.mp4",
            generation="123",
            kind="video",
            duration_s=SOURCE_S,
        )
        for i in range(count)
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=round(count * clip_s),
        title="Weekend",
        opening_title="Weekend in Lisbon",
        video_reuse_policy="once",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{i}",
                media_id=f"m{i}",
                source_start_s=1.0,
                source_end_s=1.0 + clip_s,
                output_duration_s=clip_s,
                role="hook" if i == 0 else "build",
            )
            for i in range(count)
        ],
        clip_labels=[
            ClipLabel(
                media_id=f"m{i}",
                text=f"Place {i}",
                provenance="creator",
                min_display_s=1.0,
            )
            for i in range(count)
        ],
        story_beats=[
            StoryBeat(
                beat_id="story",
                topic="Scene",
                media_ids=[m.media_id for m in media][:4],
                duration_s=round(count * clip_s),
            )
        ],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {k: getattr(m, k) for k in ("lane", "media_id", "gcs_path", "generation", "kind")}
            for m in media
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    bindings = tuple(
        PhoneSourceBinding(
            media_id=m.media_id,
            proxy_path=m.gcs_path,
            generation="123",
            original=OriginalMediaDescriptor(
                sha256=f"{i + 1:x}" * 64,
                byte_count=1000,
                duration_s=SOURCE_S,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for i, m in enumerate(media)
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        mode="content_plan",
        all_candidates={"clip_paths": []},
        assembly_plan={
            "guided_edit": guided,
            "guided_story_execution_plan": plan,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": plan["resolved_duration_s"],
                    "text_mode": "agent_text",
                    "intro_mode": "linear",
                    "intro_layout": "linear",
                    "text_elements": copy.deepcopy(plan["text_elements"]),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(
                GuidedStoryExecutionPlan.model_validate(plan), bindings
            ),
        ),
        base_generation="first",
    )
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    return job, variant, revision


def _commit(job, variant, revision, ops):
    """Compile ``ops`` and commit the payload exactly as the staged chat draft sends it."""
    compiled = compile_editor_ops(job, variant, ops)
    payload = compiled.payload
    assert payload.timeline_slots is not None and payload.text_elements is not None
    payload.guided_revision_number = revision["revision_number"]
    before_generation = variant["render_generation_id"]
    prep = gj.prepare_editor_commit(job, "guided_story", payload)
    return compiled, prep, before_generation


def _shorten_ops(target_s, *, crossfade=True, strategy="proportional"):
    ops = []
    if crossfade:
        ops.append(
            {
                "op": "patch_slots",
                "selector": {"all": True},
                "patch": {"transition_after": "crossfade", "transition_duration_s": 0.3},
            }
        )
    ops.append({"op": "set_total_duration", "target_s": target_s, "strategy": strategy})
    return ops


def _assert_clean_commit(
    job, prep, before_generation, *, target_s, clip_count=CLIP_COUNT, max_overlap_s=0.0
):
    from app.services.phone_editor import PHONE_EDITOR_PLAN_FIELD

    variant = job.assembly_plan["variants"][0]
    request = device_status(job, "guided_story").request
    recipe = request.recipe
    # recipe duration is on the phone's 1/30 s clock
    assert recipe.duration == pytest.approx(target_s, abs=FRAME_S)
    assert variant["duration_s"] == pytest.approx(target_s, abs=FRAME_S)
    assert len(recipe.tracks[0].clips) == clip_count
    assert request.identity.recipe_revision == 2
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_generation_id"] not in (None, before_generation)
    saved = variant["guided_edit_revision"]
    assert [r for r in saved["tombstones"] if r.get("lane") == "text_elements"] == []
    segments = saved["segments"]
    labels = sorted(
        (r for r in saved["text_elements"] if r["id"].startswith("clip-label-")),
        key=lambda r: r["start_s"],
    )
    assert len(labels) == clip_count
    by_media = {s["media_id"]: s for s in segments}
    for label in labels:
        # clip-label-cut-N follows media mN
        segment = by_media["m" + label["id"].rsplit("-", 1)[1]]
        assert label["start_s"] == pytest.approx(segment["output_start_s"], abs=FRAME_S)
        assert label["end_s"] == pytest.approx(segment["output_end_s"], abs=FRAME_S)
    for earlier, later in zip(labels, labels[1:]):
        # sorted by window; a label may only overlap its neighbour by the crossfade
        assert earlier["start_s"] < later["start_s"]
        assert earlier["end_s"] <= later["start_s"] + max_overlap_s + FRAME_S
    for label in labels:
        assert label["end_s"] - label["start_s"] >= 0.2
        assert label["end_s"] <= recipe.duration + FRAME_S
    # the plan the phone recipe was compiled from carries the same labels
    plan_labels = [
        e
        for e in variant[PHONE_EDITOR_PLAN_FIELD]["text_elements"]
        if e["id"].startswith("clip-label-")
    ]
    assert {e["id"] for e in plan_labels} == {e["id"] for e in labels}
    return recipe, labels, segments


def test_phone_set_total_duration_proportional_with_crossfades_commits(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    compiled, prep, before = _commit(job, variant, revision, _shorten_ops(21))
    assert prep["revision_number"] == 2
    recipe, labels, _segments = _assert_clean_commit(
        job, prep, before, target_s=21, max_overlap_s=0.3
    )
    assert [c.source_duration for c in recipe.tracks[0].clips]  # clips shortened, not dropped
    assert max(c.source_duration for c in recipe.tracks[0].clips) < CLIP_S


def test_phone_patch_slots_all_shorter_commits(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    ops = [{"op": "patch_slots", "selector": {"all": True}, "patch": {"duration_s": 4}}]
    _compiled, prep, before = _commit(job, variant, revision, ops)
    recipe, _labels, _segments = _assert_clean_commit(job, prep, before, target_s=16)
    assert all(c.source_duration == pytest.approx(4, abs=FRAME_S) for c in recipe.tracks[0].clips)


def test_phone_patch_slots_with_crossfade_shorter_commits(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    ops = [
        {
            "op": "patch_slots",
            "selector": {"all": True},
            "patch": {
                "duration_s": 5,
                "transition_after": "crossfade",
                "transition_duration_s": 0.3,
            },
        }
    ]
    _compiled, prep, before = _commit(job, variant, revision, ops)
    # 4 x 5 s minus 3 x 0.3 s of overlap (last clip's transition is ignored)
    _assert_clean_commit(job, prep, before, target_s=20 - 3 * 0.3, max_overlap_s=0.3)


def test_phone_five_clip_fractional_target_commits(monkeypatch):
    # 5 x 6 s = 30 s; 21 s / 5 clips is not a whole number of frames per clip.
    job, variant, revision = _guided_phone_job(monkeypatch, count=5, clip_s=6.0)
    _compiled, prep, before = _commit(job, variant, revision, _shorten_ops(21))
    _assert_clean_commit(job, prep, before, target_s=21, clip_count=5, max_overlap_s=0.3)


def test_phone_commit_does_not_shift_labels_outside_their_segment_after_quantization(
    monkeypatch,
):
    # 17.3 s over 4 clips -> 4.325 s each: off the 1/30 s grid on purpose.
    job, variant, revision = _guided_phone_job(monkeypatch)
    _compiled, prep, before = _commit(job, variant, revision, _shorten_ops(17.3, crossfade=False))
    recipe, labels, segments = _assert_clean_commit(job, prep, before, target_s=17.3)
    clip_windows = []
    cursor = 0.0
    for clip in recipe.tracks[0].clips:
        clip_windows.append((cursor, cursor + clip.source_duration))
        cursor += clip.source_duration
    for label, (start, end) in zip(labels, clip_windows):
        assert start - FRAME_S <= label.get("start_s") <= end + FRAME_S
        assert start - FRAME_S <= label.get("end_s") <= end + FRAME_S


def test_phone_minimum_clip_length_leaves_room_for_the_label_bar(monkeypatch):
    from app.agents.editor_ops_v2.timeline import MIN_RETARGET_CLIP_S
    from app.services.kria_editor_timeline import _MIN_BAR_S

    # the shortest clip the retarget ops allow must still hold the shortest label bar
    assert MIN_RETARGET_CLIP_S >= _MIN_BAR_S
    job, variant, revision = _guided_phone_job(monkeypatch)
    ops = [
        {
            "op": "patch_slots",
            "selector": {"all": True},
            "patch": {"duration_s": MIN_RETARGET_CLIP_S},
        }
    ]
    _compiled, prep, before = _commit(job, variant, revision, ops)
    _assert_clean_commit(job, prep, before, target_s=4 * MIN_RETARGET_CLIP_S)


def test_target_below_shortest_possible_is_a_clear_op_error_not_a_422(monkeypatch):
    from app.agents.editor_ops_v2.timeline import MIN_RETARGET_CLIP_S

    job, variant, _revision = _guided_phone_job(monkeypatch)
    with pytest.raises(KriaEditorOpError, match=r"Shortest possible is 1\.2 s") as error:
        compile_editor_ops(job, variant, [{"op": "set_total_duration", "target_s": 0.5}])
    assert f"{MIN_RETARGET_CLIP_S:g} s" in str(error.value)
    # refused at compile time: nothing was staged, so no commit/422 can follow
    assert job.assembly_plan["variants"][0]["render_generation_id"] == "first"
    assert "guided_edit_revision" not in job.assembly_plan["variants"][0]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: with crossfades the compiler gives each per-clip label its segment's "
        "output window, and neighbouring segment windows overlap by the transition "
        "duration, so two label bars (same position y_frac=0.78) are alive together for "
        "0.3 s. e.g. 5.5 <= 5.2 fails for 4 clips shortened to 21 s with 0.3 s crossfades. "
        "Commit succeeds (no 422); whether the stacked labels look wrong on device is "
        "unverified."
    ),
)
def test_crossfade_label_bars_do_not_overlap_in_time(monkeypatch):
    job, variant, revision = _guided_phone_job(monkeypatch)
    _compiled, prep, before = _commit(job, variant, revision, _shorten_ops(21))
    _assert_clean_commit(job, prep, before, target_s=21, max_overlap_s=0.0)
