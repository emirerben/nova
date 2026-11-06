"""R4-A: a card the GET serves is a card whose tap the creative-copy consent gate accepts.

The pinned tap's draft commit asks ``creative_copy_problem`` before it writes anything. An
editor-ops card (captions, music, sound effects, transitions, duplicates, hook) is committed with
no strategy to compare the copy against, so the gate refuses it whenever the thread holds ANY
decision about an on-screen title. A restating card (filler words, mute) is committed against the
head plan, so it is refused only when the plan disagrees with the decision. The serve used to
withhold cards for four of the gate's states only.

Failure catalogue (written first; each row is a test below):

  A1  an open wording discussion (authorship, write_my_own, generate, revise). Nothing is served,
      as before, and nothing the phone holds can be tapped into a refusal.
  A2  a stale decision (the footage changed after the creator answered, as a resolution or as an
      answered wording question). The gate reads "stale" as "finish choosing the wording".
      Served cards used to be refused ``consent_required``.
  A3  a settled decision on the opening title: the creator's own words, a tapped wording, or "skip
      it". The hook card writes the title and is not served; the editor-ops cards that leave the
      title alone are (R5: ``test_r5_serve_copy_gate.py`` reads the ops); the legacy chips (which go
      through the planner and its consent handling) are untouched.
  A4  a settled decision on the CLOSING title. The hook kind only looks at the opening title, so
      its card was served and refused. Same lane, same answer.
  A5  a restating card against a plan that agrees with the decision. The gate accepts it, so it is
      still served and the tap executes. Withholding it too would take the card away from every
      creator who ever wrote a title.
  A6  the same restating card against a plan that disagrees (a different title, or a cancelled
      title the plan still carries). Not served.
  A7  the legacy chips of the same thread are what they were without the decision (flag-off and
      chip behavior is byte-identical; the stripper only touches v2 cards).

Every check drives the real path: GET route -> the real worker body with a cassette -> GET ->
``submit_turn`` -> ``run_kria_turn`` -> ``_complete_draft_turn``. A tap "executes" when the turn
was PLANNED and wrote its draft; a refusal carries the typed ``suggestion_pin`` signal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.database import sync_session
from app.models import CreationThread, CreationThreadEvent
from app.services.creative_copy_decisions import (
    OPT_APPROVE,
    OPT_CANCEL,
    OPT_GENERATE,
    OPT_REVISE,
    OPT_WRITE_MY_OWN,
    authorship_question,
    fold_creative_copy,
    wording_question,
)
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"
PROFILES = (EDITOR, SIGNALS, TALKING)
# The editor-ops cards that write the title: gated under every settled decision (R5 split the
# rest off: a card whose ops leave the title alone does not carry copy).
EDITOR_OPS_KINDS = frozenset({"hook"})
STALE_DIGEST = "0" * 24


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


def append(seeded: h.Seeded, *rows: tuple[str, str, str | None, dict]) -> None:
    """Append ``(role, event_type, content, payload)`` rows after the thread's newest event."""
    with sync_session() as db:
        thread = db.get(CreationThread, seeded.thread_id, with_for_update=True)
        sequence = (
            db.execute(
                h.select(h.func.max(CreationThreadEvent.sequence)).where(
                    CreationThreadEvent.thread_id == seeded.thread_id
                )
            ).scalar_one()
            or 0
        )
        for role, event_type, content, payload in rows:
            sequence += 1
            thread.revision = int(thread.revision) + 1
            db.add(
                CreationThreadEvent(
                    thread_id=seeded.thread_id,
                    sequence=sequence,
                    revision=int(thread.revision),
                    role=role,
                    event_type=event_type,
                    content=content,
                    payload=payload,
                )
            )
        db.commit()


def _resolution(status: str, digest: str, *, target: str = "opening_title", text: str = ""):
    payload: dict = {"target": target, "status": status, "dependency_digest": digest}
    if text:
        payload["text"] = text
    return [("assistant", "assistant_response", "Got it.", {"creative_copy_resolution": payload})]


