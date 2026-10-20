"""KRI-763: "Ilk klip M1_08" pinned M1_12 because the file name was read as the 8th attachment.

Prod thread A9100235 (2026-10-19, café M1 run): twelve iPhone clips attached in the order
02,05,03,07,11,01,09,12,04,06,10,08, each stored as ``<UUID>-M1_NN_footage.mp4`` (so the app
and the clip records show ``M1_NN_footage``). The creator wrote "Ilk klip M1_08, son klip
M1_03. M1_05 klibini kullanma". The planner minted ``first "M1_08"``, ``last "M1_03"`` and
``exclude "M1_05 klibini"``; the KRI-614 file-name pass skipped all three, so the Gemini
resolver saw "M1_08" next to the aliases m001..m012 and answered "explicitly selected m008"
(the 8th attachment = M1_12). The first-clip question about "Caro's black tray clip" from the
earlier message stayed open beside it, so the picker was asked again with no suggestion.

Ways this can fail, each pinned below:
 1. "M1_08" does not match the clip named "M1_08_footage" (the generic suffix), so an ordinal
    guess decides instead.
 2. The same name typed with an extension, an upload UUID prefix, other case or a Turkish
    dotless ı does not match either.
 3. Two clips that differ only by the suffix ("M1_08" and "M1_08_footage") are ambiguous: one
    is picked silently instead of asking.
 4. "M1_05 klibini" (an exclude) is never settled by file name: ``exclude`` is not a picker
    intent and the Turkish "klibini" is not a filler word, so the resolver guesses.
 5. An exclude that only mentions a file inside a description ("clips like M1_05") is settled
    as if it named one.
 6. An earlier message's first-clip pin stays beside the new file-name pin (a stacked second
    first clip, or a picker asked again).
 7. The replacement reaches too far: another position, a pin from the same message, a
    position-less "then" row, an intent with no known source, or a tapped picker answer for an
    unrelated intent is dropped.
 8. A typed "m008" (an internal handle) is matched against the 8th clip.
 9. The resolver answers "explicitly selected m008" for words that are not that alias (a file
    name no clip carries, e.g. an older attachment with no stored name) and the guess is kept
    at full confidence.
10. The prod journey as a whole: first M1_08, last M1_03, M1_05 left out, resolver never asked.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import creator_sessions
from app.services.clip_file_names import ClipNameIndex
from app.services.clip_intent_planning import superseded_pin_ids
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_selection import ClipSelections, SelectionEntry, intent_key
from tests.kria.test_kri614_file_name_clips import _attach, _empty_db, _order, _planner, _seated

UPLOAD_ORDER = ["02", "05", "03", "07", "11", "01", "09", "12", "04", "06", "10", "08"]


def _upload(number: str) -> tuple[str, str]:
    return (f"clip-m1-{number}.mp4", f"{str(uuid.uuid4()).upper()}-M1_{number}_footage.mp4")


M1_UPLOADS = [_upload(number) for number in UPLOAD_ORDER]
EARLIER = (
    "Caro'nun elinde siyah tepsiyle geldiği klip ilk olsun. "
    "Bye bye dediğimiz klip en sonda olsun. Atakan'ın ilk ısırığı yazsın."
)
LATEST = "Ilk klip M1_08, son klip M1_03. M1_05 klibini kullanma"


def _exclude(intent_id: str, attribute: str, quote: str) -> dict:
    return {**_order(intent_id, attribute, None, quote), "op": "exclude"}


async def _plan(item, request: str, latest: str | None = None, **kwargs):  # noqa: ANN001, ANN202
    persona = SimpleNamespace(user_id=uuid.uuid4())
    clips = await creator_sessions.load_intent_clips_for_item(_empty_db(), item, persona)
    return clips, await planning.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=latest if latest is not None else request,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
        **kwargs,
    )


def _members(planned) -> dict[str, list[str]]:  # noqa: ANN001
    return {
        intent.intent_id: [a.media_id for a in intent.assignments]
        for intent in planned.resolution.intents
    }


# ── 1-3, 8: the name index ────────────────────────────────────────────────────

CLIP_NAMES = {f"id-{n}": f"M1_{n}_footage" for n in UPLOAD_ORDER}


@pytest.mark.parametrize(
    "typed",
    [
        "M1_08",
        "m1_08",
        "M1_08_footage",
        "m1_08_FOOTAGE",
        "M1_08.mp4",
        "M1_08_footage.MOV",
        "M1-08",
        "M1 08",
        "52250013-F559-42BA-BA8A-E595BD555322-M1_08_footage.mp4",
    ],
)
def test_a_file_name_matches_with_or_without_the_footage_suffix(typed):
    refs, others = ClipNameIndex(CLIP_NAMES).references(typed)

    assert [ref.media_id for ref in refs] == ["id-08"]
    assert others == []


def test_two_clips_that_differ_only_by_the_suffix_are_never_guessed():
    index = ClipNameIndex({"plain": "M1_08", "suffixed": "M1_08_footage"})

    refs, _others = index.references("M1_08")
    assert [ref.media_id for ref in refs] == [None]
    # The longer spelling still names exactly one.
    refs, _others = index.references("M1_08_footage")
    assert [ref.media_id for ref in refs] == ["suffixed"]


@pytest.mark.parametrize("typed", ["m008", "M008", "clip 8", "the 8th clip"])
def test_an_internal_handle_or_ordinal_is_not_a_file_name(typed):
    refs, _others = ClipNameIndex(CLIP_NAMES).references(typed)

    assert [ref for ref in refs if ref.media_id] == []


# ── 4-5: exclude by file name ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute",
    ["M1_05 klibini", "M1_05 klibi", "M1_05 klip", "M1_05", "M1_05_footage", "the M1_05 clip"],
)
async def test_an_exclude_that_names_a_file_is_settled_without_the_resolver(monkeypatch, attribute):
    item = await _attach(monkeypatch, M1_UPLOADS)
    message = f"{attribute} kullanma"
    resolver = _planner(monkeypatch, [_exclude("skip-05", attribute, message)])

    _clips, planned = await _plan(item, message)

    resolver.assert_not_awaited()
    assert _members(planned) == {"skip-05": ["clip-m1-05.mp4"]}
    assert planned.resolution.intents[0].op == "exclude"
    assert planned.resolution.intents[0].assignments[0].evidence == "file name"


@pytest.mark.asyncio
async def test_an_exclude_described_around_a_file_still_goes_to_the_resolver(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    message = "skip the clips like M1_05"
    resolver = _planner(monkeypatch, [_exclude("skip-like-05", "clips like M1_05", message)])

    await _plan(item, message)

    resolver.assert_awaited_once()
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["skip-like-05"]


# ── 6-7: a later pin replaces an earlier one ──────────────────────────────────

TRAY_FIRST = _order(
    "order_first_caro_tray",
    "Caro'nun elinde siyah tepsiyle geldiği klip",
    "first",
    "Caro'nun elinde siyah tepsiyle geldiği klip ilk olsun",
)
BYE_LAST = _order(
    "order_last_bye_bye", "bye bye dediğimiz klip", "last", "Bye bye dediğimiz klip en sonda olsun"
)
M108_FIRST = _order("order_first_m1_08", "M1_08", "first", "Ilk klip M1_08")
M103_LAST = _order("order_last_m1_03", "M1_03", "last", "son klip M1_03")


@pytest.mark.asyncio
async def test_a_file_name_pin_replaces_the_open_first_clip_question(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    resolver = _planner(monkeypatch, [TRAY_FIRST, M108_FIRST])

    _clips, planned = await _plan(item, f"{EARLIER}\n{LATEST}", LATEST)

    resolver.assert_not_awaited()  # no picker for the tray clip: the creator named the clip
    assert planned.resolution.question is None
    assert planned.resolution.clip_question is None
    assert _members(planned) == {"order_first_m1_08": ["clip-m1-08.mp4"]}
    assert [i.intent_id for i in planned.requested_intents] == ["order_first_m1_08"]


@pytest.mark.asyncio
async def test_only_pins_for_the_same_position_are_replaced(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    resolver = _planner(monkeypatch, [TRAY_FIRST, BYE_LAST, M108_FIRST])

    _clips, planned = await _plan(item, f"{EARLIER}\n{LATEST}", LATEST)

    # The first pin is replaced; the earlier "bye bye clip goes last" is still the creator's.
    resolver.assert_awaited_once()
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["order_last_bye_bye"]
    assert "order_first_caro_tray" not in {i.intent_id for i in planned.requested_intents}


@pytest.mark.asyncio
async def test_pins_from_the_same_message_stack_as_before(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    message = "Ilk klip M1_08 ve Caro'nun elinde siyah tepsiyle geldiği klip ilk olsun"
    resolver = _planner(monkeypatch, [TRAY_FIRST, M108_FIRST])

    await _plan(item, message)

    resolver.assert_awaited_once()
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["order_first_caro_tray"]


@pytest.mark.asyncio
async def test_a_position_less_row_from_an_earlier_message_is_kept(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    then = _order("then_tray", "Caro'nun tepsili klibi", None, "Caro'nun tepsili klibi sonra")
    resolver = _planner(monkeypatch, [then, M108_FIRST])

    await _plan(item, f"Caro'nun tepsili klibi sonra\n{LATEST}", LATEST)

    resolver.assert_awaited_once()
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["then_tray"]


@pytest.mark.asyncio
async def test_a_tapped_answer_for_the_replaced_pin_is_replaced_too(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    resolver = _planner(monkeypatch, [TRAY_FIRST, M108_FIRST])
    tray = ClipIntent.model_validate(
        {k: v for k, v in TRAY_FIRST.items() if k not in {"source_quote", "transcript_kind"}}
    )
    tapped = ClipSelections({intent_key(tray): SelectionEntry(media_ids=("clip-m1-12.mp4",))})

    _clips, planned = await _plan(item, f"{EARLIER}\n{LATEST}", LATEST, clip_selections=tapped)

    resolver.assert_not_awaited()
    assert _members(planned) == {"order_first_m1_08": ["clip-m1-08.mp4"]}


def _intent(raw: dict) -> ClipIntent:
    return ClipIntent.model_validate(
        {k: v for k, v in raw.items() if k not in {"source_quote", "transcript_kind"}}
    )


def test_a_pin_with_no_known_source_is_never_replaced():
    tray, m108 = _intent(TRAY_FIRST), _intent(M108_FIRST)
    settled = ResolvedClipIntent(
        **m108.model_dump(),
        status="resolved",
        assignments=[ClipAssignment(media_id="clip-m1-08.mp4", evidence="file name")],
    )
    known = {"order_first_m1_08": M108_FIRST["source_quote"]}

    assert (
        superseded_pin_ids(
            intents=[tray, m108], pinned=[settled], latest_user_message=LATEST, quotes=known
        )
        == set()
    )
    # With its source known (an earlier message), the same pin IS replaced.
    assert superseded_pin_ids(
        intents=[tray, m108],
        pinned=[settled],
        latest_user_message=LATEST,
        quotes={**known, "order_first_caro_tray": TRAY_FIRST["source_quote"]},
    ) == {"order_first_caro_tray"}
    # Nothing in the latest message pins by file name: nothing is replaced.
    assert (
        superseded_pin_ids(intents=[tray], pinned=[], latest_user_message=LATEST, quotes=known)
        == set()
    )
    assert (
        superseded_pin_ids(
            intents=[tray, m108], pinned=[settled], latest_user_message=None, quotes=known
        )
        == set()
    )


# ── 9: the resolver cannot invent an explicit selection ───────────────────────

UNNAMED_CLIPS = [
    IntentClip(
        media_id=f"clip-{n}",
        kind="video",
        analysis={"subject": f"scene {n}"},
        gcs_path=f"users/u/clips/clip-{n}.mp4",
    )
    for n in range(1, 13)
]


def _resolver_says_m008_is_selected(monkeypatch) -> None:  # noqa: ANN001
    output = ClipRequestResolverOutput(
        intents=[
            ResolverIntentOut(
                intent_id="first",
                assignments=[
                    ResolverAssignment(
                        media="m008", confidence=1.0, evidence="Creator explicitly selected m008"
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr(ClipRequestResolverAgent, "run", lambda self, input, *, ctx=None: output)


@pytest.mark.asyncio
async def test_an_explicit_selection_the_request_never_made_is_not_trusted(monkeypatch):
    _resolver_says_m008_is_selected(monkeypatch)
    first = ClipIntent(intent_id="first", op="order", position="first", attribute="M1_08")

    resolution = await resolve_clip_intents_for_turn(
        intents=[first],
        creator_request="Ilk klip M1_08",
        clips=UNNAMED_CLIPS,
        run_context=RunContext(),
    )

    # "M1_08" is not the alias m008: the creator is asked, nothing is pinned at 1.0.
    assert resolution.status == "needs_creator"
    assert [a.media_id for i in resolution.intents for a in i.assignments] == []


@pytest.mark.asyncio
async def test_an_explicit_selection_that_names_the_alias_is_still_trusted(monkeypatch):
    _resolver_says_m008_is_selected(monkeypatch)
    first = ClipIntent(intent_id="first", op="order", position="first", attribute="clip-8")

    resolution = await resolve_clip_intents_for_turn(
        intents=[first],
        creator_request="Start with clip-8",  # a known media id becomes the alias m008
        clips=UNNAMED_CLIPS,
        run_context=RunContext(),
    )

    assert resolution.status == "resolved"
    assert [a.media_id for i in resolution.intents for a in i.assignments] == ["clip-8"]


# ── 10: the prod journey ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prod_message_pins_the_named_files_not_the_attachment_numbers(monkeypatch):
    item = await _attach(monkeypatch, M1_UPLOADS)
    resolver = _planner(
        monkeypatch,
        [
            TRAY_FIRST,
            BYE_LAST,
            M108_FIRST,
            M103_LAST,
            _exclude("exclude_m1_05", "M1_05 klibini", "M1_05 klibini kullanma"),
        ],
    )

    _clips, planned = await _plan(item, f"{EARLIER}\n{LATEST}", LATEST)

    resolver.assert_not_awaited()  # the resolver never sees "M1_08" next to m001..m012
    assert planned.resolution.question is None
    assert planned.resolution.clip_question is None
    assert _members(planned) == {
        "order_first_m1_08": ["clip-m1-08.mp4"],  # not clip-m1-12 (the 8th attachment)
        "order_last_m1_03": ["clip-m1-03.mp4"],
        "exclude_m1_05": ["clip-m1-05.mp4"],
    }
    assert {a.evidence for intent in planned.resolution.intents for a in intent.assignments} == {
        "file name"
    }
    # No second first pin, no second last pin: the earlier descriptions were replaced.
    assert {i.intent_id for i in planned.requested_intents} == set(_members(planned))
    seated = _seated(item, planned)
    assert seated[0] == "clip-m1-08.mp4"
    assert seated[-1] == "clip-m1-03.mp4"
