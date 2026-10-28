"""KRI-547: "vertical video, keep my face in frame" judged from the phone render.

The phone Talking worker records how it framed a sideways speaker on the variant
(``speaker_framing``); `plan_facts_from_phone_variant` reads it and the framing ask
is Done when the face crop rendered, an honest "Partly" naming why when it fell
back, and "can't check yet" on a draft.
"""

from __future__ import annotations

import json
from pathlib import Path

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


# Plaza T1 (job c7430eed): the creator filmed a guide, Maximo, who is off screen for the
# first 16 s. The prod framing receipt, the brief's ask and the review line it should get.
_T1 = json.loads(
    (Path(__file__).parents[1] / "fixtures/kri1008_plaza_t1_framing_review.json").read_text()
)
_T1_FACES = _T1["speaker_framing"]["faces"]
_T1_R5 = _T1["requirement"]["description"]


@pytest.mark.parametrize(
    ("mode", "reason", "words"),
    [
        ("letterbox", "face_moves_too_much", "speaker's face moves too far"),
        ("letterbox", "no_face", "couldn't find the speaker's face reliably"),
        ("letterbox", "face_unconfirmed", "couldn't check where the speaker's face is"),
        ("letterbox", "face_under_captions", "captions over the speaker's face"),
        ("letterbox", "creator_chose_bars", "You chose black bars"),
        ("centre_fill", "no_face", "cropped to the middle"),
        ("centre_fill", "creator_chose_crop", "You chose crop"),
        # KRI-1020's tracker: two people too far apart; a reason this copy doesn't know yet
        # still gets a line that is true for every fallback.
        ("letterbox", "faces_too_far_apart", "Two people on camera are too far apart"),
        ("letterbox", "some_future_reason", "couldn't keep the speaker's face in one vertical"),
    ],
)
def test_a_fallback_is_an_honest_partly_with_the_reason(mode, reason, words):
    receipt = _receipt(_variant(_framing(mode, reason)))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert words in receipt.reason
    if mode == "letterbox" and not reason.startswith("creator"):
        assert "black bars" in receipt.reason


def test_the_plaza_review_names_the_speaker_and_says_why():
    """T1 replay: the creator filmed Maximo, so the review must not call him "you", and a
    face found in 6 of 24 frames is not "couldn't find" (KRI-1008 finding 3)."""
    req = BriefRequirement(**_T1["requirement"])
    variant = _variant(_T1["speaker_framing"])
    receipt = _receipt(variant, req)
    reply = reply_from_receipts(CreativeBrief(version=3, requirements=[req]), [receipt])
    assert receipt.reason != _T1["prod_reason"]
    assert reply.splitlines()[-1] == _T1["expected_line"]
    with reply_language_for("tr"):
        tr = _receipt(variant, req)
    assert tr.reason == _T1["expected_reason_tr"]


def test_a_face_never_found_is_not_found_in_part_of_the_clip():
    faces = {**_T1_FACES, "detected": 0, "used": 0, "ignored": 0}
    receipt = _receipt(_variant(_framing("letterbox", "no_face", faces=faces)))
    assert "couldn't find the speaker's face anywhere in the clip" in receipt.reason
    assert "part of the clip" not in receipt.reason


@pytest.mark.parametrize(("presence", "words"), [(0.22, "in part of the clip"), (0.0, "anywhere")])
def test_a_tracker_receipt_reads_its_presence_share(presence, words):
    """KRI-1020 receipt v2 records how much of the take shows the speaker (`faces.presence`)
    instead of Haar frame counts."""
    faces = {"detector": "yunet", "presence": presence, "absent_s": 27.0}
    receipt = _receipt(_variant(_framing("letterbox", "no_face", version=2, faces=faces)))
    assert words in receipt.reason


def test_a_face_fill_with_the_speaker_out_of_shot_is_partly_and_says_how_long():
    """T1 after KRI-1020: the crop follows Maximo, but the camera is on the Cabildo for the
    first 17 s, so "his face in frame the whole time" is not true of the video."""
    faces = {"detector": "yunet", "presence": 0.5, "absent_s": 17.2, "held_share": 0.97}
    req = _req(_T1_R5)
    variant = _variant(_framing("face_fill", "face_in_window", version=2, faces=faces))
    receipt = _receipt(variant, req)
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == (
        "The video fills the vertical frame and follows the speaker's face, but their face "
        "isn't in view for about 17 s of the clip"
    )
    with reply_language_for("tr"):
        tr = _receipt(variant, req)
    assert tr.reason == (
        "Video dikey ekranı dolduruyor ve konuşan kişinin yüzünü takip ediyor, ama klibin "
        "yaklaşık 17 sn'sinde konuşan kişinin yüzü görünmüyor"
    )
    brief_gap = {**faces, "absent_s": 0.4}
    short = _receipt(_variant(_framing("face_fill", "face_in_window", faces=brief_gap)), req)
    assert (short.status, short.reason) == (
        "met",
        "The video fills the vertical frame and the speaker's face stays in it throughout",
    )


