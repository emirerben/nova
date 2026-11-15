"""Qendresa item 19: the brief receipts for numbered list labels on a phone Talking edit.

Draft: a label with a list marker (or one that IS a planned recording's name) says when it will
show and that it needs to hear where the part starts. After the render: met only when the label
was placed; every other status says why in the chat's language. Flag off: today's receipts.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

PLAN_EN = (
    "I'll show it when that part of your talk starts; if I don't hear where it starts, it "
    "won't appear"
)
PLAN_TR = "Konuşmanda o bölüm başladığında gösteririm; nerede başladığını duymazsam çıkmaz"
STRATEGY = {
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "background_media": {
        "media_id": "asset-a1",
        "sequence": [
            {"media_id": "asset-a1", "spoken_phrase": "spec kit"},
            {"media_id": "asset-a2", "spoken_phrase": "impeccable"},
        ],
    },
}


def _req(rid: str, text: str, **kw) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind="text", scope="global", description="label", literal=text, **kw
    )


@pytest.fixture(autouse=True)
def _lane(monkeypatch):
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def _flag(monkeypatch, value: bool) -> None:
    monkeypatch.setitem(settings.__dict__, "spoken_list_labels_enabled", value)


def _draft(req: BriefRequirement, *, lang: str | None = None):
    facts = dataclasses.replace(plan_facts_from_strategy(STRATEGY), phone_talking=True)
    token = bind_reply_language(lang) if lang else None
    try:
        [receipt] = build_receipts([req], facts, include_unchecked=True)
    finally:
        if token is not None:
            release_reply_language(token)
    return receipt


def _element_id(row: dict) -> str:
    # The worker's ids: `list-label-<requirement>` for the typed label, `-<n>` per found item.
    item = f"-{row['list_item']}" if "list_item" in row else ""
    return f"list-label-{row['requirement_id']}{item}"


def _after(
    req: BriefRequirement,
    rows: list[dict],
    *,
    texts=(),
    lang: str | None = None,
    removed: frozenset[str] = frozenset(),
):
    """After a render whose receipt is ``rows``. Every placed row is drawn on the video under
    the element id the worker gives it (``removed`` ids are flagged removed, as an edit does)."""
    rows = [
        {**r, "element_id": r.get("element_id", _element_id(r))} if r["status"] == "placed" else r
        for r in rows
    ]
    variant = {
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "text_elements": [
            {
                "id": r["element_id"],
                "text": r["text"],
                **({"removed": True} if r["element_id"] in removed else {}),
            }
            for r in rows
            if r["status"] == "placed"
        ]
        + [{"id": "x", "text": t} for t in texts],
        "list_label_placement": {"version": 1, "rows": rows},
    }
    facts = plan_facts_from_phone_variant(variant)
    token = bind_reply_language(lang) if lang else None
    try:
        [receipt] = build_receipts([req], facts, include_unchecked=True)
    finally:
        if token is not None:
            release_reply_language(token)
    return receipt


def test_draft_marked_label_says_when_it_will_show(monkeypatch):
    _flag(monkeypatch, True)
    receipt = _draft(_req("l1", "1. Spec-kit"))
    assert (receipt.status, receipt.reason) == ("partial", PLAN_EN)


def test_draft_unmarked_text_that_is_a_planned_recording_name_is_planned(monkeypatch):
    _flag(monkeypatch, True)
    monkeypatch.setitem(settings.__dict__, "background_follows_talk_enabled", True)
    assert _draft(_req("l2", "Impeccable")).reason == PLAN_EN


def test_draft_without_item_17_a_recording_name_is_not_promised(monkeypatch):
    """Like the worker: without item 17's windows no label follows a recording."""
    _flag(monkeypatch, True)
    monkeypatch.setitem(settings.__dict__, "background_follows_talk_enabled", False)
    assert _draft(_req("l2", "Impeccable")).reason != PLAN_EN
    assert _draft(_req("l1", "1. Spec-kit")).reason == PLAN_EN


