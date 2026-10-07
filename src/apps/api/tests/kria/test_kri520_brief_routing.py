"""KRI-520: the scope router understands Turkish redo / move / remove asks.

A Turkish "2. klibi başa al" or "son klibi sil" is a quick editor edit, not a slow
full re-plan and re-render; "tekrar dene" / "sıfırdan yap" is a supported redo. The
parsers fold text with ``loose_text``, so ASCII-typed Turkish and English all-caps
("TRY AGAIN") are read the same way.
"""

from __future__ import annotations

import pytest

from app.kria.brief import (
    BriefUpdate,
    CurrentPlanShape,
    _asks_move,
    _asks_remove,
    _fold_for_redo,
    route_requirements,
    wants_full_replan,
)


def _upd(kind: str, scope: str, **kw) -> BriefUpdate:  # noqa: ANN003
    kw.setdefault("description", "x")
    return BriefUpdate(kind=kind, scope=scope, **kw)


CAN_EDIT = CurrentPlanShape(has_render=True, has_per_clip_text_lane=True, can_edit_timeline=True)
WITH_LANE = CurrentPlanShape(has_render=True, has_per_clip_text_lane=True)


# ------------------------------------------------------------------------------ redo


@pytest.mark.parametrize(
    "message",
    [
        # English, including the all-caps case the old Turkish-I fold broke.
        "TRY AGAIN",
        "Try again",
        "DO IT AGAIN",
        "START OVER",
        "do it again",
        "redo this",
        # Turkish, as typed with and without Turkish letters.
        "tekrar hazırla",
        "tekrar hazirla",
        "TEKRAR HAZIRLA",
        "yeniden oluştur",
        "yeniden olustur",
        "tekrar oluştur",
        "yeniden yap",
        "YENİDEN YAP",
        "tekrar yap",
        "baştan yap",
        "bastan yap",
        "BAŞTAN YAPALIM",
        "Baştan kes",
        "sıfırdan yap",
        "sifirdan basla",
        "SIFIRDAN",
        "tekrar dene",
        "yeniden dene",
        "bir daha yap",
        "bir daha dene",
        "Bunu bir daha yap lütfen",
        "olmadı, tekrar dene",
        "videoyu tekrar hazırla",
        "tekrar yapar mısın",
    ],
)
def test_redo_phrases_replan(message: str) -> None:
    assert wants_full_replan(message)


@pytest.mark.parametrize(
    "message",
    [
        # English ordinary edits and negations are unchanged.
        "make the title bigger again",
        "cut the intro again, it's too slow",
        "don't do that again",
        "make the text bigger",
        # Turkish ordinary edits.
        "yaz rengini değiştir",
        "tekrar kes",
        "başlığı büyüt",
        "sonra tekrar bakarız",
        "yeniden",
        "bir daha",
        # "baştan sona" is start to finish, not "from the top".
        "videoyu baştan sona izledim, güzel olmuş",
        # Negated asks are not redo asks.
        "tekrar yapma",
        "bir daha yapma",
        "tekrar deneme",
        "yeniden yapmayın",
        # A redo aimed at one element is an ordinary edit.
        "başlığı tekrar yap",
        "müziği bir daha yap",
        "yazıyı yeniden oluştur",
        "altyazıyı tekrar hazırla",
        "2. klibi tekrar yap",
    ],
)
def test_ordinary_turkish_and_english_edits_are_not_redo(message: str) -> None:
    assert not wants_full_replan(message)


def test_fold_keeps_english_capitals_ascii() -> None:
    assert _fold_for_redo("TRY AGAIN") == "try again"
    assert _fold_for_redo("Hazırla İSTANBUL") == "hazirla istanbul"
    assert _fold_for_redo("  Başlık   Yok ") == "baslik yok"


# ------------------------------------------------------------------------------ move


@pytest.mark.parametrize(
    "message",
    [
        "2. klibi başa al",
        "2. klibi basa al",
        "son klibi sona taşı",
        "SON KLİBİ SONA TAŞI",
        "Galata klibini başa getir",
        "ilk klibi en sona koy",
        "ilk klibi sona at",
        "3. klibi 1. sıraya koy",
        "2. klibi başa alır mısın",
        "kafe klibinden önce koy",
        "ilk ve son klibin yerini değiştir",
        # English still matches, in any case.
        "move the first clip to the end",
        "MOVE THE FIRST CLIP TO THE END",
    ],
)
def test_turkish_and_english_move_asks(message: str) -> None:
    assert _asks_move(_fold_for_redo(message))