def _answered(question: dict, option: str) -> list[tuple[str, str, str | None, dict]]:
    """A question the assistant asked, the creator's tapped answer, and the reply that followed (so
    the newest event is not an open question)."""
    return [
        ("assistant", "assistant_response", "A question.", {"choice_question": question}),
        (
            "user",
            "user_message",
            "My answer",
            {
                "turn_status": "completed",
                "choice_selection": {"question_id": question["question_id"], "option_key": option},
            },
        ),
        ("assistant", "assistant_review", "Noted.", {"turn_value": "review"}),
    ]


def build_events(name: str, digest: str):  # noqa: ANN201
    authorship = authorship_question(target="opening_title", dependency_digest=digest)
    wording = wording_question(
        target="opening_title", candidate="Matcha restock", dependency_digest=digest
    )
    stale_wording = wording_question(
        target="opening_title", candidate="Matcha restock", dependency_digest=STALE_DIGEST
    )
    return {
        "authorship": [
            ("assistant", "assistant_response", "A question.", {"choice_question": authorship})
        ],
        "write_my_own": _answered(authorship, OPT_WRITE_MY_OWN),
        "generate": _answered(authorship, OPT_GENERATE),
        "revise": _answered(wording, OPT_REVISE),
        "stale_resolution": _resolution("creator_supplied", STALE_DIGEST, text="My own title"),
        "stale_question": _answered(stale_wording, OPT_APPROVE),
        "creator_supplied": _resolution("creator_supplied", digest, text="My own title"),
        "approved_wording": _answered(wording, OPT_APPROVE),
        "cancelled_resolution": _resolution("cancelled", digest),
        "cancelled_answer": _answered(authorship, OPT_CANCEL),
        "closing_supplied": _resolution(
            "creator_supplied", digest, target="closing_title", text="See you"
        ),
    }[name]


# (state, what the fold calls it, whether the loader holds the whole set back)
STATES = [
    ("authorship", "authorship", True),
    ("write_my_own", "write_my_own", True),
    ("generate", "generate", True),
    ("revise", "revise", True),
    ("stale_resolution", "stale", False),
    ("stale_question", "stale", False),
    ("creator_supplied", "approved", False),
    ("approved_wording", "approved", False),
    ("cancelled_resolution", "cancelled", False),
    ("cancelled_answer", "cancelled", False),
    ("closing_supplied", "approved", False),
]


async def thread_with(world, state: str | None, profile: str, scn=None):  # noqa: ANN001, ANN202
    scn = scn or threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    if state is not None:
        append(seeded, *build_events(state, await h.media_digest_of(seeded)))
    world.refreshes.clear()
    return scn, seeded, Phone(seeded, world, prefs(scn.key), language=scn.language)


def folded_status(seeded: h.Seeded, target: str) -> str | None:
    """What the consent fold itself says about the thread right now (the fixture's precondition)."""
    from app.kria.brief_binding import snapshot_media
    from app.models import PlanItem
    from app.services.choice_questions import tag_event
    from app.services.creative_copy_decisions import media_digest

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        digest = media_digest(snapshot_media(item))
        rows = db.execute(
            h.select(
                CreationThreadEvent.role,
                CreationThreadEvent.payload,
                CreationThreadEvent.event_type,
                CreationThreadEvent.content,
            )
            .where(CreationThreadEvent.thread_id == seeded.thread_id)
            .order_by(CreationThreadEvent.sequence)
        ).all()
    states = fold_creative_copy(
        [tag_event(r, p, e, c) for r, p, e, c in rows], dependency_digest=digest
    )
    found = states.get(target)
    return found.status if found is not None else None


async def tap_on_twin(world, state, profile, card_kind, scn=None):  # noqa: ANN001, ANN202
    """Serve a twin thread (same footage, same decision) and tap ``card_kind``'s card there."""
    _scn, _seeded, phone = await thread_with(world, state, profile, scn)
    out = await phone.serve()
    card = next(c for c in out.suggestions if c.kind == card_kind)
    return await phone.tap(card, out)


