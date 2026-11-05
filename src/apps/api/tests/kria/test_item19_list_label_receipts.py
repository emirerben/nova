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


def _after(req: BriefRequirement, rows: list[dict], *, texts=(), lang: str | None = None):
    variant = {
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "text_elements": [
            {"id": f"list-label-{r['requirement_id']}", "text": r["text"]}
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
    assert _draft(_req("l2", "Impeccable")).reason == PLAN_EN


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
