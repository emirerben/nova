"""KRI-520: the scope router understands Turkish move / remove asks.

A Turkish "2. klibi başa al" or "son klibi sil" is a quick editor edit, not a slow
full re-plan and re-render. The Turkish patterns fold text with ``loose_text`` (ASCII-typed
Turkish matches) and never run on an English message. Whether a turn is a redo is the brief
extractor's reading, never the wording (KRI-1392).
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
)


def _upd(kind: str, scope: str, **kw) -> BriefUpdate:  # noqa: ANN003
    kw.setdefault("description", "x")
    return BriefUpdate(kind=kind, scope=scope, **kw)


CAN_EDIT = CurrentPlanShape(has_render=True, has_per_clip_text_lane=True, can_edit_timeline=True)
WITH_LANE = CurrentPlanShape(has_render=True, has_per_clip_text_lane=True)


def test_english_routing_reads_all_caps_like_lowercase() -> None:
    # KRI-781: the English fold used to map a capital I to a dotless ı, so all-caps
    # English with an I never matched ("REMOVE CLIP 2" -> "remove clıp 2"). It now uses the
    # shared Turkish-aware fold, so capitals route exactly like lowercase.
    assert _asks_remove("REMOVE CLIP 2") == _asks_remove("remove clip 2") is True
    # English words that are also Turkish verbs/ordinals never reach the Turkish rules.
    assert not _asks_remove("Put video 3 at the start")
    assert not _asks_move("Show the kitten once at the end")
    assert not _asks_remove("Only the best clips; my son video at the beginning")


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
    assert _asks_move(message)


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
    assert not _asks_move(message)


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
        "Remove clip 4",
    ],
)
def test_turkish_and_english_remove_asks(message: str) -> None:
    assert _asks_remove(message)


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
    assert not _asks_remove(message)


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
        # Wording alone never re-plans: the extractor's rebuild verdict does (KRI-1392).
        ([_upd("select", "global")], CAN_EDIT, "son klibi sil ve baştan yap", "editor_ops"),
        ([], CAN_EDIT, "sıfırdan yap", "editor_ops"),
        ([], CAN_EDIT, "başlığı büyüt", "editor_ops"),
        ([], CAN_EDIT, "başlığı tekrar yap", "editor_ops"),
    ],
)
def test_router_follows_turkish_asks(reqs, shape, message, expected) -> None:  # noqa: ANN001
    assert route_requirements(reqs, shape, message=message) == expected
