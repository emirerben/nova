"""End-to-end prod-shaped replay of the KRI-132 phone-voiceover-gate fix.

Reproduces the real pilot scenario: a Narrated project (edit_format
`narrated_planned`) with 5 phone-bound clips, 5 Visuals-pool photos, and an
uploaded 48s voiceover whose narration identity is already resolved. "Use
every uploaded file" + a voiceover selects the voiceover-timed guided story
lane (execution contract `guided_voiceover_v1`, render_program "guided") --
the only lane that combines Visuals with a voiceover, and the one this
KRI-132 follow-up unblocks on the phone.

This test walks all FOUR gates named in the task:
  1. `creator_capabilities.py`'s `CAPABILITY_GUIDED_VOICEOVER` (manifest layer)
  2. `content_plan_build.py`'s dispatch gate (not exercised directly here --
     see `tests/tasks/test_content_plan_build.py::
     test_phone_gate_guided_voiceover_dispatches_when_supported`)
  3. `phone_guided_plan.py`'s `_UNSUPPORTED_PHONE_LANE_CAPABILITY` narration
     lane + narration-bed validation
  4. `generative_build.py`'s `_run_phone_guided_job` bed resolution (not
     exercised directly here -- see
     `tests/tasks/test_phone_guided_dispatch.py`'s narration tests)

Gates 1, 3 are walked directly by this test (manifest -> compiled strategy ->
compiled recipe -> device-pilot validation); 2 and 4 share the exact same
`phone_guided_narration_supported()` single source of truth (see
`app.services.phone_rollout`), so this test's `prod_profile`-driven pass
(and its flag-off failure) is strong evidence for all four -- the dedicated
dispatch/worker tests referenced above additionally exercise 2 and 4 in
isolation with their own mocking.

`compile_execution_plan`'s full snapshot->plan path (guided_story.py ~1919)
needs a fully valid `EditProposalSnapshot` (story_beats, media, etc.) that
duplicates `tests/pipeline/test_guided_story_*` fixtures without adding
anything this test needs to prove; this test instead constructs the
`GuidedStoryExecutionPlan` directly (title + `_narration_caption_elements`
captions), matching `tests/pipeline/test_phone_guided_plan.py`'s
`narration_fixture()` pattern -- the compiler's stills+narration shape is
already exhaustively covered there.
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import GUIDED_VOICEOVER_EXECUTION_CONTRACT
from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderFingerprint
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    _narration_caption_elements,
    compile_execution_plan,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.speech_cleanup_apply import hydrate_speech_cleanup_snapshot
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    NarrationTrack,
    NarrationWord,
    StoryBeat,
    canonical_media_digest,
)
from app.schemas.semantic_edit import SemanticChapter, SemanticEditPlan, SemanticSource
from app.services import creator_capabilities as capabilities
from app.services.guided_speech_cleanup import (
    build_cleaned_narration,
    require_guided_cleanup_binding,
)
from app.services.phone_rollout import phone_guided_narration_supported, validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from app.services.proposal_planning import snapshot_agent_input
from app.services.semantic_edit_scheduler import schedule_semantic_edit
from tests._prod_profile import PROD_NARRATION_IDENTITY
from tests.services.test_guided_speech_cleanup import (
    fixture_ids,
    load_fixture,
    raw_narration,
    write_tone_voiceover,
)

CLIP_MEDIA_IDS = [f"analysis-proxy-ios-{n}.mp4" for n in range(1, 6)]
PHOTO_MEDIA_IDS = [f"asset-photo-{n}" for n in range(1, 6)]
ALL_MEDIA_IDS = CLIP_MEDIA_IDS + PHOTO_MEDIA_IDS
TOTAL_DURATION_S = float(PROD_NARRATION_IDENTITY["duration_s"])  # 48.0
MOMENT_DURATION_S = TOTAL_DURATION_S / len(ALL_MEDIA_IDS)  # 4.8s x 10 moments


def _phone_manifest():
    media = [{"media_id": media_id, "kind": "video"} for media_id in CLIP_MEDIA_IDS] + [
        {"media_id": media_id, "kind": "image"} for media_id in PHOTO_MEDIA_IDS
    ]
    return capabilities.resolve_creator_manifest(
        item_id="item-narrated-planned",
        edit_format="narrated_planned",
        media=media,
        phone_source_media_ids=CLIP_MEDIA_IDS,
        phone_rendering_allowed=True,
        has_voiceover=True,
        narration=dict(PROD_NARRATION_IDENTITY),
    )


def _guided_voiceover_strategy() -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        media_scope="all",
        execution_contract=GUIDED_VOICEOVER_EXECUTION_CONTRACT,
        selected_media_ids=ALL_MEDIA_IDS,
    )


def _narration_track() -> NarrationTrack:
    words = [
        NarrationWord(text="Every", start_s=0.2, end_s=0.6),
        NarrationWord(text="file", start_s=0.6, end_s=0.9),
        NarrationWord(text="tells", start_s=0.9, end_s=1.2),
        NarrationWord(text="the", start_s=1.2, end_s=1.35),
        NarrationWord(text="whole", start_s=1.35, end_s=1.7),
        NarrationWord(text="story", start_s=1.7, end_s=2.1),
    ]
    return NarrationTrack(
        gcs_path=str(PROD_NARRATION_IDENTITY["gcs_path"]),
        generation=str(PROD_NARRATION_IDENTITY["generation"]),
        duration_s=TOTAL_DURATION_S,
        words=words,
        language="en",
    )


def _approved_plan_bindings_and_visuals(
    narration: NarrationTrack,
) -> tuple[
    GuidedStoryExecutionPlan, tuple[PhoneSourceBinding, ...], tuple[PhoneVisualBinding, ...]
]:
    bindings = tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"users/creator/plan/item/{media_id}",
            generation="123",
            original=OriginalMediaDescriptor(
                sha256=str(index) * 64,
                byte_count=1000,
                duration_s=MOMENT_DURATION_S + 2,  # comfortable headroom, no refit
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for index, media_id in enumerate(CLIP_MEDIA_IDS, start=1)
    )
    visuals = tuple(
        PhoneVisualBinding(
            media_id=media_id,
            gcs_path=f"users/creator/plan/item/pool/{media_id}.jpg",
            generation="777",
            sha256=str(index) * 64,
            byte_count=2048,
        )
        for index, media_id in enumerate(PHOTO_MEDIA_IDS, start=1)
    )
    bindings_by_id = {binding.media_id: binding for binding in bindings}
    visuals_by_id = {visual.media_id: visual for visual in visuals}

    story_timeline = []
    cursor = 0.0
    for index, media_id in enumerate(ALL_MEDIA_IDS, start=1):
        is_video = media_id in bindings_by_id
        # Real approved plans persist source/output timestamps independently
        # rounded to milliseconds (see this module's own docstring on
        # `_TIMING_ROUNDING_TOLERANCE_S`) -- `round(x, 3)` at each step, so
        # the next moment's `output_start_s` chains from the ALREADY-ROUNDED
        # value instead of compounding raw float drift (4.8 isn't exactly
        # representable in binary). Skipping this rounding left
        # `resolved_duration_s` a ~1e-14 epsilon below the compiler's own
        # recomputed duration, and `compile_text_overlay` rounds a text
        # layer's `end` up to a clean value that then trips the pydantic
        # "text layer exceeds the timeline" check with zero tolerance.
        output_end = round(cursor + MOMENT_DURATION_S, 3)
        story_timeline.append(
            {
                "moment_id": f"moment-{index}",
                "beat_id": "beat",
                "topic": f"Moment {index}",
                "media_id": media_id,
                "lane": "clip" if is_video else "asset",
                "kind": "video" if is_video else "image",
                "gcs_path": (
                    bindings_by_id[media_id].proxy_path
                    if is_video
                    else visuals_by_id[media_id].gcs_path
                ),
                "generation": "123" if is_video else "777",
                "layout": "fullscreen",
                "source_start_s": 0,
                "source_end_s": MOMENT_DURATION_S,
                "output_start_s": cursor,
                "output_end_s": output_end,
                "duration_s": round(output_end - cursor, 3),
            }
        )
        cursor = output_end
    resolved_duration_s = cursor

    snapshot = EditProposalSnapshot.model_construct(narration=narration, font_family=None)
    title = TextElement(
        id="title",
        text="Every file, one story",
        start_s=0,
        end_s=resolved_duration_s,
        font_family="Inter-Bold",
        effect="static",
    ).model_dump(mode="json", exclude_none=True)
    captions = _narration_caption_elements(snapshot)
    assert captions  # the pinned words actually produced caption text_elements

    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 6,
            "proposal_version": 1,
            "media_digest": "a" * 64,
            "direction": "guided_story",
            "goal": "Show every uploaded file",
            "pace": "balanced",
            "approved_duration_s": resolved_duration_s,
            "resolved_duration_s": resolved_duration_s,
            "selected_media_ids": ALL_MEDIA_IDS,
            "story_timeline": story_timeline,
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": resolved_duration_s,
                    "resolved_duration_s": resolved_duration_s,
                    "start_s": 0,
                    "end_s": resolved_duration_s,
                }
            ],
            "text_elements": [title, *captions],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
            "narration": narration.model_dump(mode="json"),
        }
    )
    return plan, bindings, visuals


def _narration_bed(narration: NarrationTrack) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        plan_item_id="item-narrated-planned",
        generation=narration.generation,
        fingerprint=RenderFingerprint(sha256="b" * 64, byte_count=1_048_576),
        duration_s=narration.duration_s,
    )


def test_guided_voiceover_lane_renders_on_the_phone_under_prod_profile(prod_profile) -> None:
    manifest = _phone_manifest()
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is True

    plan_result = capabilities.compile_strategy_to_plan(manifest, _guided_voiceover_strategy())
    assert plan_result.strategy.render_program == "guided"

    narration = _narration_track()
    plan, bindings, visuals = _approved_plan_bindings_and_visuals(narration)
    bed = _narration_bed(narration)

    recipe = compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=bed)

    assert recipe.duration == pytest.approx(TOTAL_DURATION_S)
    voice_asset_id = f"voiceover-{bed.plan_item_id}"
    assert recipe.audio.narration_asset_id == voice_asset_id
    assert recipe.audio.original_volume == 0
    assert {"narrationAudio", "audioMix", "stillImages"} <= recipe.required_capabilities
    assert "stillImages" in recipe.required_capabilities
    validate_phone_pilot_recipe(recipe)  # must not raise under prod_profile's verified features
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_guided_voiceover_lane_is_blocked_when_its_own_flag_is_off(
    monkeypatch, prod_profile
) -> None:
    """The one flag this whole lane can roll back on its own, everything else
    held at prod defaults."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_guided_narration_rendering_enabled", False)
    assert phone_guided_narration_supported() is False

    manifest = _phone_manifest()
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is False
    assert (
        manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].reason_code
        == "unsupported_phone_audio"
    )

    narration = _narration_track()
    plan, bindings, visuals = _approved_plan_bindings_and_visuals(narration)
    bed = _narration_bed(narration)
    # The compiler itself has no opinion on the rollout flag -- it still
    # compiles when handed a bed directly (the worker is the one that
    # consults `phone_guided_narration_supported()` before ever calling it;
    # see `_run_phone_guided_job` and its dedicated dispatch tests). What
    # this flag actually blocks, end to end, is the manifest/dispatch layer
    # asserted above and in `test_content_plan_build.py`'s dispatch test.
    compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=bed)


