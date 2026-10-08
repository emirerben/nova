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


def test_other_recovery_copy_is_left_as_stored() -> None:
    with reply_language_for("tr"):
        assert _localized_block_recovery("Something else.", _receipts()) == "Something else."
        assert _localized_block_recovery("Anything.", []) == "Anything."


def test_turkish_no_title_receipt_still_gets_the_tappable_question() -> None:
    with reply_language_for("tr"):
        receipts = [{**_receipts()[0], "reason": _loc(NO_TITLE_REASON)}]
    assert receipts[0]["reason"] != NO_TITLE_REASON
    assert _blocked_title_question(receipts) is not None
