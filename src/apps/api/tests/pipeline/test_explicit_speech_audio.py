"""Explicit spoken-word cuts must never borrow their removed audio back in."""

from __future__ import annotations

from app.kria.recipes import AssetFingerprint, Canvas, MediaAsset, TimelineClip, TimelineTrack
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import (
    OriginalRenderAsset,
    ProcessedAudioRenderAsset,
    RenderAssetManifest,
    RenderFingerprint,
)
from app.pipeline.explicit_speech_audio import (
    EXPLICIT_SPEECH_AUDIO_FADE_S,
    protect_explicit_speech_cut_audio,
)


def _recipe() -> EditRecipeV2:
    fingerprint = "a" * 64
    assets = [
        MediaAsset(
            id="speaker",
            relative_path="speaker",
            fingerprint=AssetFingerprint(hex=fingerprint, byte_count=1),
        )
    ]
    manifest_assets = [
        OriginalRenderAsset(
            id="speaker",
            media_id="speaker",
            fingerprint=RenderFingerprint(sha256=fingerprint, byte_count=1),
        )
    ]
    tracks = [
        TimelineTrack(
            id="subtitled",
            kind="video",
            clips=[
                TimelineClip(
                    id="before",
                    source_asset_id="speaker",
                    source_start=0,
                    source_duration=9.22,
                    timeline_start=0,
                    rate=1,
                ),
                TimelineClip(
                    id="after",
                    source_asset_id="speaker",
                    source_start=9.70,
                    source_duration=2,
                    timeline_start=9.22,
                    rate=1,
                ),
            ],
        ),
        TimelineTrack(
            id="unrelated",
            kind="video",
            clips=[
                TimelineClip(
                    id="other",
                    source_asset_id="speaker",
                    source_start=12,
                    source_duration=1,
                    timeline_start=11.22,
                    rate=1,
                )
            ],
        ),
    ]
    return EditRecipeV2(
        canvas=Canvas(width=1080, height=1920),
        assets=assets,
        asset_manifest=RenderAssetManifest(assets=tuple(manifest_assets)),
        tracks=tracks,
    )


def test_selected_source_edges_get_short_fades_and_other_edges_do_not() -> None:
    recipe = _recipe()

    protected = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id="speaker",
        cuts=[{"status": "applied", "source_span": {"start_s": 9.22, "end_s": 9.70}}],
    )

    video = next(track for track in protected.tracks if track.id == "subtitled")
    assert video.clips[0].audio_fade_out == EXPLICIT_SPEECH_AUDIO_FADE_S
    assert video.clips[1].audio_fade_in == EXPLICIT_SPEECH_AUDIO_FADE_S
    unrelated = next(track for track in protected.tracks if track.id == "unrelated")
    assert unrelated.clips[0].audio_fade_in is None
    assert unrelated.clips[0].audio_fade_out is None


def test_no_applied_cut_or_wrong_source_is_byte_identical() -> None:
    recipe = _recipe()

    assert protect_explicit_speech_cut_audio(recipe, source_media_id="speaker", cuts=[]) is recipe
    assert (
        protect_explicit_speech_cut_audio(
            recipe,
            source_media_id="other",
            cuts=[{"status": "applied", "source_span": {"start_s": 9.22, "end_s": 9.70}}],
        )
        is recipe
    )


def test_existing_longer_fades_are_preserved() -> None:
    recipe = _recipe()
    track = recipe.tracks[0]
    track.clips[0] = track.clips[0].model_copy(update={"audio_fade_out": 0.2})

    protected = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id="speaker",
        cuts=[{"status": "applied", "source_start_s": 9.22, "source_end_s": 9.70}],
    )

    assert protected.tracks[0].clips[0].audio_fade_out == 0.2
    assert protected.tracks[0].clips[1].audio_fade_in == EXPLICIT_SPEECH_AUDIO_FADE_S


def test_adjacent_automatic_removal_edges_cannot_borrow_into_selected_word() -> None:
    recipe = _recipe()
    track = recipe.tracks[0]
    track.clips[0] = track.clips[0].model_copy(update={"source_duration": 9.205})
    track.clips[1] = track.clips[1].model_copy(
        update={"source_start": 9.715, "source_duration": 1.985}
    )

    protected = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id="speaker",
        cuts=[{"status": "applied", "source_span": {"start_s": 9.22, "end_s": 9.70}}],
    )

    assert protected.tracks[0].clips[0].audio_fade_out == EXPLICIT_SPEECH_AUDIO_FADE_S
    assert protected.tracks[0].clips[1].audio_fade_in == EXPLICIT_SPEECH_AUDIO_FADE_S


def test_fade_never_exceeds_a_short_kept_clip() -> None:
    recipe = _recipe()
    recipe.tracks[0].clips[0] = (
        recipe.tracks[0]
        .clips[0]
        .model_copy(update={"source_start": 9.218, "source_duration": 0.002})
    )

    protected = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id="speaker",
        cuts=[{"status": "applied", "source_span": {"start_s": 9.22, "end_s": 9.70}}],
    )

    assert protected.tracks[0].clips[0].audio_fade_out == 0.002


def test_processed_audio_track_gets_the_same_selected_edge_protection() -> None:
    recipe = _recipe()
    fingerprint = "b" * 64
    processed_asset = ProcessedAudioRenderAsset(
        id="processed-speaker",
        job_id="job",
        variant_id="variant",
        render_generation="generation",
        source_media_id="speaker",
        source_generation="source-generation",
        generation="processed-generation",
        fingerprint=RenderFingerprint(sha256=fingerprint, byte_count=1),
    )
    processed_track = TimelineTrack(
        id="processed-source-audio",
        kind="audio",
        clips=[
            TimelineClip(
                id="processed-before",
                source_asset_id="processed-speaker",
                source_start=0,
                source_duration=9.22,
                timeline_start=0,
                rate=1,
            ),
            TimelineClip(
                id="processed-after",
                source_asset_id="processed-speaker",
                source_start=9.70,
                source_duration=2,
                timeline_start=9.22,
                rate=1,
            ),
        ],
    )
    payload = recipe.model_dump()
    payload.update(
        assets=[
            *recipe.assets,
            MediaAsset(
                id="processed-speaker",
                relative_path="processed-speaker",
                fingerprint=AssetFingerprint(hex=fingerprint, byte_count=1),
            ),
        ],
        asset_manifest=RenderAssetManifest(assets=(*recipe.asset_manifest.assets, processed_asset)),
        tracks=[*recipe.tracks, processed_track],
    )
    recipe = EditRecipeV2(**payload)

    protected = protect_explicit_speech_cut_audio(
        recipe,
        source_media_id="speaker",
        cuts=[{"status": "applied", "source_span": {"start_s": 9.22, "end_s": 9.70}}],
    )

    track = next(track for track in protected.tracks if track.id == "processed-source-audio")
    assert track.clips[0].audio_fade_out == EXPLICIT_SPEECH_AUDIO_FADE_S
    assert track.clips[1].audio_fade_in == EXPLICIT_SPEECH_AUDIO_FADE_S
