"""KRI-547: "vertical video, keep my face in frame" judged from the phone render.

The phone Talking worker records how it framed a sideways speaker on the variant
(``speaker_framing``); `plan_facts_from_phone_variant` reads it and the framing ask
is Done when the face crop rendered, an honest "Partly" naming why when it fell
back, and "can't check yet" on a draft.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_judged,
    plan_facts_from_phone_variant,
    reply_from_receipts,
)
from app.kria.reply_language import reply_language_for

R1 = "yatay videoyu dikey (9:16) formata getir ve yüzü hep kadrajda tut"


def _req(description: str = R1, *, kind: str = "style", rid: str = "r1") -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=description)


def _framing(mode: str, reason: str, **extra) -> dict:
    return {"version": 1, "mode": mode, "reason": reason, "eligible": mode == "face_fill", **extra}


def _variant(framing: dict | None = None, **overrides) -> dict:
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "caption_cues": [{"text": "Selam.", "start_s": 0.0, "end_s": 0.8}],
        "voiceover_caption_style": "sentence",
        "text_elements": [],
    }
    if framing is not None:
        variant["speaker_framing"] = framing
    variant.update(overrides)
    return variant


def _receipt(variant: dict, req: BriefRequirement | None = None):
    req = req or _req()
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_the_variant_receipt_becomes_facts():
    facts = plan_facts_from_phone_variant(_variant(_framing("face_fill", "face_in_window")))
    assert (facts.speaker_framing, facts.speaker_framing_reason) == ("face_fill", "face_in_window")
    assert plan_facts_from_phone_variant(_variant()).speaker_framing is None
    bogus = _variant({"mode": "sideways", "reason": "x"})
    assert plan_facts_from_phone_variant(bogus).speaker_framing is None


def test_a_face_filled_render_is_done():
    receipt = _receipt(_variant(_framing("face_fill", "face_in_window")))
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert "face stays" in receipt.reason


@pytest.mark.parametrize(
    ("mode", "reason", "words"),
    [
        ("letterbox", "face_moves_too_much", "moves too far"),
        ("letterbox", "no_face", "couldn't find your face"),
        ("letterbox", "face_unconfirmed", "couldn't find your face"),
        ("letterbox", "face_under_captions", "captions over your face"),
        ("letterbox", "creator_chose_bars", "You chose black bars"),
        ("centre_fill", "no_face", "cropped to the middle"),
        ("centre_fill", "creator_chose_crop", "You chose crop"),
    ],
)
def test_a_fallback_is_an_honest_partly_with_the_reason(mode, reason, words):
    receipt = _receipt(_variant(_framing(mode, reason)))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert words in receipt.reason
    if mode == "letterbox" and not reason.startswith("creator"):
        assert "black bars" in receipt.reason


def test_an_already_vertical_clip_is_done():
    receipt = _receipt(_variant(_framing("centre_fill", "not_landscape")))
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert "already vertical" in receipt.reason


def test_a_render_without_a_framing_receipt_stays_unchecked():
    receipt = _receipt(_variant())
    assert receipt.verification == "unchecked"
    assert "didn't record" in receipt.reason


def test_a_draft_says_it_checks_the_finished_video():
    req = _req()
    receipt = check_requirement(req, PlanFacts(edit_format="subtitled"))
    assert receipt.verification == "unchecked"
    assert not is_judged(req, receipt)
    assert "finished video" in receipt.reason


def test_turkish_reasons_for_a_turkish_turn():
    with reply_language_for("tr"):
        done = _receipt(_variant(_framing("face_fill", "face_in_window")))
        bars = _receipt(_variant(_framing("letterbox", "face_moves_too_much")))
    assert "kadrajda kalıyor" in done.reason
    assert "siyah bantlarla" in bars.reason


def test_captions_and_pop_ins_keep_their_own_checkers():
    variant = _variant(_framing("face_fill", "face_in_window"))
    captions = _req("dikey video olsun ve altyazılar Türkçe olsun")
    assert "face stays" not in (_receipt(variant, captions).reason or "")
    pop_in = _req("'İlk durak' dediğinde kahve demleme videosunu tam ekran göster")
    assert "face stays" not in (_receipt(variant, pop_in).reason or "")


def test_the_kadikoy_reply_stops_overclaiming_when_the_crop_fell_back():
    brief = CreativeBrief(version=1, requirements=[_req()])
    summary = "Videonuz dikey formata göre ayarlandı ve yüz takibi sağlandı."
    with reply_language_for("tr"):
        fell_back = _receipt(_variant(_framing("letterbox", "face_moves_too_much")))
        reply = reply_from_receipts(brief, [fell_back], summary=summary)
        done = _receipt(_variant(_framing("face_fill", "face_in_window")))
        done_reply = reply_from_receipts(brief, [done], summary=summary)
    assert summary not in reply
    assert "Doğrulayamadım" not in reply and "Kısmen" in reply
    assert done_reply.startswith(summary)
    assert "Yapıldı" in done_reply
