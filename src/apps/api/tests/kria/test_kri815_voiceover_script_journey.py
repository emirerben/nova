"""KRI-815: with footage and no voiceover, Kria chat hands back a footage-grounded script.

Troya stress run T03 (finding F08, 21-22 Oct 2026): five museum clips attached, no voiceover,
and "write a speakable narration script for these 5 clips". Kria either asked a generic
question with no script, or proposed a narrated edit that the creator approved and the claim
then cancelled with ``voiceover_required`` ("Record a voiceover first..."): a doomed approval.
The retained T03 thread id is not in the ticket or the repo, so this journey uses a synthetic
five-clip museum project built the same way (phone pilot, brief on, clip understanding stored
on each clip). Swap ``CLIPS`` for the retained case when its id is supplied.

Real runtime turns (``submit_turn`` + ``run_kria_turn`` over scratch Postgres). Only two model
calls are replaced: the Main Creator's (``planner._call_main_creator``) and the script writer's
provider client (``voiceover_script._writer_client``, a one-shot cassette that records the
prompt it was sent). The writer's own ``parse()``, the server's grounding check, the payload,
the approval/claim path and every database write are the real ones.

Ways it could fail, written down before the code (each is pinned by a case below):

F1  Footage + no voiceover + script ask -> no script, or "record first" (the T03 failure).
F2  A narrated strategy without a voiceover reaches approval, then dies at claim.
F3  The script ignores some of the five clips (writer prompt lacks a clip's facts).
F4  Unknown names/dates are not flagged; the reply claims audio was generated.
F5  Script text leaks into on-screen text of the later approved edit.
F6  The script is treated as creator intent: an approval / draft / brief version is written.
F7  Revising the script changes footage, brief or saved edits.
F8  Revisions are not versioned (same script_id, revision + 1, same source clips).
F10 After the voiceover is attached, the normal narrated plan is not offered or not claimable.
F12 Turkish chat gets an English script/reply.
F13 Writer failure becomes an empty or refusal-text "script".
F14 The chat path depends on TRANSCRIPT_HELPER_ENABLED.
F15 Non-script requests change: a montage still reaches approval unchanged.
F16 Old clients lose the script (it must also be in the reply text).
    Kill switch off: no script, but still no doomed approval.
    Model evidence not in the creator's words: ask, never write.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._runtime import ModelClient, ModelInvocation, TerminalError
from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    DraftVoiceoverScript,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner, voiceover_script
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentResolution
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_kri787_merope_t1_replay import FIXTURE, prod_flags
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

TAKE_ROW = FIXTURE["take_row"]
# Five museum clips, as clip understanding stored them (no names, no dates: what is visible).
_PROXY = "analysis-proxy-ios-MUSEUM-0{}.mp4"
CLIPS = [
    (
        _PROXY.format(1),
        6.0,
        "a seated stone statue",
        "A huge seated stone statue in a bright hall.",
    ),
    (_PROXY.format(2), 5.0, "clay pots in a glass case", "Rows of painted clay pots behind glass."),
    (
        _PROXY.format(3),
        7.0,
        "a gold funerary mask",
        "A gold mask with blue stripes under a spotlight.",
    ),
    (
        _PROXY.format(4),
        6.0,
        "a carved wall",
        "A wall covered in carved symbols, a visitor walks by.",
    ),
    (_PROXY.format(5), 6.0, "a painted coffin", "A painted wooden coffin lid in a dim room."),
]
SCRIPT_ASK = "Write a speakable narration script for these 5 clips so I can record it."
TR_ASK = "Bu 5 klip için seslendirebileceğim bir anlatım metni yaz."
# 64 words, inside the writer's spoken band for 30 s of footage. "1922" and "Carter" are
# not in the clips or the ask: the backstop must flag them; the writer flags the mask claim.
SCRIPT = (
    "I walked into this hall and the first thing that stopped me was this huge stone statue, "
    "just sitting there like it owns the place. Then rows of painted clay pots, older than "
    "anything I own. The gold mask under the spotlight is the one everyone crowds around, "
    "found by Carter in 1922. Carved walls, a painted coffin lid, and a quiet room."
)
SHORTER = (
    "This hall stopped me cold. A huge stone statue, painted clay pots behind glass, and a "
    "gold mask with blue stripes glowing under a spotlight. Then carved walls and a painted "
    "coffin lid in a dim, quiet room. Walking through it felt like stepping into another "
    "time, and I didn't want to leave this place at all today."
)
TR_SCRIPT = (
    "Bu salona girer girmez dev taş heykel beni durdurdu, sanki burası onun eviymiş gibi "
    "oturuyordu. Sonra camın arkasında boyalı kil çömlekler, sahip olduğum her şeyden eski. "
    "Spot ışığının altındaki altın maske herkesin etrafında toplandığı parça. Oyma semboller "
    "dolu bir duvar, loş bir odada boyalı bir tabut kapağı ve derin bir sessizlik. Burada "
    "zaman yavaşlıyor, ben de yavaşlıyorum ve her adımda biraz daha merak ediyorum bugün."
)


@pytest.fixture(autouse=True)
def _no_clip_intent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clip intents are their own model call; these requests name no clip, so none resolve."""
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=PlannedIntentResolution([], IntentResolution(intents=[]))),
    )


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "transcript_helper_enabled", False)  # F14
    monkeypatch.setattr(settings, "kria_voiceover_script_enabled", True)
    # A Voiceover (narrated) edit renders on this iPhone once the voiceover is recorded.
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "narrationAudio"],
    )


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


