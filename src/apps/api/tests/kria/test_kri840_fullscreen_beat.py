"""KRI-840: "show the photo FULL SCREEN when I say <word>" on a phone Talking edit.

Written BEFORE the fix (failure modes: ``kri840-failure-modes.md``). Two real test-kit
prompts, replayed offline from the creator's words to the compiled device recipe and the
brief receipt, with real word timings (no network, no LLM, no database):

* Café T2 (Turkish): "'traditional Colombian dessert' dediğinde oblea fotoğrafı tam ekran
  gelsin" + the Cotopaxi photo as a small corner card at "Ecuador". Word timings are the
  whisper.cpp ones (``kria-ecuador-test/_src/work/words_en.srt``, source seconds) for the
  T2_01 footage, which is source 11.8-36.4 s (24.6 s): every time below is source - 11.8.
* Don Julio T2 (English): 'When he says "Argentinian", show the grill photo full screen for
  a moment. When he says chimichurri, show the chimichurri photo as a small card in the
  corner.' T2_01 is the 12.9 s sauces clip. The kit README measured "Argentinian" at
  about 5.0-5.6 s and "chimichurri" at about 8.0-8.8 s (+/- 0.3 s); the words around them
  are laid out on the segment times of ``whisper_en.json`` (sauces line 4.2-6.1 s).

Field shape under test: ``ReactionBeat.display_mode`` is ``"pip"`` (the default, omitted from
every dump) or ``"fullscreen"``. It is the name open PR #1756 already adds for Voiceover, so
the two lanes share one field. A grounded fullscreen card is a ``SubtitledOverlayCard`` with
``display_mode="fullscreen"`` (KRI-297's cover-fill track) and its receipt entry carries
``"display_mode": "fullscreen"`` (absent = corner card). The ask side is a brief fact
(``facts["display_mode"]`` = ``"fullscreen"`` | ``"pip"``) because KRI-667 forbids new
word-recognising regexes in ``brief_checks``.

Reading the failures on main (before the fix):

* ``GUARD`` tests are green on main on purpose: they pin what must NOT change (legacy
  strategies dump byte-identically, a corner card keeps its corner geometry, speaker audio
  and captions stay unbroken, a neutral ask is not given a display opinion).
* Every other test is red on main for one named reason, written in its assertion message.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from pydantic import ValidationError

import app.services.phone_overlay_grounding as pg
import app.services.phone_reaction_grounding as rg
import app.services.phone_visuals as phone_visuals_mod
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ResolvedCreatorManifest,
    canonical_context_hash,
)
from app.agents._schemas.reaction_beats import ReactionBeat
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    BeatFact,
    check_requirement,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    SubtitledLaneError,
    SubtitledOverlayCard,
    remap_lanes_for_cut,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.transcribe import Word
from app.services.device_render import device_status
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneVisualBinding
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_subtitled_plan import _binding, _cut_plan
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _enable_beats,
    _make_fake_bind,
    _setup_subtitled,
)

# --------------------------------------------------------------------------- real timings

# Café T2_01: whisper.cpp words (text, source start, source end); the footage starts at 11.8 s.
_CAFE_OFFSET_S = 11.8
_CAFE_DURATION_S = 24.6
_CAFE_SOURCE_WORDS = [
    ("from.", 12.80, 14.14),
    ("Hi", 14.14, 14.16),
    ("I'm", 14.16, 14.19),
    ("Karolina", 14.19, 14.35),
    ("from", 14.35, 14.35),
    ("Ecuador", 14.35, 14.45),
    ("and", 14.45, 14.60),
    ("this", 14.60, 14.98),
    ("is", 14.98, 15.43),
    ("the", 15.43, 15.59),
    ("Oblaya", 15.59, 16.20),
    ("de", 16.20, 16.31),
    ("la", 16.31, 16.52),
    ("Casa.", 16.52, 17.34),
    ("It's", 17.34, 17.41),
    ("a", 17.41, 17.44),
    ("traditional", 17.44, 18.82),
    ("Colombian", 18.82, 20.35),
    ("dessert.", 20.35, 21.18),
    ("It's", 21.18, 21.38),
    ("waffle", 21.38, 21.72),
    ("cookie", 21.72, 24.07),
    ("with", 24.07, 24.32),
    ("bread,", 24.32, 25.20),
    ("no", 25.20, 25.50),
    ("with", 25.50, 25.74),
    ("Blackberry", 25.80, 27.26),
    ("jam,", 27.26, 27.87),
    ("cheese,", 27.87, 28.39),
    ("dulce", 28.39, 28.71),
    ("de", 28.71, 28.84),
    ("leche,", 28.84, 29.43),
    ("leche", 29.43, 30.06),
    ("condensada,", 30.06, 31.91),
    ("and", 31.91, 32.78),
    ("whipped", 32.78, 33.91),
    ("cream,", 33.91, 35.02),
    ("that's", 35.02, 35.83),
    ("it.", 35.83, 36.22),
]
CAFE_WORDS = [
    {
        "text": text,
        "start_s": round(start - _CAFE_OFFSET_S, 3),
        "end_s": round(end - _CAFE_OFFSET_S, 3),
        "confidence": 1.0,
    }
    for text, start, end in _CAFE_SOURCE_WORDS
]
# "traditional Colombian dessert" starts at 17.44 - 11.8 = 5.64 s; "Ecuador" at 2.55 s.
CAFE_TRADITIONAL_S = 5.64
CAFE_ECUADOR_S = 2.55
CAFE_CUES = [
    {
        "text": "Hi, I'm Karolina from Ecuador and this is the Oblea de la Casa.",
        "start_s": 2.34,
        "end_s": 5.54,
    },
    {"text": "It's a traditional Colombian dessert.", "start_s": 5.54, "end_s": 9.38},
    {
        "text": "It's waffle cookie with bread, blackberry jam, cheese, dulce de leche, "
        "condensed milk and whipped cream, that's it.",
        "start_s": 9.38,
        "end_s": 24.42,
    },
]

# Don Julio T2_01 (12.9 s). README-measured around the two beats, +/- 0.3 s: "Argentinian"
# 5.0-5.6, "chimichurri" 8.0-8.8; the sauces line runs 4.2-6.1 s, "Thank you" 10.7-11.3.
_JULIO_DURATION_S = 12.9
JULIO_WORDS = [
    {"text": text, "start_s": start, "end_s": end, "confidence": 1.0}
    for text, start, end in (
        ("What's", 0.0, 0.5),
        ("your", 0.5, 0.8),
        ("name?", 0.8, 2.0),
        ("Lucas.", 2.0, 2.8),
        ("We", 4.20, 4.35),
        ("have", 4.35, 4.55),
        ("here", 4.55, 4.70),
        ("traditional", 4.70, 5.00),
        ("Argentinian", 5.00, 5.60),
        ("salsa", 5.60, 6.10),
        ("criollo", 6.80, 7.30),
        ("salsa,", 7.30, 7.80),
        ("chimichurri,", 8.00, 8.80),
        ("and", 8.90, 9.10),
        ("sarayla.", 9.10, 9.76),
        ("Thank", 10.70, 11.00),
        ("you.", 11.00, 11.30),
    )
]
JULIO_ARGENTINIAN_S = 5.0
JULIO_CHIMICHURRI_S = 8.0

# --------------------------------------------------------------------------- pool + manifest

_OBLEA = "11111111-aaaa-4aaa-8aaa-000000000001"
_COTOPAXI = "11111111-aaaa-4aaa-8aaa-000000000002"
_GRILL = "11111111-aaaa-4aaa-8aaa-000000000003"
_CHIMI = "11111111-aaaa-4aaa-8aaa-000000000004"
_OTHER_A = "11111111-aaaa-4aaa-8aaa-000000000005"
_OTHER_B = "11111111-aaaa-4aaa-8aaa-000000000006"
_VIDEO = "11111111-aaaa-4aaa-8aaa-000000000007"
_LABELS = {
    _OBLEA: "Oblea",
    _COTOPAXI: "Cotopaxi",
    _GRILL: "Don Julio grill",
    _CHIMI: "Chimichurri",
    _OTHER_A: "Other A",
    _OTHER_B: "Other B",
    _VIDEO: "Brewing video",
}


def _asset(media_id: str, *, kind: str = "image", duration_s: float | None = None) -> dict:
    ext = "mp4" if kind == "video" else "jpg"
    return {
        "id": media_id,
        "gcs_path": f"users/u1/plan/item1/pool/{media_id}.{ext}",
        "gcs_generation": "9",
        "kind": kind,
        "source_filename": f"{_LABELS[media_id]}.{ext}",
        "duration_s": duration_s,
        "aspect": 1.5,
        "user_context": "",
        "analysis": {},
    }


def _visual(media_id: str) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/u1/plan/item1/pool/{media_id}.jpg",
        generation="9",
        sha256="b" * 64,
        byte_count=2048,
    )


def _beat(beat_id: str, trigger: str, media_id: str, **extra: Any) -> dict:
    return {
        "beat_id": beat_id,
        "trigger": trigger,
        "visual_id": f"asset-{media_id}",
        "visual_role": "photo",
        **extra,
    }


def _ground(monkeypatch, beats, words, duration_s, assets, *, video_supported=False):  # noqa: ANN001
    """The real grounding with its two database seams and the face sampler faked.

    Passes ``allow_fullscreen=True`` when the grounding takes that gate (open PR #1756 adds it,
    default off) so these tests mean the same thing before and after it lands.
    """
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(pg, "sample_face_regions", lambda *a, **k: ([], {}))
    extra: dict[str, Any] = {}
    if "allow_fullscreen" in inspect.signature(rg.ground_phone_reaction_beats).parameters:
        extra["allow_fullscreen"] = True
    return rg.ground_phone_reaction_beats(
        lambda: None,
        job_id="job-kri840",
        beats=beats,
        closing=None,
        words=words,
        duration_s=duration_s,
        clip_path="/tmp/clip.mp4",
        video_supported=video_supported,
        **extra,
    )


def _cards_by_beat(grounded) -> dict[str, list[SubtitledOverlayCard]]:  # noqa: ANN001
    out: dict[str, list[SubtitledOverlayCard]] = {}
    for card in grounded.cards:
        beat_id = card.id.removeprefix("beat-").rsplit("-", 1)[0]
        out.setdefault(beat_id, []).append(card)
    return out


# The two kit scenarios as the Main Creator would author them.
def _cafe_beats(**oblea_extra: Any) -> list[dict]:
    return [
        _beat(
            "oblea",
            "traditional Colombian dessert",
            _OBLEA,
            display_mode="fullscreen",
            **oblea_extra,
        ),
        _beat("cotopaxi", "Ecuador", _COTOPAXI),
    ]


def _julio_beats(*, hold_s: float | None = 1.2) -> list[dict]:
    grill = _beat("grill", "Argentinian", _GRILL, display_mode="fullscreen")
    if hold_s is not None:
        grill["hold_s"] = hold_s  # "for a moment"
    return [grill, _beat("chimichurri", "chimichurri", _CHIMI)]


_CAFE_ASSETS = [_asset(_OBLEA), _asset(_COTOPAXI)]
_JULIO_ASSETS = [_asset(_GRILL), _asset(_CHIMI)]

# (scenario id, beats, words, duration, assets, fullscreen beat id, word start, corner beat id,
#  corner word start)
_SCENARIOS = {
    "cafe": (
        _cafe_beats(),
        CAFE_WORDS,
        _CAFE_DURATION_S,
        _CAFE_ASSETS,
        "oblea",
        CAFE_TRADITIONAL_S,
        "cotopaxi",
        CAFE_ECUADOR_S,
    ),
    "julio": (
        _julio_beats(),
        JULIO_WORDS,
        _JULIO_DURATION_S,
        _JULIO_ASSETS,
        "grill",
        JULIO_ARGENTINIAN_S,
        "chimichurri",
        JULIO_CHIMICHURRI_S,
    ),
}


def _fullscreen_card(grounded, beat_id: str) -> SubtitledOverlayCard:  # noqa: ANN001
    cards = _cards_by_beat(grounded).get(beat_id)
    assert cards, f"beat {beat_id!r} placed no card at all: {grounded.receipt}"
    return cards[0]


# ======================================================================== A. the beat field


def _validated(raw: dict) -> ReactionBeat:
    try:
        return ReactionBeat.model_validate(raw)
    except ValidationError as exc:  # red on main: the field does not exist
        pytest.fail(
            "ReactionBeat has no 'display_mode' field yet "
            f"({exc.errors()[0]['type']}: {exc.errors()[0]['loc']})"
        )


def test_a_reaction_beat_can_ask_for_the_full_screen() -> None:
    beat = _validated(
        _beat("oblea", "traditional Colombian dessert", _OBLEA, display_mode="fullscreen")
    )
    assert beat.display_mode == "fullscreen"
    assert _validated(_beat("cotopaxi", "Ecuador", _COTOPAXI)).display_mode == "pip"


def test_a_fullscreen_beat_survives_the_strategy_round_trip() -> None:
    try:
        strategy = CreativeStrategy.model_validate(
            {
                "edit_format": "subtitled",
                "audio_strategy": "original_audio",
                "reaction_beats": _cafe_beats(),
            }
        )
    except ValidationError as exc:
        pytest.fail(f"a strategy with a display_mode beat does not validate: {exc.errors()[0]}")
    dumped = strategy.model_dump(mode="json", exclude_none=True)["reaction_beats"]
    assert [b.get("display_mode") for b in dumped] == ["fullscreen", None], dumped
    again = CreativeStrategy.model_validate(strategy.model_dump(mode="json", exclude_none=True))
    assert again.reaction_beats[0].display_mode == "fullscreen"


# GUARD: a stored strategy with no display field must dump and hash exactly as before.
_LEGACY_BEAT = {
    "beat_id": "x",
    "trigger": "Argentinian",
    "visual_id": "asset-1",
    "visual_role": "photo",
    "hold_s": 1.2,
}
_LEGACY_BEATS_JSON = (
    '[{"beat_id": "x", "hold_s": 1.2, "occurrence": "first", "trigger": "Argentinian", '
    '"visual_id": "asset-1", "visual_role": "photo"}]'
)
_LEGACY_STRATEGY_HASH = "ae5e6605e4fb350a1c0f4d42a47b5d67d09aef588dba80047f9d0d1700569cc5"
_LEGACY_BEAT_PLAIN_DUMP = (
    '{"after": null, "beat_id": "x", "hold_s": 1.2, "occurrence": "first", "sound": null, '
    '"trigger": "Argentinian", "visual_id": "asset-1", "visual_role": "photo"}'
)


def test_guard_a_legacy_strategy_dumps_and_hashes_byte_identically() -> None:
    strategy = CreativeStrategy.model_validate(
        {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "reaction_beats": [_LEGACY_BEAT],
        }
    )
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert json.dumps(dumped["reaction_beats"], sort_keys=True) == _LEGACY_BEATS_JSON
    assert canonical_context_hash(dumped) == _LEGACY_STRATEGY_HASH, (
        "the strategy hash moved: a beat without display_mode must not grow a key"
    )
    # Dumps that keep None values (comparison rows, approval parity) must not grow one either.
    plain = ReactionBeat.model_validate(_LEGACY_BEAT).model_dump(mode="json")
    assert json.dumps(plain, sort_keys=True) == _LEGACY_BEAT_PLAIN_DUMP


# ================================================================ B. grounding, real timings


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_the_fullscreen_beat_becomes_a_fullscreen_card(monkeypatch, scenario) -> None:  # noqa: ANN001
    beats, words, duration_s, assets, fs_id, _at, _corner_id, _corner_at = _SCENARIOS[scenario]
    grounded = _ground(monkeypatch, beats, words, duration_s, assets)
    card = _fullscreen_card(grounded, fs_id)
    assert card.display_mode == "fullscreen", (
        f"{fs_id}: drawn as a {card.scale:.2f}-wide corner card at "
        f"({card.x_frac:.2f}, {card.y_frac:.2f}); display_mode is ignored"
    )


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_guard_the_card_starts_on_the_spoken_word(monkeypatch, scenario) -> None:  # noqa: ANN001
    beats, words, duration_s, assets, fs_id, at_s, _corner_id, _corner_at = _SCENARIOS[scenario]
    card = _fullscreen_card(_ground(monkeypatch, beats, words, duration_s, assets), fs_id)
    assert at_s - 0.1 <= card.start_s <= at_s + 0.5, (card.start_s, at_s)


def test_julio_for_a_moment_is_held_no_longer_than_a_moment(monkeypatch) -> None:  # noqa: ANN001
    # The model reads "for a moment" as hold_s 1.2.
    grounded = _ground(
        monkeypatch, _julio_beats(hold_s=1.2), JULIO_WORDS, _JULIO_DURATION_S, _JULIO_ASSETS
    )
    card = _fullscreen_card(grounded, "grill")
    # hold_s already works for a corner card, so these two pass on main; the mode is the red part.
    assert card.end_s - card.start_s == pytest.approx(1.2, abs=0.05)
    assert card.end_s - card.start_s <= 1.5
    assert card.display_mode == "fullscreen", "not a full-screen card (display_mode ignored)"


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_a_fullscreen_beat_without_hold_s_is_a_moment_not_the_photo_default(
    monkeypatch,  # noqa: ANN001
    scenario,  # noqa: ANN001
) -> None:
    beats, words, duration_s, assets, fs_id, _at, _corner_id, _corner_at = _SCENARIOS[scenario]
    beats = [dict(b) for b in beats]
    for beat in beats:
        beat.pop("hold_s", None)
    grounded = _ground(monkeypatch, beats, words, duration_s, assets)
    card = _fullscreen_card(grounded, fs_id)
    held = card.end_s - card.start_s
    assert 0.8 <= held <= 1.5, (
        f"{fs_id} is held {held:.1f} s; the corner-photo default is {rg._PHOTO_HOLD_S_DEFAULT} s "
        "and a full-screen cutaway is a moment (recommended default 1.5 s)"
    )


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_guard_the_corner_beat_stays_a_corner_card_at_its_word(
    monkeypatch,  # noqa: ANN001
    scenario,  # noqa: ANN001
) -> None:
    beats, words, duration_s, assets, _fs_id, _at, corner_id, corner_at = _SCENARIOS[scenario]
    grounded = _ground(monkeypatch, beats, words, duration_s, assets)
    card = _cards_by_beat(grounded)[corner_id][0]
    assert card.display_mode == "pip"
    assert corner_at - 0.1 <= card.start_s <= corner_at + 0.5
    assert card.scale < 0.5 and card.y_frac <= 0.62, "a small card above the caption band"


def test_a_corner_card_never_shortens_the_fullscreen_photo(monkeypatch) -> None:  # noqa: ANN001
    # "Colombian" (7.02 s) starts a corner card while the full-screen hold from "traditional"
    # (5.64 s, 2 s) is still on: today both share one slot and the earlier one is cut short.
    beats = [
        _beat("oblea", "traditional", _OBLEA, display_mode="fullscreen", hold_s=2.0),
        _beat("cotopaxi", "Colombian", _COTOPAXI),
    ]
    grounded = _ground(monkeypatch, beats, CAFE_WORDS, _CAFE_DURATION_S, _CAFE_ASSETS)
    card = _fullscreen_card(grounded, "oblea")
    assert card.end_s - card.start_s == pytest.approx(2.0, abs=0.02), (
        f"the full-screen photo was cut to {card.end_s - card.start_s:.2f} s by a later card "
        "(they share the 'photo' slot in _truncate_same_slot_overlaps)"
    )


def test_a_fullscreen_card_needs_no_safe_spot_for_a_corner(monkeypatch) -> None:  # noqa: ANN001
    # Face-aware corner arbitration finds no room anywhere; a full-screen cutaway has no
    # geometry to arbitrate and must still be placed.
    monkeypatch.setattr(
        rg,
        "resolve_phone_card_geometry",
        lambda overlays, **kw: ({}, {o["id"]: "no_safe_spot" for o in overlays}, "ok"),
    )
    grounded = _ground(monkeypatch, _julio_beats(), JULIO_WORDS, _JULIO_DURATION_S, _JULIO_ASSETS)
    assert "grill" in _cards_by_beat(grounded), (
        f"the full-screen photo was reported unplaced: {grounded.receipt['unplaced']}"
    )


def test_a_fullscreen_video_beat_plays_full_frame_from_its_start(monkeypatch) -> None:  # noqa: ANN001
    beats = [_beat("brew", "traditional Colombian dessert", _VIDEO, display_mode="fullscreen")]
    assets = [_asset(_VIDEO, kind="video", duration_s=1.0)]
    grounded = _ground(
        monkeypatch, beats, CAFE_WORDS, _CAFE_DURATION_S, assets, video_supported=True
    )
    card = _fullscreen_card(grounded, "brew")
    assert (card.kind, card.display_mode) == ("video", "fullscreen")
    assert card.source_start_s == 0.0
    assert card.end_s - card.start_s <= 1.0 + 1e-6, "never longer than the footage"


def test_every_occurrence_of_a_fullscreen_beat_never_overlaps_itself(monkeypatch) -> None:  # noqa: ANN001
    # "leche" is said twice 0.6 s apart. Two full-screen windows may never overlap: the
    # compiler refuses the WHOLE overlays lane for that (SubtitledLaneError -> drop_lane).
    beats = [
        _beat("leche", "leche", _OBLEA, display_mode="fullscreen", occurrence="every", hold_s=1.5)
    ]
    grounded = _ground(monkeypatch, beats, CAFE_WORDS, _CAFE_DURATION_S, _CAFE_ASSETS)
    cards = sorted(_cards_by_beat(grounded).get("leche", []), key=lambda c: c.start_s)
    assert len(cards) == 2 and all(c.display_mode == "fullscreen" for c in cards), (
        f"expected two full-screen cards, got {[(c.display_mode, c.start_s) for c in cards]}"
    )
    assert cards[0].end_s <= cards[1].start_s + 1e-9
    lanes = PhoneSubtitledLanes(overlays=cards)
    compile_phone_subtitled_plan(
        (_binding(duration_s=_CAFE_DURATION_S),),
        caption_cues=CAFE_CUES,
        visuals=(_visual(_OBLEA),),
        lanes=lanes,
    )


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_the_beat_receipt_says_which_photo_went_full_screen(monkeypatch, scenario) -> None:  # noqa: ANN001
    beats, words, duration_s, assets, fs_id, _at, corner_id, _corner_at = _SCENARIOS[scenario]
    receipt = _ground(monkeypatch, beats, words, duration_s, assets).receipt
    placed = {entry["beat_id"]: entry for entry in receipt["placed"]}
    assert placed[fs_id].get("display_mode") == "fullscreen", placed[fs_id]
    assert placed[corner_id].get("display_mode", "pip") == "pip", placed[corner_id]


# ================================================= C. the compiled recipe (what the phone draws)


def _compile(grounded, *, duration_s, cues, cut_plan=None):  # noqa: ANN001
    lanes = PhoneSubtitledLanes(overlays=grounded.cards)
    if cut_plan is not None:
        lanes, _dropped = remap_lanes_for_cut(lanes, cut_plan)
    return compile_phone_subtitled_plan(
        (_binding(duration_s=duration_s),),
        caption_cues=cues,
        visuals=tuple(_visual(card.media_id) for card in grounded.cards),
        lanes=lanes,
        cut_plan=cut_plan,
    )


def _covered(layers, start: float, end: float) -> bool:  # noqa: ANN001
    cursor = start
    for layer in sorted(layers, key=lambda x: x.start):
        if layer.start <= cursor + 1e-6:
            cursor = max(cursor, layer.end)
    return cursor >= end - 1e-6


def test_cafe_t2_oblea_fills_the_screen_while_cotopaxi_stays_a_corner_card(monkeypatch) -> None:  # noqa: ANN001
    grounded = _ground(monkeypatch, _cafe_beats(), CAFE_WORDS, _CAFE_DURATION_S, _CAFE_ASSETS)
    recipe = _compile(grounded, duration_s=_CAFE_DURATION_S, cues=CAFE_CUES)
    ids = [track.id for track in recipe.tracks]
    tracks = {track.id: track for track in recipe.tracks}

    assert "subtitled-fullscreen" in tracks, f"no full-screen track in {ids}"
    (clip,) = tracks["subtitled-fullscreen"].clips
    assert clip.visual_placement is not None and clip.visual_placement.width_fraction is None, (
        "a full-screen clip cover-fills: no width fraction"
    )
    assert CAFE_TRADITIONAL_S - 0.1 <= clip.timeline_start <= CAFE_TRADITIONAL_S + 0.5
    assert 0.8 <= clip.source_duration <= 1.5, "a moment, not the 3 s photo default"
    assert clip.volume == 0, "a cutaway is silent"
    # Cotopaxi is the one picture-in-picture card, drawn ABOVE the full-screen track.
    (corner,) = tracks["subtitled-overlays"].clips
    assert corner.visual_placement.width_fraction < 0.5
    assert ids.index("subtitled-fullscreen") < ids.index("subtitled-overlays")

    monkeypatch.setattr(settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_voice_and_captions_run_unbroken_under_the_fullscreen_photo(
    monkeypatch,  # noqa: ANN001
    scenario,  # noqa: ANN001
) -> None:
    beats, words, duration_s, assets, fs_id, _at, _corner_id, _corner_at = _SCENARIOS[scenario]
    cues = (
        CAFE_CUES
        if scenario == "cafe"
        else [
            {"text": "Here we have traditional Argentinian salsa", "start_s": 4.2, "end_s": 6.1},
            {"text": "criollo salsa, chimichurri and the third one", "start_s": 6.8, "end_s": 9.76},
            {"text": "Thank you", "start_s": 10.7, "end_s": 11.3},
        ]
    )
    grounded = _ground(monkeypatch, beats, words, duration_s, assets)
    window = _fullscreen_card(grounded, fs_id)
    recipe = _compile(grounded, duration_s=duration_s, cues=cues)

    assert any(t.id == "subtitled-fullscreen" for t in recipe.tracks), (
        "the full-screen photo never reached the recipe, so there is nothing to keep unbroken"
    )
    speaker = next(t for t in recipe.tracks if t.id == "subtitled")
    (clip,) = speaker.clips
    assert (clip.timeline_start, clip.source_start, clip.source_duration) == (
        0.0,
        0.0,
        pytest.approx(duration_s),
    )
    assert recipe.audio.original_volume == 1.0
    assert all(c.volume == 0 for t in recipe.tracks if t.id != "subtitled" for c in t.clips)
    caption_layers = [layer for layer in recipe.text_layers if layer.id.startswith("caption")]
    assert _covered(caption_layers, window.start_s, window.end_s), (
        "a caption gap inside the full-screen window"
    )


def test_julio_t2_opening_cut_keeps_the_fullscreen_photo_on_its_word(monkeypatch) -> None:  # noqa: ANN001
    # "open straight on 'Here we have ...'": the first 4.2 s are cut, so "Argentinian"
    # (5.0 s in the take) lands 0.8 s into the edit.
    grounded = _ground(monkeypatch, _julio_beats(), JULIO_WORDS, _JULIO_DURATION_S, _JULIO_ASSETS)
    cut = _cut_plan([(0.0, 4.2)], _JULIO_DURATION_S)
    cues = [
        {"text": "Here we have traditional Argentinian salsa", "start_s": 0.0, "end_s": 1.9},
        {"text": "criollo salsa, chimichurri", "start_s": 2.6, "end_s": 4.6},
    ]
    recipe = _compile(grounded, duration_s=_JULIO_DURATION_S, cues=cues, cut_plan=cut)
    tracks = {t.id: t for t in recipe.tracks}
    assert "subtitled-fullscreen" in tracks, f"no full-screen track in {list(tracks)}"
    (clip,) = tracks["subtitled-fullscreen"].clips
    assert 0.8 - 0.1 <= clip.timeline_start <= 0.8 + 0.5
    assert clip.source_duration <= 1.5
    speaker = tracks["subtitled"].clips
    assert speaker[0].source_start == pytest.approx(4.2), "the speaker still starts at the cut"


def test_a_fullscreen_beat_and_the_fullscreen_images_sequence_never_collide(monkeypatch) -> None:  # noqa: ANN001
    """``overlay_display="fullscreen_images"`` plus a beat: the sequence spreads the OTHER images
    evenly; if one lands on the beat's window the compiler refuses the whole overlays lane
    ("full-screen overlay windows must not overlap")."""
    beat_card = SubtitledOverlayCard(  # what the fixed grounding hands the worker
        id="beat-oblea-1",
        media_id=_OBLEA,
        gcs_path=_asset(_OBLEA)["gcs_path"],
        generation="9",
        start_s=CAFE_TRADITIONAL_S,
        end_s=CAFE_TRADITIONAL_S + 1.5,
        display_mode="fullscreen",
    )
    beat_window = (beat_card.start_s, beat_card.end_s)
    pool = [_asset(_OBLEA), _asset(_OTHER_A), _asset(_OTHER_B)]
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: pool)
    monkeypatch.setattr(pg, "sample_face_regions", lambda *a, **k: ([], {}))
    sequence = pg.ground_phone_subtitled_overlays(
        lambda: None,
        job_id="job-kri840",
        words=CAFE_WORDS,
        duration_s=_CAFE_DURATION_S,
        clip_path="/tmp/clip.mp4",
        occupied=[beat_window],
        used_media_ids=frozenset({_OBLEA}),
        layout="fullscreen_images",
    )
    clashes = [
        (c.id, c.start_s, c.end_s)
        for c in sequence.cards
        if c.display_mode == "fullscreen"
        and c.start_s < beat_window[1]
        and beat_window[0] < c.end_s
    ]
    assert not clashes, f"sequence cards sit on the beat's full-screen window: {clashes}"
    lanes = PhoneSubtitledLanes(overlays=[beat_card, *sequence.cards])
    try:
        compile_phone_subtitled_plan(
            (_binding(duration_s=_CAFE_DURATION_S),),
            caption_cues=CAFE_CUES,
            visuals=tuple(_visual(m) for m in (_OBLEA, _OTHER_A, _OTHER_B)),
            lanes=lanes,
        )
    except SubtitledLaneError as exc:  # pragma: no cover - only reached when the clash survives
        pytest.fail(f"the whole overlays lane would be dropped: {exc}")


# ====================================================== D. the receipt (brief_checks, KRI-667)


def _ask(text: str, *, trigger: str, display_mode: str | None) -> BriefRequirement:
    facts: dict[str, Any] = {"triggers": [trigger]}
    if display_mode is not None:
        facts["display_mode"] = display_mode
    return BriefRequirement(id="r1", kind="style", scope="global", description=text, facts=facts)


_FS_ASK_EN = _ask(
    'When he says "Argentinian", show the grill photo full screen for a moment.',
    trigger="Argentinian",
    display_mode="fullscreen",
)
_FS_ASK_TR = _ask(
    "'traditional Colombian dessert' dediğinde oblea fotoğrafı tam ekran gelsin",
    trigger="traditional Colombian dessert",
    display_mode="fullscreen",
)
_CORNER_ASK_EN = _ask(
    "When he says chimichurri, show the chimichurri photo as a small card in the corner.",
    trigger="chimichurri",
    display_mode="pip",
)
_NEUTRAL_ASK_EN = _ask(
    'When he says "Argentinian", show the grill photo.',
    trigger="Argentinian",
    display_mode=None,
)


def _receipt_variant(trigger: str, label: str, *, display_mode: str | None) -> dict:
    entry: dict[str, Any] = {
        "beat_id": "b1",
        "trigger": trigger,
        "at_s": 5.0,
        "end_s": 6.2,
        "visual_label": label,
    }
    if display_mode is not None:
        entry["display_mode"] = display_mode
    return {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "phone_beat_receipt": {
            "version": 1,
            "matcher": "phrase",
            "face_sampling": "ok",
            "placed": [entry],
            "unplaced": [],
            "closing": {"status": "none", "badge": "none"},
        },
    }


def test_a_corner_card_is_never_reported_as_full_screen() -> None:
    facts = plan_facts_from_phone_variant(
        _receipt_variant("Argentinian", "Don Julio grill", display_mode=None)
    )
    receipt = check_requirement(_FS_ASK_EN, facts)
    assert receipt.status == "partial", (receipt.status, receipt.reason)
    assert "corner" in (receipt.reason or "").lower(), receipt.reason


def test_a_corner_card_is_never_reported_as_full_screen_in_turkish() -> None:
    facts = plan_facts_from_phone_variant(
        _receipt_variant("traditional Colombian dessert", "Oblea", display_mode=None)
    )
    with reply_language_for("tr"):
        receipt = check_requirement(_FS_ASK_TR, facts)
    assert receipt.status == "partial", (receipt.status, receipt.reason)
    assert "köşe" in (receipt.reason or "").lower(), receipt.reason


def test_a_fullscreen_card_is_never_reported_as_the_corner_card_that_was_asked() -> None:
    facts = plan_facts_from_phone_variant(
        _receipt_variant("chimichurri", "Chimichurri", display_mode="fullscreen")
    )
    receipt = check_requirement(_CORNER_ASK_EN, facts)
    assert receipt.status == "partial", (receipt.status, receipt.reason)
    assert "full" in (receipt.reason or "").lower() and "screen" in (receipt.reason or "").lower()


def test_guard_the_right_display_is_met() -> None:
    fullscreen = plan_facts_from_phone_variant(
        _receipt_variant("Argentinian", "Don Julio grill", display_mode="fullscreen")
    )
    corner = plan_facts_from_phone_variant(
        _receipt_variant("chimichurri", "Chimichurri", display_mode=None)
    )
    assert check_requirement(_FS_ASK_EN, fullscreen).status == "met"
    assert check_requirement(_CORNER_ASK_EN, corner).status == "met"


@pytest.mark.parametrize("display_mode", [None, "fullscreen"])
def test_guard_an_ask_that_names_no_display_gets_no_display_verdict(display_mode) -> None:  # noqa: ANN001
    facts = plan_facts_from_phone_variant(
        _receipt_variant("Argentinian", "Don Julio grill", display_mode=display_mode)
    )
    assert check_requirement(_NEUTRAL_ASK_EN, facts).status == "met"


# ---- the draft (before the render): the strategy's own beats

_MEDIA_LABELS = {f"asset-{k}": v for k, v in _LABELS.items()}


def _manifest() -> ResolvedCreatorManifest:
    media = [{"media_id": "phone-a", "kind": "video"}] + [
        {"media_id": k, "kind": "image", "label": v} for k, v in _MEDIA_LABELS.items()
    ]
    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="00000000-0000-4000-8000-000000000840",
        edit_format="subtitled",
        render_program="guided",
        media=media,
        capabilities={
            "dispatch_render": available,
            "reaction_beats": available,
            "media_overlays": available,
            "media_overlays:fullscreen": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _draft_facts(beats: list[dict], **strategy_extra: Any):  # noqa: ANN202
    data = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "reaction_beats": beats,
        **strategy_extra,
    }
    try:
        strategy = CreativeStrategy.model_validate(data).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        pytest.fail(f"the draft strategy does not validate: {exc.errors()[0]['type']}")
    return plan_facts_from_strategy(strategy, manifest=_manifest())


def _grill(**extra: Any) -> dict:
    return _beat("grill", "Argentinian", _GRILL, **extra)


def test_the_draft_says_done_for_a_fullscreen_beat_that_was_asked_full_screen() -> None:
    facts = _draft_facts([_grill(display_mode="fullscreen", hold_s=1.2)])
    assert facts.reaction_beats is not None and [
        getattr(b, "display_mode", "pip") for b in facts.reaction_beats
    ] == ["fullscreen"], facts.reaction_beats
    assert check_requirement(_FS_ASK_EN, facts).status == "met"


def test_the_draft_never_says_done_for_a_corner_beat_that_was_asked_full_screen() -> None:
    facts = _draft_facts([_grill()])  # no display_mode: the corner card
    receipt = check_requirement(_FS_ASK_EN, facts)
    assert receipt.status == "partial", (receipt.status, receipt.reason)
    assert "corner" in (receipt.reason or "").lower(), receipt.reason


def test_the_draft_never_says_done_for_a_fullscreen_beat_that_was_asked_as_a_corner_card() -> None:
    facts = _draft_facts([_beat("chimi", "chimichurri", _CHIMI, display_mode="fullscreen")])
    receipt = check_requirement(_CORNER_ASK_EN, facts)
    assert receipt.status == "partial", (receipt.status, receipt.reason)


# ---- "full screen at a word" satisfied only by overlay_display="fullscreen_images"

_MISLEADING = (
    "can't check the pop-ins",
    "no pop-ins timed to your words",
    "no pop-in for",
)
_HONEST = ("spread", "evenly", "across the", "not timed", "not at")


def _assert_honest_not_done(receipt) -> None:  # noqa: ANN001
    assert receipt.status != "met", "full-screen photos spread over the talk are not 'at the word'"
    reason = (receipt.reason or "").lower()
    assert not any(m in reason for m in _MISLEADING), f"misleading reason: {receipt.reason!r}"
    assert any(h in reason for h in _HONEST), (
        f"the reason must say the photos are spread over the talk, not at the word: {reason!r}"
    )


def test_even_slot_fullscreen_images_are_not_reported_as_the_word_timed_ask() -> None:
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "phone_beat_receipt": None,
        "phone_overlay_receipt": {
            "version": 1,
            "matcher": "sequence",
            "layout": "mixed",
            "face_sampling": "skipped",
            "placed": [
                {
                    "media_id": _GRILL,
                    "label": "Don Julio grill",
                    "start_s": 2.5,
                    "end_s": 5.5,
                    "kind": "image",
                    "display": "fullscreen",
                    "reason": "full-screen, evenly spaced across the talk",
                }
            ],
            "unplaced": [],
            "wishlist": [],
        },
    }
    _assert_honest_not_done(check_requirement(_FS_ASK_EN, plan_facts_from_phone_variant(variant)))


def test_a_draft_with_only_fullscreen_images_is_not_the_word_timed_ask() -> None:
    try:
        strategy = CreativeStrategy.model_validate(
            {
                "edit_format": "subtitled",
                "audio_strategy": "original_audio",
                "overlay_display": "fullscreen_images",
            }
        ).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        pytest.fail(f"strategy does not validate: {exc.errors()[0]['type']}")
    facts = plan_facts_from_strategy(strategy, manifest=_manifest())
    _assert_honest_not_done(check_requirement(_FS_ASK_EN, facts))


def test_the_beat_fact_defaults_to_the_corner_card() -> None:
    # GUARD for the dataclass shape the fix extends: a BeatFact built with only the old
    # arguments still means "a corner card".
    fact = BeatFact(trigger="Argentinian", visual_id="x")
    assert getattr(fact, "display_mode", "pip") == "pip"


# ============================== E. the real worker task, from the strategy to the pinned recipe


def _run_worker(monkeypatch, *, words, duration_s, assets, beats):  # noqa: ANN001, ANN202
    """``_run_phone_subtitled_job`` for real: real beat grounding, real KRI-176 pass over an
    empty pool, real lane compile, real receipt. Only storage/DB/Whisper seams are faked."""
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch,
        duration_s=duration_s,
        transcript_words=[Word(w["text"], w["start_s"], w["end_s"], 1.0) for w in words],
    )
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: assets)
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: [])
    monkeypatch.setattr(pg, "sample_face_regions", lambda *a, **k: ([], {}))
    job.all_candidates["creator_strategy"] = {"reaction_beats": beats}

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", (job.status, job.failure_reason, job.error_detail)
    variant = job.assembly_plan["variants"][0]
    return variant, device_status(job, "subtitled").request.recipe


def test_worker_julio_t2_pins_a_recipe_with_the_grill_full_screen(monkeypatch) -> None:  # noqa: ANN001
    variant, recipe = _run_worker(
        monkeypatch,
        words=JULIO_WORDS,
        duration_s=_JULIO_DURATION_S,
        assets=_JULIO_ASSETS,
        beats=_julio_beats(),
    )
    tracks = {t.id: t for t in recipe.tracks}
    assert "subtitled-fullscreen" in tracks, (
        f"tracks {list(tracks)}; beat receipt {variant['phone_beat_receipt']['placed']}; "
        f"lane receipt {variant.get('phone_lane_receipt')}"
    )
    (grill,) = tracks["subtitled-fullscreen"].clips
    assert JULIO_ARGENTINIAN_S - 0.1 <= grill.timeline_start <= JULIO_ARGENTINIAN_S + 0.5
    assert grill.source_duration <= 1.5, "for a moment"
    (chimi,) = tracks["subtitled-overlays"].clips
    assert chimi.visual_placement.width_fraction < 0.5
    assert variant["phone_lane_receipt"]["dropped"] == [], "the whole overlays lane was dropped"
    placed = {e["beat_id"]: e for e in variant["phone_beat_receipt"]["placed"]}
    assert placed["grill"].get("display_mode") == "fullscreen"
    assert placed["chimichurri"].get("display_mode", "pip") == "pip"
    # The receipts the creator reads, from the same variant.
    facts = plan_facts_from_phone_variant(variant)
    assert check_requirement(_FS_ASK_EN, facts).status == "met"
    assert check_requirement(_CORNER_ASK_EN, facts).status == "met"


def test_worker_cafe_t2_turkish_pins_oblea_full_screen_and_cotopaxi_in_the_corner(
    monkeypatch,  # noqa: ANN001
) -> None:
    variant, recipe = _run_worker(
        monkeypatch,
        words=CAFE_WORDS,
        duration_s=_CAFE_DURATION_S,
        assets=_CAFE_ASSETS,
        beats=_cafe_beats(),
    )
    tracks = {t.id: t for t in recipe.tracks}
    assert "subtitled-fullscreen" in tracks, (
        f"tracks {list(tracks)}; beat receipt {variant['phone_beat_receipt']['placed']}"
    )
    (oblea,) = tracks["subtitled-fullscreen"].clips
    assert CAFE_TRADITIONAL_S - 0.1 <= oblea.timeline_start <= CAFE_TRADITIONAL_S + 0.5
    assert oblea.source_duration <= 1.5
    (cotopaxi,) = tracks["subtitled-overlays"].clips
    assert cotopaxi.visual_placement.width_fraction < 0.5
    facts = plan_facts_from_phone_variant(variant)
    with reply_language_for("tr"):
        assert check_requirement(_FS_ASK_TR, facts).status == "met"


def test_worker_never_lets_a_corner_card_pass_for_the_full_screen_ask(monkeypatch) -> None:  # noqa: ANN001
    # The model forgot display_mode (or the fullscreen gate is off): the photo is a corner
    # card, and the receipt for the full-screen ask must say so.
    beats = [_beat("grill", "Argentinian", _GRILL, hold_s=1.2)]
    variant, recipe = _run_worker(
        monkeypatch,
        words=JULIO_WORDS,
        duration_s=_JULIO_DURATION_S,
        assets=_JULIO_ASSETS,
        beats=beats,
    )
    assert not any(t.id == "subtitled-fullscreen" for t in recipe.tracks)
    receipt = check_requirement(_FS_ASK_EN, plan_facts_from_phone_variant(variant))
    assert receipt.status == "partial", (receipt.status, receipt.reason)
    assert "corner" in (receipt.reason or "").lower(), receipt.reason
