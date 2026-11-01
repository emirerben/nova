"""KRI-1009: a Talking title the Main Creator pins to the top corner must not vanish.

Plaza T1 "maximo 1" (2026-10-28, phone-bound Talking edit, one 35.4 s horizontal take). The
creator wrote: 'Title "The Cabildo <flag>" at the top, never over his face.' The brief kept
it as r2 (kind text, literal "The Cabildo <flag>", facts position=top_center). The Main
Creator put the words into ``pinned_texts`` (a whole-video corner pin) instead of
``opening_title``. A Talking edit has no corner-text lane, so ``check_strategy_for_runtime_v2``
dropped the pin; the runtime then waived r2 as an automatic omission, drafted "Here's the
simpler version." with "- Left out: Title at the top ..." and went straight to the render
approval. The creator was never asked, and the video came out with no title.

The chat runs through the real runtime over scratch Postgres (``submit_turn`` +
``run_kria_turn``). Only the provider transport is replaced (``planner.default_client``, as in
the KRI-721 readiness replay): the Main Creator's JSON is scripted, and the planner, the
strategy policy, the receipts, the hold-back, the approval card and the binding are the
server's own. A harness that swaps the whole planner output for a pre-built
``PlannedKriaTurn`` skips the strategy policy and cannot reproduce this bug.

Failure modes (written before the fix):
F1 the pin is dropped and waived as "Left out" (the incident);
F2 the pin is promoted but never reaches the phone recipe (planner and worker disagree);
F3 a creator-quoted text that no lane can draw is still waived without asking the creator;
F4 the "simpler version" tap does not leave the named text out, or leaves out more than it
   named;
F5 a title with the wrong words (the model dropped the flag) is receipted as met.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._runtime import ModelInvocation
from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import ChoiceSelectionIn, SubmitTurnBody
from app.kria.brief import load_latest_brief_sync
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import creator_capabilities as capabilities
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from app.tasks.kria_runtime import run_kria_turn
from tests._prod_profile import apply_prod_profile
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled
from tests.tasks.test_phone_subtitled_title_worker import _faces, _title_layers

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

CLIP_ID = "analysis-proxy-ios-3E33F71E-FB1F-400A-865A-4D99D1694EDC.mp4"
DURATION_S = 35.416667
TITLE = "The Cabildo \U0001f1e6\U0001f1f7"  # "The Cabildo" + the Argentine flag
MESSAGE = (
    "Talking video. I filmed Maximo horizontally in front of the Cabildo. Make it vertical, "
    "no black bars, and keep his face in frame the whole time. Start at “So yeah, for "
    "example, this building here is the oldest” and end right after “That’s "
    f"where Argentina was born”. Title “{TITLE}” at the top, never over his "
    "face. Captions word by word. Don’t cut anything inside his answer"
)
# `PHONE_RENDER_VERIFIED_FEATURES` as the prod api machine held it on 2026-10-28.
PROD_VERIFIED_FEATURES = [
    "alphaOverlay", "animatedText", "audioDucking", "audioMix", "authoredText",
    "basicComposition", "behindSubject", "cameraEffects", "captions", "carouselEffects",
    "clipTransitions", "crossfade", "customEffects", "editorMedia", "freehandDrawing",
    "freezeFrame", "goldenHourLook", "hdr", "hevcDecode", "local1080Export", "mediaCards",
    "motionPresets", "motionScenes", "musicBed", "narrationAudio", "positionedText",
    "processedAudio", "semanticCamera", "singleWordText", "slidePosts", "soundEffects",
    "sourceCrop", "stillImages", "textBend", "variableSpeed", "visualBlocks", "visualVideos",
]  # fmt: skip
SIMPLER = "simpler_version"
# KRI-721 approval readiness (default off) holds a draft whose ask it cannot verify, so each
# chat journey runs with it off and on. The incident's r2 carries facts.position=top_center,
# which a Talking draft cannot check (receipt partial/unchecked even when opening_title holds
# the words): with readiness on that alone holds the draft, whatever happens to the pin.
READINESS = pytest.mark.parametrize(
    "readiness", [False, True], ids=["readiness_off", "readiness_on"]
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under: phone pilot, Talking, brief binding, questions.

    Only the paid understanding steps are off (a replay makes no model call but the Main
    Creator's, which the test scripts).
    """
    apply_prod_profile(monkeypatch)
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "main_creator_agent_enabled": True,
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "subtitled_archetype_enabled": True,
        "phone_subtitled_rendering_enabled": True,
        "phone_subtitled_title_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        # Off by default; the journeys below run both ways (`READINESS`).
        "kria_approval_readiness_enabled": False,
        "kria_clip_understanding_enabled": False,
        "clip_intents_enabled": False,
        "ios_device_only_mode": False,
        "phone_render_verified_features": PROD_VERIFIED_FEATURES,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