class Cassette(ModelClient):
    """One recorded writer response per call; remembers the prompts it was sent."""

    supports_clarification_retry = False

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, *, model: str, prompt: str, **_kwargs) -> ModelInvocation:  # noqa: ANN003
        self.prompts.append(prompt)
        if not self.responses:
            raise TerminalError("cassette exhausted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ModelInvocation(raw_text=response)


def _written(script: str, checks: list[dict] | None = None) -> str:
    return json.dumps({"text": script, "needs_check": checks or []})


@dataclass
class Chat:
    user_id: uuid.UUID
    thread_id: uuid.UUID
    item_id: uuid.UUID
    replies: list[CreationThreadEvent] = field(default_factory=list)
    main_creator_inputs: list = field(default_factory=list)


def _seed(*, voiceover: bool = False, edit_format: str = "narrated") -> Chat:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        rows = [_phone_clip(user_id, thread_id, *clip) for clip in CLIPS]
        item.edit_format = edit_format
        item.idea = "Museum visit"
        item.clip_gcs_paths = [row["gcs_path"] for row in rows]
        item.clip_assignments = rows
        if voiceover:
            _attach_voiceover(item, user_id, thread_id)
        thread.state = {
            "media": [{"media_id": row["media_id"], "filename": row["media_id"]} for row in rows],
            "media_count": len(rows),
            "edit_format": edit_format,
        }
        db.commit()
        item_id = item.id
    return Chat(user_id, thread_id, item_id)


def _phone_clip(user_id, thread_id, media_id, seconds, subject, summary) -> dict:  # noqa: ANN001
    """One iPhone analysis-proxy clip row, shaped like prod's (``kri787`` take row)."""
    row = copy.deepcopy(TAKE_ROW)
    row["media_id"] = row["manifest_identity"] = media_id
    row["gcs_path"] = f"users/{user_id}/creation-threads/{thread_id}/{media_id}"
    row["duration_s"] = seconds
    proxy = row["upload_contract"]["proxy"]
    proxy["duration_s"] = proxy["original"]["duration_s"] = seconds
    proxy["original"]["sha256"] = hashlib.sha256(media_id.encode()).hexdigest()
    row["analysis"] = {
        "duration_s": seconds,
        "subject": subject,
        "description": summary,
        "understanding": {"kind": "video", "subject": subject, "summary": summary},
    }
    return row


def _attach_voiceover(item: PlanItem, user_id, thread_id) -> None:  # noqa: ANN001
    """What the app's existing (ownership-checked) voiceover attach leaves on the item."""
    item.voiceover_gcs_path = f"users/{user_id}/creation-threads/{thread_id}/voiceover.m4a"
    item.voiceover_generation = 7
    item.voiceover_duration_s = 29.0
    item.audio_mode = "voiceover"


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
                    CreationThreadEvent.role == "assistant",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


def _count(model, thread_id: uuid.UUID) -> int:  # noqa: ANN001
    with sync_session() as db:
        return int(
            db.execute(
                select(func.count()).select_from(model).where(model.thread_id == thread_id)
            ).scalar_one()
        )


def _brief_versions(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(
            db.execute(
                text("select count(*) from creative_brief_versions where thread_id = :t"),
                {"t": thread_id},
            ).scalar_one()
        )


def _jobs(chat: Chat) -> int:
    """Render Jobs minted for this creator (the seeded project starts with none)."""
    with sync_session() as db:
        return int(
            db.execute(
                select(func.count()).select_from(Job).where(Job.user_id == chat.user_id)
            ).scalar_one()
        )


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    chat: Chat,
    message: str,
    output: MainCreatorOutput,
    *,
    writer: Cassette | None = None,
    locale: str | None = None,
) -> CreationThreadEvent:
    async def _model(inputs, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        chat.main_creator_inputs.append(inputs.agent_input)
        return output

    monkeypatch.setattr(planner, "_call_main_creator", _model)
    monkeypatch.setattr(
        voiceover_script, "_writer_client", lambda: writer or Cassette(TerminalError("no call"))
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=chat.thread_id,
            creator_id=chat.user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri815-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(chat.thread_id),
            ),
            locale=locale,
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    reply = _last_reply(chat.thread_id)
    chat.replies.append(reply)
    return reply


def _script_action(evidence: str = SCRIPT_ASK, note: str | None = None) -> MainCreatorOutput:
    return MainCreatorOutput(
        action=DraftVoiceoverScript(
            kind="draft_voiceover_script", request_evidence=evidence, revision_note=note
        )
    )


def _narrated() -> MainCreatorOutput:
    return MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                edit_format="narrated",
                audio_strategy="voiceover",
                media_scope="all",
                pacing="balanced",
                target_duration_s=30,
                rationale="Your voiceover carries the museum walk; the clips follow it.",
            ),
            summary="A narrated walk through the museum, cut to your voiceover.",
        )
    )