def test_guided_voiceover_lane_is_blocked_without_narration_identity(
    monkeypatch, prod_profile
) -> None:
    """Cloud rule unchanged: `guided_voiceover_executable` still requires a
    resolved narration identity -- an item whose voiceover hasn't finished
    probing yet must not advertise the lane just because the phone rollout
    is fully on."""
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-narrated-planned",
        edit_format="narrated_planned",
        media=[{"media_id": media_id, "kind": "video"} for media_id in CLIP_MEDIA_IDS]
        + [{"media_id": media_id, "kind": "image"} for media_id in PHOTO_MEDIA_IDS],
        phone_source_media_ids=CLIP_MEDIA_IDS,
        phone_rendering_allowed=True,
        has_voiceover=True,
        # No narration identity supplied.
    )
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is False
    assert (
        manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].reason_code
        == "narration_identity_missing"
    )
    with pytest.raises(Exception):  # noqa: B017 - MixedMediaTimingUnavailableError family
        capabilities.compile_strategy_to_plan(manifest, _guided_voiceover_strategy())


def test_guided_voiceover_lane_fails_closed_without_verified_narration_audio(
    monkeypatch, prod_profile
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "phone_render_verified_features", ["stillImages"])
    assert phone_guided_narration_supported() is False

    manifest = _phone_manifest()
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].available is False
    assert (
        manifest.capabilities[capabilities.CAPABILITY_GUIDED_VOICEOVER].reason_code
        == "unsupported_phone_audio"
    )