def test_draft_unmarked_text_that_only_mentions_a_name_is_not_planned(monkeypatch):
    _flag(monkeypatch, True)
    assert _draft(_req("c1", "comment SPEC KIT")).reason != PLAN_EN


def test_draft_in_turkish(monkeypatch):
    _flag(monkeypatch, True)
    assert _draft(_req("l1", "1. Spec-kit"), lang="tr").reason == PLAN_TR


def test_draft_flag_off_is_todays_receipt(monkeypatch):
    _flag(monkeypatch, False)
    receipt = _draft(_req("l1", "1. Spec-kit"))
    assert receipt.status == "partial"
    assert receipt.reason == "That exact text isn't in this draft."


def test_draft_with_a_spoken_trigger_keeps_the_kri707_plan(monkeypatch):
    _flag(monkeypatch, True)
    receipt = _draft(_req("l1", "1. Spec-kit", facts={"triggers": ["spec kit"]}))
    assert receipt.reason != PLAN_EN


def _row(status: str, **kw) -> dict:
    return {
        "requirement_id": "l1",
        "text": "1. Spec-kit",
        "item": "Spec-kit",
        "status": status,
        **kw,
    }


def test_after_render_placed_label_is_met():
    assert _after(_req("l1", "1. Spec-kit"), [_row("placed", face_collision=False)]).status == "met"


def test_after_render_label_on_a_face_is_not_met():
    receipt = _after(_req("l1", "1. Spec-kit"), [_row("placed", face_collision=True)])
    assert receipt.status == "partial" and "face" in (receipt.reason or "")


@pytest.mark.parametrize(
    ("status", "en", "tr"),
    [
        (
            "not_heard",
            "I didn't hear where that part starts, so the label isn't in",
            "O bölümün nerede başladığını duymadım, o yüzden etiket yok",
        ),
        (
            "no_time",
            "That part starts too close to the end to show the label",
            "O bölüm sona çok yakın başlıyor, etiketi gösteremedim",
        ),
        (
            "same_moment",
            "Another label starts at the same moment, so this one isn't shown",
            "Aynı anda başka bir etiket başlıyor, bu yüzden bu gösterilmedi",
        ),
        (
            "error",
            "I couldn't place “1. Spec-kit” on this video",
            "“1. Spec-kit” yazısını bu videoya yerleştiremedim",
        ),
    ],
)
def test_after_render_unplaced_label_says_why_never_met(status, en, tr):
    req = _req("l1", "1. Spec-kit")
    # Even when other text on the video happens to contain the same words.
    english = _after(req, [_row(status)], texts=["1. Spec-kit"])
    assert (english.status, english.reason) == ("partial", en)
    turkish = _after(req, [_row(status)], texts=["1. Spec-kit"], lang="tr")
    assert (turkish.status, turkish.reason) == ("partial", tr)


# --- review round ------------------------------------------------------------------------


def _typed(rid="l1", text="1. which company it sends data to?", **kw) -> dict:
    return {"requirement_id": rid, "text": text, "item": text[3:], "status": "placed", **kw}


def _item(number, **kw) -> dict:
    return {
        "requirement_id": "l1",
        "list_item": number,
        "text": f"{number}. which x?",
        "status": "placed",
        "basis": "spoken_list",
        "face_collision": False,
        **kw,
    }


def test_f4_only_item_one_found_is_partial_not_met():
    req = _req("l1", "1. which company it sends data to?")
    rows = [_typed(face_collision=False, list_found=1)]
    en = _after(req, rows)
    assert (en.status, en.reason) == (
        "partial",
        "I showed this one, but couldn't find the rest of your list in the take",
    )
    tr = _after(req, rows, lang="tr")
    assert (tr.status, tr.reason) == (
        "partial",
        "Bunu gösterdim ama listenin geri kalanını çekiminde bulamadım",
    )