def _montage() -> MainCreatorOutput:
    return MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                edit_format="montage",
                audio_strategy="original_audio",
                media_scope="selected",
                selected_media_ids=[media_id for media_id, *_ in CLIPS],
                pacing="balanced",
                target_duration_s=24,
                rationale="A quick walk through the five rooms with their own sound.",
            ),
            summary="A quick montage of the museum with the clips' own sound.",
        )
    )


def _script(reply: CreationThreadEvent) -> dict:
    script = (reply.payload or {}).get("voiceover_script")
    assert isinstance(script, dict), f"no script on the reply: {reply.content!r}"
    return script


def _report(name: str, chat: Chat, **extra) -> None:  # noqa: ANN003
    """Repeatable evidence: KRI815_REPORT_DIR=... pytest this file writes one JSON per case."""
    root = os.environ.get("KRI815_REPORT_DIR")
    if not root:
        return
    path = Path(root) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "replies": [
                    {"content": r.content, "payload": r.payload, "event_type": r.event_type}
                    for r in chat.replies
                ],
                "approvals": _count(CreatorAgentApproval, chat.thread_id),
                "drafts": _count(CreatorEditDraft, chat.thread_id),
                "jobs": _jobs(chat),
                **extra,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )


# == F1/F3/F4/F6/F16: the T03 ask gets a grounded script and nothing else ========================


@pytest.mark.asyncio
async def test_script_ask_returns_a_grounded_script_with_record_or_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _seed()
    writer = Cassette(
        _written(SCRIPT, [{"phrase": "the one everyone crowds around", "reason": "a guess"}])
    )
    reply = await _turn(monkeypatch, chat, SCRIPT_ASK, _script_action(), writer=writer)

    script = _script(reply)
    assert script["status"] == "draft" and script["text"] == SCRIPT
    assert script["continuation"] == "record_or_import"
    assert script["revision"] == 1 and script["script_id"].startswith("vos-")
    assert script["item_id"] == str(chat.item_id)
    assert script["source_media_ids"] == [media_id for media_id, *_ in CLIPS]
    assert script["lines"] and " ".join(script["lines"]).split() == SCRIPT.split()
    # F3: every clip's stored understanding reached the writer, in play order.
    prompt = writer.prompts[0]
    for number, (_, _, subject, summary) in enumerate(CLIPS, start=1):
        assert f"Clip {number}" in prompt and subject in prompt and summary in prompt
    assert SCRIPT_ASK in prompt
    # F4: the writer's own flag plus the unbacked name and year.
    flagged = [check["phrase"] for check in script["needs_check"]]
    assert flagged[0] == "the one everyone crowds around"
    assert "Carter" in flagged and "1922" in flagged
    assert "Check before recording" in reply.content and "“1922”" in reply.content
    assert "generated" not in reply.content.lower()
    # F16: an old client reads the script as the reply text.
    assert reply.content.endswith(SCRIPT)
    # F6: no approval, no draft, no Job, no brief version.
    assert _count(CreatorAgentApproval, chat.thread_id) == 0
    assert _count(CreatorEditDraft, chat.thread_id) == 0
    assert _jobs(chat) == 0
    assert _brief_versions(chat.thread_id) == 0
    # The Main Creator was offered the script action (footage, no voiceover).
    assert chat.main_creator_inputs[-1].voiceover_script_available is True
    _report("j1_script", chat, writer_prompt=prompt)