def executes(tap) -> bool:  # noqa: ANN001
    return bool(tap.planned and tap.refused is None and tap.new_drafts == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(("state", "folds_to", "held_back"), STATES, ids=[s[0] for s in STATES])
async def test_a_every_served_card_passes_the_consent_gate(
    talking_world, state, folds_to, held_back, profile
):
    rec = Recorder(f"negative A {state} {profile}")
    scn, seeded, phone = await thread_with(talking_world, state, profile)
    target = "closing_title" if state == "closing_supplied" else "opening_title"
    assert folded_status(seeded, target) == folds_to, "the fixture is not the state it names"

    out = await phone.serve()
    served = [c.kind for c in out.suggestions]
    # The control: the same footage with no decision at all offers cards in every profile.
    _c_scn, _c_seeded, control_phone = await thread_with(talking_world, None, profile)
    control = await control_phone.serve()
    assert control.suggestions, f"the control thread served nothing for {profile}"

    results: dict[str, bool] = {}
    for card in out.suggestions:
        twin = await tap_on_twin(talking_world, state, profile, card.kind)
        results[card.kind] = executes(twin)
    rec.negative(
        f"{state} ({folds_to}, {profile}): every card the GET serves executes when tapped",
        KEY,
        "no served card is refused by the consent gate",
        all(results.values()),
        f"served {served}; executed {results}",
    )
    if held_back:
        rec.negative(
            f"{state} ({profile}): an open wording discussion serves no card",
            KEY,
            "nothing served",
            not served,
            f"served {served}",
        )
    else:
        rec.negative(
            f"{state} ({folds_to}, {profile}): the card that writes the title is not offered",
            KEY,
            "no hook card (the gate cannot prove one against a plan)",
            not (set(served) & EDITOR_OPS_KINDS),
            f"served {served}",
        )
    rec.negative(
        f"{state} ({profile}): the legacy chips are the ones the thread had without a decision",
        KEY,
        "chips untouched by the v2 stripper",
        held_back or [c.kind for c in out.chips] == [c.kind for c in control.chips],
        f"chips {[c.kind for c in out.chips]} vs control {[c.kind for c in control.chips]}",
    )
    rec.finish()


def with_plan(scn, **strategy):  # noqa: ANN001, ANN003, ANN202
    return replace(scn, key=scn.key, strategy={**scn.strategy, **strategy})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "plan", "offered"),
    [
        # The plan carries the very words the creator supplied: the gate accepts a restate.
        ("creator_supplied", {"opening_title": "My own title"}, True),
        # The plan carries other words than the creator's: "The proposed text differs".
        ("creator_supplied", {"opening_title": "Another title"}, False),
        # The plan has no title at all while the creator supplied one: the same refusal.
        ("creator_supplied", {}, False),
        # A cancelled title the plan still carries: "You asked to leave that text out".
        ("cancelled_resolution", {"opening_title": "A leftover title"}, False),
        # A cancelled title the plan records as left out: nothing to compare, accepted.
        ("cancelled_resolution", {"omitted_copy_targets": ["opening_title"]}, True),
    ],
    ids=[
        "supplied-same-title",
        "supplied-other-title",
        "supplied-no-title",
        "cancelled-title-kept",
        "cancelled-title-omitted",
    ],
)
async def test_a_a_restating_card_follows_the_plan_it_restates(talking_world, state, plan, offered):
    rec = Recorder(f"negative A5/A6 filler {state} {sorted(plan)}")
    scn = with_plan(threads.SCENARIOS[KEY], **plan)
    _s, seeded, phone = await thread_with(talking_world, state, TALKING, scn)
    out = await phone.serve()
    served = [c.kind for c in out.suggestions]
    rec.negative(
        f"filler card {'served' if offered else 'withheld'} (plan fields {sorted(plan) or 'none'})",
        KEY,
        "served exactly when the consent gate accepts the head plan",
        ("filler_words" in served) == offered,
        f"served {served}",
    )
    rec.negative(
        "the card that writes the title is withheld either way",
        KEY,
        "no hook card",
        not (set(served) & EDITOR_OPS_KINDS),
        f"served {served}",
    )
    if offered:
        card = next(c for c in out.suggestions if c.kind == "filler_words")
        tap = await phone.tap(card, out)
        rec.negative(
            "the served filler card executes",
            KEY,
            "planned, one draft, approval staged",
            executes(tap) and tap.after["approvals"] == tap.before["approvals"] + 1,
            f"refused={tap.refused} reply={tap.reply[:80]!r} rows {tap.before} -> {tap.after}",
        )
    rec.finish()