def test_replay_fails_if_the_compiler_narration_gate_is_reverted() -> None:
    """Regression fence for gate #3 (`phone_guided_plan.py`): a narration bed
    that doesn't match the plan's pinned identity must still be rejected --
    proves this replay would have caught that gate being silently dropped."""
    narration = _narration_track()
    plan, bindings, visuals = _approved_plan_bindings_and_visuals(narration)
    stale_bed = _narration_bed(narration).model_copy(update={"generation": "stale"})
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=stale_bed)


# --- "Clean up speech" on the phone: the cleaned derivative is the bed ------
#
# The draft task cuts the confirmed preflight CutPlan out of the raw voiceover
# once and pins the resulting WAV as the approved narration. Planning,
# captions and the phone recipe then all see one timeline -- the cleaned one
# -- so the recipe keeps the exact single-narration-clip shape the iPhone
# already renders. The fixture is synthetic with the structure of the
# reported 48.6 s voiceover (9 keep segments, 41.59 s cleaned, 91 words).


def _probe_duration_s(path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _scheduled_cleaned_story(narration: NarrationTrack) -> tuple[dict, EditProposalSnapshot]:
    """Schedule and compile (v8) a guided story against the cleaned narration.

    Mirrors the planner's server half: the semantic plan names one chapter per
    upload in order and binds no on-screen text, so captions come only from
    the narration words.
    """
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"users/creator/plan/item/{media_id}",
            generation="123",
            kind="video",
            duration_s=5.0,
        )
        for media_id in CLIP_MEDIA_IDS
    ] + [
        MediaRef(
            lane="asset",
            media_id=media_id,
            gcs_path=f"users/creator/plan/item/pool/{media_id}.jpg",
            generation="777",
            kind="image",
        )
        for media_id in PHOTO_MEDIA_IDS
    ]
    beats = [
        StoryBeat(
            beat_id=f"beat-{index}",
            topic=f"Moment {index}",
            media_ids=[media_id],
            duration_s=round(narration.duration_s / len(ALL_MEDIA_IDS), 3),
        )
        for index, media_id in enumerate(ALL_MEDIA_IDS, start=1)
    ]
    seed = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the story of one landmark",
        duration_s=narration.duration_s,
        title="One light on the coast",
        media=media,
        story_beats=beats,
        narration=narration,
    )
    agent_input = snapshot_agent_input(seed, creator_request="A narrated story")
    assert agent_input.narration_duration_s == narration.duration_s
    semantic = SemanticEditPlan(
        title=seed.title,
        chapters=[
            SemanticChapter(
                chapter_id=beat.beat_id,
                topic=beat.topic,
                role="hook" if index == 0 else "payoff" if index == len(beats) - 1 else "build",
                weight=beat.duration_s,
                sources=[SemanticSource(media_id=media_id) for media_id in beat.media_ids],
            )
            for index, beat in enumerate(seed.story_beats)
        ],
        text_bindings=[],
    )
    scheduled = schedule_semantic_edit(semantic, agent_input)
    approved = EditProposalSnapshot.model_validate(
        {
            **seed.model_dump(mode="json"),
            "duration_s": scheduled.duration_s,
            "frame_schedule": scheduled.schedule.model_dump(),
            "story_beats": [beat.model_dump() for beat in scheduled.story_beats],
            "montage_text_bindings": [],
        }
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(approved.media, approved.narration),
        "approved_proposal": approved.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(ref, key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
            for ref in approved.media
        ],
    }
    return compile_execution_plan(guided, track=None), approved


