"""Tahini: a phone Voiceover first draft held for four asks the video then did anyway.

Incident (prod job 1d062f21, thread c07abe31, 2026-11-17, iPhone, Voiceover = a recorded 33.1 s
voiceover over 12 recipe clips, ``narrated_planned``). The first draft was held:

- r3 "Keep my whole voiceover." (``audio``, ``keep_whole_take``): "This narrated planned edit
  cuts your footage down; a Talking edit keeps the whole take." (``needs_choice``). A Voiceover
  edit plays its voiceover whole; the render kept all 33.1 s, cleanup ``checked_no_change``.
- r4 "Open on the finished dish ... the crispy topping stirred in the pan, ..." (``order``,
  ``first_clip``): "I can't confirm this draft does it yet." The worker pins the clips the
  draft's resolved first-position order intents seat.
- r17 "White captions, a few words at a time, following my voice." (``caption_color``): "I
  can't confirm ...". The Voiceover writer draws a typed caption look like the Talking one.
- r20 "Keep kitchen sounds quiet under my voice." (no facts): "I can't confirm ...". The
  footage plays under the voice in every Voiceover edit (0.15 in this render).

"Make it without these" waived all four, and the finished video's review said "Left out as
agreed" for each, over a video that kept the whole voiceover and the kitchen sound under it.

What is replayed, and with what. The brief rows are the job's binding word for word
(``tests/fixtures/tahini_voiceover_hold.json``); the media rows are the snapshot's clips without
their analyses. The real ``assess_readiness``, ``readiness_reply``, ``_simpler_version_offer``,
``build_receipts`` and the finished-video review (``_approved_generation_requirements_review``)
run on them. The hold turn's own strategy is not recorded: the approved (simpler) one stands in.
It carries one order intent more than the hold turn could have had (the finished-dish group of
3 clips with no order, which reads "You didn't say what order ...", a checked question, where
prod's r4 was unchecked), so the hold replay leaves that intent out; the approved strategy keeps
it in `test_an_opening_group_with_no_order_still_asks`. Prod's flag profile, plus phone text
styling on (the job's stamped ``phone_text_style`` proves prod runs it; the snapshot still lists
the code default). No model call; nothing ran on a phone.

Ways the fix can fail, written down before the code:

r3 (``narrated_whole_voiceover_promise_enabled``)
 F1  r3 still needs_choice, or any reply tells a Voiceover creator to make a Talking edit.
 F2  the promise is not approvable: not a render promise, no Turkish or German line, or not a
     format limit (the draft reply would ask "simpler version?" for it).
 F3  a whole-CLIP ask on the same draft ("Keep my whole clip.", timing) is promised.
 F4  an audio keep-whole row that turns speech cleanup on is promised.
 F5  speech cleanup already on for the item: promised as whole anyway.
 F6  after the render: cleanup applied or a named line cut still reads met; or a render whose
     cleanup did not run (or found nothing) is not met.
 F7  switch off is not byte-identical (the red control).

r4, r17, r20 (``narrated_draft_promises_enabled``)
 F8  r4 still held when the draft's resolved order intents seat the asked first clip.
 F9  r4 promised when nothing seats it, under a filming-order key, or for an unordered group.
 F10 r17 still held; or the look is promised with phone text styling off (nothing is drawn).
 F11 r20 still held; or "Make sure my voice sounds clear.", "Keep the music quiet under my
     voice." or "kitchen sounds" in a ``style`` row is read as the footage bed.
 F12 switch off is not byte-identical, or one switch moves another switch's rows.

The review (``left_out_recheck_enabled``)
 F13 r3 / r20 still "Left out as agreed" over a video that did them.
 F14 "Done anyway" for what the video did not do: r17 (the captions were drawn black, outlined,
     as sentences; no white look was asked of the render) or r4 (no order to judge).
 F15 switch off is not prod's review word for word, or the live asks' receipts change.
 F16 no Turkish or German line for "Done anyway".
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.reply_language import reply_language_for
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime
from tests.kria.test_kri1465_narrated_needless_holds import phone_voiceover  # noqa: F401

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "tahini_voiceover_hold.json").read_text("utf-8")
)
CREATOR = uuid.UUID("00000000-0000-4000-8000-00000007a41e")
THREAD = uuid.UUID("c07abe31-d193-4a8f-ac6a-8d2a90b53926")
WAIVED = ("r3", "r4", "r17", "r20")
SWITCHES = (
    "narrated_whole_voiceover_promise_enabled",
    "narrated_draft_promises_enabled",
    "left_out_recheck_enabled",
)
CANT_CONFIRM = "I can't confirm this draft does it yet."
UNDER_VOICE = "Your footage sound plays under your voice"
# The finished-dish group the approved strategy carries and the hold turn's could not (above).
UNORDERED_GROUP = "order_finished_dish_opening"


@pytest.fixture
def tahini(phone_voiceover, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    for switch in SWITCHES:
        assert getattr(settings, switch) is True, switch


def _off(monkeypatch: pytest.MonkeyPatch, *switches: str) -> None:
    for switch in switches or SWITCHES:
        monkeypatch.setattr(settings, switch, False)


def _brief(version: str = "brief_v1", extra: list[BriefRequirement] = ()) -> CreativeBrief:
    data = copy.deepcopy(FIXTURE[version])
    brief = CreativeBrief.model_validate(data)
    return brief.model_copy(update={"requirements": [*brief.requirements, *extra]})


def _hold_strategy(**changes) -> dict:
    strategy = copy.deepcopy(FIXTURE["strategy"])
    strategy["resolved_clip_intents"] = [
        intent
        for intent in strategy["resolved_clip_intents"]
        if intent["intent_id"] != UNORDERED_GROUP
    ]
    return strategy | changes


def _manifest():
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415

    clips = FIXTURE["media"]["clip_assignments"]
    return capabilities.resolve_creator_manifest(
        item_id="item-tahini",
        edit_format="narrated_planned",
        has_voiceover=True,
        narration={
            "gcs_path": "users/creator/thread/voice.m4a",
            "generation": "1",
            "duration_s": FIXTURE["media"]["voiceover_duration_s"],
        },
        media=[{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in clips],
        phone_source_media_ids=[c["media_id"] for c in clips],
        phone_rendering_allowed=True,
        runs_guided_voiceover=False,
    )


def _report(brief: CreativeBrief | None = None, strategy: dict | None = None) -> dict:
    from app.kria.approval_readiness import assess_readiness  # noqa: PLC0415

    return assess_readiness(
        brief or _brief(),
        strategy if strategy is not None else _hold_strategy(),
        media=copy.deepcopy(FIXTURE["media"]),
        manifest=_manifest(),
        creator_id=CREATOR,
    )


def _rows(report: dict) -> dict[str, tuple[str, str | None]]:
    return {r["requirement_id"]: (r["disposition"], r["reason"]) for r in report["requirements"]}


def _prod_rows() -> dict[str, tuple[str, str | None]]:
    return {r["requirement_id"]: (r["disposition"], r["reason"]) for r in FIXTURE["hold"]["rows"]}


def _req(rid: str, kind: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=description, facts=facts)


# -- the first draft ------------------------------------------------------------------------------


def test_the_first_draft_is_ready_and_holds_nothing(tahini) -> None:
    """F1 / F8 / F10 / F11: every other row is what prod said on that turn."""
    report = _report()
    rows = _rows(report)

    assert report["ready"] is True, rows
    assert rows["r3"] == ("unverified", bc._NARRATED_WHOLE_VOICEOVER_PLAN)
    assert rows["r4"] == ("unverified", bc._NARRATED_ORDER_PLAN)
    assert rows["r17"] == ("executable", "Captions are white.")
    assert rows["r20"] == ("executable", UNDER_VOICE)
    prod = _prod_rows()
    assert {rid: row for rid, row in rows.items() if rid not in WAIVED} == {
        rid: row for rid, row in prod.items() if rid not in WAIVED
    }
    assert not any("Talking" in (reason or "") for _disposition, reason in rows.values())


def test_switches_off_reproduce_the_prod_hold_word_for_word(tahini, monkeypatch) -> None:
    """F7 / F12 (red control): all 20 rows, the reply and the simpler-version offer."""
    from app.kria.approval_readiness import APPROVABLE, readiness_reply  # noqa: PLC0415
    from app.tasks.kria_runtime import _simpler_version_offer  # noqa: PLC0415

    _off(monkeypatch)
    brief = _brief()
    report = _report(brief)
    held = [
        r["requirement_id"] for r in report["requirements"] if r["disposition"] not in APPROVABLE
    ]

    assert _rows(report) == _prod_rows()
    assert held == FIXTURE["hold"]["omitted_requirement_ids"] == list(WAIVED)
    sentence, _question = _simpler_version_offer(brief, held)
    reply = FIXTURE["hold"]["reply"]
    assert reply.startswith(readiness_reply(brief, report) + "\n"), readiness_reply(brief, report)
    assert sentence in reply


@pytest.mark.parametrize(
    ("switch", "rows"),
    [
        ("narrated_whole_voiceover_promise_enabled", ("r3",)),
        ("narrated_draft_promises_enabled", ("r4", "r17", "r20")),
        ("left_out_recheck_enabled", ()),
    ],
)
def test_each_switch_restores_only_its_own_rows(tahini, monkeypatch, switch, rows) -> None:
    """F12."""
    on = _rows(_report())
    _off(monkeypatch, switch)
    off = _rows(_report())
    prod = _prod_rows()
    for rid in WAIVED:
        assert off[rid] == (prod[rid] if rid in rows else on[rid]), rid


@pytest.mark.parametrize("language", ["tr", "de"])
def test_the_promises_are_approvable_in_turkish_and_german(tahini, language) -> None:
    """F2."""
    from app.kria.copy_catalog import translate  # noqa: PLC0415

    with reply_language_for(language):
        report = _report()
        rows = _rows(report)
    assert report["ready"] is True, rows
    for rid, english in (
        ("r3", bc._NARRATED_WHOLE_VOICEOVER_PLAN),
        ("r4", bc._NARRATED_ORDER_PLAN),
    ):
        local = bc._REASON_TR[english] if language == "tr" else translate("de", english)
        assert local and local != english, (rid, local)
        assert rows[rid] == ("unverified", local), rows[rid]


def test_the_promises_are_render_promises_and_format_limits() -> None:
    """F2: never the "simpler version?" question on the draft reply's receipts path."""
    for promise in (bc._NARRATED_WHOLE_VOICEOVER_PLAN, bc._NARRATED_ORDER_PLAN):
        assert promise in bc.RENDER_PROMISE_REASONS
        for text in bc.reason_in_every_language(promise):
            assert bc.is_format_limit(text), text
    assert bc._VOICEOVER_CUT not in bc.RENDER_PROMISE_REASONS