# -- the recorded project -------------------------------------------------------------------


def _take_row(user_id: uuid.UUID, thread_id: uuid.UUID) -> dict[str, Any]:
    """The phone-bound clip assignment: Maximo's 35.4 s horizontal take, as an analysis proxy."""
    original = {
        "kind": "video",
        "width": 1920,
        "height": 1080,
        "sha256": "3" * 64,
        "has_audio": True,
        "byte_count": 90_000_000,
        "duration_s": DURATION_S,
        "orientation_degrees": 0,
    }
    return {
        "kind": "video",
        "media_id": CLIP_ID,
        "gcs_path": f"users/{user_id}/creation-threads/{thread_id}/{CLIP_ID}",
        "has_audio": True,
        "duration_s": DURATION_S,
        "manifest_identity": CLIP_ID,
        "storage_generation": "1793025574588251",
        "source_filename": "maximo_1.mp4",
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "width": 568,
                "height": 320,
                "original": original,
                "duration_s": DURATION_S,
                "frame_rate": 30.0,
                "timing_version": 1,
                "orientation_degrees": 0,
            },
        },
        "analysis": {
            "duration_s": DURATION_S,
            "width": 568,
            "height": 320,
            "subject": "a man in front of a colonial building",
            "understanding": {"kind": "video", "speech": {"has_speech": True, "language": "en"}},
        },
    }


def _seed_cabildo_project() -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session_id = _seed_runtime_project()
    row = _take_row(user_id, thread_id)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "subtitled"
        item.idea = "Untitled video"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        thread.state = {
            "media": [{"media_id": CLIP_ID, "filename": "maximo_1.mp4"}],
            "media_count": 1,
            "edit_format": "subtitled",
            "reply_language": "en",
        }
        db.commit()
    return user_id, thread_id


def _strategy_json(pin: str | None) -> dict[str, Any]:
    """The Main Creator's strategy for the incident: title as a top-corner pin (or none)."""
    strategy: dict[str, Any] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "caption_style": "karaoke",
        "pacing": "balanced",
        "direction": "native",
        "media_scope": "selected",
        "target_duration_s": 35,
        "selected_media_ids": [CLIP_ID],
        "rationale": "The whole 35.4 second take is the answer, so the edit keeps its length.",
    }
    if pin is not None:
        strategy["pinned_texts"] = [{"text": pin, "corner": "top_center"}]
    return strategy


