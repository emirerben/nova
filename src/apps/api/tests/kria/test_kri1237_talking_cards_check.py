"""KRI-1237: a phone Talking "show my screenshots when I talk about them" ask, before and after.

Qendresa Q1 (prod 2026-11-06, thread b931bc4e, TestFlight 1910): she asked "When I talk about
something, show the matching screenshot or screen recording in the top or bottom part of the
screen, not over my face". The draft judged it with the word-triggered pop-in check, which only
sees the planner's reaction beats, and said "This draft has no pop-ins timed to your words."
(`not_possible`, readiness `unsupported`): the hold "I can't make this yet". Yet the phone
Talking render grounds the Visuals pool as cards on the words they match
(`ground_phone_subtitled_overlays`): job e7d1e06f placed all five screenshots. Nothing read that
receipt afterwards either.

Ways the fix could fail (written before the fix):
1. The draft still answers `not_possible` / readiness `unsupported` for the Q1 ask (the bug).
2. The draft promises cards the render will not place: overlay lane closed, no Visuals in the
   pool, only screen recordings while video cards are off, the full-screen layout instead of
   cards, a Montage/cloud edit rather than a phone Talking one, or several speaking clips
   (the worker skips the lanes then).
3. The promise swallows a word-named pop-in ask ("when I say Spec Kit, pop up its sticker"),
   whose own reaction-beat verdict must stand.
4. The promise has no Turkish twin, or is not a format promise, so a Turkish reply leaks English
   or the draft asks "make it without these?" about something the render does.
5. After render, the receipt quotes the original take's seconds (the receipt's clock) instead of
   the finished video's: speech cleanup cut 6.6 s from Q1, so 0:08 would be wrong by 3 s.
6. After render, a Visual that did not appear is not named, or one dropped by the cut is called
   shown.
7. After render, "met" with nothing placed, or a verdict read off a receipt that is not about
   word-matched cards (a hand-authored lane, a failed matcher, the full-screen sequence).
8. After render, the names are the raw upload names (iOS UUID prefix, a cut-off ".p").
9. With no overlay receipt (an older render, lane off) a card verdict is invented.

Evidence level: offline replay of the real prod records (`fixtures/kri1237_qendresa_q1_cards.json`,
read-only admin GETs), no model call, no device.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import reply_language_for
from app.services import creator_capabilities as capabilities
from tests.kria.test_kri1235_render_promises_approvable import (
    Q1_BRIEF,
    Q1_STRATEGY,
    q1_prod,  # noqa: F401 - the prod switches of the Q1 run (KRI-1235's replay)
)

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1237_qendresa_q1_cards.json").read_text(
        encoding="utf-8"
    )
)
CREATOR = uuid.UUID("00000000-0000-4000-8000-000000001237")
TAKE = FIXTURE["media"][0]["media_id"]
STRATEGY = FIXTURE["strategy"]
BRIEF_V1 = CreativeBrief.model_validate(FIXTURE["brief_v1"])
REQS = {req.id: req for req in BRIEF_V1.requirements}
CARDS = REQS["r4"]
PROD_ROWS = {row["requirement_id"]: row for row in FIXTURE["turn1_readiness_rows"]}
PROD_RECEIPTS = {row["requirement_id"]: row for row in FIXTURE["turn1_receipts"]}
VARIANT = FIXTURE["variant"]

PROMISE_EN = (
    "I'll show your Visuals as cards when you talk about them; one you never talk about "
    "won't appear"
)
# Independent expectation: the finished-video second each screenshot appears at, worked by hand
# from the receipt's source-take start minus every `silence_cut.removed` span before it
# (0-3.04 retake, 12.965-14.334, 21.105-21.71, 36.105-37.495 pauses):
#   8.48-3.04=5.44 | 15.32-4.409=10.911 | 22.82-5.014=17.806 | 31.06-5.014=26.046
#   | 39.32-6.404=32.916
SHOWN_AT = {
    "Screenshot 1 spec-kit": "0:05",
    "Screenshot 2 impeccable": "0:11",
    "Screenshot 3 tradingage": "0:18",
    "Screenshot 4 agents": "0:26",
    "Screenshot 5 gamestudio": "0:33",
}
# The receipt's own (source-take) clock: quoting these is failure mode 5.
SOURCE_CLOCK = ("0:08", "0:15", "0:23", "0:31", "0:39")
NOT_SHOWN = (
    "Screen Recording 1 spec",
    "Screen Recording 2 impe",
    "Screen Recording 3 trad",
    "Screen Recording 4 agen",
    "Screen Recording 5 game",
)


@pytest.fixture(autouse=True)
def _gate(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prod as Q1 ran: the phone Talking writer for this account, overlay lanes on."""
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    # On in prod (job e7d1e06f carries `phone_text_style`; the code default is off).
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)


