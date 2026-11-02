"""KRI-776: "six moments from it, about 2 seconds each" must reach the brief and the plan.

Incident (prod 6b689783a, thread EE7B4DCA, 2026-10-21, Merope café kit M2): one 68 s take and
one Cotopaxi photo. "Make a montage of six moments from it, about 2 seconds each, in this
order: Caro arriving with the black tray, her hello wave, ... Caro's bye-bye wave." The Main
Creator kept that sentence as brief r1 `order` "Caro arriving with tray, her hello wave,
Cotopaxi photo, ..." with `facts: {}`: no moment noun, no count, no length. `brief_moment_ask`
was None, the KRI-596/686 moment-window path never ran, and the phone cut the take once
(56.9-59.9 s): a 4.7 s draft against the 14 s asked, refused after approval. The draft and the
approval card still promised "I will cut the 6 exact moments in order". The recovery message
("Cut six seperate moments from the video, about 2 seconds each, in the order I gave") changed
nothing: "six seperate moments" lost its count to the adjective, and the brief kept r1 as is.

Ways this can fail, each pinned below:

1. The count and the per-moment length live only in the model's paraphrase; when the
   paraphrase drops them, the brief carries no moment ask (the incident).
2. A count with one word between the number and the noun ("six separate moments", the typo
   "seperate", "altı ayrı an", "the 6 exact moments") loses the count.
3. That one-word slot over-reaches: "2 more moments", "3 extra shots", "5 minute shots",
   "2 sec moments", "I filmed 6 separate shots" become counts.
4. The server's copy over-reaches: a message with no moment ask (a footage count, a per-clip
   hold, a total length, a picker answer, a quoted title such as "Top 5 moments") adds a
   moment requirement and slices the takes.
5. The server's copy duplicates: a brief that already carries the same ask (the recovery turn
   restating it, or the model recording it itself) gets a second moment requirement, or a
   moment requirement the model wrote without the numbers gets a twin instead of the facts.
6. The server's copy corrupts the turn: it adds to a batch `apply_updates` refuses, writes a
   description over the 300-character limit, or brings back a moment ask the creator withdrew
   in this very message.
7. A changed count ("make it 8 moments") leaves the old count live next to the new one.
8. Read but not planned: with no creator total proven (the photo's "1 second" next to "about
   14 seconds"), the moments are sized 2.5 s instead of the 2 s asked, so six of them plus the
   photo overshoot the 14 s; or the windows overlap, leave the take, or play out of order.
9. The approval text promises moments the plan does not hold: the draft check has no cut
   lengths, so the moment requirement is never judged before approval and "I will cut the 6
   exact moments" reaches the card over a one-cut plan (also when only the summary, not the
   creator, names the count).
10. Wired into one planner path only: the first turn (Main Creator) carries the ask, the
    edit-first path (brief extractor, how the recovery turn is read) does not.
11. Wired into two planner paths but not the third: the deferred extraction
    (`extract_deferred_brief`, the post-render copilot-first path with approval readiness)
    reads the same extractor, whose paraphrase drops the numbers, and stores the brief
    without them. The same call can run without the prior brief (a brief that already holds
    the ask gets a twin, a changed count a second live ask), or add a moment ask to a message
    that gave none.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.brief_extractor import BriefExtractionOutput
from app.agents._schemas.creator_agent import AskUser, CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.planner import (
    PlannedKriaTurn,
    _enrich_best_parts_snapshot,
    _moment_count_notice,
    adapt_creator_action,
)
from app.kria.style_asks import brief_moment_ask, carry_moment_ask, moment_ask
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, Job, PlanItem
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_creative_brief import _ASK, _MANIFEST, _wire_planner
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit
from tests.pipeline.test_unified_montage import visual

# -- the incident's own words and media -------------------------------------------------

VIDEO = "analysis-proxy-ios-11CF148F-2374-4097-B299-FC3926E878BD.mp4"
PHOTO = "asset-6ae61e1a-96ff-4776-8f95-54ace669db23"
TAKE_S = 68.37
PROMPT = (
    "One 68-second take at Merope, a Colombian café: Caro brings us the house oblea and Atakan "
    "takes the first bite. Make a montage of six moments from it, about 2 seconds each, in this "
    "order: Caro arriving with the black tray, her hello wave, the plate pushed close to the "
    "camera, Atakan's first bite, his thumbs-up, Caro's bye-bye wave. Put the Cotopaxi photo "
    'right after the hello wave for 1 second with the text "From Ecuador 🇪🇨". Freeze the frame '
    "for one second on Atakan's bite. Keep the original sound, no music. Title: \"Colombian "
    'dessert, Ecuadorian hands". About 14 seconds.'
)
MOMENT_SENTENCE = (
    "Make a montage of six moments from it, about 2 seconds each, in this order: Caro arriving "
    "with the black tray, her hello wave, the plate pushed close to the camera, Atakan's first "
    "bite, his thumbs-up, Caro's bye-bye wave."
)
# What the brief keeps of it: the list after the colon stays with the order ask (r1).
MOMENT_ASK = "Make a montage of six moments from it, about 2 seconds each, in this order"
RECOVERY = (
    "Cut six seperate moments from the video, about 2 seconds each, in the order I gave. If you "
    "can’t freeze the frame, skip the freeze"
)
SUMMARY = (
    "I will cut the 6 exact moments in order, insert the photo with text, and keep the "
    "original cafe sound."
)
# What clip analysis returned for the take (job 417eecd4): talk beats, not the named moments.
BEST_MOMENTS = [
    {"start_s": 10.74, "end_s": 15.34, "energy": 6.0, "description": "waitress introduces herself"},
    {"start_s": 15.34, "end_s": 22.84, "energy": 7.0, "description": "waitress describes dessert"},
    {"start_s": 42.44, "end_s": 45.74, "energy": 6.0, "description": "waitress explains the name"},
    {"start_s": 50.04, "end_s": 52.04, "energy": 5.0, "description": "waitress hands over dessert"},
    {"start_s": 57.04, "end_s": 59.84, "energy": 8.0, "description": "man takes first bite"},
]


def _turn1_updates() -> list[BriefUpdate]:
    """Brief v1 exactly as the Main Creator wrote it on turn 1 (r1-r7)."""
    rows = [
        {
            "kind": "order",
            "scope": "global",
            "description": (
                "Caro arriving with tray, her hello wave, Cotopaxi photo, plate pushed close, "
                "Atakan's bite, his thumbs-up, Caro's bye-bye wave"
            ),
        },
        {
            "kind": "timing",
            "scope": f"clip:{PHOTO}",
            "description": "1 second",
            "facts": {"duration_s": 1},
        },
        {
            "kind": "timing",
            "scope": "global",
            "description": "Freeze the frame for one second on Atakan's bite",
            "facts": {"duration_s": 1, "freeze_frame": True},
        },
        {
            "kind": "audio",
            "scope": "global",
            "description": "Keep the original sound, no music",
            "facts": {"audio_intent": "source_unchanged"},
        },
        {"kind": "text", "scope": "title", "literal": "Colombian dessert, Ecuadorian hands"},
        {"kind": "text", "scope": f"clip:{PHOTO}", "literal": "From Ecuador 🇪🇨"},
        {
            "kind": "timing",
            "scope": "global",
            "description": "About 14 seconds",
            "facts": {"duration_s": 14},
        },
    ]
    return [BriefUpdate(operation="add", **row) for row in rows]


def _brief(updates, prior: CreativeBrief | None = None, *, version: int = 1) -> CreativeBrief:
    return apply_updates(prior, updates, source_turn_id="t").model_copy(update={"version": version})


RECORDED_V1 = _brief(_turn1_updates())  # what prod stored: no moment ask anywhere


def _select(text: str, **facts) -> BriefRequirement:  # noqa: ANN003
    return BriefRequirement(id="r9", kind="select", scope="global", description=text, facts=facts)


def _count_length(brief: CreativeBrief) -> tuple:
    ask = brief_moment_ask(brief)
    return (ask.count, ask.each_s) if ask is not None else (None, None)


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


# -- 2, 3: the count keeps across one adjective, and only there ---------------------------


@pytest.mark.parametrize(
    ("text", "count", "each_s"),
    [
        (MOMENT_SENTENCE, 6, 2.0),
        (RECOVERY, 6, 2.0),
        ("Cut six separate moments from the video", 6, None),
        ("6 distinct shots from the take", 6, None),
        ("altı ayrı an seç, her biri 2 saniye", 6, 2.0),
        ("I will cut the 6 exact moments in order", 6, None),
        ("6 short moments", 6, None),  # a filler word keeps working as before
    ],
)
def test_a_count_keeps_across_one_adjective(text: str, count: int, each_s: float | None) -> None:
    ask = moment_ask(_select(text))
    assert ask is not None and (ask.count, ask.each_s) == (count, each_s), text


@pytest.mark.parametrize(
    "text",
    [
        "add 2 more moments",
        "give me 3 extra shots",
        "take 5 minute shots",
        "2 sec moments please",
        "I filmed 6 separate shots",
        "use my 6 separate clips",
        "6 separate clips, 2 seconds each",
    ],
)
def test_the_adjective_slot_never_makes_a_count(text: str) -> None:
    ask = moment_ask(_select(text))
    assert ask is None or ask.count is None, text


# -- 1, 4-7: the creator's own words reach the brief ---------------------------------------


def test_the_incident_turn_carries_the_creators_count_and_length() -> None:
    updates = carry_moment_ask(None, _turn1_updates(), PROMPT)

    # The model's own seven updates are untouched and keep their order.
    assert list(updates[:7]) == _turn1_updates()
    (moment,) = updates[7:]
    assert (moment.operation, moment.kind, moment.scope) == ("add", "select", "global")
    assert moment.description == MOMENT_ASK
    assert moment.facts == {"moment_count": 6, "moment_length_s": 2.0}
    assert _count_length(_brief(updates)) == (6, 2.0)
    # The recorded brief alone holds no moment ask: this is what prod lost.
    assert brief_moment_ask(RECORDED_V1) is None


def test_the_recovery_message_alone_would_have_carried_it() -> None:
    withdraw_freeze = BriefUpdate(
        operation="remove", target_requirement_id="r3", expected_version=1
    )

    updates = carry_moment_ask(RECORDED_V1, [withdraw_freeze], RECOVERY)

    assert updates[0] == withdraw_freeze
    (moment,) = updates[1:]
    assert moment.facts == {"moment_count": 6, "moment_length_s": 2.0}
    assert moment.description.startswith("Cut six seperate moments from the video")
    after = _brief(updates, RECORDED_V1, version=2)
    assert _count_length(after) == (6, 2.0)
    assert {req.id for req in after.live()} == {"r1", "r2", "r4", "r5", "r6", "r7", "r8"}


def test_a_brief_that_already_carries_the_ask_gets_nothing_new() -> None:
    carried = _brief(carry_moment_ask(None, _turn1_updates(), PROMPT))
    withdraw_freeze = BriefUpdate(
        operation="remove", target_requirement_id="r3", expected_version=1
    )
    assert carry_moment_ask(carried, [withdraw_freeze], RECOVERY) == (withdraw_freeze,)

    # The model recorded the numbers itself (KRI-667 facts): nothing to add.
    own = _turn1_updates()
    own[0] = own[0].model_copy(update={"facts": {"moment_count": 6, "moment_length_s": 2}})
    assert carry_moment_ask(None, own, PROMPT) == tuple(own)


def test_a_moment_requirement_without_numbers_gets_the_facts_not_a_twin() -> None:
    picked = BriefUpdate(kind="select", scope="global", description="the best moments of the take")

    updates = carry_moment_ask(None, [picked], PROMPT)

    (only,) = updates
    assert only.description == picked.description
    assert only.facts == {"moment_count": 6, "moment_length_s": 2.0}


def test_a_changed_count_replaces_the_old_one() -> None:
    carried = _brief(carry_moment_ask(None, _turn1_updates(), PROMPT), version=2)
    moment_id = next(req.id for req in carried.live() if req.kind == "select")

    (change,) = carry_moment_ask(carried, [], "make it 8 moments")

    assert (change.operation, change.target_requirement_id) == ("change", moment_id)
    assert change.expected_version == 2
    after = _brief([change], carried, version=3)
    assert _count_length(after) == (8, 2.0)
    assert [req.id for req in after.live() if req.kind == "select"] == [moment_id]


def test_a_withdrawn_moment_ask_does_not_come_back() -> None:
    carried = _brief(carry_moment_ask(None, _turn1_updates(), PROMPT), version=2)
    moment_id = next(req.id for req in carried.live() if req.kind == "select")
    remove = BriefUpdate(operation="remove", target_requirement_id=moment_id, expected_version=2)

    message = "Forget the six moments, just use the best part"
    assert carry_moment_ask(carried, [remove], message) == (remove,)


@pytest.mark.parametrize(
    "message",
    [
        "His thumbs-up: clip 1",
        "I filmed 10 clips, use 4 of them",
        "use 4 clips, 2 seconds each",
        "photos 2 seconds each",
        "make it 20 seconds",
        'Title: "Top 5 moments in Lisbon"',
        "Başlık: “En güzel 5 an” olsun",
        "",
    ],
)
def test_a_message_without_a_moment_ask_adds_nothing(message: str) -> None:
    updates = _turn1_updates()
    assert carry_moment_ask(None, updates, message) == tuple(updates)


def test_words_the_creator_wants_on_screen_are_not_a_moment_ask() -> None:
    # An unquoted title the model recorded as exact text is words to show, not six cuts.
    title = BriefUpdate(kind="text", scope="title", literal="6 moments of joy")
    assert carry_moment_ask(None, [title], "Title: 6 moments of joy") == (title,)
    # The same words with no recorded title are a moment ask.
    (moment,) = carry_moment_ask(None, [], "Cut 6 moments of joy")
    assert moment.facts == {"moment_count": 6}


def test_a_long_moment_sentence_fits_the_brief() -> None:
    long = "Make a montage of six moments, about 2 seconds each: " + ", ".join(
        f"the shot of the {name}" for name in ("harbour", "boat", "tray", "plate", "bite") * 6
    )
    (moment,) = carry_moment_ask(None, [], long)
    assert len(moment.description) <= 300
    assert _count_length(_brief([moment])) == (6, 2.0)


def test_a_batch_the_brief_refuses_is_left_for_the_planner_to_refuse() -> None:
    stale = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=7,
        kind="order",
        scope="global",
        description="x",
    )
    assert carry_moment_ask(RECORDED_V1, [stale], PROMPT) == (stale,)


# -- 10: both planner paths carry it (real Postgres session) -------------------------------


def _seed(*, job_status: str | None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        if job_status is not None:
            job = Job(
                id=uuid.uuid4(),
                user_id=user_id,
                status=job_status,
                mode="content_plan",
                raw_storage_path="users/t/raw.mp4",
                selected_platforms=[],
            )
            db.add(job)
            db.flush()
            db.get(PlanItem, item_id).current_job_id = job.id
        db.commit()
    return user_id, thread_id, item_id


async def _plan_turn(monkeypatch, *, message, prior, job_status, creator_updates, extracted):  # noqa: ANN001, ANN202
    user_id, thread_id, item_id = _seed(job_status=job_status)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)  # as in prod (M2)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", True)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item_id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=prior))
    target = SimpleNamespace(
        job_id=uuid.uuid4(),
        snapshot={"text_bars": [], "allowed_op_families": ["text", "clip"]},
        conversation=[],
    )
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    monkeypatch.setattr(
        planner,
        "run_copilot_turn",
        AsyncMock(
            return_value=SimpleNamespace(
                intent="edit",
                ops=[{"op": "edit_text", "bar_index": 0, "text": "x"}],
                reply="r",
                outcome="proposed",
                rejection_reasons=[],
                unmet_requests=[],
            )
        ),
    )

    async def extractor(_inputs, **_kw):  # noqa: ANN001, ANN202
        return BriefExtractionOutput(brief_updates=list(extracted))

    async def creator(_inputs, **_kw):  # noqa: ANN001, ANN202
        return SimpleNamespace(action=AskUser(**_ASK), brief_updates=list(creator_updates))

    monkeypatch.setattr(planner, "_call_brief_extractor", extractor)
    monkeypatch.setattr(planner, "_call_main_creator", creator)
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await planner.plan_live_turn(
                db, thread_id=thread_id, item_id=item_id, creator_id=user_id, user_message=message
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_first_turn_brief_carries_six_moments_of_two_seconds(monkeypatch) -> None:  # noqa: ANN001
    result = await _plan_turn(
        monkeypatch,
        message=PROMPT,
        prior=None,
        job_status=None,
        creator_updates=_turn1_updates(),
        extracted=(),
    )

    assert result.brief_updates[:7] == tuple(_turn1_updates())
    assert _count_length(_brief(result.brief_updates)) == (6, 2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_status", "reader"),
    [
        # Prod: the last render failed, so the Main Creator read the recovery message.
        ("processing_failed", "creator"),
        # After a finished render the brief extractor reads an edit first (KRI-188).
        ("variants_ready", "extractor"),
    ],
)
async def test_the_recovery_turn_carries_them_on_either_reader(
    monkeypatch,  # noqa: ANN001
    job_status: str,
    reader: str,
) -> None:
    withdraw_freeze = BriefUpdate(
        operation="remove", target_requirement_id="r3", expected_version=1
    )
    result = await _plan_turn(
        monkeypatch,
        message=RECOVERY,
        prior=RECORDED_V1,
        job_status=job_status,
        creator_updates=(withdraw_freeze,) if reader == "creator" else (),
        extracted=(withdraw_freeze,) if reader == "extractor" else (),
    )

    after = _brief(result.brief_updates, RECORDED_V1, version=2)
    assert _count_length(after) == (6, 2.0)
    assert "r3" not in {req.id for req in after.live()}


# -- 11: the deferred extraction carries them too (no database: the planner's own fakes) ----


async def _deferred(monkeypatch, *, message, prior, extracted):  # noqa: ANN001, ANN202
    """`extract_deferred_brief` with an extractor that returns exactly `extracted`."""
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, copilot, creator_runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=None
    )
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=prior))

    async def extractor(_inputs, **_kw):  # noqa: ANN001, ANN202
        return BriefExtractionOutput(brief_updates=list(extracted))

    monkeypatch.setattr(planner, "_call_brief_extractor", extractor)
    updates, route = await planner.extract_deferred_brief(
        db, thread_id=item.id, item_id=item.id, creator_id=creator_id, user_message=message
    )
    copilot.assert_not_awaited()  # nothing but the brief extraction ran
    assert not creator_runs
    return updates, route


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["first_message", "recovery_message"])
async def test_the_deferred_extraction_carries_what_the_extractor_lost(
    monkeypatch,  # noqa: ANN001
    case: str,
) -> None:
    withdraw_freeze = BriefUpdate(
        operation="remove", target_requirement_id="r3", expected_version=1
    )
    if case == "first_message":
        message, prior, extracted = PROMPT, None, _turn1_updates()
    else:
        message, prior, extracted = RECOVERY, RECORDED_V1, [withdraw_freeze]

    updates, route = await _deferred(monkeypatch, message=message, prior=prior, extracted=extracted)

    # The extractor's own updates stay first and untouched; the numbers ride behind them.
    assert list(updates[: len(extracted)]) == list(extracted)
    (moment,) = updates[len(extracted) :]
    assert moment.facts == {"moment_count": 6, "moment_length_s": 2.0}
    after = _brief(updates, prior, version=2 if prior else 1)
    assert _count_length(after) == (6, 2.0)
    freeze_kept = "r3" in {req.id for req in after.live()}
    assert freeze_kept is (case == "first_message")  # the recovery message withdraws r3
    assert route is not None  # not the clarify early-return


@pytest.mark.asyncio
async def test_the_deferred_extraction_reads_the_brief_it_extends(monkeypatch) -> None:  # noqa: ANN001
    carried = _brief(carry_moment_ask(None, _turn1_updates(), PROMPT), version=2)
    moment_id = next(req.id for req in carried.live() if req.kind == "select")

    # The recovery message restates an ask the brief already holds: nothing new, no twin.
    updates, _route = await _deferred(monkeypatch, message=RECOVERY, prior=carried, extracted=[])
    assert updates == ()

    # A changed count changes that requirement; the old count does not stay live beside it.
    updates, _route = await _deferred(
        monkeypatch, message="make it 8 moments", prior=carried, extracted=[]
    )
    (change,) = updates
    assert (change.operation, change.target_requirement_id) == ("change", moment_id)
    assert change.expected_version == 2
    after = _brief(updates, carried, version=3)
    assert _count_length(after) == (8, 2.0)
    assert [req.id for req in after.live() if req.kind == "select"] == [moment_id]


@pytest.mark.asyncio
async def test_the_deferred_extraction_adds_no_moment_ask_to_a_message_without_one(
    monkeypatch,  # noqa: ANN001
) -> None:
    title = BriefUpdate(kind="text", scope="title", literal="Colombian dessert, Ecuadorian hands")

    updates, _route = await _deferred(
        monkeypatch,
        message='Change the title to "Colombian dessert, Ecuadorian hands"',
        prior=RECORDED_V1,
        extracted=[title],
    )

    assert updates == (title,)


# -- 8: the take is cut into six ~2 s moments, in order, and the phone plan holds them -----


STRATEGY = {
    "direction": "guided_story",
    "edit_format": "montage",
    "render_program": "guided",
    "pacing": "balanced",
    "media_scope": "all",
    "selected_media_ids": [],
    "audio_strategy": "original_audio",
    "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
    "opening_title": "Colombian dessert, Ecuadorian hands",
    "target_duration_s": 14,
    "video_reuse_policy": "once",
    "resolved_clip_intents": [
        {
            "op": "order",
            "status": "resolved",
            "intent_id": "order_sequence",
            "attribute": (
                "Caro arriving with the black tray, her hello wave, the plate pushed close to "
                "the camera, Atakan's first bite, his thumbs-up, Caro's bye-bye wave"
            ),
            "assignments": [{"media_id": VIDEO, "confidence": 0.9, "evidence": "the take"}],
        },
        {
            "op": "caption",
            "status": "resolved",
            "intent_id": "caption_cotopaxi",
            "attribute": "the Cotopaxi photo",
            "assignments": [{"media_id": PHOTO, "confidence": 0.9, "evidence": "a mountain"}],
            "caption_text": "From Ecuador 🇪🇨",
            "creator_text": "From Ecuador 🇪🇨",
            "caption_grounding": "creator_text",
        },
    ],
}
PROXY = f"users/u/creation-threads/t/{VIDEO}"


def _snapshot(take_s: float = TAKE_S) -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": VIDEO,
                "kind": "video",
                "gcs_path": PROXY,
                "storage_generation": "1792587128136153",
                "duration_s": take_s,
                "analysis": {"best_moments": BEST_MOMENTS, "duration_s": take_s},
            }
        ]
    }


def _carried_brief() -> CreativeBrief:
    return _brief(carry_moment_ask(None, _turn1_updates(), PROMPT))


def test_the_take_is_cut_into_six_moments_of_about_two_seconds_in_order() -> None:
    brief = _carried_brief()
    enriched = _enrich_best_parts_snapshot(STRATEGY, _snapshot(), brief)
    length_plan = enriched["length_plan"]

    assert not length_plan["conflicts"]
    windows = [(w["source_start_s"], w["source_end_s"]) for w in length_plan["windows"]]
    assert len(windows) == 6
    assert all(end - start == pytest.approx(2.0, abs=0.01) for start, end in windows)
    assert windows == sorted(windows)  # the take plays forward, one moment after the other
    assert all(0 <= start < end <= TAKE_S for start, end in windows)
    assert all(b[0] >= a[1] for a, b in zip(windows, windows[1:], strict=False))
    assert _moment_count_notice(STRATEGY, brief, length_plan) is None

    # The phone plan the worker builds from that approved plan holds the six moments and the
    # photo, and lands within the creator's "about 14 seconds".
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-776"),
        brief,
        latest_message=PROMPT,
        media_snapshot=enriched,
    )
    clips = [
        UnifiedClip(
            media_id=VIDEO,
            proxy_path=PROXY,
            generation="1792587128136153",
            duration_s=TAKE_S,
            width=1080,
            height=1920,
            analysis=_snapshot()["clip_assignments"][0]["analysis"],
        )
    ]
    photo = dataclasses.replace(visual(1), manifest_id=PHOTO)
    plan = plan_unified_montage(
        clips,
        brief_view(binding.resolve()),
        strategy=STRATEGY,
        clip_intents_enabled=True,
        approved_length_plan=length_plan,
        visuals=[photo],
    )
    take_cuts = [
        (cut.source_start_s, cut.source_end_s)
        for cut in plan.snapshot.fast_cuts
        if cut.media_id == VIDEO
    ]
    assert take_cuts == windows
    assert abs(plan.duration_s - 14.0) <= 1.4  # the brief's 10 % length check
    receipts = {
        receipt.requirement_id: receipt
        for receipt in build_receipts(
            brief.live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
        )
    }
    moment_id = next(req.id for req in brief.live() if req.kind == "select")
    assert receipts[moment_id].status == "met", receipts[moment_id].reason
    assert receipts["r7"].status == "met", receipts["r7"].reason


def test_moments_without_a_given_length_keep_their_default_size() -> None:
    # Only the creator's own "2 seconds each" sizes a moment: "six very short moments"
    # with no total stays at the KRI-596 default share.
    brief = _brief(
        [BriefUpdate(kind="select", scope="global", description="six very short moments")]
    )
    windows = _enrich_best_parts_snapshot(STRATEGY, _snapshot(), brief)["length_plan"]["windows"]
    assert len(windows) == 6
    assert all(w["source_end_s"] - w["source_start_s"] == pytest.approx(2.5) for w in windows)


# -- 9: the approval text never promises moments the plan does not hold --------------------


def test_a_summary_that_counts_more_moments_than_planned_gets_a_notice() -> None:
    one_cut = _enrich_best_parts_snapshot(STRATEGY, _snapshot(), RECORDED_V1)["length_plan"]
    assert len(one_cut["windows"]) == 1  # the incident's plan

    notice = _moment_count_notice(STRATEGY, RECORDED_V1, one_cut, summary=SUMMARY)

    assert notice == "I could fit 1 moment, not 6."
    # A summary that promises what the plan holds, or no count at all, gets none.
    six = _enrich_best_parts_snapshot(STRATEGY, _snapshot(), _carried_brief())["length_plan"]
    assert _moment_count_notice(STRATEGY, RECORDED_V1, six, summary=SUMMARY) is None
    plain = "A montage of the café visit with the original sound."
    assert _moment_count_notice(STRATEGY, RECORDED_V1, one_cut, summary=plain) is None


@pytest.fixture
def _runtime_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])


@pytest_asyncio.fixture
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


async def _draft(monkeypatch, take_s: float) -> tuple[str, str | None, str | None]:  # noqa: ANN001
    """Run the real runtime-v2 draft turn for the incident's brief and a planned length."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage"}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [PROXY]
        item.clip_assignments = _snapshot(take_s)["clip_assignments"]
        revision = int(thread.revision)
        db.commit()
    # The incident's brief without r1: its order is settled by the strategy's resolved intents,
    # which this drafted strategy does not carry (the runtime would ask an order question).
    updates = carry_moment_ask(None, _turn1_updates()[1:], PROMPT)
    snapshot = _enrich_best_parts_snapshot(
        STRATEGY, {"clip_paths": [PROXY], **_snapshot(take_s)}, _brief(updates)
    )
    planned = PlannedKriaTurn(
        plan=adapt_creator_action(
            ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="guided_story",
                    edit_format="montage",
                    audio_strategy="original_audio",
                    pacing="balanced",
                    render_program="guided",
                    selected_media_ids=[],
                    rationale="Six moments of the café take in order, then the photo.",
                    target_duration_s=14,
                ),
                summary=SUMMARY,
            )
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=updates,
        brief_route="replan",
        brief_clip_ids=(VIDEO,),
        media_snapshot=snapshot,
    )

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    accepted = await _submit(user_id, thread_id, PROMPT, revision)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        draft = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    # The draft, or the reply that held it back before approval.
                    CreationThreadEvent.event_type.in_(("draft_applied", "assistant_response")),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        approval = db.execute(
            select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
        ).scalar_one_or_none()
        return (
            result["status"],
            str(draft.content) if draft else None,
            approval.consequence_summary if approval else None,
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_runtime_flags", "_dispose_async_engine")
async def test_six_moments_card_keeps_the_checked_summary_and_freeze_limit(monkeypatch) -> None:  # noqa: ANN001
    status, reply, card = await _draft(monkeypatch, TAKE_S)

    assert status == "awaiting_approval"
    # KRI-777's unsupported freeze stays visible beside the fulfilled moment ask.
    # Both the reply and approval card use the checked summary, not the model's promise.
    assert reply.startswith("Not everything you asked for made it in:\n")
    assert "Couldn't: Freeze the frame for one second on Atakan's bite" in reply
    assert "I can't freeze a frame in this edit yet, so that moment plays normally" in reply
    assert "you asked for 1s" not in reply
    assert any(
        line.startswith("- Done: Make a montage of six moments")
        and line.endswith("(6 moments, the longest 2.0 s)")
        for line in reply.splitlines()
    )
    assert card == f"Render this draft: {reply}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_runtime_flags", "_dispose_async_engine")
async def test_a_plan_with_one_cut_never_promises_six_moments(monkeypatch) -> None:  # noqa: ANN001
    # A 4 s take cannot hold six 2 s moments: the plan keeps one window. The draft is held
    # back with the checked shortfall, and no approval card promises the six moments.
    status, reply, card = await _draft(monkeypatch, 4.0)

    assert status == "completed"
    assert card is None
    assert "(The video has 1 moment, not 6)" in reply
    assert "6 exact moments" not in reply