def _model_output(*, pin: str | None = TITLE, brief: bool = True) -> dict[str, Any]:
    """The Main Creator's JSON for the incident: the pin, plus brief r1 (captions) and r2.

    ``brief=False`` is a follow-up turn that states no new requirement (a tap on a choice).
    """
    output = {
        "action": {
            "kind": "propose_strategy",
            "strategy": _strategy_json(pin),
            "summary": "Maximo's answer, vertical, with the title at the top and word captions.",
            "render_intent_evidence": {
                "pinned_texts": f"Title “{TITLE}” at the top, never over his face"
            },
        },
        "brief_updates": [
            {
                "kind": "style",
                "scope": "global",
                "description": "Captions word by word",
                "facts": {"captions": "on"},
            },
            {
                "kind": "text",
                "scope": "global",
                "literal": TITLE,
                "description": "Title at the top, never over his face",
                "facts": {"position": "top_center"},
            },
        ],
    }
    if not brief:
        del output["brief_updates"]
    return output


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    message: str,
    output: dict[str, Any],
    *,
    choice: tuple[str, str] | None = None,
) -> CreationThreadEvent:
    """One creator turn; the model replies with ``output`` (provider transport replay)."""
    calls: list[str] = []

    class Transport:
        def invoke(self, **kwargs):  # noqa: ANN003, ANN202
            calls.append(kwargs["model"])
            return ModelInvocation(raw_text=json.dumps(output))

    monkeypatch.setattr("app.kria.planner.default_client", lambda: Transport())
    selection = (
        {"choice_selection": ChoiceSelectionIn(question_id=choice[0], option_key=choice[1])}
        if choice
        else {}
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri1009-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **selection,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    assert len(calls) == 1, f"the scripted model output was not the only model call: {calls}"
    return _last_reply(thread_id)


def _pending_approval(thread_id: uuid.UUID) -> CreatorAgentApproval | None:
    with sync_session() as db:
        return db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one_or_none()


def _drafts_applied(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return len(
            db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            )
            .scalars()
            .all()
        )


def _head_strategy(thread_id: uuid.UUID) -> dict[str, Any]:
    with sync_session() as db:
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        return dict(draft.snapshot_json["strategy"])


def _live_title_requirement(thread_id: uuid.UUID):  # noqa: ANN202
    """The brief's live requirement carrying the creator's title words, or None."""
    with sync_session() as db:
        live = load_latest_brief_sync(db, thread_id).live()
    return next((req for req in live if req.literal == TITLE), None)


def _receipt(reply: CreationThreadEvent, requirement_id: str) -> dict[str, Any] | None:
    receipts = (reply.payload or {}).get("requirement_receipts") or []
    return next((r for r in receipts if r.get("requirement_id") == requirement_id), None)


# == 1. the incident, replayed through the real planner policy ==================================


@pytest.mark.parametrize(
    "readiness",
    # KRI-1113 closed the readiness-on gap: a title whose words the draft carries and whose
    # position only the finished video shows is approved as `unverified`, not held.
    [False, True],
    ids=["readiness_off", "readiness_on"],
)
@pytest.mark.asyncio
async def test_t1_top_corner_title_is_drafted_as_the_opening_title(monkeypatch, readiness):
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", readiness)
    user_id, thread_id = _seed_cabildo_project()
    reply = await _turn(monkeypatch, user_id, thread_id, MESSAGE, _model_output())

    # The turn reaches the render approval with a draft, not a question.
    assert reply.event_type == "draft_applied", (reply.event_type, reply.content)
    approval = _pending_approval(thread_id)
    assert approval is not None, "no render approval was offered"

    # Nothing is reported as left out or unrenderable, in the reply or on the approval card.
    for shown in (reply.content, approval.consequence_summary):
        assert "Left out" not in shown, shown
        assert "cannot render" not in shown, shown

    # The title is the draft's opening title (the KRI-467 phone Talking lane), not a corner pin.
    strategy = _head_strategy(thread_id)
    assert strategy.get("opening_title") == TITLE, strategy
    assert not strategy.get("pinned_texts"), strategy

    # The creator's title is still a live requirement, and its receipt is not a left-out row.
    requirement = _live_title_requirement(thread_id)
    assert requirement is not None, "the title requirement was superseded"
    receipt = _receipt(reply, requirement.id)
    assert receipt is not None, f"no receipt for {requirement.id}: {reply.payload}"
    assert receipt["status"] != "not_possible", receipt


# == 2. the planner's strategy reaches the phone recipe =========================================


async def _planner_strategy() -> dict[str, Any]:
    """What the server hands dispatch for the T1 pin: the real planner + strategy policy output.

    The same ``_plan_creator_action`` the runtime calls (the policy step needs no database),
    on the phone-bound Talking manifest, so a fix anywhere in that boundary shows up here.
    """
    manifest = capabilities.resolve_creator_manifest(
        item_id="maximo-1",
        edit_format="subtitled",
        media=[{"media_id": CLIP_ID, "kind": "video", "duration_s": DURATION_S}],
        phone_source_media_ids=[CLIP_ID],
        phone_rendering_allowed=True,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy.model_validate(_strategy_json(TITLE)),
        summary="Maximo's answer, vertical, with the title at the top.",
        render_intent_evidence=CreatorRenderIntentEvidence(
            pinned_texts=f"Title “{TITLE}” at the top, never over his face"
        ),
    )
    planned = await planner._plan_creator_action(
        None,
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        user_message=MESSAGE,
        manifest=manifest,
        inputs=SimpleNamespace(
            agent_input=SimpleNamespace(conversation=[]),
            intent_clips=[],
            creative_copy_state={},
            creative_copy_digest="d" * 24,
            creator_request=MESSAGE,
            raw_creator_request=MESSAGE,
        ),
        output=MainCreatorOutput(action=action),
    )
    assert planned.plan.mode == "act", planned.plan
    # Dispatch persists the approved strategy as `CreativeStrategy.model_dump(mode="json")`.
    strategy = CreativeStrategy.model_validate(planned.plan.intents[0].arguments["strategy"])
    return strategy.model_dump(mode="json")


@pytest.mark.asyncio
async def test_t1_title_reaches_the_phone_recipe(monkeypatch):
    strategy = await _planner_strategy()
    # A face in the top band for the first two seconds: the title must not sit on it.
    _faces(monkeypatch, [ProtectedRegion(0.0, 2.0, NormalizedBox(0.25, 0.04, 0.75, 0.42), "face")])
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["creator_strategy"] = strategy

    await asyncio.to_thread(gb._run_generative_job, str(job.id))

    assert job.status == "awaiting_device", (job.status, job.error_detail)
    variant = job.assembly_plan["variants"][0]
    rows = variant.get("text_elements") or []
    assert [row["id"] for row in rows] == ["opening-title"], (
        f"no title row on the variant; the strategy dispatched was {strategy}"
    )
    [row] = rows
    assert row["text"] == TITLE
    # Placed off the face: the default top row collided, so the worker moved it below the band.
    placement = variant["opening_title_placement"]
    assert placement["status"] in {"moved", "moved_below_face"}, placement
    assert row["y_frac"] > 0.42, row
    [layer] = _title_layers(job)
    assert layer.id == "text-0"
    assert "".join(run.text for run in layer.runs) == TITLE
    assert device_status(job, "subtitled").request.recipe is not None


# == 3. a quoted text no lane can draw is asked about, never waived =============================


@READINESS
@pytest.mark.asyncio
async def test_quoted_text_that_cannot_be_drawn_is_asked_before_the_draft(monkeypatch, readiness):
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", readiness)
    # The Talking title lane is closed (its kill switch): no lane draws these words at all.
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", False)
    user_id, thread_id = _seed_cabildo_project()

    asked = await _turn(monkeypatch, user_id, thread_id, MESSAGE, _model_output())

    # Turn 1: no draft, no approval; the creator is asked, with the text named.
    assert asked.event_type == "assistant_response", (asked.event_type, asked.content)
    assert _drafts_applied(thread_id) == 0, "a draft was made before the creator was asked"
    assert _pending_approval(thread_id) is None, "a render approval was offered"
    question = (asked.payload or {}).get("choice_question")
    assert question, f"no choice card on the reply: {asked.content}"
    options = {option["key"]: option for option in question["options"]}
    assert SIMPLER in options, list(options)
    named = " ".join([asked.content, *(str(o.get("description") or "") for o in options.values())])
    assert TITLE in named, named
    assert _live_title_requirement(thread_id) is not None, "the title was waived without asking"

    # Turn 2: the creator taps the simpler version; the same strategy comes back, with no new
    # requirements (a tap states none, and a repeat would add r3/r4 and change the offer).
    drafted = await _turn(
        monkeypatch,
        user_id,
        thread_id,
        "Make this simpler version",
        _model_output(brief=False),
        choice=(question["question_id"], SIMPLER),
    )
    assert drafted.event_type == "draft_applied", (drafted.event_type, drafted.content)
    assert _pending_approval(thread_id) is not None, "no render approval after the tap"
    assert "Left out as agreed" in drafted.content, drafted.content
    assert TITLE in drafted.content, drafted.content
    assert _live_title_requirement(thread_id) is None, "the agreed omission left the title live"


# == 4. negative control: wrong words are never receipted as the creator's title ===============


@READINESS
@pytest.mark.asyncio
async def test_wrong_title_text_is_not_reported_done(monkeypatch, readiness):
    """The model pins "The Cabildo" (the flag dropped); the creator quoted "The Cabildo <flag>".

    Whatever the planner does with the pin, other words never satisfy the brief's text
    requirement: it stays live, no "Done" line names it and its receipt is never "met".
    """
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", readiness)
    user_id, thread_id = _seed_cabildo_project()

    reply = await _turn(monkeypatch, user_id, thread_id, MESSAGE, _model_output(pin="The Cabildo"))

    done = [line for line in reply.content.splitlines() if "Done" in line and "Cabildo" in line]
    assert not done, reply.content
    requirement = _live_title_requirement(thread_id)
    assert requirement is not None, f"the creator's title was retired: {reply.content}"
    receipt = _receipt(reply, requirement.id)
    assert receipt is None or receipt["status"] != "met", receipt
