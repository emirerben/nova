"""R5-S6: the consent gate reads what a pinned tap changes, not only that a title was decided.

KRI-506: wording is consented to before an edit that carries it. The pinned editor-ops commit has
no plan to compare the copy against, so the gate refused EVERY such tap on a thread with any
decision about an on-screen title, and the serve took the captions, music, sound-effects,
transition and duplicates cards off the phone of every creator who had ever written, approved or
skipped a title. Product call (decided): a pinned editor-ops draft whose ops leave the title lane
alone carries no copy, so the decision is none of its business. A draft that touches the title lane
(the hook card's ``set_title``), every draft that is not a tapped card, and every open wording
discussion stay gated exactly as before.

Failure catalogue (written first; each row is a test below):

  N1  cards do not come back where they are safe. In every settled state (a stale decision, the
      creator's own words, a tapped wording, "skip it", the closing title) the captions, music,
      sound-effects, transition and duplicates cards the same footage serves without a decision
      are served, and each one EXECUTES when tapped (served implies executes, the other half of
      the r4 matrix).
  N2  the hook stays gated: the hook card is the one editor-ops card that writes the title. It is
      withheld in every settled state, and a hook card the phone held from BEFORE the decision is a
      typed refusal on tap (the planner's proof of the card against the decided title, or the
      consent gate behind it), in the thread's language, that writes nothing.
  N3  an open wording discussion (authorship, write_my_own, generate, revise) still serves nothing,
      and the gate itself still refuses a pinned neutral op there (the loader and the gate agree).
  N4  the gate, state by state, op by op: a neutral pinned op passes every settled state and stale;
      an op that touches the title lane (``set_title``, ``add_text``, ``rewrite_text``, anything the
      allow-list does not name), a mixed list, an empty list and a draft that is not a tapped card
      (no ``pinned_ops``) are refused in every state a decision exists.
  N5  KRI-129 holds beside it: with the creator's own title in the brief the hook card is still not
      offered, and the cards that stay are the ones the ask does not cover.
  N6  the legacy chips of a decided thread are untouched (they go through the planner and its own
      consent handling), and the allow-list the gate trusts is the one the tap enforces.

Every served-card check drives the real path: GET -> worker (cassette) -> GET -> ``submit_turn`` ->
``run_kria_turn`` -> ``_complete_draft_turn``.
"""

from __future__ import annotations

import pytest

from app.database import sync_session
from app.kria.brief_binding import snapshot_media
from app.models import PlanItem
from app.services import creative_copy_gate as gate
from app.services.creative_copy_decisions import media_digest
from app.services.suggestions import pinned
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r4_serve_copy_gate import (
    STATES,
    append,
    build_events,
    executes,
    folded_status,
    prefs,
    tap_on_twin,
    thread_with,
)
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

KEY = "nermin-matcha-update"
PROFILES = (EDITOR, SIGNALS, TALKING)
NEUTRAL_KINDS = frozenset({"captions", "music", "sfx", "transition", "duplicates"})
OPEN = [s for s in STATES if s[2]]
SETTLED = [s for s in STATES if not s[2]]
SETTLED_IDS = [s[0] for s in SETTLED]