def _manifest(media: list[dict] | None = None):
    return capabilities.resolve_creator_manifest(
        item_id="item-kri-1237",
        edit_format="subtitled",
        media=FIXTURE["media"] if media is None else media,
        phone_source_media_ids=[TAKE],
        phone_rendering_allowed=True,
    )


def _draft_facts(manifest=None, *, phone_talking: bool = True, **strategy):  # noqa: ANN001, ANN003
    facts = plan_facts_from_strategy(
        {**STRATEGY, **strategy},
        clip_ids=[TAKE],
        manifest=_manifest() if manifest is None else manifest,
    )
    return dataclasses.replace(facts, phone_talking=phone_talking)


def _receipt(req: BriefRequirement, facts):  # noqa: ANN001, ANN202
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _readiness(**strategy):  # noqa: ANN003, ANN202
    report = assess_readiness(
        BRIEF_V1,
        {**STRATEGY, **strategy},
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


def _promise() -> str:
    """The new reason constant, imported per test so origin/main shows each test's failure."""
    from app.kria.brief_checks import _TALKING_CARDS_PLAN  # noqa: PLC0415

    return _TALKING_CARDS_PLAN


# ------------------------------------------------------------------- 1. the Q1 draft (turn 1)


def test_q1_replay_reproduces_prod_for_the_asks_this_fix_does_not_touch() -> None:
    """The replay is faithful: the title, the five list labels and "no music" read as prod did.

    (r1 / r2 also hang on the item 12 / 13 switches, which this replay leaves at their defaults.)
    """
    rows = _readiness()
    for rid in ("r3", "r5", "r6", "r7", "r8", "r9", "r11"):
        assert rows[rid]["reason"] == PROD_ROWS[rid]["reason"], rid
    # The title and "no music" were never held; the list labels were, until KRI-1235 (#2142).
    for rid in ("r3", "r11"):
        assert rows[rid]["disposition"] == PROD_ROWS[rid]["disposition"], rid
    assert PROD_ROWS["r4"]["disposition"] == "unsupported"
    assert PROD_RECEIPTS["r4"]["reason"] == "This draft has no pop-ins timed to your words."


def test_q1_cards_ask_is_a_render_promise_not_a_refusal() -> None:
    """Failure mode 1: prod said "This draft has no pop-ins timed to your words"."""
    rows = _readiness()
    assert rows["r4"]["disposition"] != "unsupported", rows["r4"]
    assert rows["r4"]["reason"] == PROMISE_EN == _promise()
    receipt = _receipt(CARDS, _draft_facts())
    assert (receipt.status, receipt.reason) == ("partial", PROMISE_EN)
    assert receipt.verification == "checked"


def test_q1_cards_promise_is_approvable() -> None:
    """The promise is one of KRI-1235's render promises: it never holds her first draft."""
    row = _readiness()["r4"]
    assert row["disposition"] in APPROVABLE, row
    assert row["reason"] == PROMISE_EN


def test_q1_first_draft_is_not_held_with_prod_switches(q1_prod) -> None:  # noqa: ANN001, F811
    """KRI-1234's hold: with KRI-1235's promises (#2142) and this one, nothing holds turn 1.

    KRI-1235's replay (its prod switches, its turn-1 strategy with the ending) on her real
    uploads: its own manifest holds the take alone, where no card can be promised."""
    report = assess_readiness(
        Q1_BRIEF,
        {**Q1_STRATEGY, "selected_media_ids": [TAKE]},
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
        protected_regions=[],
        protected_marks=True,
    )
    rows = {row["requirement_id"]: row for row in report["requirements"]}
    assert rows["r4"]["disposition"] in APPROVABLE, rows["r4"]
    held = {rid for rid, row in rows.items() if row["disposition"] not in APPROVABLE}
    assert not held, held
    assert report["ready"] is True


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_promise_is_a_format_promise_in_both_languages(language: str) -> None:
    """Failure mode 4: a Turkish reply says it in Turkish; the reply never asks to simplify."""
    with reply_language_for(language):
        receipt = _receipt(CARDS, _draft_facts())
    assert receipt.status == "partial"
    if language == "tr":
        assert receipt.reason != PROMISE_EN
        assert "kart" in receipt.reason
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)


