from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.active_narration_source import (
    ActiveNarrationRequest,
    RegisteredNarrationMedia,
    resolve_active_narration_source,
)


def media(
    media_id: str,
    *,
    kind: str = "video",
    generation: str = "1",
    duration_s: float = 30.0,
    has_audio: bool = True,
    coverage: float | None = None,
    foreground: bool = True,
    trim_start_s: float = 0.0,
    trim_end_s: float | None = None,
) -> RegisteredNarrationMedia:
    return RegisteredNarrationMedia(
        media_id=media_id,
        storage_path=f"users/u/{media_id}",
        generation=generation,
        media_kind=kind,  # type: ignore[arg-type]
        duration_s=duration_s,
        has_audio=has_audio,
        speech_coverage=coverage,
        foreground=foreground,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        manifest_identity=f"assignment:{media_id}",
    )


def request(**changes: object) -> ActiveNarrationRequest:
    values = {
        "edit_format": "narrated",
        "audio_mode": "kria",
        # Opaque fixture value, not DETECTOR_VERSION; must differ from the v2 literal below.
        "detector_policy": "mixed-gap-v1:apply",
        "clips": (),
        "voiceover": None,
        "pinned_spine_media_id": None,
        "target_duration_s": None,
    }
    values.update(changes)
    return ActiveNarrationRequest(**values)  # type: ignore[arg-type]


def test_active_uploaded_or_recorded_voiceover_wins_and_ignores_visual_changes() -> None:
    voiceover = media("take.webm", kind="audio", duration_s=42.0)
    first = resolve_active_narration_source(
        request(
            audio_mode="voiceover",
            voiceover=voiceover,
            clips=(media("low.mp4", coverage=0.2), media("high.mp4", coverage=0.99)),
        )
    )
    reordered = resolve_active_narration_source(
        request(
            audio_mode="voiceover",
            voiceover=replace(voiceover, speech_coverage=0.01),
            clips=(
                media("new-visual.mp4", has_audio=False),
                media("high.mp4", coverage=0.99),
                media("low.mp4", coverage=0.2),
            ),
        )
    )

    assert first.source is not None
    assert reordered.source is not None
    assert first.source.source_kind == "voiceover"
    assert first.source.media_id == "take.webm"
    assert first.source.resolved_renderer == "narrated"
    assert first.source.window_duration_s == pytest.approx(42.0)
    assert first.source.source_policy_fingerprint == reordered.source.source_policy_fingerprint


def test_standalone_voiceover_is_analyzable_without_video() -> None:
    result = resolve_active_narration_source(
        request(
            audio_mode="voiceover",
            voiceover=media("take.m4a", kind="audio", duration_s=18.0),
        )
    )

    assert result.available is True
    assert result.video_present is False
    assert result.source is not None
    assert result.source.media_kind == "audio"


def test_standalone_audio_snapshot_is_reused_when_video_is_attached_later() -> None:
    voiceover = media("take.m4a", kind="audio", duration_s=18.0)
    audio_only = resolve_active_narration_source(
        request(audio_mode="voiceover", voiceover=voiceover)
    )
    with_video = resolve_active_narration_source(
        request(
            audio_mode="voiceover",
            voiceover=voiceover,
            clips=(media("visual.mp4", has_audio=False),),
        )
    )

    assert audio_only.source is not None
    assert with_video.source is not None
    assert audio_only.video_present is False
    assert with_video.video_present is True
    assert (
        audio_only.source.source_policy_fingerprint == with_video.source.source_policy_fingerprint
    )


def test_inactive_voiceover_does_not_displace_embedded_self_narration() -> None:
    embedded = media("camera.mp4")
    result = resolve_active_narration_source(
        request(
            edit_format="subtitled",
            audio_mode="original",
            voiceover=media("old-take.webm", kind="audio"),
            clips=(embedded,),
        )
    )

    assert result.source is not None
    assert result.source.source_kind == "embedded_spine"
    assert result.source.media_id == embedded.media_id
    assert result.source.resolved_renderer == "subtitled"


def test_narrated_single_video_uses_its_embedded_audio_even_without_voiceover() -> None:
    result = resolve_active_narration_source(
        request(audio_mode="voiceover", clips=(media("walkthrough.mp4"),))
    )

    assert result.source is not None
    assert result.source.media_id == "walkthrough.mp4"
    assert result.source.resolved_renderer == "subtitled"


def test_multi_clip_spine_matches_renderer_coverage_and_ignores_background() -> None:
    result = resolve_active_narration_source(
        request(
            edit_format="talking_head",
            clips=(
                media("first.mp4", coverage=0.2),
                media("speaker.mp4", coverage=0.8),
                media("background.mp4", coverage=1.0, foreground=False),
            ),
        )
    )

    assert result.source is not None
    assert result.source.media_id == "speaker.mp4"
    assert result.source.source_kind == "embedded_spine"
    assert result.source.resolved_renderer == "talking_head"