def kinds(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(("state", "folds_to", "held_back"), SETTLED, ids=SETTLED_IDS)
async def test_n1_n2_n6_a_settled_title_decision_keeps_the_cards_that_leave_the_title_alone(
    talking_world, state, folds_to, held_back, profile
):
    rec = Recorder(f"R5 copy gate N1 {state} {profile}")
    _scn, seeded, phone = await thread_with(talking_world, state, profile)
    target = "closing_title" if state == "closing_supplied" else "opening_title"
    assert folded_status(seeded, target) == folds_to, "the fixture is not the state it names"
    out = await phone.serve()
    _c, _s, control_phone = await thread_with(talking_world, None, profile)
    control = await control_phone.serve()
    expected = [k for k in kinds(control) if k in NEUTRAL_KINDS]
    assert expected, f"the control served no neutral card for {profile}: {kinds(control)}"

    rec.negative(
        f"N1 {state} ({profile}): the cards that leave the title alone are served",
        KEY,
        f"{expected} all served",
        all(k in kinds(out) for k in expected),
        f"served {kinds(out)}; the control serves {kinds(control)}",
    )
    results = {}
    for kind in [k for k in kinds(out) if k in NEUTRAL_KINDS]:
        twin = await tap_on_twin(talking_world, state, profile, kind)
        results[kind] = executes(twin)
    rec.negative(
        f"N1 {state} ({profile}): every one of them executes when tapped",
        KEY,
        "planned, one revision each",
        bool(results) and all(results.values()),
        f"executed {results}",
    )
    rec.negative(
        f"N2 {state} ({profile}): the hook card, which writes the title, is not offered",
        KEY,
        "no hook card",
        "hook" not in kinds(out),
        f"served {kinds(out)}",
    )
    rec.negative(
        f"N6 {state} ({profile}): the legacy chips are what the thread had without a decision",
        KEY,
        "chips untouched",
        [c.kind for c in out.chips] == [c.kind for c in control.chips],
        f"chips {[c.kind for c in out.chips]} vs control {[c.kind for c in control.chips]}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize(("state", "folds_to", "held_back"), SETTLED, ids=SETTLED_IDS)
async def test_n2_a_hook_card_held_from_before_the_decision_is_a_typed_refusal(
    talking_world, state, folds_to, held_back, language
):
    key = KEY if language == "en" else f"{KEY}/tr"
    rec = Recorder(f"R5 copy gate N2 {state} {language}")
    from tests.e2e_suggestions import threads
    from tests.e2e_suggestions.driver import Phone

    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, EDITOR)
    phone = Phone(seeded, talking_world, prefs(key), language=scn.language)
    held = await phone.serve()
    hook = next((c for c in held.suggestions if c.kind == "hook"), None)
    assert hook is not None, f"the thread did not serve a hook card: {kinds(held)}"
    append(seeded, *build_events(state, await _digest(seeded)))
    tap = await phone.tap(hook, held)
    rec.negative(
        f"N2 {state} ({language}): the held hook card is a typed refusal, nothing written",
        key,
        "refused (the planner's proof or the consent gate), no draft, no approval",
        (tap.refused or {}).get("reason") in {"consent_required", "base_moved"}
        and not tap.planned
        and tap.new_drafts == 0
        and tap.after["approvals"] == tap.before["approvals"],
        f"signal={tap.refused} reply={tap.reply[:80]!r}",
    )
    rec.finish()


async def _digest(seeded) -> str:  # noqa: ANN001
    with sync_session() as db:
        return media_digest(snapshot_media(db.get(PlanItem, seeded.item_id)))


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(("state", "folds_to", "held_back"), OPEN, ids=[s[0] for s in OPEN])
async def test_n3_an_open_discussion_serves_nothing(
    talking_world, state, folds_to, held_back, profile
):
    rec = Recorder(f"R5 copy gate N3 {state} {profile}")
    _scn, seeded, phone = await thread_with(talking_world, state, profile)
    assert folded_status(seeded, "opening_title") == folds_to
    out = await phone.serve()
    rec.negative(
        f"N3 {state} ({profile}): nothing is served",
        KEY,
        "no card",
        not out.suggestions,
        f"served {kinds(out)}",
    )
    rec.finish()


# ── N4: the gate, state by state, op by op ──────────────────────────────────────────


@pytest.fixture
def digest_of(talking_world):  # noqa: ANN001, ANN201
    async def make(profile):  # noqa: ANN001, ANN202
        _scn, seeded, _phone = await thread_with(talking_world, None, profile)
        return seeded, await _digest(seeded)

    return make


def problem(events, media, **kw):  # noqa: ANN001, ANN003, ANN201
    from app.services.choice_questions import tag_event

    tagged = [tag_event(r, p, e, c) for r, e, c, p in events]
    return gate.creative_copy_problem(tagged, media, **kw)


NEUTRAL_OPS = sorted(pinned.PINNED_OP_ALLOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "folds_to", "held_back"), STATES, ids=[s[0] for s in STATES])
async def test_n4_the_gate_reads_the_ops_state_by_state(digest_of, state, folds_to, held_back):
    seeded, digest = await digest_of(EDITOR)
    with sync_session() as db:
        media = snapshot_media(db.get(PlanItem, seeded.item_id))
    events = build_events(state, digest)
    refuse = lambda **kw: problem(events, media, **kw) is not None  # noqa: E731
    open_state = held_back

    # a tapped card that leaves the title lane alone: only an open discussion refuses it
    for ops in (["add_sfx"], ["set_mix"], ["remove_clip"], NEUTRAL_OPS, ["set_caption_meta"] * 2):
        assert refuse(pinned_ops=ops) is open_state, (state, ops)
    # everything else stays gated, in every state a decision exists
    for ops in (
        ["set_title"],
        ["add_text"],
        ["rewrite_text"],
        ["add_sfx", "set_title"],
        ["something_new"],
        [],
        [""],
    ):
        assert refuse(pinned_ops=ops), (state, ops)
    assert refuse(), "a draft that is not a tapped card is gated as it always was"
    assert refuse(pinned_ops=None)
    # a plan that carries the decision's own words is judged by the plan, as before
    assert refuse(strategy={}, pinned_ops=NEUTRAL_OPS) is (
        open_state or problem(events, media, strategy={}) is not None
    )


def test_n4_no_decision_no_problem():
    from app.kria.brief_binding import snapshot_media as _media

    assert (
        gate.creative_copy_problem([], _media(None) if False else {}, pinned_ops=["set_title"])
        is None
    )
    assert gate.creative_copy_problem([], {}) is None


def test_n6_the_gate_trusts_the_list_the_tap_enforces():
    from app.services.creative_copy_decisions import OPT_APPROVE  # noqa: F401
    from app.services.suggestion_chips import _OPEN_COPY_STATES

    assert gate.TITLE_NEUTRAL_OPS == pinned.PINNED_OP_ALLOW
    assert gate.TITLE_NEUTRAL_OPS.isdisjoint(pinned.COPY_OP_ALLOW)
    assert gate.OPEN_COPY_STATES == _OPEN_COPY_STATES


# ── N5: KRI-129 beside it ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n5_the_creators_own_title_still_withholds_the_hook(talking_world):
    rec = Recorder("R5 copy gate N5 creator's title")
    _scn, _seeded, phone = await thread_with(talking_world, "creator_supplied", EDITOR)
    out = await phone.serve()
    rec.negative(
        "N5 the creator wrote the title: no hook card, the neutral cards stay",
        KEY,
        "hook absent, captions/music/sfx/transition served",
        "hook" not in kinds(out) and NEUTRAL_KINDS & {"captions", "music"} <= set(kinds(out)),
        f"served {kinds(out)}",
    )
    rec.finish()