# ------------------------------------------------- 2. no promise where the render places none


def _no_promise(facts) -> None:  # noqa: ANN001
    receipt = _receipt(CARDS, facts)
    assert receipt.reason != PROMISE_EN, receipt
    assert receipt.status != "met", receipt


def test_no_promise_when_the_overlay_lane_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", False)
    _no_promise(_draft_facts())


def test_no_promise_without_visuals_in_the_pool() -> None:
    _no_promise(_draft_facts(_manifest([FIXTURE["media"][0]])))


def test_no_promise_for_screen_recordings_only_while_video_cards_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    videos = [row for row in FIXTURE["media"] if row["kind"] == "video"]
    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", False)
    _no_promise(_draft_facts(_manifest(videos)))
    # ... and the screenshots alone are enough while video cards are off.
    images = [FIXTURE["media"][0], *(row for row in FIXTURE["media"] if row["kind"] == "image")]
    assert _receipt(CARDS, _draft_facts(_manifest(images))).reason == PROMISE_EN


def test_no_card_promise_on_the_full_screen_layout() -> None:
    _no_promise(_draft_facts(overlay_display="fullscreen"))


def test_no_promise_off_the_phone_talking_writer() -> None:
    _no_promise(_draft_facts(phone_talking=False))


def test_no_promise_when_several_clips_may_speak() -> None:
    """The worker skips the overlay lanes when several clips speak."""
    second = {"media_id": "analysis-proxy-ios-SECOND.mp4", "kind": "video", "duration_s": 20.0}
    media = [FIXTURE["media"][0], second, *FIXTURE["media"][1:]]
    _no_promise(_draft_facts(_manifest(media), selected_media_ids=[TAKE, second["media_id"]]))


def test_a_named_word_pop_in_keeps_its_own_verdict() -> None:
    """Failure mode 3: a beat asked on a named word is the planner's, judged as before."""
    named = BriefRequirement(
        id="r4",
        kind="style",
        scope="global",
        description='When I say "Spec Kit", pop up the Spec Kit screenshot',
        facts={"triggers": ["Spec Kit"]},
    )
    receipt = _receipt(named, _draft_facts())
    # Today's verdict for a named beat the planner did not author, unchanged by the fix.
    assert (receipt.status, receipt.reason) == (
        "not_possible",
        "This draft has no pop-ins timed to your words.",
    )


# ------------------------------------------------------------- 3. the finished video (job e7d1e06f)


def test_finished_q1_names_each_screenshot_and_when_it_shows() -> None:
    """Failure modes 5, 6 and 8 on the real receipt."""
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(VARIANT))
    assert receipt.status == "met", receipt
    assert receipt.verification == "checked"
    reason = receipt.reason
    for name, at in SHOWN_AT.items():
        assert f"{at} {name}" in reason, (name, reason)
    assert not any(clock in reason for clock in SOURCE_CLOCK), reason
    for name in NOT_SHOWN:
        assert name in reason, (name, reason)
    assert "174F3FF6" not in reason and ".p," not in reason and ".png" not in reason