@pytest.mark.parametrize("language", ["en", "tr"])
def test_no_framing_reason_calls_the_speaker_you(language):
    """Every speaker-framing line is right when the creator filmed someone else."""
    variants = [_variant()]
    for mode, reason in [
        ("face_fill", "face_in_window"),
        ("letterbox", "face_moves_too_much"),
        ("letterbox", "no_face"),
        ("letterbox", "face_unconfirmed"),
        ("letterbox", "face_under_captions"),
        ("letterbox", "creator_chose_bars"),
        ("centre_fill", "no_face"),
        ("centre_fill", "face_unconfirmed"),
        ("centre_fill", "creator_chose_crop"),
        ("centre_fill", "not_landscape"),
        ("letterbox", "faces_too_far_apart"),
        ("centre_fill", "some_future_reason"),
    ]:
        variants.append(_variant(_framing(mode, reason)))
        variants.append(_variant(_framing(mode, reason, faces=_T1_FACES)))
        tracked = {"detector": "yunet", "presence": 0.5, "absent_s": 17.2}
        variants.append(_variant(_framing(mode, reason, version=2, faces=tracked)))
    req = _req(_T1_R5)
    with reply_language_for(language):
        reasons = [_receipt(variant, req).reason or "" for variant in variants]
        reasons.append(check_requirement(req, PlanFacts(edit_format="subtitled")).reason or "")
    for text in reasons:
        folded = text.casefold()
        if language == "en":
            assert "your face" not in folded and "framed you" not in folded, text
            continue
        words = folded.replace(",", " ").split()
        assert "seni" not in words, text
        # Turkish spells "your face" and "the speaker's face" alike (yüzünü); only the
        # possessor in front of it tells them apart. "o yüzden" is "so".
        for i, word in enumerate(words):
            if word.startswith("yüz") and word != "yüzden":
                assert i > 0 and words[i - 1] == "kişinin", text


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


# --- KRI-547 follow-up: a pop-in dropped for lack of room says so ---------------------

R3 = "her 'kahve' kelimesinde küçük bir fincan sesi koy"
R4 = "'İlk durak' dediğinde kahve demleme videosunu köşede küçük göster"
_KAHVE = {"beat_id": "kahve-sesi", "trigger": "kahve", "at_s": 5.08, "sound_label": "Glass clink"}


def _beats_variant(placed: list, unplaced: list) -> dict:
    return _variant(
        _framing("face_fill", "face_in_window"),
        phone_beat_receipt={
            "version": 1,
            "matcher": "phrase",
            "face_sampling": "ok",
            "placed": placed,
            "unplaced": unplaced,
            "closing": {"status": "none", "badge": "none"},
        },
    )


_NO_ROOM = [{"beat_id": "ilk-durak-video", "trigger": "İlk durak", "reason": "no_safe_spot"}]


@pytest.mark.parametrize(("placed", "status"), [([_KAHVE], "partial"), ([], "not_possible")])
def test_the_kadikoy_card_without_room_is_told_as_that(placed, status):
    variant = _beats_variant(placed, _NO_ROOM)
    assert plan_facts_from_phone_variant(variant).beat_room_drops == (("İlk durak", "no_room"),)
    with reply_language_for("tr"):
        tr = _receipt(variant, _req(R4))
    with reply_language_for("en"):
        en = _receipt(variant, _req(R4))
    assert (tr.status, tr.verification) == (status, "checked")
    assert tr.reason == "İlk durak için bir yüzü ya da altyazıları kapatmadan ekranda yer yoktu."
    assert en.reason == (
        "There was no room on screen for İlk durak without covering a face or the captions."
    )
    assert "sözlerine göre çıkan görsel yok" not in tr.reason


def test_another_asks_dropped_card_is_not_blamed_on_the_sound_ask():
    with reply_language_for("en"):
        placed = _receipt(_beats_variant([_KAHVE], _NO_ROOM), _req(R3, kind="audio"))
        nothing = _receipt(_beats_variant([], _NO_ROOM), _req(R3, kind="audio"))
    assert placed.status == "met"
    assert "no room" not in (nothing.reason or "")


def test_a_pop_in_that_collided_with_another_says_so():
    variant = _beats_variant(
        [_KAHVE], [{"beat_id": "moda", "trigger": "Moda", "reason": "overlap"}]
    )
    with reply_language_for("en"):
        receipt = _receipt(variant, _req('when I say "Moda" show my photo'))
    assert receipt.status == "not_possible"
    assert receipt.reason == "Moda would have landed on another pop-in at the same moment."


def test_a_never_heard_word_is_still_never_heard():
    variant = _beats_variant(
        [_KAHVE], [{"beat_id": "moda", "trigger": "Moda", "reason": "never_heard"}]
    )
    assert plan_facts_from_phone_variant(variant).beat_room_drops == ()
    with reply_language_for("en"):
        receipt = _receipt(variant, _req('when I say "Moda" show my photo'))
    assert "never heard" in receipt.reason