def test_f4_a_found_list_is_met():
    req = _req("l1", "1. which company it sends data to?")
    rows = [_typed(face_collision=False, list_found=3), _item(2), _item(3)]
    assert _after(req, rows).status == "met"


def test_f4_an_item_row_on_a_face_makes_the_requirement_partial_with_the_face_reason():
    req = _req("l1", "1. which company it sends data to?")
    rows = [_typed(face_collision=False, list_found=3), _item(2, face_collision=True), _item(3)]
    receipt = _after(req, rows)
    assert receipt.status == "partial" and "face" in (receipt.reason or "")


def test_f7_a_wording_cue_with_no_trigger_is_the_workers_list_label(monkeypatch):
    """'... when I talk about them' makes `_is_spoken_text_cue` true, but with no trigger the
    worker draws it as a list label, so the draft says when it will show, not 'tell me the word'."""
    _flag(monkeypatch, True)
    req = _req("q1", "1. which company it sends data to?")
    req = req.model_copy(
        update={"description": "Show each of the four things as big text when I talk about them"}
    )
    facts = dataclasses.replace(plan_facts_from_strategy(STRATEGY), phone_talking=True)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    assert (receipt.status, receipt.reason) == ("partial", PLAN_EN)


def test_f7_a_real_trigger_stays_kri707s(monkeypatch):
    _flag(monkeypatch, True)
    req = _req("q1", "1. which company it sends data to?", facts={"triggers": ["company"]})
    req = req.model_copy(update={"description": "show it when I say company"})
    facts = dataclasses.replace(plan_facts_from_strategy(STRATEGY), phone_talking=True)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    assert receipt.reason != PLAN_EN


# --- review of #2116: the exact-text check reads the label row the video draws ----------------

LONG = (
    "1. which company it sends your data to, and whether it keeps a copy of everything you "
    "ever typed"
)


def _drawn(
    req: BriefRequirement,
    drawn: str | None,
    *,
    status="placed",
    texts=(),
    lang=None,
    removed: bool = False,
):
    """After a render whose list-label row reads ``drawn`` (None: no such row on the video;
    ``removed``: the row is there but flagged removed, as an edit leaves it); the placement
    receipt keeps the asked text, as the worker writes it."""
    row = _row(status, text=req.literal, item=req.literal[3:], face_collision=False)
    if status == "placed":
        row["element_id"] = f"list-label-{req.id}"
    elements = (
        [{"id": f"list-label-{req.id}", "text": drawn, **({"removed": True} if removed else {})}]
        if drawn is not None
        else []
    )
    variant = {
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "text_elements": elements + [{"id": "x", "text": t} for t in texts],
        "list_label_placement": {"version": 1, "rows": [row]},
    }
    facts = plan_facts_from_phone_variant(variant)
    token = bind_reply_language(lang) if lang else None
    try:
        [receipt] = build_receipts([req], facts, include_unchecked=True)
    finally:
        if token is not None:
            release_reply_language(token)
    return receipt


def test_after_render_a_shortened_label_is_never_met():
    """The old 80-character cut drew "... a copy of everythin": the receipt row still held the
    whole text, and any other text with the same words made it "met"."""
    req = _req("l1", LONG)
    cut = LONG[:80]
    for texts in ((LONG,), ()):
        receipt = _drawn(req, cut, texts=texts)
        assert receipt.status == "partial", texts
        assert receipt.reason == f"The label on the video reads “{cut}”, not your exact words"
    turkish = _drawn(req, cut, texts=(LONG,), lang="tr")
    assert (turkish.status, turkish.reason) == (
        "partial",
        f"Videodaki etiket “{cut}” yazıyor, tam senin yazdığın gibi değil",
    )


def test_after_render_the_whole_label_is_met():
    req = _req("l1", LONG)
    assert _drawn(req, LONG).status == "met"
    # Spacing and letter case are not the creator's words changing.
    assert _drawn(req, "  ".join(LONG.upper().split())).status == "met"


