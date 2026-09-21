"""KRI-127: matcher_clip_metas reads the clip activity through the shared record.

Legacy (no ``understanding`` block) analyses must stay byte-identical, since
`detected_subject` also feeds the sport-label matcher in generative_build.py
(`_canonical_context_sport_labels`), which only accepts an exact single-alias
match against that same string.
"""

from __future__ import annotations

from app.pipeline.guided_story import matcher_clip_metas
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat
from app.services.clip_understanding import UNDERSTANDING_KEY, understanding_payload


def _snapshot(analysis: dict) -> EditProposalSnapshot:
    return EditProposalSnapshot(
        direction="guided_story",
        duration_s=10,
        title="Test",
        media=[
            MediaRef(
                lane="clip",
                media_id="m1",
                gcs_path="users/u/1.mp4",
                generation="1",
                kind="video",
                analysis=analysis,
            )
        ],
        story_beats=[
            StoryBeat(beat_id="b1", topic="Topic", media_ids=["m1"], duration_s=5.0),
        ],
    )


def test_legacy_analysis_without_understanding_block_is_byte_identical() -> None:
    legacy_analysis = {
        "subject": "man playing basketball",
        "description": "a man dribbles a basketball on an outdoor court",
        "on_screen_text": "nice shot",
        "source": "clip_metadata",
    }

    [meta] = matcher_clip_metas(_snapshot(legacy_analysis))

    assert meta.detected_subject == "man playing basketball"


def test_new_style_analysis_appends_activity_but_never_setting() -> None:
    meta_source = {
        "detected_subject": "man playing basketball",
        "summary": "A man practices free throws at a park court.",
        "setting": "an outdoor basketball court",
        "activity": "shooting free throws",
    }
    analysis = {
        "subject": "man playing basketball",
        "description": "a man dribbles a basketball on an outdoor court",
        UNDERSTANDING_KEY: understanding_payload(
            __import__("types").SimpleNamespace(**meta_source)
        ),
    }

    [meta] = matcher_clip_metas(_snapshot(analysis))

    assert meta.detected_subject.startswith("man playing basketball")
    assert "shooting free throws" in meta.detected_subject
    # A place must never reach the on-screen sport-label matcher.
    assert "an outdoor basketball court" not in meta.detected_subject


def test_new_style_analysis_without_subject_still_appends_activity() -> None:
    meta_source = {
        "detected_subject": "",
        "activity": "cooking pasta",
        "setting": "a home kitchen",
    }
    analysis = {
        UNDERSTANDING_KEY: understanding_payload(
            __import__("types").SimpleNamespace(**meta_source)
        ),
    }

    [meta] = matcher_clip_metas(_snapshot(analysis))

    assert "cooking pasta" in meta.detected_subject
    assert "a home kitchen" not in meta.detected_subject