def test_finished_q1_in_turkish() -> None:
    with reply_language_for("tr"):
        receipt = _receipt(CARDS, plan_facts_from_phone_variant(VARIANT))
    assert receipt.status == "met"
    assert "Screenshot 1 spec-kit" in receipt.reason
    assert "0:05" in receipt.reason
    assert "Shown" not in receipt.reason and "Not shown" not in receipt.reason


def _variant(**receipt_changes):  # noqa: ANN003, ANN202
    variant = copy.deepcopy(VARIANT)
    variant["phone_overlay_receipt"] = {**variant["phone_overlay_receipt"], **receipt_changes}
    return variant


def test_wrong_content_control_nothing_placed_is_never_met() -> None:
    """Failure mode 7: the same Visuals, none on screen (the matcher found no moment)."""
    receipt_rows = VARIANT["phone_overlay_receipt"]
    moved = [
        {"media_id": row["media_id"], "label": row["label"], "reason": "no_spoken_match"}
        for row in receipt_rows["placed"]
    ]
    variant = _variant(placed=[], unplaced=[*receipt_rows["unplaced"], *moved])
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    assert receipt.status == "not_possible", receipt
    assert "Screenshot 1 spec-kit" in receipt.reason
    assert not any(at in receipt.reason for at in SHOWN_AT.values()), receipt.reason


def test_a_card_the_cleanup_cut_is_named_as_cut_not_shown() -> None:
    """Failure mode 6: the worker demotes a card whose moment speech cleanup removed."""
    receipt_rows = VARIANT["phone_overlay_receipt"]
    first, *rest = receipt_rows["placed"]
    demoted = {
        "media_id": first["media_id"],
        "label": first["label"],
        "reason": "speech_cleanup_cut",
    }
    variant = _variant(placed=rest, unplaced=[*receipt_rows["unplaced"], demoted])
    reason = _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason
    assert "0:05 Screenshot 1 spec-kit" not in reason
    assert "Screenshot 1 spec-kit (its moment was cut out)" in reason, reason
    assert "0:11 Screenshot 2 impeccable" in reason


def test_no_times_when_the_cut_record_is_missing() -> None:
    """Failure mode 5: cleanup ran but its record is gone: name the cards, quote no second."""
    variant = copy.deepcopy(VARIANT)
    variant.pop("silence_cut")
    reason = _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason
    assert "Screenshot 1 spec-kit" in reason
    assert not any(at in reason for at in (*SHOWN_AT.values(), *SOURCE_CLOCK)), reason


@pytest.mark.parametrize("matcher", ["manual", "failed", "sequence"])
def test_no_card_verdict_off_a_receipt_about_something_else(matcher: str) -> None:
    """Failure modes 7 and 9: only a word-matched card receipt speaks to this ask."""
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(_variant(matcher=matcher)))
    assert not any(name in (receipt.reason or "") for name in SHOWN_AT), receipt
    assert receipt.status != "met"


def test_no_card_verdict_without_an_overlay_receipt() -> None:
    variant = copy.deepcopy(VARIANT)
    variant.pop("phone_overlay_receipt")
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    assert not any(name in (receipt.reason or "") for name in SHOWN_AT), receipt
    assert receipt.status != "met"


# ------------------------------------------------ 4. review follow-ups (same failure modes)


def test_her_ask_in_turkish_gets_the_same_promise() -> None:
    """Failure mode 4, EN/TR parity: the decision reads no English words (KRI-667)."""
    req = CARDS.model_copy(
        update={
            "description": (
                "Bir şeyden bahsettiğimde, eşleşen ekran görüntüsünü ya da ekran kaydını yüzümün "
                "üstüne değil ekranın üst ya da alt kısmına koy"
            )
        }
    )
    with reply_language_for("tr"):
        receipt = _receipt(req, _draft_facts())
    assert receipt.status == "partial"
    assert receipt.reason == _receipt_tr_promise()


def _receipt_tr_promise() -> str:
    with reply_language_for("tr"):
        return _receipt(CARDS, _draft_facts()).reason


