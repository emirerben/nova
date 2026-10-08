"""KRI-520: editor-op notes and the no-match question follow the chat language."""

from __future__ import annotations

from app.kria.reply_language import reply_language_for
from app.services.kria_editor_ops_text import zero_match_message


def test_zero_match_message_in_turkish_and_unchanged_english() -> None:
    selector = {"group": "labels", "clip_ids": ["c2"], "contains": "Kadıköy"}
    english = zero_match_message(selector)
    assert english == (
        "I couldn't find any clip label on that clip containing “Kadıköy”, so I changed "
        "nothing. Which text did you mean?"
    )
    with reply_language_for("tr"):
        assert zero_match_message(selector) == (
            "O klipte “Kadıköy” içeren bir klip etiketi bulamadım, bu yüzden hiçbir şeyi "
            "değiştirmedim. Hangi yazıyı kastettin?"
        )
        assert zero_match_message({"ids": ["t1"]}).startswith("O yazıyı bulamadım")
        assert zero_match_message({"group": "title"}, find="Yaz").startswith(
            "“Yaz” içeren bir başlık bulamadım"
        )