def test_the_draft_reply_says_will_do_at_render(tahini) -> None:
    """The draft turn's own receipts (`kria_runtime` builds these facts for a Voiceover draft)."""
    manifest = _manifest()
    facts = dataclasses.replace(
        bc.plan_facts_from_strategy(
            _hold_strategy(), clip_ids=[m.media_id for m in manifest.media], manifest=manifest
        ),
        phone_narrated=True,
        phone_text=True,
        media_snapshot=copy.deepcopy(FIXTURE["media"]),
    )
    brief = _brief()
    receipts = {
        r.requirement_id: r
        for r in bc.build_receipts(brief.live(), facts, include_unchecked=True, strict_order=True)
    }
    assert (receipts["r3"].status, receipts["r3"].verification) == ("partial", "checked")
    assert not bc.needs_creator_choice(receipts["r3"])
    reply = bc.reply_from_receipts(brief, list(receipts.values()), summary="Here's your draft.")
    assert "- Will do at render: Keep my whole voiceover." in reply, reply
    assert "Talking" not in reply


# -- what must still answer as today ---------------------------------------------------------------

WHOLE_CLIP = _req("x1", "timing", "Keep my whole clip.", keep_whole_take=True)
WHOLE_CLEANED = _req(
    "x2",
    "audio",
    "Keep my whole voiceover but cut the pauses.",
    keep_whole_take=True,
    speech_cleanup=True,
)
VOICE_QUALITY = _req("x3", "audio", "Make sure my voice sounds clear.")
MUSIC_QUIET = _req("x4", "audio", "Keep the music quiet under my voice.")
SOUNDS_AS_PICTURE = _req("x5", "style", "Keep kitchen sounds quiet under my voice.")