# == F2: a narrated plan without a voiceover never reaches approval ==============================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "self_narration",
    # On: the policy let the plan through, the creator approved, the claim cancelled it
    # (`voiceover_required`). Off: "That kind of edit isn't available". Neither had a script.
    [True, False],
    ids=["self-narration-on-doomed-approval", "self-narration-off-unavailable"],
)
async def test_narrated_strategy_without_voiceover_returns_a_script_not_an_approval(
    monkeypatch: pytest.MonkeyPatch, self_narration: bool
) -> None:
    monkeypatch.setattr(settings, "narrated_self_narration_enabled", self_narration)
    chat = _seed()
    writer = Cassette(_written(SHORTER))
    reply = await _turn(
        monkeypatch, chat, "Make a narrated video of my museum visit", _narrated(), writer=writer
    )

    assert _script(reply)["text"] == SHORTER
    assert _count(CreatorAgentApproval, chat.thread_id) == 0
    assert _count(CreatorEditDraft, chat.thread_id) == 0
    assert _jobs(chat) == 0
    assert "Record a voiceover first" not in reply.content
    _report("j2_no_doomed_approval", chat)


# == F7/F8: revisions are versioned and touch nothing else =======================================


@pytest.mark.asyncio
async def test_revising_the_script_keeps_footage_brief_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _seed()
    await _turn(monkeypatch, chat, SCRIPT_ASK, _script_action(), writer=Cassette(_written(SCRIPT)))
    first = _script(chat.replies[-1])
    with sync_session() as db:
        item = db.get(PlanItem, chat.item_id)
        before = (item.clip_assignments, item.edit_format, item.audio_mode)
    briefs = _brief_versions(chat.thread_id)

    writer = Cassette(_written(SHORTER))
    note = "make it a bit calmer, drop the name"
    reply = await _turn(
        monkeypatch,
        chat,
        f"Thanks! {note}",
        _script_action(evidence=note, note=note),
        writer=writer,
    )
    second = _script(reply)
    assert second["revision"] == 2 and second["script_id"] == first["script_id"]
    assert second["source_media_ids"] == first["source_media_ids"]
    assert second["text"] == SHORTER
    assert "updated script" in reply.content
    # The writer rewrote the earlier draft with the creator's change.
    assert SCRIPT in writer.prompts[0] and note in writer.prompts[0]
    with sync_session() as db:
        item = db.get(PlanItem, chat.item_id)
        assert (item.clip_assignments, item.edit_format, item.audio_mode) == before
    assert _brief_versions(chat.thread_id) == briefs
    assert _count(CreatorAgentApproval, chat.thread_id) == 0
    _report("j4_revision", chat)


