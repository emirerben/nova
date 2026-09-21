"""Shared clip understanding record (KRI-127): one projection for every reader."""

from types import SimpleNamespace

from app.schemas.clip_understanding import UNDERSTANDING_KEY, ClipUnderstanding
from app.services.clip_understanding import clip_record, understanding_payload


def _meta(**overrides):
    base = {
        "detected_subject": "people playing volleyball",
        "transcript": "okay so this is the final point",
        "summary": "Friends play a volleyball match on a sand court.",
        "setting": "outdoor sand volleyball court in a city park",
        "activity": "playing volleyball",
        "people_count": 6,
        "speaks_to_camera": True,
        "people_note": "one man faces the camera and narrates",
        "brands": ["Mikasa"],
        "content_type": "action",
        "audio_type": "dialogue",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_payload_round_trips_through_clip_record():
    moments = [{"start_s": 1.0, "end_s": 3.5, "energy": 7.0, "description": "spike at the net"}]
    analysis = {
        "subject": "people playing volleyball",
        "description": "spike at the net",
        UNDERSTANDING_KEY: understanding_payload(_meta(), best_moments=moments),
    }

    record = clip_record(analysis, kind="video")

    assert record.activity == "playing volleyball"
    assert record.setting.startswith("outdoor sand volleyball court")
    assert record.people.count == 6
    assert record.people.speaks_to_camera is True
    assert record.speech.has_speech is True
    assert record.speech.to_camera is True
    assert record.brands == ["Mikasa"]
    assert record.content_type == "action"
    assert record.notable_moments[0].description == "spike at the net"


def test_speech_to_camera_needs_actual_speech():
    payload = understanding_payload(_meta(transcript="", speaks_to_camera=True))

    assert payload["speech"]["has_speech"] is False
    assert payload["speech"]["to_camera"] is False


def test_payload_tolerates_a_bare_legacy_meta():
    payload = understanding_payload(SimpleNamespace(detected_subject="man on grass"))

    record = ClipUnderstanding.model_validate(payload)
    assert record.subject == "man on grass"
    assert record.activity == ""
    assert record.people.count is None


def test_legacy_video_analysis_reads_transcript_from_on_screen_text():
    # Pre-KRI-127 videos stored the spoken transcript under `on_screen_text`.
    legacy = {
        "subject": "people playing soccer",
        "description": "a goal is scored",
        "on_screen_text": "what a goal",
        "source": "clip_metadata",
        "best_moments": [{"start_s": 0.0, "end_s": 2.0, "energy": 5, "description": "goal"}],
        "analysis_version": 7,
    }

    record = clip_record(legacy, kind="video")

    assert record.subject == "people playing soccer"
    assert record.summary == "a goal is scored"
    assert record.speech.transcript == "what a goal"
    assert record.speech.has_speech is True
    assert record.on_screen_text == ""
    assert record.notable_moments[0].description == "goal"


def test_image_analysis_keeps_real_on_screen_text():
    image = {
        "subject": "restaurant menu",
        "description": "a printed menu on a table",
        "on_screen_text": "Pasta 12",
        "brands": ["Barilla"],
        "source": "image_metadata",
    }

    record = clip_record(image, kind="image")

    assert record.kind == "image"
    assert record.on_screen_text == "Pasta 12"
    assert record.speech.transcript == ""
    assert record.brands == ["Barilla"]


def test_missing_or_malformed_analysis_never_raises():
    assert clip_record(None).is_empty()
    assert clip_record({}).is_empty()
    broken = {"subject": "dog", UNDERSTANDING_KEY: {"people": "not-a-dict"}}
    assert clip_record(broken).subject == "dog"


def test_block_falls_back_to_top_level_subject_and_description():
    analysis = {
        "subject": "two men on grass",
        "description": "they laugh",
        UNDERSTANDING_KEY: {"activity": "sitting and talking"},
    }

    record = clip_record(analysis)

    assert record.subject == "two men on grass"
    assert record.summary == "they laugh"
    assert record.activity == "sitting and talking"


def test_prompt_view_drops_empty_fields_and_caps_transcript():
    record = clip_record(
        {UNDERSTANDING_KEY: understanding_payload(_meta(transcript="word " * 400))}
    )

    view = record.prompt_view(transcript_chars=50)

    assert "on_screen_text" not in view
    assert len(view["speech"]["transcript"]) <= 50
    assert view["people"] == {
        "speaks_to_camera": True,
        "count": 6,
        "note": "one man faces the camera and narrates",
    }
    assert clip_record({}).prompt_view() == {}
