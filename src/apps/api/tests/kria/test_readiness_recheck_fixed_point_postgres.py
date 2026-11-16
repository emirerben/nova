"""A first request refused with "This renderer would change the proposed edit" (prod 2026-11-16).

Prod thread 59f3707e, one 14.03 s phone clip, montage, Turkish chat: "Add the following lyrics
as text, line by line: ...". The Main Creator planned, the brief stored the lyrics (r1), and
approval readiness held the very first draft: "Bu oluşturucu önerilen düzenlemeyi değiştirir.
Önce desteklenen sürümü hazırlamam gerekiyor." (``approval_requirements_not_ready``, r1
``unsupported``). No draft was saved and nothing told anyone which field moved.

The gate: readiness re-runs the strategy policy on the strategy the draft offers and held on ANY
difference. The planner canonicalizes that strategy with the same check first
(``_with_approval_readiness``), so the gate assumes it is a fixed point; that premise already
failed once (PR #2339, length 14.005). The prod strategy is redacted (digest only), so the
re-check's disagreement is reproduced here by making readiness's own check rewrite a field, the
way the KRI-1447 suite reproduces its length refusal.

Ways this can fail, written before the fix:

 F1 the incident: a re-check that rewrites only a field no live ask is about (here ``pacing``
    under a lyrics ask) still holds the first request with the opaque sentence and no draft.
 F2 the adopted strategy is not what the draft saves, so approval's re-check (which reads only
    the saved strategy) disagrees and cancels the approval the creator just tapped.
 F3 the hold is opaque: the changed field names are not in ``brief_coverage``.
 F4 over-reach: a rewrite of a field an ask IS about (``caption_style`` under a text ask) is
    adopted silently instead of asked with the KRI-1447 "simpler version?" card.
 F5 that card's "Evet" re-plans into the same hold (the consent path does not know this
    refusal), or drafts something other than the strategy the re-check keeps.
 F6 a re-check that is not a fixed point on its own output (it would rewrite the adopted
    strategy again) is adopted, so approval would cancel it later.
 F7 post-approval loosened: a rewrite found when the creator approves still cancels.
 F8 a change of what the edit is (``edit_format``) or an ask of a kind with no field map is
    ever treated as normalization.
 F9 the Tadic length case (#2339) and the KRI-1447 card suite change (run beside this file).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import approval_readiness, planner
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.brief import BriefRequirement, BriefUpdate
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.kria.strategy_policy import CheckedStrategy
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from app.services.choice_questions import SIMPLER_VERSION_REASON
from app.tasks.kria_runtime import run_kria_turn
from tests._prod_profile import apply_prod_profile
from tests.kria import test_kri759_simpler_version_refusal as k759
from tests.kria.test_kri775_merope_m2_replay import FIXTURE as MEROPE
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CLIP = "analysis-proxy-ios-0b5c3a52-5a0e-4a7e-9a55-0f2b1f6c9e11.mp4"
PATH = f"users/test/creation-threads/lyrics/{CLIP}"
# Stand-in lyrics with the incident's shape (six short Turkish lines); never the creator's.
LINES = (
    "Bir satır şarkı",
    "İkinci dize gelir",
    "Üçüncü söz burada",
    "Dördüncü çığlık",
    "Beşinci yazı",
    "Altıncı ve son",
)
SPACED = " ".join(LINES)
MESSAGE = f"Add the following lyrics as text, line by line: {SPACED}"
OPAQUE = (
    "This renderer would change the proposed edit. I need to prepare the supported version first.",
    "Bu oluşturucu önerilen düzenlemeyi değiştirir. Önce desteklenen sürümü hazırlamam gerekiyor.",
)
SIMPLER_TR = "Daha sade bir sürüm yapayım mı?"
_REAL_CHECK = approval_readiness.check_strategy_for_runtime_v2


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_prod_profile(monkeypatch)
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    for name in (
        "kria_creative_brief_user_ids",
        "kria_brief_binding_user_ids",
        "phone_render_user_ids",
    ):
        monkeypatch.setattr(settings, name, [])
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=k759.PlannedIntentResolution([], k759.IntentResolution(intents=[]))),
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _seed() -> tuple[uuid.UUID, uuid.UUID]:
    """The incident's project before its first turn: one 14.03 s phone proxy, montage, tr."""
    user_id, thread_id, _ = _seed_runtime_project()
    row = copy.deepcopy(MEROPE["renders"][0]["media_snapshot"]["clip_assignments"][0])
    row["upload_contract"]["proxy"]["duration_s"] = 14.033333333333333
    row["upload_contract"]["proxy"]["original"].update(
        duration_s=14.005, sha256=hashlib.sha256(CLIP.encode()).hexdigest()
    )
    row.update(
        media_id=CLIP,
        manifest_identity=CLIP,
        gcs_path=PATH,
        duration_s=14.033333,
        source_filename="lyrics.mov",
        storage_generation="1",
        library_asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, CLIP)),
        library_source_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"source-{CLIP}")),
        analysis={
            "subject": "crowd",
            "description": "A stadium crowd singing",
            "understanding": {
                "subject": "crowd",
                "summary": "A stadium crowd singing",
                "setting": "stadium",
                "activity": "singing",
            },
            "on_screen_text": "",
            "best_moments": [],
            "transcript": "",
        },
    )
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [PATH]
        item.clip_assignments = [row]
        thread.state = {
            "media": [{"media_id": CLIP}],
            "media_count": 1,
            "edit_format": "montage",
            "reply_language": "tr",
        }
        db.commit()
    return user_id, thread_id