def test_a_sound_on_my_words_is_not_a_card() -> None:
    """Failure mode 3: a sound pop-in keeps its own check (a card plays no sound)."""
    req = CARDS.model_copy(
        update={"description": "When I talk about something, play a ding sound", "facts": {}}
    )
    assert _receipt(req, _draft_facts()).reason != PROMISE_EN


def test_the_planner_beats_still_answer_on_the_finished_video() -> None:
    """Failure mode 3: where word-triggered pop-ins were placed, their verdict stands."""
    variant = copy.deepcopy(VARIANT)
    variant["phone_beat_receipt"] = {
        "version": 1,
        "matcher": "phrase",
        "placed": [{"trigger": "Spec Kit", "visual_label": "Screenshot 1 spec-kit"}],
        "unplaced": [],
    }
    reason = _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason or ""
    assert "Screen Recording" not in reason and "Shown:" not in reason, reason


def test_long_names_never_overflow_the_receipt() -> None:
    """Failure mode 8: six placed cards with 60-character names still fit, cut at a name."""
    long_name = "Screenshot 2026-11-05 at 14.22.31 spec-kit demo walkthrough"
    rows = [
        {"media_id": f"m{i}", "label": f"{long_name} {i}", "start_s": 5.0 + 6 * i, "end_s": 9.0}
        for i in range(8)
    ]
    variant = _variant(placed=rows)
    receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    assert receipt.status == "met"
    assert len(receipt.reason) <= 300
    assert receipt.reason.endswith(".") and "more" in receipt.reason, receipt.reason


def test_a_short_title_keeps_its_dot() -> None:
    """Failure mode 8: only an extension the receipt's 60-character cap cut short is dropped."""
    variant = _variant(
        placed=[{"media_id": "m1", "label": "Q3.m", "start_s": 9.0, "end_s": 12.0}], unplaced=[]
    )
    assert "Q3.m" in _receipt(CARDS, plan_facts_from_phone_variant(variant)).reason


# ----------------------------------- 5. review #2153: the omissions always fit (failure mode 6)

LONG = "Screenshot 2026-11-05 at 14.22.31 spec-kit demo walkthrough"  # 60 chars, the worker cap
_MISS_MARK = {"en": "; not shown: ", "tr": "; çıkmadı: "}


def _long_rows(count: int, prefix: str, *, placed: bool) -> list[dict]:
    rows = []
    for i in range(count):
        label = f"{prefix} {i}"[-60:] if prefix != LONG else f"{LONG[: 60 - len(str(i)) - 1]} {i}"
        row: dict[str, Any] = {"media_id": f"{prefix[:3]}-{i}", "label": label}
        if placed:
            row.update(start_s=5.0 + 4 * i, end_s=8.0 + 4 * i)
        else:
            row["reason"] = "no_spoken_match"
        rows.append(row)
    return rows


def _accounted(section: str, labels: list[str]) -> int:
    """Labels named in ``section`` plus the "and N more" / "N Visuals" it folds the rest into."""
    import re  # noqa: PLC0415 - parses the test's own receipt text

    named = sum(label in section for label in labels)
    folded = re.findall(r"(\d+) (?:more|tane daha|Visuals|görsel)\b", section)
    return named + sum(int(n) for n in folded)


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize(
    ("placed", "unplaced"),
    [
        (_long_rows(8, LONG, placed=True), VARIANT["phone_overlay_receipt"]["unplaced"]),
        (
            _long_rows(8, LONG, placed=True),
            _long_rows(12, "Screen Recording " + "x" * 30, placed=False),
        ),
        (_long_rows(1, LONG, placed=True), _long_rows(14, LONG, placed=False)),
    ],
    ids=["emir-8-long-shown-q1-misses", "8-long-shown-12-long-misses", "1-shown-14-long-misses"],
)
def test_the_left_out_visuals_are_always_told(language: str, placed, unplaced) -> None:  # noqa: ANN001
    """Emir's #2153 review: 8 long shown names used the whole receipt and the 5 misses vanished
    while it said "met". The sentence now keeps room for what was left out (named, or counted)."""
    variant = _variant(placed=placed, unplaced=unplaced)
    with reply_language_for(language):
        receipt = _receipt(CARDS, plan_facts_from_phone_variant(variant))
    reason = receipt.reason or ""
    assert receipt.status == "met"
    assert len(reason) <= 300 and reason.endswith("."), reason
    assert receipt.full_reason == reason  # nothing cut by the 300-character cap
    assert _MISS_MARK[language] in reason, reason
    shown_part, missed_part = reason.split(_MISS_MARK[language], 1)
    shown_labels = [row["label"] for row in placed]
    missed_labels = [str(row["label"]).split("-", 5)[-1] for row in unplaced]
    assert _accounted(shown_part, shown_labels) == len(placed), reason
    assert _accounted(missed_part, missed_labels) == len(unplaced), reason