def test_after_render_a_label_too_long_to_fit_says_so():
    req = _req("l1", LONG)
    english = _drawn(req, None, status="too_long", texts=(LONG,))
    assert (english.status, english.reason) == (
        "partial",
        "That label is too long to fit on the screen whole, so I left it out instead of cutting it",
    )
    turkish = _drawn(req, None, status="too_long", lang="tr")
    assert (turkish.status, turkish.reason) == (
        "partial",
        "Bu etiket ekrana tam sığmayacak kadar uzun; kesmek yerine hiç koymadım",
    )


# --- second review of #2116: every found item and the label's own row are checked ------------
#
# Ways this receipt could lie (each is a test below):
# R1 the typed label is drawn, a found item is left out (too long, same moment, no time, error,
#    never heard) and the list is still "met";
# R2 several found items are left out and the reason names only one of them;
# R3 a found item's receipt says placed but its row is gone from the video (removed by an edit);
# R4 the receipt counts more found items than it has rows for, and the missing rows are ignored;
# R5 the typed label's own row is missing or removed and other text with the same words (or the
#    captions) makes it "met";
# R6 the controls: a whole list drawn, and a label drawn as typed, are still "met".

LIST_REQ = _req("l1", "1. which company it sends data to?")


@pytest.mark.parametrize("status", ["too_long", "same_moment", "no_time", "error", "not_heard"])
def test_r1_a_found_item_left_out_makes_the_list_partial(status):
    rows = [_typed(face_collision=False, list_found=3), _item(2, status=status), _item(3)]
    receipt = _after(LIST_REQ, rows)
    assert (receipt.status, receipt.reason) == (
        "partial",
        "I showed this one, but couldn't show 1 more from your list: “2. which x?”",
    )


def test_r2_every_left_out_item_is_named():
    rows = [
        _typed(face_collision=False, list_found=4),
        _item(2, status="too_long"),
        _item(3),
        _item(4, status="same_moment"),
    ]
    en = _after(LIST_REQ, rows)
    assert (en.status, en.reason) == (
        "partial",
        "I showed this one, but couldn't show 2 more from your list: “2. which x?”, “4. which x?”",
    )
    tr = _after(LIST_REQ, rows, lang="tr")
    assert (tr.status, tr.reason) == (
        "partial",
        "Bunu gösterdim ama listenden 2 maddeyi daha gösteremedim: “2. which x?”, “4. which x?”",
    )


def test_r3_a_found_item_removed_from_the_video_counts_as_left_out():
    rows = [_typed(face_collision=False, list_found=3), _item(2), _item(3)]
    receipt = _after(LIST_REQ, rows, removed=frozenset({"list-label-l1-3"}))
    assert (receipt.status, receipt.reason) == (
        "partial",
        "I showed this one, but couldn't show 1 more from your list: “3. which x?”",
    )


def test_r4_found_items_without_a_receipt_row_are_still_left_out():
    rows = [_typed(face_collision=False, list_found=4), _item(2)]
    receipt = _after(LIST_REQ, rows)
    assert (receipt.status, receipt.reason) == (
        "partial",
        "I showed this one, but couldn't show 2 more from your list",
    )


@pytest.mark.parametrize("removed", [False, True])
def test_r5_a_label_whose_own_row_is_gone_is_never_met(removed):
    req = _req("l1", LONG)
    drawn = LONG if removed else None
    # Other text (or the captions) holding the same words does not stand in for the label.
    receipt = _drawn(req, drawn, texts=(req.literal,), removed=removed)
    assert (receipt.status, receipt.reason) == ("partial", f"“{LONG}” isn't on the video")
    turkish = _drawn(req, drawn, texts=(req.literal,), removed=removed, lang="tr")
    assert (turkish.status, turkish.reason) == ("partial", f"“{LONG}” videoda yok")