def test_cleaned_derivative_compiles_single_clip_pilot_valid(
    monkeypatch, tmp_path, prod_profile
) -> None:
    fixture = load_fixture()
    owner_id, item_id, analysis_id = fixture_ids(fixture)
    root = tmp_path / "bucket"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(root))
    raw = raw_narration(fixture)
    write_tone_voiceover(root / raw.gcs_path, duration_s=raw.duration_s, generation=raw.generation)
    snapshot = hydrate_speech_cleanup_snapshot(fixture["snapshot"])
    kept_s = sum(end - start for start, end in snapshot.cut_plan.keep_segments)

    # The real cut: the raw AAC recording becomes a sample-exact WAV of the kept speech.
    cleaned = build_cleaned_narration(raw, fixture["snapshot"], owner_id=owner_id, item_id=item_id)
    wav = root / cleaned.gcs_path
    assert wav.read_bytes()[:4] == b"RIFF"
    assert _probe_duration_s(wav) == pytest.approx(kept_s, abs=0.05)
    assert cleaned.duration_s == pytest.approx(41.59, abs=0.05)
    assert len(cleaned.words) == fixture["expected"]["cleaned_word_count"]
    assert cleaned.words[-1].end_s <= cleaned.duration_s
    job_plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cleanup_preflight_contract": "snapshot_v1",
        "_speech_cleanup_internal": {"preflight_snapshot": fixture["snapshot"]},
    }
    assert require_guided_cleanup_binding(job_plan, cleaned)["analysis_attempt_id"] == analysis_id

    # Planning and compile v8 see the cleaned timeline, not the 48.6 s recording.
    plan, approved = _scheduled_cleaned_story(cleaned)
    assert plan["compiler_version"] == 8
    assert plan["resolved_duration_s"] == pytest.approx(cleaned.duration_s, abs=0.05)
    assert plan["narration"]["speech_cleanup"] == cleaned.speech_cleanup.model_dump(mode="json")
    captions = [
        element
        for element in plan["text_elements"]
        if (element.get("source_params") or {}).get("source") == "caption_cue"
    ]
    assert captions
    assert max(element["end_s"] for element in captions) == pytest.approx(
        fixture["expected"]["cleaned_last_word_end_s"], abs=0.01
    )
    assert [element["id"] for element in plan["text_elements"] if element not in captions] == [
        "guided-title"
    ]

    # The phone recipe plays the derivative as ONE narration clip, pilot-valid in prod.
    media_by_id = {ref.media_id: ref for ref in approved.media}
    bindings, visuals = [], []
    for index, moment in enumerate(plan["story_timeline"], start=1):
        ref = media_by_id[moment["media_id"]]
        if ref.kind == "video":
            bindings.append(
                PhoneSourceBinding(
                    media_id=ref.media_id,
                    proxy_path=moment["gcs_path"],
                    generation=str(moment["generation"]),
                    original=OriginalMediaDescriptor(
                        sha256=format(index, "x") * 64,
                        byte_count=1000,
                        duration_s=ref.duration_s,
                        width=1080,
                        height=1920,
                        has_audio=True,
                    ),
                )
            )
        else:
            visuals.append(
                PhoneVisualBinding(
                    media_id=ref.media_id,
                    gcs_path=moment["gcs_path"],
                    generation=str(moment["generation"]),
                    sha256=format(index, "x") * 64,
                    byte_count=2048,
                )
            )
    bed = PhoneNarrationBed(
        plan_item_id=str(item_id),
        generation=cleaned.generation,
        fingerprint=RenderFingerprint(
            sha256=hashlib.sha256(wav.read_bytes()).hexdigest(), byte_count=wav.stat().st_size
        ),
        duration_s=cleaned.duration_s,
    )
    guided_plan = GuidedStoryExecutionPlan.model_validate(plan)

    recipe = compile_phone_guided_plan(
        guided_plan, tuple(bindings), visuals=tuple(visuals), narration=bed
    )

    validate_phone_pilot_recipe(recipe)
    assert recipe.duration == pytest.approx(plan["resolved_duration_s"])
    [narration_track] = [track for track in recipe.tracks if track.id == "narration"]
    [clip] = narration_track.clips
    assert (clip.source_start, clip.timeline_start) == (0, 0)
    assert clip.source_duration == pytest.approx(cleaned.duration_s)
    assert recipe.audio.narration_asset_id == f"voiceover-{item_id}"
    assert recipe.audio.original_volume == 0
    # The raw 48.6 s recording can never stand in for the approved derivative.
    raw_bed = bed.model_copy(update={"generation": raw.generation, "duration_s": raw.duration_s})
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        compile_phone_guided_plan(
            guided_plan, tuple(bindings), visuals=tuple(visuals), narration=raw_bed
        )