@pytest.mark.parametrize("language", ["en", "tr"])
def test_nothing_shown_and_many_long_misses_still_count_them(language: str) -> None:
    unplaced = _long_rows(14, LONG, placed=False)
    with reply_language_for(language):
        receipt = _receipt(
            CARDS, plan_facts_from_phone_variant(_variant(placed=[], unplaced=unplaced))
        )
    reason = receipt.reason or ""
    assert receipt.status == "not_possible"
    assert len(reason) <= 300 and receipt.full_reason == reason, reason
    _lead, missed_part = reason.split(": ", 1)
    assert _accounted(missed_part, [row["label"] for row in unplaced]) == 14, reason


# ------------------------- 6. review #2153: an editor Save after the render (failure mode 10)
# The worker's `phone_overlay_receipt` is not rewritten by a Save, so a card the creator removed
# or moved must never be reported as the render placed it. These run the real Save
# (`prepare_editor_commit` -> `_persist_lane_state`), then the finished-video receipt.


def _two_card_talking_job(monkeypatch: pytest.MonkeyPatch):
    """A 10 s Talking take cleaned at 4-5 s with two Visual cards, as the render placed them:
    A at output 1.0-3.0 s (take 1.0 s), B at output 6.0-8.0 s (take 7.0 s), and the worker's
    receipt on the take's clock."""
    from types import SimpleNamespace  # noqa: PLC0415

    from app.kria.device_render import make_device_request  # noqa: PLC0415
    from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan  # noqa: PLC0415
    from app.pipeline.silence_cut import CutPlan, Removal  # noqa: PLC0415
    from app.services.device_render import pin_device_request  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD  # noqa: PLC0415
    from tests.pipeline.test_phone_subtitled_plan import (  # noqa: PLC0415
        _binding,
        _overlay_card,
        _photo_visual,
    )
    from tests.routes.test_phone_talking_range_commit import _CUES, _enable_range  # noqa: PLC0415

    _enable_range(monkeypatch)
    monkeypatch.setattr(settings, "phone_sfx_speech_duck_enabled", False)
    binding = _binding(duration_s=10.0)
    visual_a = _photo_visual(media_id="shot-a", gcs_path="users/owner/plan/item/pool/shot-a.png")
    visual_b = _photo_visual(
        media_id="shot-b", gcs_path="users/owner/plan/item/pool/shot-b.png", generation="78"
    )
    cards = [
        _overlay_card(
            id="pip-0", media_id="shot-a", gcs_path=visual_a.gcs_path, start_s=1.0, end_s=3.0
        ),
        _overlay_card(
            id="pip-1",
            media_id="shot-b",
            gcs_path=visual_b.gcs_path,
            generation="78",
            start_s=6.0,
            end_s=8.0,
        ),
    ]
    cut = CutPlan(
        keep_segments=[(0.0, 4.0), (5.0, 10.0)],
        removed=[Removal(start_s=4.0, end_s=5.0, reason="pause")],
        time_saved_s=1.0,
    )
    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=_CUES,
        cut_plan=cut,
        lanes=PhoneSubtitledLanes(overlays=cards),
        visuals=(visual_a, visual_b),
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        all_candidates={"clip_paths": [binding.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [v.model_dump(mode="json") for v in (visual_a, visual_b)],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                    "silence_cut_outcome": "applied",
                    "silence_cut": {"removed": [{"start_s": 4.0, "end_s": 5.0}]},
                    "phone_overlay_receipt": {
                        "version": 1,
                        "matcher": "agent",
                        "face_sampling": "ok",
                        "placed": [
                            {
                                "media_id": "shot-a",
                                "label": "Screenshot A.png",
                                "kind": "image",
                                "start_s": 1.0,
                                "end_s": 3.0,
                            },
                            {
                                "media_id": "shot-b",
                                "label": "Screenshot B.png",
                                "kind": "image",
                                "start_s": 7.0,
                                "end_s": 9.0,
                            },
                        ],
                        "unplaced": [],
                        "wishlist": [],
                    },
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _save_cards(job, **request) -> None:  # noqa: ANN001, ANN003
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", **request),
        user_id="owner",
        plan_item_id="item",
    )


def _card_payload(card_id: str, path: str, start_s: float, end_s: float) -> dict:
    return {
        "id": card_id,
        "kind": "image",
        "src_gcs_path": path,
        "display_mode": "pip",
        "x_frac": 0.5,
        "y_frac": 0.4,
        "scale": 0.35,
        "start_s": start_s,
        "end_s": end_s,
    }


def _final(job, language: str = "en"):  # noqa: ANN001, ANN202
    with reply_language_for(language):
        return _receipt(CARDS, plan_facts_from_phone_variant(job.assembly_plan["variants"][0]))


def test_before_any_save_the_render_receipt_speaks(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _final(_two_card_talking_job(monkeypatch))
    assert receipt.status == "met"
    assert "0:01 Screenshot A" in receipt.reason and "0:06 Screenshot B" in receipt.reason


def test_removing_every_card_is_never_met(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _two_card_talking_job(monkeypatch)
    _save_cards(job, media_overlays=[])
    variant = job.assembly_plan["variants"][0]
    assert variant["phone_overlay_receipt"]["placed"]  # the worker's record is untouched
    for language in ("en", "tr"):
        receipt = _final(job, language)
        assert receipt.status == "not_possible", receipt
        assert "Screenshot A" in receipt.reason and "Screenshot B" in receipt.reason
        assert "0:01" not in receipt.reason and "0:06" not in receipt.reason


def test_removing_one_card_names_it_as_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _two_card_talking_job(monkeypatch)
    _save_cards(
        job,
        media_overlays=[_card_payload("pip-0", "users/owner/plan/item/pool/shot-a.png", 1.0, 3.0)],
    )
    receipt = _final(job)
    assert receipt.status == "met"
    assert "0:01 Screenshot A" in receipt.reason
    shown, missed = receipt.reason.split("; not shown: ")
    assert "Screenshot B" in missed and "removed" in missed and "Screenshot B" not in shown


def test_a_moved_card_is_told_at_its_new_time(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _two_card_talking_job(monkeypatch)
    _save_cards(
        job,
        media_overlays=[
            _card_payload("pip-0", "users/owner/plan/item/pool/shot-a.png", 2.0, 3.5),
            _card_payload("pip-1", "users/owner/plan/item/pool/shot-b.png", 8.2, 9.0),
        ],
    )
    reason = _final(job).reason
    assert "0:02 Screenshot A" in reason and "0:08 Screenshot B" in reason, reason
    assert "0:01" not in reason and "0:06" not in reason


def test_a_range_edit_moves_the_times_with_the_take(monkeypatch: pytest.MonkeyPatch) -> None:
    """Range drops the take's first second: A (take 1.0 s) now opens the video, B plays at 5 s."""
    from tests.routes.test_phone_talking_range_commit import _SLOT0, _SLOT1, _row  # noqa: PLC0415

    job = _two_card_talking_job(monkeypatch)
    _save_cards(job, timeline_slots=[_row(_SLOT0, 1.0, 3.0), _row(_SLOT1, 5.0, 5.0)])
    reason = _final(job).reason
    assert "0:00 Screenshot A" in reason and "0:05 Screenshot B" in reason, reason


def test_an_unreadable_saved_edit_gives_no_card_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a Save whose card lanes cannot be read never certifies the old receipt."""
    job = _two_card_talking_job(monkeypatch)
    _save_cards(job, media_overlays=[])
    variant = job.assembly_plan["variants"][0]
    field = next(key for key in variant if key.startswith("_phone_subtitled_editor_lanes"))
    variant[field] = {"version": 1, "lanes": "garbled"}
    receipt = _final(job)
    assert receipt.status != "met" and "Screenshot" not in (receipt.reason or ""), receipt


def test_the_saved_lanes_field_name_matches_the_editor() -> None:
    from app.kria.brief_checks import _EDITOR_LANES_FIELD  # noqa: PLC0415
    from app.services.phone_subtitled_editor import (
        PHONE_SUBTITLED_EDITOR_LANES_FIELD,  # noqa: PLC0415
    )

    assert _EDITOR_LANES_FIELD == PHONE_SUBTITLED_EDITOR_LANES_FIELD


# ---------------- 7. review #2153: the authored-timeline Save after a clip deletion (mode 10)
# Deleting a piece of the take moves the edit to the authored timeline
# (`stage_initial_authored_baseline`), whose Save compiles `variant["media_overlays"]` and writes
# no saved-lanes marker. Its cards are kept by file path only, so they cannot be matched to the
# receipt's Visuals here: none left is exact, any left gives no card verdict (fail closed).


def _authored(job, *, drop_first_piece: bool = False):  # noqa: ANN001, ANN202
    from app.routes import generative_jobs as gj  # noqa: PLC0415
    from app.services.editor_empty_drafts import stage_initial_authored_baseline  # noqa: PLC0415

    variant = gj._find_variant(job, "subtitled")
    slots = gj.editor_deletion_timeline(job, variant)
    return stage_initial_authored_baseline(
        job, "subtitled", slots[1:] if drop_first_piece else slots
    )


def _authored_save(staged, **request) -> None:  # noqa: ANN001, ANN003
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    gj.prepare_editor_commit(
        staged,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", editor_state_version=2, **request),
        user_id="owner",
        plan_item_id="item",
    )


def test_authored_save_removing_every_card_is_never_met(monkeypatch: pytest.MonkeyPatch) -> None:
    staged = _authored(_two_card_talking_job(monkeypatch))
    _authored_save(staged, media_overlays=[])
    variant = staged.assembly_plan["variants"][0]
    assert variant["editor_timeline_mode"] == "authored"
    assert variant["phone_overlay_receipt"]["placed"]  # the worker's record is untouched
    for language in ("en", "tr"):
        receipt = _final(staged, language)
        assert receipt.status == "not_possible", receipt
        assert "Screenshot A" in receipt.reason and "Screenshot B" in receipt.reason
        assert "0:01" not in receipt.reason and "0:06" not in receipt.reason


def test_authored_save_after_deleting_a_piece_never_quotes_the_old_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emir's stronger case: the first piece of the take is gone (the video is 5 s), the cards
    stay; the receipt must not claim Screenshot B at 0:06, past the end of the video."""
    staged = _authored(_two_card_talking_job(monkeypatch), drop_first_piece=True)
    kept = staged.assembly_plan["variants"][0]["media_overlays"]
    assert len(kept) == 2  # both cards are still in the edit
    _authored_save(staged, media_overlays=kept)
    receipt = _final(staged)
    assert receipt.status != "met", receipt
    assert "0:06" not in (receipt.reason or "") and "Screenshot" not in (receipt.reason or "")


def test_an_authored_edit_before_its_save_is_not_judged_by_the_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _authored(_two_card_talking_job(monkeypatch), drop_first_piece=True)
    receipt = _final(staged)
    assert receipt.status != "met" and "Screenshot" not in (receipt.reason or ""), receipt
