"""Why an idea may cite a photo only when `stills_buildable()` says so (KRI-944).

The phone writer renders a pinned still next to video cuts, but every prepared camera-roll story
is also checked against its sources after export, and that check reads original videos only.
These pin both facts and the coupling between them, so the gate cannot drift from the verifier.
"""

import uuid

import pytest

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MontageAudioPlan,
    StoryBeat,
)
from app.services import actual_export_verification as verifier
from app.services.media_moments import STILL_HOLD_S, stills_buildable
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from app.services.proposal_planning import refresh_snapshot_schedule


def photo_and_video_recipe():
    """The recipe a story of video, photo, video, photo compiles to on the phone writer."""
    refs, cuts, beats, bindings, visuals = {}, [], [], [], []
    kinds = ["video", "image", "video", "image"]
    for index, kind in enumerate(kinds):
        media_id = str(uuid.uuid4())
        if kind == "video":
            path = f"users/u/media-library/analysis-proxy-{media_id}.mp4"
            refs[media_id] = MediaRef(
                lane="clip",
                media_id=media_id,
                gcs_path=path,
                generation="42",
                kind="video",
                duration_s=12.0,
                content_hash="a" * 64,
            )
            bindings.append(
                PhoneSourceBinding(
                    media_id=media_id,
                    proxy_path=path,
                    generation="42",
                    original=OriginalMediaDescriptor(
                        sha256="a" * 64,
                        byte_count=1000,
                        duration_s=12,
                        width=1080,
                        height=1920,
                        has_audio=True,
                    ),
                )
            )
            start, end = 1.0, 5.0
        else:
            path = f"users/u/media-library/{media_id}.jpg"
            refs[media_id] = MediaRef(
                lane="asset",
                media_id=media_id,
                gcs_path=path,
                generation="42",
                kind="image",
                content_hash="b" * 64,
            )
            visuals.append(
                PhoneVisualBinding(
                    media_id=media_id,
                    gcs_path=path,
                    generation="42",
                    sha256="b" * 64,
                    byte_count=2048,
                )
            )
            start, end = 0.0, STILL_HOLD_S
        role = "hook" if index == 0 else "payoff" if index == len(kinds) - 1 else "build"
        cut_id = str(uuid.uuid4())
        cuts.append(
            FastMontageCut(
                cut_id=cut_id,
                media_id=media_id,
                source_start_s=start,
                source_end_s=end,
                output_duration_s=end - start,
                role=role,
            )
        )
        beats.append(
            StoryBeat(
                beat_id=cut_id, topic=role, thought="", media_ids=[media_id], duration_s=end - start
            )
        )
    snapshot = refresh_snapshot_schedule(
        EditProposalSnapshot(
            direction="fast_montage",
            goal="A day out",
            title="A day out",
            text_policy="none",
            duration_s=sum(cut.output_duration_s for cut in cuts),
            media=list(refs.values()),
            story_beats=beats,
            fast_cuts=cuts,
            video_reuse_policy="allow_repeat",
            montage_audio=MontageAudioPlan(
                preserve_source_audio=True,
                source_media_ids=[m for m, ref in refs.items() if ref.kind == "video"],
            ),
            output_orientation="portrait",
        )
    )
    plan = GuidedStoryExecutionPlan.model_validate(compile_proposal_execution_plan(snapshot))
    return compile_phone_guided_plan(plan, tuple(bindings), tuple(visuals))


def test_the_phone_writer_renders_photos_between_videos_but_the_verifier_cannot_check_them():
    recipe = photo_and_video_recipe()
    clips = recipe.tracks[0].clips
    stills = [clip for clip in clips if clip.source_asset_id.startswith("visual-")]
    assert len(clips) == 4 and len(stills) == 2
    assert all(clip.source_duration == STILL_HOLD_S for clip in stills)
    assert "stillImages" in recipe.required_capabilities
    # The verifier either covers a still or declares it unsupported, never half of each.
    assert (verifier.unsupported_reasons(recipe) == []) is verifier.VERIFIES_STILL_PICTURES
    if not verifier.VERIFIES_STILL_PICTURES:
        assert verifier.unsupported_reasons(recipe) == ["non_original_picture"]


@pytest.mark.parametrize("covered", [False, True])
@pytest.mark.parametrize("device_verified", [False, True])
def test_a_story_may_cite_a_photo_only_with_both_the_device_and_the_verifier(
    monkeypatch, covered, device_verified
):
    monkeypatch.setattr(verifier, "VERIFIES_STILL_PICTURES", covered)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["stillImages"] if device_verified else []
    )
    assert stills_buildable() is (covered and device_verified)
