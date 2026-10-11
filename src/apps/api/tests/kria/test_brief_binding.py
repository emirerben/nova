"""Failure modes: later revisions and malformed/foreign pins cannot alter approval."""

import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding


def test_pin_is_immutable_and_detects_tampering():
    thread = uuid.uuid4()
    brief = CreativeBrief(
        version=3,
        requirements=[BriefRequirement(id="r1", kind="text", scope="title", literal="Approved")],
    )
    pin = BriefBinding.create(thread, brief, latest_message="Keep this exactly")
    brief.requirements[0].literal = "Later revision"
    assert pin.resolve(thread).requirements[0].literal == "Approved"
    raw = pin.model_dump(mode="json")
    raw["brief"]["requirements"][0]["literal"] = "Tampered"
    with pytest.raises(ValueError):
        BriefBinding.model_validate(raw)
    with pytest.raises(ValueError):
        pin.resolve(uuid.uuid4())


def test_explicit_no_brief_is_distinct_from_legacy_absence():
    thread = uuid.uuid4()
    pin = BriefBinding.create(thread, None)
    assert pin.state == "none"
    assert pin.resolve(thread) is None
    assert pin.digest


def test_media_binding_pins_versions_but_ignores_later_analysis_for_identity():
    from types import SimpleNamespace

    from app.kria.brief_binding import media_identity, snapshot_media

    item = SimpleNamespace(
        clip_gcs_paths=["clips/one.mp4"],
        clip_assignments=[
            {
                "media_id": "one",
                "gcs_path": "clips/one.mp4",
                "storage_generation": "7",
                "analysis": {"summary": "football"},
            }
        ],
        voiceover_gcs_path="voice.wav",
        voiceover_generation="3",
    )
    pin = BriefBinding.create(
        uuid.uuid4(), None, latest_message="Football first", media_snapshot=snapshot_media(item)
    )
    item.clip_assignments[0]["analysis"]["summary"] = "later answer"
    assert pin.media_snapshot["clip_assignments"][0]["analysis"]["summary"] == "football"
    assert media_identity(pin.media_snapshot) == media_identity(snapshot_media(item))
    item.clip_assignments[0]["storage_generation"] = "8"
    assert media_identity(pin.media_snapshot) != media_identity(snapshot_media(item))
    raw = pin.model_dump(mode="json")
    raw["media_snapshot"]["clip_assignments"][0]["storage_generation"] = "99"
    with pytest.raises(ValueError, match="digest"):
        BriefBinding.model_validate(raw)


def test_media_binding_pins_server_recorded_song_and_voice_capacity_not_target():
    from types import SimpleNamespace

    from app.kria.brief_binding import media_identity, snapshot_media

    item = SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path="voice.wav",
        voiceover_generation="9",
        voiceover_duration_s=31.25,
        song_gcs_path="song.m4a",
        song_generation=8,
        song_duration_s=42.0,
        song_analysis={
            "status": "ready",
            "duration_s": 42.0,
            "lines": [{"start_s": 4.0, "end_s": 12.5, "text": "selected section"}],
        },
        song_alignment={"takes": {"clip-1": {"delta_s": 4.0}}},
        # A model target must never become source capacity.
        target_duration_s=99,
    )

    snapshot = snapshot_media(item)
    assert snapshot["voiceover_duration_s"] == 31.25
    assert snapshot["voiceover"] == {"duration_s": 31.25}
    assert snapshot["song_duration_s"] == 42.0
    assert snapshot["song_analysis"]["lines"][0]["start_s"] == 4.0
    assert snapshot["song_alignment"]["takes"]["clip-1"]["delta_s"] == 4.0
    assert "target_duration_s" not in snapshot

    # Timing evidence participates in the immutable binding digest, but does
    # not turn analysis into source identity for stale-media checks.
    pin = BriefBinding.create(uuid.uuid4(), None, media_snapshot=snapshot)
    item.song_duration_s = 43.0
    assert pin.media_snapshot["song_duration_s"] == 42.0
    assert media_identity(pin.media_snapshot) == media_identity(snapshot_media(item))
    altered = pin.model_dump(mode="json")
    altered["media_snapshot"]["song_duration_s"] = 43.0
    with pytest.raises(ValueError, match="digest"):
        BriefBinding.model_validate(altered)


def test_media_binding_leaves_unknown_source_capacity_unknown():
    from types import SimpleNamespace

    from app.kria.brief_binding import snapshot_media

    snapshot = snapshot_media(
        SimpleNamespace(
            clip_gcs_paths=[],
            clip_assignments=[],
            voiceover_gcs_path="voice.wav",
            voiceover_generation="9",
            voiceover_duration_s=None,
            song_gcs_path="song.m4a",
            song_generation=8,
            song_duration_s=None,
            song_analysis={"status": "pending"},
            song_alignment=None,
        )
    )
    assert snapshot["voiceover"] is None
    assert snapshot["voiceover_duration_s"] is None
    assert snapshot["song_duration_s"] is None
    assert snapshot["song_analysis"] == {"status": "pending"}


def test_legacy_draft_hash_does_not_gain_binding_fields():
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot

    raw, _ = canonical_snapshot(KriaDraftDocument(kind="initial"))
    assert "brief_binding" not in raw
    assert "brief_coverage" not in raw
