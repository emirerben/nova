"""KRI-552: text shortened for a creator-facing question is never cut mid-word."""

from __future__ import annotations

import pytest

from app.kria.text_trim import trim_at_word


def test_the_prod_label_keeps_whole_words() -> None:
    label = "the part where I restart the kilometer thirty sentence"

    # The old 40-character slice ended "...restart the kilometer t".
    assert label[:40] == "the part where I restart the kilometer t"
    assert trim_at_word(label, 40) == "the part where I restart the kilometer…"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short enough", 40, "short enough"),
        ("exactly ten", 11, "exactly ten"),
        ("one two three", 9, "one two…"),
        ("one two, three", 10, "one two…"),  # no dangling comma before the ellipsis
        ("supercalifragilistic", 8, "superca…"),  # one word with nowhere to break
        ("klibi çamaşır sepetinde buldum", 20, "klibi çamaşır…"),
        ("line one\nline two is long", 14, "line one\nline…"),
    ],
)
def test_trim_cases(text: str, limit: int, expected: str) -> None:
    out = trim_at_word(text, limit)

    assert out == expected
    assert len(out) <= limit


# ---------------------------------------------------------------- KRI-1408: whole replies
#
# ylab build 1977, a German hold reply over 1,200 characters was cut at a fixed letter count:
# "…sie sind auf Deutsch; Ich s…". Emir's "2A": shorten the longest reasons first, never cut a
# word. Ways `fit_lines` can go wrong, written before the code:
#
# 1. A word is cut in half anywhere ("Ich s…").
# 2. The result is longer than the limit.
# 3. A short line (a label, the question) is shortened while a long reason stays whole.
# 4. The ask a line names is lost while its reason survives, or a "(" is left unclosed.
# 5. Text that already fits changes at all.
# 6. One word longer than the limit (no space anywhere) cannot fit: it is still cut, never
#    longer than the limit.
# 7. So many lines that even shortened reasons cannot fit: the end is dropped at a whole word.

from app.kria.text_trim import fit_lines  # noqa: E402

_YLAB_REASON = (
    "Jede Stimme in deinem Take bekommt Untertitel, auch die außerhalb des Bildes; sie sind "
    "auf Deutsch; ich schreibe Usersmagic, Nike, Delivery Hero, Mina Foundation, Mexico City, "
    "Kria und 4 weitere so, wie du sie geschrieben hast, und prüfe sie im fertigen Video"
)


def _words(text: str) -> set[str]:
    spaced = text.replace("\n", " ").replace("(", " ").replace(")", " ")
    return {word.strip(",;:") for word in spaced.split()}


def _ylab_reply() -> str:
    lines = [
        f"- Teilweise erledigt: Ask number {i} for the edit ({_YLAB_REASON})" for i in range(5)
    ]
    return "Nicht alles, was du wolltest, ist im Video:\n" + "\n".join(lines)


def test_kri1408_the_ylab_reply_keeps_whole_words_and_every_ask() -> None:
    text = _ylab_reply()
    assert len(text) > 1200
    out = fit_lines(text, 1200)

    assert len(out) <= 1200  # 2
    for word in _words(out):  # 1: every word is one of the original's, or ends in "…"
        assert word in _words(text) or word.endswith("…"), word
        if word.endswith("…"):
            assert word[:-1] in _words(text) or word == "…", word
    for i in range(5):  # 4: every ask is still named
        assert f"Ask number {i} for the edit (" in out
    assert out.count("(") == out.count(")")  # 4
    assert out.startswith("Nicht alles, was du wolltest, ist im Video:\n")  # 3


def test_the_longest_reason_is_shortened_first() -> None:
    short = "- Erledigt: Titel (passt)"
    long = "- Teilweise erledigt: Untertitel (" + " ".join(["wort"] * 60) + ")"
    text = f"{short}\n{long}"
    out = fit_lines(text, len(text) - 40)

    assert out.split("\n")[0] == short  # 3
    assert out.split("\n")[1].endswith("…)")
    assert len(out) <= len(text) - 40


def test_text_that_fits_is_unchanged() -> None:
    text = _ylab_reply()
    assert fit_lines(text, len(text)) == text  # 5


def test_one_unbreakable_word_still_fits() -> None:
    out = fit_lines("x" * 50, 20)
    assert len(out) <= 20  # 6


def test_too_many_lines_drop_the_end_at_a_whole_word() -> None:
    text = "\n".join(f"- Erledigt: Ask {i} (kurz)" for i in range(200))
    out = fit_lines(text, 300)

    assert len(out) <= 300  # 7
    assert out.endswith("…")
    assert all(w in _words(text) or w.endswith("…") for w in _words(out))