@pytest.mark.parametrize(
    "extra", [WHOLE_CLIP, WHOLE_CLEANED, VOICE_QUALITY, MUSIC_QUIET, SOUNDS_AS_PICTURE]
)
def test_neighbouring_asks_answer_as_today(tahini, monkeypatch, extra) -> None:
    """F3 / F4 / F11: the same draft, one more ask, the same verdict as with every switch off."""
    on = _rows(_report(_brief(extra=[extra])))[extra.id]
    _off(monkeypatch)
    assert on == _rows(_report(_brief(extra=[extra])))[extra.id]


def test_a_whole_clip_ask_keeps_its_question(tahini) -> None:
    """F3: the ask is about footage, which a Voiceover edit does cut."""
    disposition, reason = _rows(_report(_brief(extra=[WHOLE_CLIP])))["x1"]
    assert disposition == "needs_choice", reason
    assert reason.startswith("This narrated planned edit cuts your footage down"), reason


def test_speech_cleanup_on_says_the_voiceover_may_be_cut(tahini) -> None:
    """F5."""
    brief = _brief()
    facts = dataclasses.replace(
        bc.plan_facts_from_strategy(_hold_strategy(), speech_cleanup_enabled=True),
        phone_narrated=True,
    )
    (receipt,) = bc.build_receipts(
        [r for r in brief.live() if r.id == "r3"], facts, include_unchecked=True
    )
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Speech cleanup is on"), receipt.reason


