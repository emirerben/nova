"""KRI-520: a blocked render's recovery, written in English by the render worker, reads
Turkish in a Turkish chat; anything that isn't exactly that copy shows as stored."""

from __future__ import annotations

from app.kria.brief_checks import NO_TITLE_REASON, _loc, render_block_recovery
from app.kria.reply_language import reply_language_for
from app.tasks.kria_runtime import _blocked_title_question, _localized_block_recovery


def _receipts() -> list[dict]:
    return [
        {
            "requirement_id": "r1",
            "status": "not_possible",
            "verification": "checked",
            "reason": NO_TITLE_REASON,
        }
    ]


def test_worker_written_block_recovery_is_reworded_for_a_turkish_chat() -> None:
    stored = render_block_recovery(_receipts()).message  # the worker binds no language
    assert _localized_block_recovery(stored, _receipts()) == stored
    with reply_language_for("tr"):
        turkish = _localized_block_recovery(stored, _receipts())
    assert turkish.startswith("Videoyu henüz oluşturamadım")
    assert '"başlık olmasın"' in turkish


# What the phone Voiceover worker stored when it could not place the clips on the narration
# (prod job ef8e35b8, a Turkish chat). KRI-592: the chat showed it in English.
NARRATED_ALIGNMENT_STORED = (
    "I couldn't match the narration to the actions reliably. Your draft is saved. "
    "Should I try again or use a simpler clip sequence?"
)


def test_the_voiceover_alignment_recovery_reads_turkish_in_a_turkish_chat() -> None:
    unjudged = [{**_receipts()[0], "verification": "unchecked", "status": "partial"}]
    # An English chat keeps the stored copy.
    assert _localized_block_recovery(NARRATED_ALIGNMENT_STORED, []) == NARRATED_ALIGNMENT_STORED
    with reply_language_for("tr"):
        # With no brief (no receipts) and with a brief whose receipts judged nothing.
        for receipts in ([], unjudged):
            turkish = _localized_block_recovery(NARRATED_ALIGNMENT_STORED, receipts)
            assert turkish == (
                "Seslendirmeyi kliplerle güvenilir bir şekilde eşleştiremedim. Taslağın "
                "kaydedildi. Tekrar mı deneyeyim, yoksa daha basit bir klip sırası mı "
                "kullanayım?"
            )


def test_other_recovery_copy_is_left_as_stored() -> None:
    with reply_language_for("tr"):
        assert _localized_block_recovery("Something else.", _receipts()) == "Something else."
        assert _localized_block_recovery("Anything.", []) == "Anything."


def test_turkish_no_title_receipt_still_gets_the_tappable_question() -> None:
    with reply_language_for("tr"):
        receipts = [{**_receipts()[0], "reason": _loc(NO_TITLE_REASON)}]
    assert receipts[0]["reason"] != NO_TITLE_REASON
    assert _blocked_title_question(receipts) is not None