@pytest.mark.parametrize(
    "message",
    [
        "başlığı büyüt",
        "sonra başlığı büyüt",
        "videoyu sona kadar izle",
        "müziği sona kadar çal",
        "sona ekle",
        "ilk klibi sil",
        "ilk klibi başa alma",
        "klibi sona taşıma",
        "show one at a time",
        "make the title bigger",
    ],
)
def test_ordinary_sentences_are_not_move_asks(message: str) -> None:
    assert not _asks_move(_fold_for_redo(message))


# ---------------------------------------------------------------------------- remove


@pytest.mark.parametrize(
    "message",
    [
        "son klibi sil",
        "SON KLİBİ SİL",
        "3. klibi çıkar",
        "3. klibi cikar",
        "ilk klibi kaldır",
        "ikinci klibi videodan çıkar",
        "üçüncü klibi sil",
        "klip 3'ü sil",
        "3 numaralı klibi sil",
        "3. klip silinsin",
        "5. videoyu kaldırır mısın",
        # English still matches.
        "delete the last clip",
        "REMOVE CLIP 4",
    ],
)
def test_turkish_and_english_remove_asks(message: str) -> None:
    assert _asks_remove(_fold_for_redo(message))


@pytest.mark.parametrize(
    "message",
    [
        # Semantic selection and counts go to the planner, not an editor op.
        "komik olmayan klipleri sil",
        "tüm klipleri sil",
        "3 klibi sil",
        "ilk klipleri sil",
        # Not a removal at all.
        "ilk klibi kısalt",
        "son klibi uzat",
        "3. klibi yavaşlat",
        "ilk klibi beğendim",
        # Negations.
        "ilk klibi silme",
        "3. klibi çıkarma",
        "3. klip silinmesin",
    ],
)
def test_ordinary_sentences_are_not_remove_asks(message: str) -> None:
    assert not _asks_remove(_fold_for_redo(message))


# --------------------------------------------------------------------- the router


@pytest.mark.parametrize(
    ("reqs", "shape", "message", "expected"),
    [
        # A positional move / removal stays a quick editor edit when the clip family is open.
        (
            [_upd("order", "global", facts={"key": "position"})],
            CAN_EDIT,
            "2. klibi başa al",
            "editor_ops",
        ),
        (
            [_upd("order", "global", facts={"key": "position"})],
            CAN_EDIT,
            "son klibi sona taşı",
            "editor_ops",
        ),
        ([_upd("select", "global")], CAN_EDIT, "son klibi sil", "editor_ops"),
        ([_upd("select", "global")], CAN_EDIT, "3. klibi çıkar", "editor_ops"),
        ([_upd("select", "global")], CAN_EDIT, "ilk klibi kaldır", "editor_ops"),
        # ... but not when clip ops are withheld.
        ([_upd("select", "global")], WITH_LANE, "son klibi sil", "replan"),
        # Semantic selection and basis-driven ordering still need the planner.
        ([_upd("select", "global")], CAN_EDIT, "komik olmayan klipleri sil", "replan"),
        (
            [_upd("order", "global", facts={"key": "capture_time"})],
            CAN_EDIT,
            "çektiğim saate göre sırala",
            "replan",
        ),
        # A redo phrase wins over everything.
        ([_upd("select", "global")], CAN_EDIT, "son klibi sil ve baştan yap", "replan"),
        ([], CAN_EDIT, "sıfırdan yap", "replan"),
        ([], CAN_EDIT, "TRY AGAIN", "replan"),
        ([], CAN_EDIT, "başlığı büyüt", "editor_ops"),
        ([], CAN_EDIT, "başlığı tekrar yap", "editor_ops"),
    ],
)
def test_router_follows_turkish_asks(reqs, shape, message, expected) -> None:  # noqa: ANN001
    assert route_requirements(reqs, shape, message=message) == expected
