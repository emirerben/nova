"""KRI-758: ONE described clip ("…bıraktığı kısa klibi kullanma") is not "the short clips".

Incident (prod thread 91256BF1, 2026-10-19, café kit M1): twelve clips, eleven of
2.2–6.4 s and one of 15.8 s. The creator asked to leave out a single clip by what it
shows: "Caro'nun eğilip tabağı masaya bıraktığı kısa klibi kullanma". ``length_asks`` read
``kısa klibi`` (singular accusative) + ``kullanma`` as ``LengthAsk("short", "skip")``; the
clips split cleanly at 6.4 s → 15.8 s, so "short" was eleven clips and
``apply_clip_scope`` kept one (``kria_clip_scope_applied model_selected=11 selected=1``).

Ways the fix could go wrong, written down before the code (each has a case):

1. The incident sentence is still read as a length group.
2. Same sentence on the real 12-clip manifest still narrows the pick.
3. Other singular spellings of the same thing (bare noun after the relative clause,
   possessive accusative, other nouns, English "the short clip where …") are still read as
   groups.
4. The fix over-reaches: plural / group phrases stop scoping ("kısa klipleri kullanma",
   "uzun çekimlerden", "skip the short clips", "from the long takes", "skip any short clip").
5. The superlatives, singular by nature, stop working ("en uzun klibi atla",
   "skip the longest clip").
6. The guard that keeps other asks' clips regresses: a length scope removes a clip the
   strategy itself places (montage audio source), so the render contract breaks.
7. Control: the plural phrase on the same manifest still narrows to the long clip, so
   cases 1–2 prove the fix and not a harness that cannot narrow anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.kria.clip_scope import LengthAsk, apply_clip_scope, length_asks

# The creator's message, verbatim (café kit M1_00_PROMPT.txt).
INCIDENT_PROMPT = (
    "Merope diye Kolombiyalı bir kafede Ekvadorlu Caro bize evin tatlısı obleayı getirdi, "
    'ilk ısırığı Atakan aldı. Başlık: "Kolombiya tatlısı, Ekvadorlu eller". Caro\'nun '
    'elinde siyah tepsiyle geldiği klip ilk olsun; el sallayıp "bye bye" dediğimiz klip '
    "en sonda. \"Hi, I'm Karolina, I'm from Ecuador\" dediği cümle baştan sona duyulsun. "
    'Tabağı kameraya uzattığı klibe "Oblea de la casa" etiketi koy, Atakan\'ın ısırdığı '
    "klibe \"Atakan'ın ilk ısırığı\" etiketi. Caro'nun eğilip tabağı masaya bıraktığı kısa "
    "klibi kullanma. Yazılar kimsenin yüzüne gelmesin. Müzik koyma, kliplerin kendi sesi "
    "kalsın. 20 saniye."
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri623_tigre_m2_montage_turn.json"
_BASE: dict[str, Any] = json.loads(FIXTURE.read_text())["main_creator_input"]["capability_manifest"]

# Eleven short clips and one 15.8 s clip: one clear split, "short" = eleven clips.
SHORT_SECONDS = (2.2, 2.9, 3.1, 3.4, 3.8, 4.1, 4.6, 5.0, 5.4, 5.9, 6.4)
LONG_SECONDS = 15.8
SHORT_IDS = [f"M1_{n:02d}.mp4" for n in range(1, 12)]
LONG_ID = "M1_12.mp4"
ALL_IDS = [*SHORT_IDS, LONG_ID]


def _manifest() -> ResolvedCreatorManifest:
    media = [
        {"kind": "video", "label": None, "media_id": media_id, "duration_s": seconds}
        for media_id, seconds in zip(ALL_IDS, (*SHORT_SECONDS, LONG_SECONDS), strict=True)
    ]
    return ResolvedCreatorManifest.model_validate({**_BASE, "media": media})


def _montage(**update: Any) -> CreativeStrategy:
    return CreativeStrategy.model_validate(
        {"edit_format": "montage", "media_scope": "selected", **update}
    )


# -- 1 + 3: the words ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # 1: the incident, whole message and the one sentence.
        INCIDENT_PROMPT,
        "Caro'nun eğilip tabağı masaya bıraktığı kısa klibi kullanma.",
        # 3: the same clip, other spellings.
        "Caro'nun bıraktığı kısa klip kullanma",  # bare noun after the relative clause
        "gülümsediği kısa klibi atla",
        "oturduğumuz uzun çekimi çıkar",
        "kısa klibini kullanma",  # possessive accusative
        "kısa klibi sil",
        "şu kısa videoyu atla",
        "kısa çekimi çıkar",
        "uzun görüntüyü kullanma",
        "kısa kaydı atla",
        # 3: English, one clip named by what it shows.
        "skip the short clip where Caro leans in",
        "don't use the short clip with the black tray",
        "skip the short one",
        "leave out the long take",
        "only the long take",
        "skip the short handheld clip",
        "drop my short video",
    ],
)
def test_one_clip_is_never_a_length_group(text: str) -> None:
    assert length_asks(text) == []


# -- 4: groups keep working --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("kısa klipleri kullanma", [LengthAsk("short", "skip")]),
        ("Caro'nun bıraktığı kısa klipleri kullanma", [LengthAsk("short", "skip")]),
        ("uzun çekimlerden en iyi altı anı seç", [LengthAsk("long", "only")]),
        ("uzun çekimden en iyi anı seç", [LengthAsk("long", "only")]),
        ("kısa videolar hariç", [LengthAsk("short", "skip")]),
        ("uzun çekimleri atla", [LengthAsk("long", "skip")]),
        ("sadece uzun videoları kullan", [LengthAsk("long", "only")]),
        ("kısa klipleri de kullan", [LengthAsk("short", "also")]),
        ("skip the short clips", [LengthAsk("short", "skip")]),
        ("pick six moments from the long takes", [LengthAsk("long", "only")]),
        ("only the long takes", [LengthAsk("long", "only")]),
        ("leave the short ones out", [LengthAsk("short", "skip")]),
        # "any short clip" is a group, whatever the number of the noun.
        ("skip any short clip", [LengthAsk("short", "skip")]),
    ],
)
def test_group_phrases_still_scope(text: str, asks: list[LengthAsk]) -> None:
    assert length_asks(text) == asks


# -- 5: the single longest / shortest clip is its own group -----------------------------


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("en uzun klibi atla", [LengthAsk("longest", "skip")]),
        ("en kısa klibi kullanma", [LengthAsk("shortest", "skip")]),
        ("skip the longest clip", [LengthAsk("longest", "skip")]),
        ("drop the shortest one", [LengthAsk("shortest", "skip")]),
    ],
)
def test_superlatives_stay_scoped(text: str, asks: list[LengthAsk]) -> None:
    assert length_asks(text) == asks


# -- 2 + 7: on the real manifest ---------------------------------------------------------


def test_the_incident_keeps_all_twelve_clips() -> None:
    manifest = _manifest()
    # What the model picked in prod (it kept the eleven it understood), and "everything".
    for strategy in (
        _montage(selected_media_ids=SHORT_IDS),
        _montage(selected_media_ids=ALL_IDS),
        _montage(media_scope="all"),
    ):
        scoped = apply_clip_scope(manifest, strategy, creator_texts=[INCIDENT_PROMPT])
        assert scoped is strategy


def test_the_plural_phrase_on_the_same_manifest_still_narrows() -> None:
    # 7: control. The harness can narrow; only the singular wording changed the outcome.
    manifest = _manifest()
    strategy = _montage(selected_media_ids=ALL_IDS)
    scoped = apply_clip_scope(
        manifest, strategy, creator_texts=["Caro'nun bıraktığı kısa klipleri kullanma"]
    )
    assert scoped.selected_media_ids == [LONG_ID]


# -- 6: a length scope never removes a clip the strategy itself places ----------------------


def test_a_length_scope_keeps_the_clip_the_strategy_plays_audio_from() -> None:
    manifest = _manifest()
    spoken = SHORT_IDS[3]
    strategy = _montage(
        selected_media_ids=ALL_IDS,
        montage_audio={
            "source_media_ids": [spoken],
            "preview_source_beds": False,
            "preserve_source_audio": True,
        },
    )
    scoped = apply_clip_scope(manifest, strategy, creator_texts=["kısa klipleri kullanma"])
    assert set(scoped.selected_media_ids) == {spoken, LONG_ID}