def test_r6_a_whole_list_and_a_whole_label_are_still_met():
    rows = [_typed(face_collision=False, list_found=3), _item(2), _item(3)]
    assert _after(LIST_REQ, rows).status == "met"
    assert _drawn(_req("l1", LONG), LONG, texts=(LONG,)).status == "met"


# --- KRI-1471: the example filed in the description (Q1 run 3, prod 2026-11-14) -----------
#
# "When I list the five repos, show each one as big text, like “1. Spec-kit”." arrived as one
# `text / per_clip` row with no literal. The per-clip check answered "None of the 11 clips got
# its own text in this draft." (`not_possible`), readiness held the draft and "Make it without
# these" dropped her labels.
#
# Ways the receipt can go wrong, written before the code:
#  X1 the prod row still gets the clip-text refusal;      X2 the Turkish turn still does;
#  X3 a marked literal filed per-clip still does;
#  X4 the example switch off, or item 19's own switch off, is not today's receipt;
#  X5 a montage / cloud draft (not the phone Talking writer) loses its per-clip check;
#  X6 a per-clip ask with no numbered example stops being refused on Talking;
#  X7 after the render the row is judged as clip text, not by its own labels;
#  X8 one label of five is called met;                    X9 a label never heard is called met.

Q1_ASK = "When I list the five repos, show each one as big text, like “1. Spec-kit”."
NO_CLIP_TEXT = "None of the 11 clips got its own text in this draft."
CLIPS = [f"clip-{n}" for n in range(11)]


def _example(literal: str | None = None, description: str = Q1_ASK) -> BriefRequirement:
    return BriefRequirement(
        id="r5",
        kind="text",
        scope="per_clip",
        description=description,
        literal=literal,
        facts={"numbered": True, "text_size": "large", "show_when_mentioned": True},
    )


