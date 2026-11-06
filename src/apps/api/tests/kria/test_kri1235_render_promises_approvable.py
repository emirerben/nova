"""KRI-1235: Kria never holds a phone Talking draft over its own render promises.

Prod 2026-11-06 20:49Z (TestFlight 1910, Fly v2122, thread b931bc4e, Qendresa Q1 "Talking +
screenshot cards"): the first turn answered "I can't make this yet:" and listed promises, not
refusals: "If Clean up speech is offered when you approve, choose it and every pause is cut down
to about 0.3 s ..." (item 13), "I'll show it the moment you say that word in your take ..." x5
(item 19) and, on the next turn, "I'll draw a curly arrow next to your ending text" (item 14).
"Make it without these" then stripped all of them. Root cause: approval readiness made every
checked partial receipt `needs_choice` except the five old speech-cleanup reasons it knew
(`_cleanup_deferred`); the plan promises added that week were missing from that list.

Ways the fix can fail, each pinned below:

 1. a reason a checker emits as a render promise is missing from the list (today's bug):
    the prod replay, and a guard over every plan-promise constant in `brief_checks`;
 2. prefix / substring matching lets a shortfall through ("<promise>; ... so it may still be in
    your video", "<promise> and more"): each part must equal a promise exactly;
 3. a refusal that shares words with a promise (`_TIGHT_KIND_LONG_ONLY`, `_RESTART_STAYS`,
    `_CLOSING_ARROW_MISSING`, `_TALKING_SPOKEN_TEXT_ANY_VOICE` ...) becomes approvable;
 4. a `not_possible` receipt worded like a promise is approved: only `partial` is deferred;
 5. a Turkish turn stores the Turkish twin, so an English-only list re-blocks Turkish creators;
 6. promises that contain "; " themselves (spoken text, list labels) break a naive split;
 7. a pinned spoken-text promise has a dynamic tail: it is matched by recomputing the exact
    sentence for that requirement, so another word or time stays held;
 8. the hold reply (and "Make it without these", which reads the same rows) still names
    promises: only rows that are truly held are listed;
 9. a joined cleanup reason longer than the receipt's 300-character cap is cut and re-blocks;
10. the approval-time re-check disagrees with the draft's verdict ("evidence changed").
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.approval_readiness import (
    APPROVABLE,
    _promised_at_render,
    assess_readiness,
    readiness_reply,
)
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for
from app.kria.spoken_text_ask import requirement_spoken_text as bc_spoken_text
from tests.kria.test_brief_checks_speaker_background import (  # noqa: F401
    CREATOR,
    TAKE,
    _gate,
    _manifest,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _lanes_features

TURN_1 = "17e9fb29-b7e4-453a-9d59-62410cefd660"


def _r(rid: str, kind: str, description: str, *, literal=None, facts=None, scope="global"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        source_turn_id=TURN_1,
    )


# Brief v1 of thread b931bc4e, as the render job's binding holds it (admin jobs debug of
# e7d1e06f, read-only), with each row back to "open" and `superseded_by` dropped: that is
# what turn 1 read before "Make it without these" superseded them.
Q1_BRIEF = CreativeBrief(
    version=1,
    requirements=[
        _r(
            "r1",
            "style",
            "Cut out all my pauses and mistakes so it's tight",
            facts={"pauses": "tight", "cut_restarts": True, "speech_cleanup": True},
        ),
        _r(
            "r2",
            "style",
            "Captions word by word in the middle of the screen, white, two words at a time",
            facts={
                "captions": "word_by_word",
                "caption_color": "#FFFFFF",
                "caption_words": 2,
                "caption_position": "middle",
            },
        ),
        _r(
            "r3",
            "text",
            "Start with a big title for the first 3 seconds",
            literal="5 repos / I'd build with / this weekend!!",
            scope="title",
        ),
        _r(
            "r4",
            "style",
            "When I talk about something, show the matching screenshot or screen recording in "
            "the top or bottom part of the screen, not over my face",
            facts={"keep_text_off": [{"kind": "face"}]},
        ),
        *(
            _r(
                rid,
                "text",
                "show each one as big text when listed",
                literal=literal,
                facts={"triggers": [trigger]},
            )
            for rid, literal, trigger in (
                ("r5", "1. Spec-kit", "Spec Kit"),
                ("r6", "2. Impeccable", "Impeccable"),
                ("r7", "3. Trading Agents", "Trading Agents"),
                ("r8", "4. The Agents Marketplace", "The Agents Marketplace"),
                ("r9", "5. Claude Code Game Studios", "Cloud Code Game Studios"),
            )
        ),
        _r(
            "r10",
            "text",
            "End with comment REPO and an arrow for the last 3 seconds",
            literal="comment REPO",
            facts={"closing": "comment REPO"},
        ),
        _r("r11", "audio", "Only my voice, no music", facts={"audio_intent": "no_music"}),
    ],
)
# The job's strategy (turn 3) with the ending turn 1 still had: its rationale says "I've removed
# the closing arrow request", and turn 1's r10 receipt found the ending words in the draft.
Q1_STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "Your talking take with the opening title, word-by-word captions, no music.",
    "edit_format": "subtitled",
    "caption_style": "karaoke",
    "opening_title": "5 repos / I'd build with / this weekend!!",
    "opening_title_duration_s": 3.0,
    "closing_title": "comment REPO",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "story_structure": ["Opening title", "Creator lists the 5 AI projects"],
    "target_duration_s": 49,
    "selected_media_ids": [TAKE],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}
# What prod's readiness said on turn 1 (event seq 7, `brief_coverage.approval_readiness`).
_SPOKEN = (
    "I'll show it the moment you say that word in your take; if I don't hear the word, it "
    "won't appear"
)
PROD_TURN_1 = {
    "r1": (
        "needs_choice",
        "If Clean up speech is offered when you approve, choose it and every pause is cut down "
        "to about 0.3 s; Clean up speech also cuts a sentence you restarted if it finds one, "
        "and I'll tell you after",
    ),
    "r2": ("executable", None),
    "r3": ("executable", None),
    "r4": ("unsupported", "This draft has no pop-ins timed to your words."),
    **{rid: ("needs_choice", _SPOKEN) for rid in ("r5", "r6", "r7", "r8", "r9")},
    "r10": ("needs_choice", "I'll draw a curly arrow next to your ending text"),
    "r11": ("executable", "No music, as asked."),
}
PROMISE_ROWS = ("r1", "r5", "r6", "r7", "r8", "r9", "r10")


@pytest.fixture
def q1_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod on 2026-11-06 for her account: every Qendresa switch on, protocol 42."""
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)
    monkeypatch.setitem(settings.__dict__, "spoken_list_labels_enabled", True)
    monkeypatch.setattr(settings, "creator_cta_ending_enabled", True, raising=False)
    monkeypatch.setattr(settings, "freehand_drawing_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_closing_title_enabled", True)
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", True)
    # The caption look (r2) and the word-timed pop-in lanes (r4), as prod read them.
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", True, raising=False)
    monkeypatch.setattr(settings, "media_overlays_enabled", True, raising=False)
    monkeypatch.setattr(settings, "phone_subtitled_reaction_beats_enabled", True, raising=False)
    monkeypatch.setattr(settings, "sound_effects_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        _lanes_features(
            "authoredText",
            "freehandDrawing",
            "captionShape",
            "stillImages",
            "visualBlocks",
            "alphaOverlay",
            "soundEffects",
        ),
    )


