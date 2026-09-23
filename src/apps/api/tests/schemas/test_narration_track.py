import math

import pytest
from pydantic import ValidationError

from app.schemas.edit_proposal import NarrationTrack


def test_narration_track_forbids_extra_fields_and_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        NarrationTrack(
            gcs_path="voiceover/a.m4a",
            generation="7",
            duration_s=2.0,
            words=[],
            unexpected="ignored",
        )
    with pytest.raises(ValidationError):
        NarrationTrack(gcs_path="voiceover/a.m4a", generation="7", duration_s=math.inf)


def test_narration_words_only_require_monotonic_starts() -> None:
    track = NarrationTrack(
        gcs_path="voiceover/a.m4a",
        generation="7",
        duration_s=2.0,
        words=[
            {"text": "one", "start_s": 0.0, "end_s": 0.8},
            # Whisper streams can overlap word end times slightly.
            {"text": "two", "start_s": 0.7, "end_s": 1.2},
        ],
    )
    assert len(track.words) == 2


def test_point_timestamps_preserve_words_in_adjacent_caption_cues():
    from types import SimpleNamespace

    from app.pipeline.guided_story import _narration_caption_elements

    track = NarrationTrack(
        gcs_path="voice",
        generation="1",
        duration_s=3,
        words=[
            {"text": "In", "start_s": 0, "end_s": 0.5},
            {"text": "Michigan.", "start_s": 0.5, "end_s": 0.5},
            {"text": "We", "start_s": 1, "end_s": 1},
            {"text": "played.", "start_s": 1, "end_s": 1.5},
        ],
    )
    cues = _narration_caption_elements(SimpleNamespace(narration=track, font_family=None))
    assert [cue["text"] for cue in cues] == ["In Michigan.", "We played."]
    assert [(cue["start_s"], cue["end_s"]) for cue in cues] == [(0, 0.5), (1, 1.5)]
    with pytest.raises(ValidationError):
        track.model_validate(
            {**track.model_dump(), "words": [{"text": "bad", "start_s": 1, "end_s": 0.5}]}
        )


_PROVENANCE = {
    "analysis_id": "0f0f0f0f-3333-4333-8333-333333333333",
    "source_gcs_path": "users/u/plan/i/voiceover.m4a",
    "source_generation": "1700000000000001",
    "source_duration_s": 48.618667,
    "cut_sha256": "a" * 64,
}


def _legacy_digest(media, narration: NarrationTrack) -> str:
    """The exact pre-provenance ``canonical_media_digest`` formula."""

    import hashlib
    import json

    identities = sorted(
        (
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
                "content_hash": ref.content_hash or "",
            }
            for ref in media
        ),
        key=lambda row: (row["lane"], row["media_id"]),
    )
    payload = json.dumps(
        {
            "media": identities,
            "narration": {
                "gcs_path": narration.gcs_path,
                "generation": narration.generation,
                "duration_s": round(float(narration.duration_s), 6),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _media():
    from app.schemas.edit_proposal import MediaRef

    return [
        MediaRef(
            lane="clip",
            media_id="clip-1",
            gcs_path="users/u/plan/i/clip-1.mp4",
            generation="3",
            kind="video",
            duration_s=5.0,
        )
    ]


def test_cleaned_narration_provenance_validates_and_round_trips() -> None:
    track = NarrationTrack(
        gcs_path="users/u/plan/i/speech-cleanup/a/" + "b" * 32 + ".wav",
        generation="9",
        duration_s=41.59,
        caption_style="sentence",
        speech_cleanup=_PROVENANCE,
    )

    dumped = track.model_dump(mode="json")
    assert dumped["speech_cleanup"] == _PROVENANCE
    assert NarrationTrack.model_validate(dumped) == track
    for field, value in (
        ("analysis_id", "not-a-uuid"),
        ("cut_sha256", "A" * 64),
        ("source_duration_s", 0),
        ("source_generation", ""),
    ):
        with pytest.raises(ValidationError):
            NarrationTrack.model_validate(
                {**dumped, "speech_cleanup": {**_PROVENANCE, field: value}}
            )
    with pytest.raises(ValidationError):
        NarrationTrack.model_validate(
            {**dumped, "speech_cleanup": {**_PROVENANCE, "unexpected": 1}}
        )


def test_legacy_narration_dump_and_digest_stay_byte_identical() -> None:
    import json

    from app.schemas.edit_proposal import canonical_media_digest

    track = NarrationTrack(
        gcs_path="users/u/plan/i/voiceover.m4a",
        generation="1700000000000001",
        duration_s=48.618667,
        words=[{"text": "Hello.", "start_s": 0.1, "end_s": 0.5}],
        language="en",
    )

    assert json.dumps(track.model_dump(mode="json"), sort_keys=True) == json.dumps(
        {
            "gcs_path": "users/u/plan/i/voiceover.m4a",
            "generation": "1700000000000001",
            "duration_s": 48.618667,
            "words": [{"text": "Hello.", "start_s": 0.1, "end_s": 0.5, "confidence": 1.0}],
            "language": "en",
        },
        sort_keys=True,
    )
    assert "speech_cleanup" not in track.model_dump_json()
    assert canonical_media_digest(_media(), track) == _legacy_digest(_media(), track)


def test_digest_pins_cleaned_narration_provenance() -> None:
    from app.schemas.edit_proposal import canonical_media_digest

    cleaned = NarrationTrack(
        gcs_path="users/u/plan/i/speech-cleanup/a/" + "b" * 32 + ".wav",
        generation="9",
        duration_s=41.59,
        speech_cleanup=_PROVENANCE,
    )
    bare = cleaned.model_copy(update={"speech_cleanup": None})
    other_cut = cleaned.model_copy(
        update={
            "speech_cleanup": cleaned.speech_cleanup.model_copy(update={"cut_sha256": "c" * 64})
        }
    )
    other_source = cleaned.model_copy(
        update={
            "speech_cleanup": cleaned.speech_cleanup.model_copy(
                update={"source_generation": "1700000000000002"}
            )
        }
    )

    digests = {
        canonical_media_digest(_media(), track)
        for track in (cleaned, bare, other_cut, other_source)
    }
    assert len(digests) == 4
    assert canonical_media_digest(_media(), bare) == _legacy_digest(_media(), bare)