def _example_switch(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(settings, "list_label_example_ask_enabled", value)


def _clip_draft(req: BriefRequirement, *, phone_talking: bool = True, lang: str | None = None):
    facts = dataclasses.replace(
        plan_facts_from_strategy(STRATEGY, clip_ids=CLIPS), phone_talking=phone_talking
    )
    token = bind_reply_language(lang) if lang else None
    try:
        [receipt] = build_receipts([req], facts, include_unchecked=True)
    finally:
        if token is not None:
            release_reply_language(token)
    return receipt


def test_x1_the_prod_row_is_the_list_label_promise(monkeypatch):
    _flag(monkeypatch, True)
    _example_switch(monkeypatch, True)
    receipt = _clip_draft(_example())
    assert (receipt.status, receipt.reason) == ("partial", PLAN_EN)
    assert receipt.requirement_id == "r5" and receipt.verification == "checked"


def test_x2_x3_in_turkish_and_as_a_marked_per_clip_literal(monkeypatch):
    _flag(monkeypatch, True)
    _example_switch(monkeypatch, True)
    assert _clip_draft(_example(), lang="tr").reason == PLAN_TR
    assert _clip_draft(_example(literal="1. Spec-kit")).reason == PLAN_EN


@pytest.mark.parametrize(("master", "example"), [(True, False), (False, True), (False, False)])
def test_x4_either_switch_off_is_todays_receipt(monkeypatch, master, example):
    _flag(monkeypatch, master)
    _example_switch(monkeypatch, example)
    receipt = _clip_draft(_example())
    assert (receipt.status, receipt.reason) == ("not_possible", NO_CLIP_TEXT)


def test_x5_a_draft_off_the_phone_talking_writer_keeps_its_per_clip_check(monkeypatch):
    _flag(monkeypatch, True)
    _example_switch(monkeypatch, True)
    receipt = _clip_draft(_example(), phone_talking=False)
    assert (receipt.status, receipt.reason) == ("not_possible", NO_CLIP_TEXT)


def test_x6_a_per_clip_ask_with_no_numbered_example_is_still_refused(monkeypatch):
    _flag(monkeypatch, True)
    _example_switch(monkeypatch, True)
    for description in ("the landmark shown in each clip", "name each place, like “Paris”"):
        receipt = _clip_draft(_example(description=description))
        assert (receipt.status, receipt.reason) == ("not_possible", NO_CLIP_TEXT), description


def _q1_rows(found: int, **typed) -> list[dict]:
    names = ["Impeccable", "Trading Agents", "The Agents Marketplace", "Cloud Code Game Studios"]
    first = {"requirement_id": "r5", "text": "1. Spec-kit", "item": "Spec-kit", "status": "placed"}
    return [
        {**first, "face_collision": False, "list_found": found, **typed},
        *(
            {
                "requirement_id": "r5",
                "list_item": number,
                "text": f"{number}. {name}",
                "status": "placed",
                "basis": "spoken_list",
                "face_collision": False,
            }
            for number, name in enumerate(names[: found - 1], 2)
        ),
    ]


def test_x7_after_the_render_five_placed_labels_are_met(monkeypatch):
    _example_switch(monkeypatch, True)
    assert _after(_example(), _q1_rows(5)).status == "met"
    assert _after(_example(literal="1. Spec-kit"), _q1_rows(5)).status == "met"


def test_x8_only_the_typed_label_found_is_partial(monkeypatch):
    _example_switch(monkeypatch, True)
    receipt = _after(_example(), _q1_rows(1))
    assert (receipt.status, receipt.reason) == (
        "partial",
        "I showed this one, but couldn't find the rest of your list in the take",
    )


def test_x9_a_label_never_heard_says_so(monkeypatch):
    _example_switch(monkeypatch, True)
    row = {"requirement_id": "r5", "text": "1. Spec-kit", "item": "Spec-kit", "status": "not_heard"}
    receipt = _after(_example(), [row])
    assert (receipt.status, receipt.reason) == (
        "partial",
        "I didn't hear where that part starts, so the label isn't in",
    )


def test_after_the_render_switch_off_judges_the_row_as_before(monkeypatch):
    _example_switch(monkeypatch, False)
    assert _after(_example(), _q1_rows(5)).status != "met"


# --- KRI-1471: a label left on a card or a caption line is never "Done" -------------------
#
# The Q1 run 3 replay: every label was drawn over its screenshot card and the done-list said
# "Done". When the worker finds no clear spot it now records ``text_overlap`` on the row. Ways
# the receipt can go wrong: O1 it still says met; O2 the Turkish turn does; O3 an overlap on
# one of the spoken items (rows 2..) is lost; O4 a clear label stops being met.

OVERLAP_EN = "“1. Spec-kit” sits over something else on screen; I couldn't find a clear spot for it"
OVERLAP_TR = "“1. Spec-kit” ekranda başka bir şeyin üstünde duruyor; ona boş bir yer bulamadım"


def test_o1_o2_a_label_left_on_a_card_is_partial_and_says_so(monkeypatch):
    _example_switch(monkeypatch, True)
    rows = _q1_rows(5, text_overlap=True)
    receipt = _after(_example(), rows)
    assert (receipt.status, receipt.reason) == ("partial", OVERLAP_EN)
    assert _after(_example(), rows, lang="tr").reason == OVERLAP_TR
    assert _after(_req("r5", "1. Spec-kit"), rows).status == "partial"  # item 19's own row too


def test_o3_an_overlapping_spoken_item_makes_the_list_partial(monkeypatch):
    _example_switch(monkeypatch, True)
    rows = _q1_rows(5)
    rows[3] = {**rows[3], "text_overlap": True}
    receipt = _after(_example(), rows)
    assert (receipt.status, receipt.reason) == ("partial", OVERLAP_EN)


def test_o4_clear_labels_are_still_met(monkeypatch):
    _example_switch(monkeypatch, True)
    assert _after(_example(), _q1_rows(5, text_overlap=False)).status == "met"