def _assess(brief: CreativeBrief = Q1_BRIEF, strategy: dict | None = None) -> dict:
    return assess_readiness(
        brief,
        dict(strategy or Q1_STRATEGY),
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
        protected_regions=[],
        protected_marks=True,
    )


def _rows(report: dict) -> dict[str, dict]:
    return {row["requirement_id"]: row for row in report["requirements"]}


# ------------------------------------------------------------------ 1. the prod replay


def test_q1_turn1_replay_says_what_prod_said(q1_prod):
    """The replay reaches prod's own reasons, so the verdicts below are about prod's input.

    r4 (the cards) matched prod word for word when this was written; KRI-1237 changes its
    verdict on purpose, so it is not pinned here."""
    rows = _rows(_assess())
    for rid, (_disposition, reason) in PROD_TURN_1.items():
        if rid != "r4":
            assert rows[rid]["reason"] == reason, rid


def test_q1_turn1_holds_nothing_kria_promised(q1_prod):
    report = _assess()
    rows = _rows(report)
    for rid in PROMISE_ROWS:
        assert rows[rid]["disposition"] == "unverified", rows[rid]
    for rid in ("r2", "r3", "r11"):
        assert rows[rid]["disposition"] == PROD_TURN_1[rid][0], rows[rid]
    # r4 (the cards) is KRI-1237's: whatever it says, it is the only row that may hold.
    held = {rid for rid, row in rows.items() if row["disposition"] not in APPROVABLE}
    assert held <= {"r4"}, held
    assert report["ready"] is (not held)