@pytest.mark.parametrize(
    "strategy",
    [
        pytest.param(_hold_strategy(resolved_clip_intents=[]), id="nothing-seats-it"),
        pytest.param(FIXTURE["strategy"], id="approved-with-unordered-group"),
    ],
)
def test_r4_is_promised_only_when_the_draft_seats_it(tahini, monkeypatch, strategy) -> None:
    """F9: as today when no resolved intent seats the first clip, or a group has no order."""
    on = _rows(_report(strategy=strategy))["r4"]
    _off(monkeypatch)
    assert on == _rows(_report(strategy=strategy))["r4"]
    assert on[1] != bc._NARRATED_ORDER_PLAN


def test_an_opening_group_with_no_order_still_asks(tahini) -> None:
    """The approved strategy's finished-dish group: a real question, unchanged."""
    disposition, reason = _rows(_report(strategy=FIXTURE["strategy"]))["r4"]
    assert disposition == "needs_choice"
    assert reason.startswith("You didn't say what order the clips in"), reason


def test_r4_under_a_filming_order_key_is_as_today(tahini, monkeypatch) -> None:
    """F9."""
    brief = _brief()
    r4 = next(r for r in brief.requirements if r.id == "r4")
    keyed = r4.model_copy(update={"facts": {**r4.facts, "key": "capture_time"}})
    brief = brief.model_copy(
        update={"requirements": [keyed if r.id == "r4" else r for r in brief.requirements]}
    )
    on = _rows(_report(brief))["r4"]
    _off(monkeypatch)
    assert on == _rows(_report(brief))["r4"]


def test_the_look_is_not_promised_without_phone_text_styling(tahini, monkeypatch) -> None:
    """F10: with styling off no look is stamped, so nothing is drawn."""
    monkeypatch.setattr(settings, "phone_text_styling_enabled", False)
    assert _rows(_report())["r17"] == ("evidence_missing", CANT_CONFIRM)


# -- the finished video ----------------------------------------------------------------------------


def _variant(**changes) -> dict:
    return copy.deepcopy(FIXTURE["variant"]) | changes


def _rendered_r3(variant: dict):
    brief = _brief()
    facts = bc.plan_facts_from_phone_variant(variant)
    (receipt,) = bc.build_receipts(
        [r for r in brief.live() if r.id == "r3"], facts, include_unchecked=True
    )
    return receipt


def test_the_finished_video_keeps_the_whole_voiceover(tahini) -> None:
    """F6: the recorded render (no cleanup outcome on a device render = cleanup did not run)."""
    receipt = _rendered_r3(_variant())
    assert (receipt.status, receipt.verification) == ("met", "checked")
    receipt = _rendered_r3(_variant(silence_cut_outcome="no_change"))
    assert (receipt.status, receipt.verification) == ("met", "checked")


