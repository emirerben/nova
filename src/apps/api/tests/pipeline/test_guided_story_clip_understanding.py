"""KRI-127: the shared record must NOT leak into ``matcher_clip_metas``.

``detected_subject`` feeds the on-screen sport-label matcher
(`generative_build._canonical_context_sport_labels`, exact single-alias match)
and the music matcher prompt. Replaying the real KRI-126 clips showed that
appending the record's free-text ``activity`` gains a wrong label ("some people
playing soccer in the background") and loses a correct one ("foot-volleyball"
makes two aliases match). Open-vocabulary labels ship behind a flag with a
grounding step instead; until then this string stays byte-identical.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_understanding_block_never_changes_the_label_matcher_input() -> None:
    legacy_analysis = {
        "subject": "people walking across a field",
        "description": "a group crosses a park",
        "on_screen_text": "come on",
        "source": "clip_metadata",
    }
    meta_source = SimpleNamespace(
        detected_subject="people walking across a field",
        activity="walking across a field, some people playing soccer in the background",
        setting="football pitch in a park",
        clip_summary="A group crosses a park while others play soccer behind them.",
    )
    new_analysis = {**legacy_analysis, UNDERSTANDING_KEY: understanding_payload(meta_source)}

    [legacy] = matcher_clip_metas(_snapshot(legacy_analysis))
    [new] = matcher_clip_metas(_snapshot(new_analysis))

    assert new.detected_subject == legacy.detected_subject == "people walking across a field"
    assert "soccer" not in new.detected_subject