def test_the_hold_reply_names_only_what_is_held(q1_prod):
    report = _assess()
    reply = readiness_reply(Q1_BRIEF, report)
    for rid in PROMISE_ROWS:
        assert Q1_BRIEF.live()[int(rid[1:]) - 1].text() not in reply, rid
    for phrase in ("0.3 s", "the moment you say that word", "curly arrow"):
        assert phrase not in reply
    held = [row for row in report["requirements"] if row["disposition"] not in APPROVABLE]
    assert reply.count("\n- ") == len(held)


def test_turn2_ending_alone_is_approvable(q1_prod):
    """Turn 2 (seq 9) held the draft on the arrow alone: "I'll draw a curly arrow ..."."""
    brief = CreativeBrief(
        version=2, requirements=[r for r in Q1_BRIEF.requirements if r.id in {"r2", "r3", "r10"}]
    )
    report = _assess(brief)
    assert _rows(report)["r10"]["disposition"] == "unverified"
    assert report["ready"] is True


def test_the_approval_recheck_agrees_with_the_draft(q1_prod):
    first, second = _assess(), _assess()
    assert first["digest"] == second["digest"]


def test_a_turkish_turn_is_not_held_either(q1_prod):
    with reply_language_for("tr"):
        rows = _rows(_assess())
    for rid in PROMISE_ROWS:
        assert rows[rid]["disposition"] == "unverified", rows[rid]
        assert rows[rid]["reason"] != PROD_TURN_1[rid][1], "expected the Turkish twin"


# ----------------------------------------------- 2. each promise, through its real checker


@pytest.mark.parametrize(
    ("ask", "promise"),
    [
        (_r("t", "style", "cut every pause so it's tight", facts={"pauses": "tight"}), None),
        (
            _r("t", "text", "show it when I list it", literal="1. Spec-kit"),
            bc._TALKING_LIST_LABEL_PLAN,
        ),
        (
            _r("t", "text", "show it when I say it", literal="Spec-kit", facts={"triggers": ["X"]}),
            bc._TALKING_SPOKEN_TEXT_PLAN,
        ),
    ],
    ids=["tight-pauses", "list-label", "spoken-text"],
)
def test_each_promise_is_approvable_on_its_own(q1_prod, ask, promise):
    report = _assess(CreativeBrief(version=1, requirements=[ask]))
    (row,) = report["requirements"]
    if promise is not None:
        assert row["reason"] == promise, row
    assert row["disposition"] == "unverified", row
    assert report["ready"] is True


# KRI-724: "the second time I say kit, at 0:14" picked on the occurrence question.
_PIN_ANSWER = {
    "conflict": "speech_occurrence",
    "kind": "speech_occurrence",
    "option": "occ_2_of_3_t1420",
    "input_digest": "kri1235",
    "requirement_ids": ["p"],
    "value": "kit",
}


def test_a_pinned_spoken_text_promise_is_approvable(q1_prod):
    """Item 7: the creator picked which time she says the word; the promise names it."""
    ask = _r("p", "text", "show it when I say it", literal="Spec-kit", facts={"triggers": ["kit"]})
    report = _assess(
        CreativeBrief(version=1, requirements=[ask]),
        {**Q1_STRATEGY, "choice_answers": [_PIN_ANSWER]},
    )
    (row,) = report["requirements"]
    assert row["reason"].startswith(bc._TALKING_SPOKEN_TEXT_PINNED), row
    assert "0:14" in row["reason"], row
    assert row["disposition"] == "unverified", row