def test_explicit_renderer_spine_is_never_replaced_by_a_higher_scored_clip() -> None:
    result = resolve_active_narration_source(
        request(
            edit_format="talking_head",
            clips=(media("pinned.mp4"), media("other.mp4")),
            pinned_spine_media_id="pinned.mp4",
        )
    )

    assert result.source is not None
    assert result.source.media_id == "pinned.mp4"


@pytest.mark.parametrize(
    ("clips", "reason"),
    [
        ((), "no_media"),
        ((media("silent.mp4", has_audio=False),), "no_audio"),
        ((media("unverified.mp4", generation="", coverage=0.9),), "unverified_generation"),
        (
            (media("one.mp4", coverage=0.4), media("two.mp4", coverage=None)),
            "speech_coverage_unavailable",
        ),
        (
            (media("one.mp4", coverage=0.14), media("two.mp4", coverage=0.1)),
            "no_speech_spine",
        ),
    ],
)
def test_unavailable_sources_are_typed(
    clips: tuple[RegisteredNarrationMedia, ...], reason: str
) -> None:
    result = resolve_active_narration_source(request(edit_format="talking_head", clips=clips))

    assert result.available is False
    assert result.reason == reason


def test_subtitled_never_guesses_between_multiple_embedded_sources() -> None:
    result = resolve_active_narration_source(
        request(edit_format="subtitled", clips=(media("one.mp4"), media("two.mp4")))
    )

    assert result.source is None
    assert result.reason == "ambiguous_embedded_source"


def test_talking_head_window_is_trimmed_then_capped_by_renderer_policy() -> None:
    result = resolve_active_narration_source(
        request(
            edit_format="talking_head",
            target_duration_s=30.0,
            clips=(
                media(
                    "long.mp4",
                    duration_s=500.0,
                    coverage=0.9,
                    trim_start_s=10.0,
                    trim_end_s=450.0,
                ),
            ),
        )
    )

    assert result.source is not None
    # min(max(target * 2, 120), 300), relative to the active trim start.
    assert (result.source.window_start_s, result.source.window_end_s) == (10.0, 130.0)


@pytest.mark.parametrize(
    ("edit_format", "audio_mode", "voiceover"),
    [
        ("subtitled", "kria", None),
        ("narrated", "voiceover", media("take.wav", kind="audio", duration_s=900.0)),
    ],
)
def test_every_source_kind_is_bounded_to_the_global_analysis_window(
    edit_format: str,
    audio_mode: str,
    voiceover: RegisteredNarrationMedia | None,
) -> None:
    clips = () if voiceover is not None else (media("long.mp4", duration_s=900.0),)
    result = resolve_active_narration_source(
        request(
            edit_format=edit_format,
            audio_mode=audio_mode,
            voiceover=voiceover,
            clips=clips,
        )
    )

    assert result.source is not None
    assert result.source.window_duration_s == 300.0


@pytest.mark.parametrize(
    "mutation",
    [
        {"generation": "2"},
        {"trim_end_s": 20.0},
    ],
)
def test_selected_source_generation_and_window_change_the_fingerprint(
    mutation: dict[str, object],
) -> None:
    original = media("camera.mp4", trim_end_s=25.0)
    before = resolve_active_narration_source(
        request(edit_format="subtitled", clips=(original,))
    ).source
    after = resolve_active_narration_source(
        request(edit_format="subtitled", clips=(replace(original, **mutation),))
    ).source

    assert before is not None and after is not None
    assert before.source_policy_fingerprint != after.source_policy_fingerprint


def test_format_audio_and_detector_policy_change_the_fingerprint() -> None:
    clip = media("camera.mp4")
    base = request(edit_format="subtitled", audio_mode="kria", clips=(clip,))

    def fingerprint(candidate: ActiveNarrationRequest) -> str:
        source = resolve_active_narration_source(candidate).source
        assert source is not None
        return source.source_policy_fingerprint

    fingerprints = {
        fingerprint(base),
        fingerprint(replace(base, edit_format="narrated")),
        fingerprint(replace(base, audio_mode="original")),
        fingerprint(replace(base, detector_policy="mixed-gap-v2:apply")),
    }

    assert len(fingerprints) == 4


def test_private_source_snapshot_round_trips_exact_identity_but_not_supporting_media() -> None:
    result = resolve_active_narration_source(
        request(edit_format="subtitled", clips=(media("camera.mp4"),))
    )

    assert result.source is not None
    snapshot = result.source.to_private_dict()
    assert snapshot["storage_path"] == "users/u/camera.mp4"
    assert snapshot["generation"] == "1"
    assert snapshot["source_policy_fingerprint"] == result.source.source_policy_fingerprint
