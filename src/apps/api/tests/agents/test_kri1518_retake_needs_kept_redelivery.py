"""KRI-1518: a "restarted sentence" is cut only when the speaker says it again in what stays.

Prod job 91270ec0 ("Altının yolu", 2026-11-17, phone Voiceover, Turkish, consented Clean up
speech). The voiceover circles one phrase three ways before moving on:

    … bunu anlatacağız. Buradan aldıkları, Ant Dağları'ndan aldıkları, çıkardıkları altınlar,
    buradan aldıkları altınlar, hepsini Armada Española, yani …

The paid `retake_detector` answer auto-applied a `retake` removal of 22.24–28.48 s, and the
finished video goes "… bunu anlatacağız. Hepsini Armada Española …": all three deliveries are
gone, "Hepsini" refers to nothing, and the creator's quoted cue "Ant Dağları'ndan" has no word
left to land on. The prompt says "ALWAYS KEEP THE LAST TAKE", but nothing in code enforced it:
`normalize_retake_spans` only checks a span's shape, and it merges back-to-back spans into one
block, so one span per delivery also removes every delivery.

The exact model answer is not in the saved job (its agent_run sits under request_id
`speech-cleanup-retakes:165215b4…`), so every answer shape that removes all three deliveries is
replayed through the real agent (`RetakeDetectorAgent.run` with a mocked model): one span over
the lot, one span per delivery, and the same with the spans out of order.

Word texts are the kit transcript of the voiceover (`T4_voiceover.m4a`); times are spread
evenly inside each transcript line, because only the word ORDER decides this check.

How it can fail (written before the code):

F1  A span with nothing re-said after it still cuts (the prod answer, as one span).
F2  Back-to-back spans are each "followed" by the next delivery, but that delivery is cut too:
    the re-delivery must be found in words that STAY, not in another cut span.
F3  A real restart stops being cut. The re-said take often starts with a short word ("At
    kilometer 32", "So today we're"), carries a different number, or follows a restart marker
    ("sorry", "No, again.", "dur baştan alayım") that is never re-said. Controls: every
    retake golden of the eval suite, with its own expected answer.
F4  Turkish word forms or spelling defeat the match: case and dotted capitals ("İspanyollar"),
    letters with marks (ş, ğ, ı), apostrophes and suffixes ("Cusco" / "Cusco'ya").
F5  An echo far away counts as the re-delivery: only the opening of what stays right after the
    span can re-say it, never a recap later in the clip.
F6  The guard reaches the creator-decided `review_candidates` list. Those are asked, not cut,
    so they keep today's rules (the existing overlap filter still runs against the auto list).
F7  Short words alone make two different sentences a "re-say" (review on #2378): "I" + "a",
    or Turkish "ve" + "bir", are shared by most neighbouring sentences. Short words may help
    reach the count ("At kilometer 30… sorry. At kilometer 32" shares "at" + "kilometer"),
    but at least one shared word must be a real word of four letters or more. Control: the
    prod KRI-1288 Q2 retake ("Five projects the AI builder." said twice) still cuts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.retake_detector import (
    RetakeDetectorAgent,
    RetakeDetectorInput,
    RetakeDetectorOutput,
)

pytestmark = pytest.mark.journey("kri-551-consented-retakes")

MODEL = RetakeDetectorAgent.spec.model

# Kit transcript lines 6–8 of the voiceover (source seconds), each line's words spread evenly.
_LINES = (
    (19.62, 22.02, "İspanyolular bunu tabii ki nasıl silaha çevirdi bunu anlatacağız."),
    (
        22.02,
        28.96,
        "Buradan aldıkları Ant Dağları'ndan aldıkları çıkardıkları altınlar "
        "buradan aldıkları altınlar hepsini",
    ),
    (28.96, 37.02, "Armada Española yani bu meşhur İspanyol deniz donanmasını kurup"),
)


def _altin_words() -> list[dict]:
    words: list[dict] = []
    for start, end, text in _LINES:
        tokens = text.split()
        step = (end - start) / len(tokens)
        for k, token in enumerate(tokens):
            words.append(
                {
                    "i": len(words),
                    "text": token,
                    "start_s": round(start + k * step, 3),
                    "end_s": round(start + (k + 1) * step - 0.02, 3),
                }
            )
    return words


ALTIN = _altin_words()
TEXTS = [w["text"] for w in ALTIN]
FIRST = TEXTS.index("Buradan")  # first delivery starts
ANT = TEXTS.index("Ant")  # second delivery starts
SECOND_BURADAN = TEXTS.index("buradan")  # third delivery starts
HEPSINI = TEXTS.index("hepsini")
LAST_ALTINLAR = HEPSINI - 1


def _span(start: int, end: int) -> dict:
    return {"start_word": start, "end_word": end, "reason": "abandoned delivery re-said later"}


def _run(mock_client, words: list[dict], answer: dict, language: str = "tr"):
    mock_client.queue(MODEL, answer)
    return RetakeDetectorAgent(mock_client).run(RetakeDetectorInput(words=words, language=language))


def _spans(out: RetakeDetectorOutput) -> list[tuple[int, int]]:
    return [(s.start_word, s.end_word) for s in out.retakes]


# ── F1/F2: the prod shapes that remove every delivery ────────────────────────


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"retakes": [_span(FIRST, LAST_ALTINLAR)]}, id="one-span-over-all-three"),
        pytest.param(
            {
                "retakes": [
                    _span(FIRST, ANT - 1),
                    _span(ANT, SECOND_BURADAN - 1),
                    _span(SECOND_BURADAN, LAST_ALTINLAR),
                ]
            },
            id="one-span-per-delivery",
        ),
        pytest.param(
            {
                "retakes": [
                    _span(SECOND_BURADAN, LAST_ALTINLAR),
                    _span(FIRST, ANT - 1),
                    _span(ANT, SECOND_BURADAN - 1),
                ]
            },
            id="one-span-per-delivery-unordered",
        ),
    ],
)
def test_kri1518_no_delivery_of_the_gold_phrase_is_left_so_nothing_is_cut(
    mock_client, answer
) -> None:
    assert TEXTS[HEPSINI + 1 : HEPSINI + 3] == ["Armada", "Española"]
    out = _run(mock_client, ALTIN, answer)
    assert _spans(out) == []


def test_kri1518_a_span_followed_only_by_another_cut_span_is_not_re_said(mock_client) -> None:
    # F2: the first delivery IS followed by more "aldıkları", but that word is cut as well.
    out = _run(
        mock_client,
        ALTIN,
        {"retakes": [_span(FIRST, ANT - 1), _span(ANT + 1, LAST_ALTINLAR)]},
    )
    assert _spans(out) == []


def test_kri1518_keeping_the_last_delivery_still_cuts_the_earlier_ones(mock_client) -> None:
    # Control for F1: the same phrase with the last delivery kept is a re-said take.
    out = _run(mock_client, ALTIN, {"retakes": [_span(FIRST, SECOND_BURADAN - 1)]})
    assert _spans(out) == [(FIRST, SECOND_BURADAN - 1)]


# ── F3: every real restart of the eval goldens still cuts ────────────────────

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures/agent_evals/retake_detector/golden"


@pytest.mark.parametrize(
    "name", ["en_restart", "tr_restart", "green3_s2_restart", "n3_marathon_restart"]
)
def test_kri1518_every_golden_restart_is_still_cut(mock_client, name: str) -> None:
    fixture = json.loads((_GOLDEN / f"{name}.json").read_text())
    expected = [(s["start_word"], s["end_word"]) for s in fixture["output"]["retakes"]]
    assert expected  # the fixture is a positive case
    out = _run(
        mock_client,
        fixture["input"]["words"],
        json.loads(fixture["raw_text"]),
        language=fixture["input"]["language"],
    )
    assert _spans(out) == expected


def _plain(tokens: str) -> list[dict]:
    return [
        {"i": i, "text": t, "start_s": round(i * 0.3, 2), "end_s": round(i * 0.3 + 0.25, 2)}
        for i, t in enumerate(tokens.split())
    ]


def test_kri1518_a_flub_re_said_with_a_new_number_is_still_cut(mock_client) -> None:
    words = _plain("This lens costs four hun— this lens costs five hundred dollars today.")
    out = _run(mock_client, words, {"retakes": [_span(0, 4)]}, language="en")
    assert _spans(out) == [(0, 4)]


# ── F4: Turkish forms ─────────────────────────────────────────────────────────


def test_kri1518_turkish_case_marks_and_suffixes_still_match(mock_client) -> None:
    words = _plain("İŞTE Cusco şehrine— pardon. İşte Cusco'ya geldik arkadaşlar bugün.")
    out = _run(mock_client, words, {"retakes": [_span(0, 3)]})
    assert _spans(out) == [(0, 3)]


# ── F5: a far echo is no re-delivery ─────────────────────────────────────────


def test_kri1518_an_echo_later_in_the_clip_is_not_the_re_delivery(mock_client) -> None:
    words = _plain(
        "Lumo phones filmed it from drones. Then the crowd cheered and the models walked "
        "out slowly. Lumo phones filmed it all."
    )
    out = _run(mock_client, words, {"retakes": [_span(0, 5)]}, language="en")
    assert _spans(out) == []


# ── F6: the asked list keeps today's rules ───────────────────────────────────


def test_kri1518_review_candidates_are_not_judged_by_the_guard(mock_client) -> None:
    out = _run(
        mock_client,
        ALTIN,
        {"retakes": [], "review_candidates": [_span(FIRST, LAST_ALTINLAR)]},
    )
    assert out.retakes == []
    assert [(s.start_word, s.end_word) for s in out.review_candidates] == [(FIRST, LAST_ALTINLAR)]


# ── F7: short words alone are no re-say ──────────────────────────────────────


@pytest.mark.parametrize(
    ("tokens", "span", "language"),
    [
        pytest.param(
            "I went to a market today. I bought a new phone there and left.",
            (0, 5),
            "en",
            id="en-only-I-and-a",
        ),
        pytest.param(
            "Sabah ve bir kahve içtik. Akşam ve bir film izledik sonra eve döndük.",
            (0, 4),
            "tr",
            id="tr-only-ve-and-bir",
        ),
    ],
)
def test_kri1518_sentences_sharing_only_short_words_are_not_a_re_say(
    mock_client, tokens: str, span: tuple[int, int], language: str
) -> None:
    out = _run(mock_client, _plain(tokens), {"retakes": [_span(*span)]}, language=language)
    assert _spans(out) == []


_KRI1288_Q2 = Path(__file__).resolve().parents[1] / "fixtures/kri1288_q2_run2_tight_pauses.json"


def test_kri1518_the_prod_kri1288_q2_retake_still_cuts(mock_client) -> None:
    fixture = json.loads(_KRI1288_Q2.read_text())
    words = [
        {"i": i, "text": w["text"], "start_s": w["start_s"], "end_s": w["end_s"]}
        for i, w in enumerate(fixture["words"])
    ]
    retake = next(r for r in fixture["consented"] if r["reason"] == "retake")
    inside = [w["i"] for w in words if w["end_s"] <= retake["end_s"] + 0.05]
    span = (inside[0], inside[-1])
    assert [w["text"] for w in words[: span[1] + 1]] == [
        "Five",
        "projects",
        "the",
        "AI",
        "builder.",
    ]
    out = _run(mock_client, words, {"retakes": [_span(*span)]}, language="en")
    assert _spans(out) == [span]
