"""KRI-547: reading "vertical video, keep my face in frame" off the brief."""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.speaker_framing_ask import (
    asks_vertical_framing,
    brief_framing_requirement_ids,
    requirement_asks_vertical_framing,
)

# The prod brief (thread 9b625eec, 2026-10-08): r1 is the framing ask.
KADIKOY_R1 = "yatay videoyu dikey (9:16) formata getir ve yüzü hep kadrajda tut"


@pytest.mark.parametrize(
    "text",
    [
        KADIKOY_R1,
        "Yatay çektim ama dikey video istiyorum, yüzüm hep kadrajda kalsın",
        "YÜZÜM HEP KADRAJDA KALSIN",
        "yuzum hep kadrajda kalsin",  # typed without Turkish letters
        "dikeye çevir",
        "videoyu dikey yap",
        "dikey olsun",
        "beni kadrajda tut",
        "yüzümü takip et",
        "yüzüm kesilmesin",
        "tam ekran olsun",
        "ekranı doldursun",
        "siyah bant olmasın",
        "9:16 please",
        "make it 9x16",
        "Make it vertical",
        "turn the video into a vertical one",
        "crop it to portrait",
        "switch to vertical",
        "I want it vertical",
        "I want a vertical video",
        "vertical format please",
        "keep my face in frame",
        "make sure my face stays in the frame the whole time",
        "keep me in the shot",
        "track my face",
        "center my face",
        "don't cut off my head",
        "fill the whole screen",
        "full screen please",
        "no black bars",
        "get rid of the black bars",
    ],
)
def test_framing_asks_are_read(text):
    assert asks_vertical_framing(text)


@pytest.mark.parametrize(
    "text",
    [
        "add a vertical pan at the start",
        "vertical wipe between the clips",
        "use my vertical clip first",
        "the vertical video goes last",
        "make the text vertical",
        "dikey çektiğim videoyu kullan",
        "dikey olan klibi başa koy",
        "dikey yazı olsun",
        "dikey kaydırma efekti ekle",
        "show the brewing video full screen",
        "kahve demleme videosunu tam ekran göster",
        "put the photo full screen when I say Moda",
        "'İlk durak' dediğinde kahve demleme videosunu köşede küçük göster",
        "altyazılar Türkçe olsun; Moda, Bahariye, Yeldeğirmeni ve Kadıköy doğru yazılsın",
        "her 'kahve' kelimesinde küçük bir fincan sesi koy",
        "yüz kere söyledim",
        "faster cuts",
        "",
        None,
    ],
)
def test_other_asks_are_not_framing(text):
    assert not asks_vertical_framing(text)


def _req(rid="r1", kind="style", description=KADIKOY_R1, status="open", **extra):
    return BriefRequirement(
        id=rid, kind=kind, scope="global", description=description, status=status, **extra
    )


def test_only_live_style_requirements_count():
    assert requirement_asks_vertical_framing(_req())
    assert not requirement_asks_vertical_framing(_req(kind="text"))
    assert not requirement_asks_vertical_framing(_req(kind="select"))
    assert not requirement_asks_vertical_framing(_req(status="superseded"))
    assert requirement_asks_vertical_framing(_req(description=None, literal="9:16"))


def test_brief_ids_name_every_framing_requirement():
    brief = CreativeBrief(
        version=1,
        requirements=[
            _req("r1"),
            _req("r2", kind="text", description="altyazılar Türkçe olsun"),
            _req("r3", kind="style", description="keep my face in frame"),
            _req("r4", kind="style", description="faster cuts"),
        ],
    )
    assert brief_framing_requirement_ids(brief) == ["r1", "r3"]
    assert brief_framing_requirement_ids(None) == []
    assert brief_framing_requirement_ids(CreativeBrief()) == []