def test_a_pin_for_another_time_is_not_this_promise():
    """Item 7: the pinned promise is re-derived for its requirement, never matched by prefix."""
    ask = _r("p", "text", "show it when I say it", literal="Spec-kit", facts={"triggers": ["kit"]})
    facts = bc.plan_facts_from_strategy({**Q1_STRATEGY, "choice_answers": [_PIN_ANSWER]})
    said = bc._pinned_spoken_text_plan(bc_spoken_text(ask), facts)
    assert said is not None and _promised_at_render(ask, _partial(said), facts)
    for other in (said.replace("0:14", "0:15"), said.replace("“kit”", "“Spec”")):
        assert other != said
        assert not _promised_at_render(ask, _partial(other), facts), other


# --------------------------------------------- 3. what must still hold (negative controls)


def _partial(reason: str, status: str = "partial") -> RequirementReceipt:
    return RequirementReceipt(
        requirement_id="x", status=status, verification="checked", reason=reason
    )


_ASK = _r("x", "style", "cut the pauses", facts={"speech_cleanup": True})


@pytest.mark.parametrize(
    "reason",
    [
        bc._TIGHT_KIND_LONG_ONLY,
        bc._TIGHT_RENDER_LONG_ONLY,
        bc._CLEANUP_UNAVAILABLE,
        bc._RESTART_STAYS,
        bc._NAMED_CUTS_STAY,
        bc._CLOSING_ARROW_MISSING,
        bc._TALKING_SPOKEN_TEXT_ANY_VOICE,
        bc._TALKING_SPOKEN_TEXT_NO_WORD,
        f"{bc._TIGHT_IF_OFFERED}; {bc._NAMED_CUTS_STAY}",
        f"{bc._TIGHT_IF_OFFERED}; {bc._RESTART_STAYS}",
        f"{bc._CLOSING_ARROW_PLAN} and a sparkle",
        f"{bc._TALKING_LIST_LABEL_PLAN}.",
        bc._TALKING_SPOKEN_TEXT_PLAN.split("; ")[0],
        f"{bc._TIGHT_IF_OFFERED}; ",
        "",
    ],
)
def test_a_shortfall_is_never_a_promise(reason):
    assert not _promised_at_render(_ASK, _partial(reason), bc.PlanFacts())


def test_a_refusal_worded_like_a_promise_is_never_approved():
    receipt = _partial(bc._CLOSING_ARROW_PLAN, status="not_possible")
    assert not _promised_at_render(_ASK, receipt, bc.PlanFacts())


def test_an_arrow_the_creator_must_draw_still_holds(q1_prod, monkeypatch):
    """The ending switch off: "you can draw one with the pen in the editor" is a shortfall."""
    monkeypatch.setattr(settings, "creator_cta_ending_enabled", False, raising=False)
    rows = _rows(_assess())
    assert rows["r10"]["reason"] == bc._CLOSING_ARROW_MISSING, rows["r10"]
    assert rows["r10"]["disposition"] == "needs_choice"


def test_a_restart_that_stays_in_still_holds(q1_prod, monkeypatch):
    """Retake detection off: "a sentence you restarted stays in" is a shortfall (KRI-1176)."""
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", False)
    rows = _rows(_assess())
    assert rows["r1"]["reason"] == f"{bc._TIGHT_IF_OFFERED}; {bc._RESTART_STAYS}", rows["r1"]
    assert rows["r1"]["disposition"] == "needs_choice"


def test_a_list_label_the_lane_cannot_draw_still_holds(q1_prod, monkeypatch):
    """List labels off: the label ask keeps its own checker's verdict, never a promise."""
    monkeypatch.setitem(settings.__dict__, "spoken_list_labels_enabled", False)
    label = _r("t", "text", "show it when I list it", literal="1. Spec-kit")
    (row,) = _assess(CreativeBrief(version=1, requirements=[label]))["requirements"]
    assert row["reason"] != bc._TALKING_LIST_LABEL_PLAN, row
    assert row["disposition"] not in APPROVABLE, row


# ------------------------------------------------------- 4. the guard for future features

