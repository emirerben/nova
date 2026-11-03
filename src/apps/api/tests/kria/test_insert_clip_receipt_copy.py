"""What the finished video tells the creator about the clips it paused his talk for (parent
KRI-1151, review of #2086, finding 5 and the copy minors).

The receipt names each clip it means (by the name the app shows), says per clip what moved or
was matched by a weaker means, offers the nearest sentence for a clip it left out, and never
claims a reason that was not the reason. Written from the worker's receipt rows.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.reply_language import reply_language_for


def _req() -> BriefRequirement:
    return BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description="Place the clips between my speech",
        facts={"insert_clip": "the clips"},
    )


def _verdict(placed, dropped):
    facts = plan_facts_from_phone_variant(
        {
            "resolved_archetype": "subtitled",
            "render_destination": "device",
            "render_status": "ready",
            "phone_insert_clips": {"placed": placed, "dropped": dropped},
        }
    )
    (receipt,) = build_receipts([_req()], facts, include_unchecked=True, strict_order=True)
    return receipt


def _row(name, **kw):
    return {"media_id": f"id-{name}", "name": name, "basis": "topic", **kw}


def test_each_clip_left_out_is_named_and_the_nearest_sentence_is_offered():
    verdict = _verdict(
        [_row("IMG_1")],
        [
            {
                "media_id": "x",
                "name": "IMG_7382",
                "reason": "no_matching_sentence",
                "after_text": "Ortada henüz alınmış bir karar yok",
            },
            {"media_id": "y", "name": "IMG_7390", "reason": "no_matching_sentence"},
        ],
    )
    assert verdict.status == "partial"
    assert "1 of 3" in verdict.reason
    assert "IMG_7382" in verdict.reason and "IMG_7390" in verdict.reason
    assert 'Put the IMG_7382 clip after "Ortada henüz alınmış bir karar yok" anyway?' in (
        verdict.reason
    )


def test_two_clips_left_out_for_one_reason_are_both_named_not_one_collapsed_reason():
    verdict = _verdict(
        [],
        [
            {"media_id": "x", "name": "A_1", "reason": "no_matching_sentence"},
            {"media_id": "y", "name": "B_2", "reason": "no_matching_sentence"},
        ],
    )
    assert verdict.status == "not_possible"
    assert verdict.reason.count("never talk about") == 2


def test_notes_for_the_clips_that_did_play_are_shown_beside_the_ones_left_out():
    verdict = _verdict(
        [_row("IMG_1", overrode=["hook_window"]), _row("IMG_2", via="guess", why="unclear")],
        [{"media_id": "x", "name": "IMG_9", "reason": "no_speech"}],
    )
    assert verdict.status == "partial"
    assert "IMG_9" in verdict.reason
    assert "IMG_1" in verdict.reason and "first seconds" in verdict.reason


def test_a_clip_placed_at_a_guess_says_so_and_asks_where_it_should_go():
    verdict = _verdict([_row("IMG_2", via="guess", why="unclear")], [])
    assert verdict.status == "met"
    assert "IMG_2" in verdict.reason and "best guess" in verdict.reason


def test_a_clip_with_no_facts_is_told_apart_from_an_unclear_one():
    nothing = _verdict([_row("IMG_3", via="guess", why="no_facts")], [])
    unclear = _verdict([_row("IMG_3", via="guess", why="unclear")], [])
    assert nothing.reason != unclear.reason
    assert "nothing to go on" in nothing.reason


def test_matched_by_words_does_not_claim_the_check_was_unavailable_unless_it_was():
    unavailable = _verdict([_row("A", via="keyword", why="check_unavailable")], [])
    other = _verdict([_row("A", via="keyword", why="invalid_answer")], [])
    assert "unavailable" in unavailable.reason
    assert "unavailable" not in other.reason and "by its words" in other.reason


def test_a_take_end_note_names_the_clip_and_the_time_it_was_given():
    verdict = _verdict([_row("IMG_4", adjusted="take_end")], [])
    assert "IMG_4" in verdict.reason and "time you gave" in verdict.reason


def test_a_clip_left_out_for_length_says_the_number():
    verdict = _verdict(
        [_row("IMG_1")],
        [{"media_id": "x", "name": "BIG_1", "reason": "over_device_budget", "would_be_s": 2100.0}],
    )
    assert "35 minutes" in verdict.reason and "30" in verdict.reason


def test_a_long_list_is_cut_at_a_whole_clip_and_stays_within_the_receipt_limit():
    dropped = [
        {
            "media_id": f"m{k}",
            "name": f"IMG_{7000 + k}",
            "reason": "no_matching_sentence",
            "after_text": "Kadıköy'deki tekellere neden maç öncesi",
        }
        for k in range(8)
    ]
    verdict = _verdict([_row("IMG_1")], dropped)
    assert len(verdict.reason) <= 300
    assert verdict.reason.rstrip(".?").endswith(("and more", "anyway"))


def test_turkish_names_the_clip_and_makes_the_offer():
    with reply_language_for("tr"):
        verdict = _verdict(
            [],
            [
                {
                    "media_id": "x",
                    "name": "IMG_7382",
                    "reason": "no_matching_sentence",
                    "after_text": "Bunların hepsi tesadüf mü?",
                }
            ],
        )
    assert "IMG_7382 klibi" in verdict.reason
    assert 'yine de "Bunların hepsi tesadüf mü?" cümlesinden sonra koyayım mı?' in verdict.reason


def test_a_clip_with_no_known_name_is_still_told_apart_by_what_happened_to_it():
    verdict = _verdict([], [{"media_id": "x", "reason": "no_matching_sentence"}])
    assert "one clip" in verdict.reason