def _model(monkeypatch: pytest.MonkeyPatch, *, first: bool = True) -> None:
    """The Main Creator's first-turn answer: the lyrics as one line of text, the brief's r1."""
    strategy = CreativeStrategy.model_validate(
        {
            "direction": "guided_story",
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "selected_media_ids": [CLIP],
            "media_scope": "selected",
            "render_program": "guided",
            "target_duration_s": 14.03,
            "pinned_texts": [{"text": SPACED, "corner": "bottom_center"}],
            "rationale": "The crowd clip with the lyrics on it.",
        }
    )
    updates = (
        [
            BriefUpdate(
                operation="add",
                kind="text",
                scope="global",
                literal=SPACED,
                description="Sözleri satır satır göster: " + " / ".join(LINES),
            )
        ]
        if first
        else []
    )

    async def _call(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return MainCreatorOutput(
            action=ProposeStrategy(
                kind="propose_strategy",
                strategy=strategy,
                summary="Sözler kalabalık klibinin üstünde.",
                render_intent_evidence=CreatorRenderIntentEvidence(pinned_texts=MESSAGE),
            ),
            brief_updates=updates,
        )

    monkeypatch.setattr(planner, "_call_main_creator", _call)


def _recheck_rewrites(monkeypatch: pytest.MonkeyPatch, field: str, values: dict) -> None:
    """Readiness's own policy re-check rewrites ``field`` (``values``: from -> to); the planner's
    canonicalization (``strategy_policy`` directly) does not, as the incident's did not."""

    def check(manifest, strategy, **kwargs):  # noqa: ANN001, ANN202
        checked = _REAL_CHECK(manifest, strategy, **kwargs)
        current = (
            getattr(checked.strategy, field, None) if isinstance(checked, CheckedStrategy) else None
        )
        if isinstance(checked, CheckedStrategy) and current in values:
            return replace(
                checked, strategy=checked.strategy.model_copy(update={field: values[current]})
            )
        return checked

    monkeypatch.setattr(approval_readiness, "check_strategy_for_runtime_v2", check)


def _events(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            )
        )


async def _turn(
    user_id: uuid.UUID, thread_id: uuid.UUID, message: str
) -> tuple[str, CreationThreadEvent]:
    with sync_session() as db:
        revision = int(db.get(CreationThread, thread_id).revision)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"recheck-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    reply = next(
        e
        for e in reversed(_events(thread_id))
        if e.event_type in {"draft_applied", "assistant_response"}
    )
    return result["status"], reply


def _head(thread_id: uuid.UUID) -> CreatorEditDraft | None:
    with sync_session() as db:
        return db.scalars(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).one_or_none()


def _pending(thread_id: uuid.UUID) -> list[CreatorAgentApproval]:
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            )
        )


async def _approve(user_id: uuid.UUID, thread_id: uuid.UUID) -> str:
    (approval,) = _pending(thread_id)
    with sync_session() as db:
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        try:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval.id,
                creator_id=user_id,
                decision="approve",
                body=body,
            )
        except Exception:  # noqa: BLE001, S110 - a cancelled approval raises; its status says so
            pass
    with sync_session() as db:
        return db.get(CreatorAgentApproval, approval.id).status


