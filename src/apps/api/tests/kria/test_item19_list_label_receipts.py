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