def test_a_render_whose_cleanup_cut_the_voiceover_is_partly(tahini) -> None:
    """F6."""
    receipt = _rendered_r3(_variant(silence_cut_outcome="applied"))
    assert (receipt.status, receipt.reason) == ("partial", bc._VOICEOVER_CUT)
    cut = {
        "requirement_ids": ["r9"],
        "quote": "and the secret",
        "status": "applied",
        "source_span": {"start_s": 4.0, "end_s": 5.0},
        "word_indexes": [10, 11, 12],
    }
    receipt = _rendered_r3(_variant(explicit_speech_cuts=[cut]))
    assert (receipt.status, receipt.reason) == ("partial", bc._VOICEOVER_CUT)


# -- the review after "Make it without these" -----------------------------------------------------


def _review(monkeypatch: pytest.MonkeyPatch, language: str | None = None) -> tuple[str, list]:
    """The real review the chat posted for the finished video (thread c07abe31, event 31)."""
    brief = _brief("brief_v2")
    variant = _variant()
    record = copy.deepcopy(FIXTURE["narrated_alignment"])
    job = SimpleNamespace(
        id=uuid.UUID("1d062f21-a468-48bb-8074-42e1f062a740"),
        content_plan_item_id=None,
        assembly_plan={
            "narrated_alignment": record,
            "variants": [variant],
            "speech_cleanup_contract": FIXTURE["speech_cleanup_contract"],
            DEVICE_RENDER_FIELD: {
                variant["variant_id"]: {
                    "published_attempt": variant["render_generation_id"],
                    "requirement_generation": record["generation_id"],
                }
            },
        },
        all_candidates={"creator_strategy": copy.deepcopy(FIXTURE["strategy"])},
    )
    binding = BriefBinding.create(THREAD, brief)
    execution = SimpleNamespace(
        result={"brief_binding": binding.model_dump(mode="json")}, target_draft_id=None
    )
    thread = SimpleNamespace(id=THREAD, creator_id=CREATOR)
    coverage = {"accepted_omission_ids": list(WAIVED), "agreed_omission_ids": list(WAIVED)}
    monkeypatch.setattr(kria_runtime, "_approved_draft_coverage", lambda _db, _ex: coverage)
    monkeypatch.setattr(kria_runtime, "_with_clip_sound_toggles", lambda _j, _v, facts, **_: facts)
    with reply_language_for(language):
        return kria_runtime._approved_generation_requirements_review(
            None, thread, job, variant, execution, "The narrated cut is ready."
        )


def test_switch_off_reproduces_the_prod_review_word_for_word(tahini, monkeypatch) -> None:
    """F15 (red control). The word-cue and pace receipts (NARRATED_CUE_RECEIPTS_ENABLED, same
    PR) also change this review, so prod's text needs both switches off."""
    _off(monkeypatch, "left_out_recheck_enabled")
    _off(monkeypatch, "narrated_cue_receipts_enabled")
    text, _receipts = _review(monkeypatch)
    assert text == FIXTURE["review"]["reply"]


def test_the_review_says_done_anyway_for_what_the_video_did(tahini, monkeypatch) -> None:
    """F13 / F14 / F15: r3 and r20 were done; r4 and r17 stay left out; live receipts same."""
    text, receipts = _review(monkeypatch)
    lines = text.split("\n")
    assert "- Done anyway: Keep my whole voiceover." in lines, text
    assert "- Done anyway: Keep kitchen sounds quiet under my voice." in lines, text
    assert "- Left out as agreed: White captions, a few words at a time, following my voice." in (
        lines
    )
    assert any(line.startswith("- Left out as agreed: Open on the finished dish") for line in lines)
    _off(monkeypatch, "left_out_recheck_enabled")
    off_text, off_receipts = _review(monkeypatch)
    assert receipts == off_receipts
    assert text.replace("- Done anyway: ", "- Left out as agreed: ") == off_text


@pytest.mark.parametrize(
    ("language", "line"),
    [
        ("tr", "- Yine de yapıldı: Keep my whole voiceover."),
        ("de", "- Trotzdem erledigt: Keep my whole voiceover."),
    ],
)
def test_done_anyway_in_turkish_and_german(tahini, monkeypatch, language, line) -> None:
    """F16."""
    text, _receipts = _review(monkeypatch, language)
    assert line in text.split("\n"), text