@pytest.mark.asyncio
async def test_the_incident_drafts_the_renderers_normalization_and_names_it(monkeypatch):
    """F1, F2, F3: the first request drafts, saves the checked strategy, approves."""
    user_id, thread_id = _seed()
    _model(monkeypatch)
    _recheck_rewrites(monkeypatch, "pacing", {"balanced": "fast"})

    status, reply = await _turn(user_id, thread_id, MESSAGE)

    assert status == "awaiting_approval", reply.content
    assert reply.event_type == "draft_applied"
    assert not any(text in (reply.content or "") for text in OPAQUE)
    head = _head(thread_id)
    assert head.snapshot_json["strategy"]["pacing"] == "fast"
    # The card's chips name what the draft saves, never the pace it replaced.
    assert not {"Tempo: Dengeli", "Balanced pacing"} & set(head.snapshot_json["changes"])
    assert head.snapshot_json["strategy"]["pinned_texts"] == [
        {"text": SPACED, "corner": "bottom_center"}
    ]
    readiness = head.snapshot_json["brief_coverage"]["approval_readiness"]
    assert readiness["ready"] is True
    assert readiness["recheck"] == {
        "changed_fields": ["pacing"],
        "requirement_fields": [],
        "adopted": True,
    }
    assert "adopted_strategy" not in readiness
    # F2: approval re-reads only the saved strategy and agrees with it.
    assert await _approve(user_id, thread_id) == "approved"


@pytest.mark.asyncio
async def test_approval_still_cancels_on_a_rewrite_it_finds(monkeypatch):
    """F7: the post-approval re-check is unchanged: any rewrite there cancels."""
    user_id, thread_id = _seed()
    _model(monkeypatch)
    _recheck_rewrites(monkeypatch, "pacing", {"balanced": "fast"})
    status, _ = await _turn(user_id, thread_id, MESSAGE)
    assert status == "awaiting_approval"

    # Between the card and the tap the renderer's check moves again.
    _recheck_rewrites(monkeypatch, "pacing", {"fast": "relaxed"})
    assert await _approve(user_id, thread_id) == "cancelled"


@pytest.mark.asyncio
async def test_a_recheck_that_would_rewrite_its_own_output_is_never_adopted(monkeypatch):
    """F6: not a fixed point on the adopted strategy: held as before, the fields named."""
    user_id, thread_id = _seed()
    _model(monkeypatch)
    _recheck_rewrites(monkeypatch, "pacing", {"balanced": "fast", "fast": "relaxed"})

    status, reply = await _turn(user_id, thread_id, MESSAGE)

    assert status == "completed" and reply.event_type == "assistant_response"
    assert OPAQUE[1] in reply.content
    assert _head(thread_id) is None and not _pending(thread_id)
    recheck = reply.payload["brief_coverage"]["approval_readiness"]["recheck"]
    assert recheck["changed_fields"] == ["pacing"] and recheck["adopted"] is False
    assert recheck["again"]["changed_fields"] == ["pacing"]


@pytest.mark.asyncio
async def test_a_rewrite_of_an_asked_field_asks_with_the_card_and_evet_drafts_it(monkeypatch):
    """F4, F5: the text ask's field is the creator's call, asked on the KRI-1447 card."""
    user_id, thread_id = _seed()
    _model(monkeypatch)
    _recheck_rewrites(monkeypatch, "caption_style", {"auto": "clean"})

    status, held = await _turn(user_id, thread_id, MESSAGE)

    assert status == "completed" and held.event_type == "assistant_response"
    assert SIMPLER_TR in held.content
    assert not any(text in held.content for text in OPAQUE)
    question = held.payload["choice_question"]
    assert question["conflict"] == SIMPLER_VERSION_REASON
    assert question["dropped_fields"] == ["caption_style"]
    readiness = held.payload["brief_coverage"]["approval_readiness"]
    assert readiness["recheck"] == {
        "changed_fields": ["caption_style"],
        "requirement_fields": ["caption_style"],
    }
    assert readiness["policy_refusal"]["fields"] == ["caption_style"]
    assert _head(thread_id) is None and not _pending(thread_id)

    # F5: "Evet" drafts exactly what the re-check keeps, once, and approval agrees.
    _model(monkeypatch, first=False)
    status, drafted = await _turn(user_id, thread_id, "Evet")
    assert status == "awaiting_approval", drafted.content
    assert _head(thread_id).snapshot_json["strategy"]["caption_style"] == "clean"
    assert await _approve(user_id, thread_id) == "approved"


def _req(kind: str) -> BriefRequirement:
    return BriefRequirement(id="r1", kind=kind, scope="global", description="x")


def test_what_an_ask_is_about_is_never_a_normalization() -> None:
    """F8: the field map is conservative."""
    tied = approval_readiness.requirement_tied_fields
    # What the edit is, with or without asks.
    assert tied(("edit_format", "pacing"), []) == ("edit_format",)
    assert tied(("audio_strategy",), [_req("text")]) == ("audio_strategy",)
    # A text ask owns its text fields, not the pace; a timing ask owns the pace.
    assert tied(("pacing", "pinned_texts"), [_req("text")]) == ("pinned_texts",)
    assert tied(("pacing", "pinned_texts"), [_req("timing")]) == ("pacing",)
    # An ask of a kind with no field map ties every field.
    assert tied(("pacing", "direction"), [_req("style")]) == ("pacing", "direction")