# A constant named like a draft plan that is deliberately NOT approvable, and why. A new
# `_..._PLAN` / `_..._PLANNED` / `_..._AT_APPROVAL` / `_..._IF_OFFERED` / `_..._AT_RENDER`
# reason must land in `RENDER_PROMISE_REASONS` or here, so no feature re-blocks its own draft
# silently (KRI-1235) and none is approved without a decision.
NOT_RENDER_PROMISES = {
    # Never emitted any more (KRI-790 replaced it with `_TALKING_WINDOW_PLAN`).
    "_TALKING_OPENING_CUT_PLAN": "unused",
    # Dynamic tail ("from <line> to right after <line>"): an exact match needs the line
    # re-derived like the pinned spoken text. Held today; KRI-790 follow-up.
    "_TALKING_WINDOW_PLAN": "dynamic tail, not matched yet",
    # Voiceover route: "If they repeat, I'll need the occurrence you mean" is a question.
    "_VOICEOVER_SPOKEN_TEXT_PLAN": "asks the creator which occurrence",
    # KRI-724: the cut and the noise filter are promised before the render resolves them; the
    # receipts path treats them as format limits. Held on the readiness path until replayed.
    "_SPEECH_CUT_DRAFT_PLAN": "not replayed on readiness yet",
    "_NOISE_REDUCTION_PLANNED": "not replayed on readiness yet",
    # KRI-849: appended to another receipt with a dynamic time tail.
    "_UNCLEAR_OMIT_PLAN": "dynamic tail",
    "_UNCLEAR_WRITE_PLAN": "dynamic tail",
    # Prefix of a dynamic sentence: matched by recomputing it (`_pinned_spoken_text_plan`).
    "_TALKING_SPOKEN_TEXT_PINNED": "matched by recomputation, never as a prefix",
    # `met` and unchecked: an answer to read, never a hold (`_animation_plan_receipt`).
    "_ANIMATION_PLANNED": "met, never held",
    # KRI-1161: refusals (`not_possible`), "This draft doesn't put ... behind you".
    "_SB_NOT_PLANNED": "a refusal",
    "_SBC_NOT_PLANNED": "a refusal",
}
_PLAN_NAME = re.compile(r"^_[A-Z0-9_]+_(PLAN|PLANNED|AT_APPROVAL|IF_OFFERED|AT_RENDER)$")


def _plan_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in inspect.getmembers(bc)
        if _PLAN_NAME.match(name) and isinstance(value, str)
    }


def test_every_plan_promise_constant_is_decided():
    promises = set(bc.RENDER_PROMISE_REASONS)
    undecided = {
        name
        for name, value in _plan_constants().items()
        if value not in promises and name not in NOT_RENDER_PROMISES
    }
    assert not undecided, f"add to RENDER_PROMISE_REASONS or NOT_RENDER_PROMISES: {undecided}"
    both = {name for name in NOT_RENDER_PROMISES if _plan_constants().get(name) in promises}
    assert not both, both


def test_the_named_q1_promises_are_listed():
    for name in (
        "_TIGHT_PLANNED",
        "_TIGHT_AT_APPROVAL",
        "_TIGHT_IF_OFFERED",
        "_TALKING_SPOKEN_TEXT_PLAN",
        "_TALKING_LIST_LABEL_PLAN",
        "_CLOSING_ARROW_PLAN",
        "_CLEANUP_IF_OFFERED",
        "_RESTART_CUT_AT_APPROVAL",
    ):
        assert getattr(bc, name) in bc.RENDER_PROMISE_REASONS, name


@pytest.mark.parametrize("promise", bc.RENDER_PROMISE_REASONS)
def test_every_promise_is_approvable_in_both_languages(promise):
    assert promise in bc._REASON_TR, "a Turkish turn stores the twin; add it"
    for text in (promise, bc._REASON_TR[promise]):
        assert _promised_at_render(_ASK, _partial(text), bc.PlanFacts()), text


def test_the_longest_cleanup_promise_fits_a_receipt():
    """Item 9: tight pauses + a named line + a restart, joined, in either language."""
    parts = (bc._TIGHT_IF_OFFERED, bc._NAMED_CUT_AT_RENDER, bc._RESTART_CUT_AT_APPROVAL)
    for joined in ("; ".join(parts), "; ".join(bc._REASON_TR[p] for p in parts)):
        assert len(joined) <= 300, len(joined)
        assert _promised_at_render(_ASK, _partial(joined), bc.PlanFacts())