@pytest.mark.asyncio
async def test_a_later_turn_never_silently_replaces_the_revised_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8: r2 is what the creator asked for; "make the video" or a stale re-fire keeps it."""
    chat = _seed()
    await _turn(monkeypatch, chat, SCRIPT_ASK, _script_action(), writer=Cassette(_written(SCRIPT)))
    note = "make it a bit calmer, drop the name"
    await _turn(
        monkeypatch,
        chat,
        note,
        _script_action(evidence=note, note=note),
        writer=Cassette(_written(SHORTER)),
    )
    assert _script(chat.replies[-1])["revision"] == 2

    writer = Cassette()  # an empty cassette: any re-draft would show up in its prompts
    for message, output in (
        ("ok, make the narrated video now", _narrated()),
        # The model re-fires turn 1's ask with a change the creator never typed.
        ("looks good", _script_action(note="make it more dramatic")),
        ("looks good", _script_action()),
    ):
        reply = await _turn(monkeypatch, chat, message, output, writer=writer)
        assert "voiceover_script" not in (reply.payload or {}), message
    assert writer.prompts == []
    assert _count(CreatorAgentApproval, chat.thread_id) == 0
    assert _count(CreatorEditDraft, chat.thread_id) == 0
    _report("j4b_revision_kept", chat)


# == F10/F5: after recording, the normal narrated edit is offered and claimable ==================


@pytest.mark.asyncio
async def test_after_the_voiceover_is_attached_the_narrated_edit_reaches_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _seed()
    await _turn(monkeypatch, chat, SCRIPT_ASK, _script_action(), writer=Cassette(_written(SCRIPT)))
    with sync_session() as db:
        _attach_voiceover(db.get(PlanItem, chat.item_id), chat.user_id, chat.thread_id)
        db.commit()

    reply = await _turn(monkeypatch, chat, "I recorded it with your script.", _narrated())
    assert "voiceover_script" not in (reply.payload or {})
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == chat.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        draft = db.get(CreatorEditDraft, approval.draft_id)
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, chat.thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    # F5: the spoken script is never on-screen text of the approved edit.
    snapshot = json.dumps(draft.snapshot_json, ensure_ascii=False)
    for line in _script(chat.replies[0])["lines"]:
        assert line not in snapshot
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=chat.thread_id,
            approval_id=approval.id,
            creator_id=chat.user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
    assert claim is not None, _last_reply(chat.thread_id).content
    with sync_session() as db:
        assert db.get(PlanItem, chat.item_id).audio_mode == "voiceover"
        assert db.get(CreatorAgentApproval, approval.id).status != "cancelled"
    _report("j3_record_then_render", chat, claim_strategy=claim.strategy)


# == F12: Turkish chat, Turkish script and reply =================================================


@pytest.mark.asyncio
async def test_turkish_chat_gets_a_turkish_script(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _seed()
    writer = Cassette(_written(TR_SCRIPT))
    reply = await _turn(
        monkeypatch, chat, TR_ASK, _script_action(evidence=TR_ASK), writer=writer, locale="tr-TR"
    )
    script = _script(reply)
    assert script["language"] == "tr" and script["text"] == TR_SCRIPT
    assert "Kliplerinde görünenlere göre" in reply.content
    assert "Turkish" in writer.prompts[0]


# == F13: a writer failure is honest, never a fake script ========================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        TerminalError("provider down"),
        _written("As an AI I cannot write a script without more information about this."),
    ],
    ids=["provider-error", "refusal-text"],
)
async def test_writer_failure_is_an_honest_reply_without_a_script(
    monkeypatch: pytest.MonkeyPatch, response: str | Exception
) -> None:
    chat = _seed()
    writer = Cassette(response, response, response)
    reply = await _turn(monkeypatch, chat, SCRIPT_ASK, _script_action(), writer=writer)
    script = _script(reply)
    assert script["status"] == "failed" and "text" not in script
    assert script["continuation"] == "record_or_import"
    assert "couldn't write a script" in reply.content
    assert "As an AI" not in reply.content
    assert _count(CreatorAgentApproval, chat.thread_id) == 0


# == the model's reading is never the creator's request ==========================================


@pytest.mark.asyncio
async def test_script_action_without_creator_words_asks_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _seed()
    writer = Cassette(_written(SCRIPT))
    reply = await _turn(
        monkeypatch,
        chat,
        "Make something nice from my museum trip",
        _script_action(evidence="write me a voiceover script"),
        writer=writer,
    )
    assert "voiceover_script" not in (reply.payload or {})
    assert writer.prompts == []
    assert "voiceover script" in reply.content


# == kill switch and regression controls (F15) ===================================================


@pytest.mark.asyncio
async def test_kill_switch_still_prevents_the_doomed_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_voiceover_script_enabled", False)
    chat = _seed()
    writer = Cassette(_written(SCRIPT))
    reply = await _turn(monkeypatch, chat, "Make a narrated video", _narrated(), writer=writer)
    assert "voiceover_script" not in (reply.payload or {})
    assert writer.prompts == []
    assert "Record or import it first" in reply.content
    assert _count(CreatorAgentApproval, chat.thread_id) == 0
    assert chat.main_creator_inputs[-1].voiceover_script_available is False


@pytest.mark.asyncio
async def test_a_montage_request_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _seed(edit_format="montage")
    reply = await _turn(monkeypatch, chat, "Make a quick montage of my museum trip", _montage())
    assert "voiceover_script" not in (reply.payload or {})
    assert _count(CreatorAgentApproval, chat.thread_id) == 1


@pytest.mark.asyncio
async def test_with_a_voiceover_attached_no_script_is_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _seed(voiceover=True)
    reply = await _turn(monkeypatch, chat, "Make a narrated video", _narrated())
    assert "voiceover_script" not in (reply.payload or {})
    assert chat.main_creator_inputs[-1].voiceover_script_available is False
    assert _count(CreatorAgentApproval, chat.thread_id) == 1
