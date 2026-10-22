"""KRI-808 on real Postgres: the background clip analysis reads the chat's language row.

The offline replay (``test_kri808_clip_speech_language.py``) stands in the chat-language
lookup; here ``analyze_kria_clips`` runs end to end against a scratch database with a real
``CreationThread`` (``reply_language="en"``) and the real store. Only Gemini and whisper
are the recorded Don Julio M1 outputs.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services.clip_understanding import clip_record
from app.services.speech_segments import ground_excerpt
from app.tasks import kria_clip_understanding as task
from tests.tasks.test_kri808_clip_speech_language import (  # noqa: F401 - pytest fixture
    _REAL_CHAT_LANGUAGE,
    _entry,
    _words,
    whisper,
)


@pytest.mark.usefixtures("whisper")
def test_background_analysis_reads_the_chats_language_and_stores_spoken_words(
    monkeypatch,
) -> None:
    """Real task loop + real Postgres: thread language "en", Gemini and whisper recorded."""
    from unittest.mock import MagicMock

    from app.database import sync_session
    from app.models import ContentPlan, CreationThread, Persona, PlanItem, User
    from app.services import creator_clip_analysis

    monkeypatch.setattr(task, "_chat_language", _REAL_CHAT_LANGUAGE, raising=False)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(task.analyze_kria_clips, "apply_async", MagicMock())

    def gemini(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        entry = _entry(raw["media_id"])
        return {**raw, **entry, "generation": "7"}, MagicMock()

    monkeypatch.setattr(creator_clip_analysis, "analyze_clip_assignment", gemini)
    user_id, persona_id, plan_id, item_id = (uuid.uuid4() for _ in range(4))
    rows = [
        {
            "gcs_path": f"users/u/{clip}.mp4",
            "media_id": clip,
            "kind": "video",
            "storage_generation": "7",
        }
        for clip in ("M1_05", "M1_09", "M1_08")
    ]
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="Lunch at Don Julio",
                item_status="awaiting_clips",
                clip_assignments=rows,
            )
        )
        db.flush()
        db.add(
            CreationThread(
                creator_id=user_id,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                state={"reply_language": "en"},
            )
        )
        db.commit()

    result = task.analyze_kria_clips.run(str(item_id))

    assert result["status"] == "done" and result["analyzed"] == 3
    with sync_session() as db:
        stored = {
            row["media_id"]: clip_record(row["analysis"]).speech
            for row in db.get(PlanItem, item_id).clip_assignments
        }
    assert stored["M1_05"].language == "en"
    assert ground_excerpt(_words("M1_05", stored["M1_05"]), "What's your name?") is not None
    assert "Gracias" in " ".join(seg.text for seg in stored["M1_09"].segments)
    assert stored["M1_08"].language == "es"  # the waiter's Spanish, untouched
