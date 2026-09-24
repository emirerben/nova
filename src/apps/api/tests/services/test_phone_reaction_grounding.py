"""Unit tests for `app.services.phone_reaction_grounding` (KRI-178).

`ground_phone_reaction_beats` is exercised directly (not via the phone
worker), with its two DB seams (`_load_ready_pool_assets`, `_load_sfx_entries`)
and `resolve_phone_card_geometry` monkeypatched per test -- the real phrase
matcher, overlap-truncation, and closing-media logic run for real, since they
own the actual behavior this module exists to provide.
"""

from __future__ import annotations

import json

import app.services.phone_reaction_grounding as rg
from app.services.sfx_catalog import SfxEntry


def _open_session():
    raise AssertionError("the DB seams should be monkeypatched in these tests")


def _asset(
    id_: str,
    *,
    kind: str = "image",
    generation: str | None = "7",
    filename: str = "x.jpg",
    aspect: float = 1.0,
) -> dict:
    return {
        "id": id_,
        "gcs_path": f"users/u1/plan/item1/pool/{id_}.jpg",
        "gcs_generation": generation,
        "kind": kind,
        "source_filename": filename,
        "duration_s": None,
        "aspect": aspect,
        "user_context": "",
        "analysis": {},
    }


def _word(text: str, start_s: float, end_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": end_s, "confidence": 1.0}


def _sfx(id_: str, name: str, search_terms: tuple[str, ...] = ()) -> SfxEntry:
    return SfxEntry(
        id=id_, name=name, category=None, search_terms=search_terms or (name.casefold(),)
    )


def _patch(
    monkeypatch, *, assets: list[dict] | None = None, sfx: list[SfxEntry] | None = None
) -> None:
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: assets or [])
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: sfx or [])


# --- fold_tokens --------------------------------------------------------------


def test_fold_tokens_strips_accents_and_punctuation():
    assert rg.fold_tokens("Leão,") == ["leao"]


def test_fold_tokens_splits_apostrophe_suffix_that_is_not_possessive():
    assert rg.fold_tokens("Vlahović'in") == ["vlahovic", "in"]


def test_fold_tokens_drops_possessive():
    assert rg.fold_tokens("Messi's") == ["messi"]


def test_fold_tokens_number_word_with_filler_prefix():
    assert rg.fold_tokens("Number three") == ["3"]


def test_fold_tokens_hash_number():
    assert rg.fold_tokens("#3") == ["3"]


def test_fold_tokens_no_dot_number():
    assert rg.fold_tokens("no. 3") == ["3"]


def test_fold_tokens_turkish_numara():
    assert rg.fold_tokens("numara 3") == ["3"]


def test_fold_tokens_turkish_number_word():
    assert rg.fold_tokens("üç") == ["3"]


def test_fold_tokens_turkish_dotless_i_number_word():
    assert rg.fold_tokens("altı") == ["6"]


def test_fold_tokens_turkish_dotted_i():
    assert rg.fold_tokens("İstanbul") == ["istanbul"]


def test_fold_tokens_bare_no_is_untouched():
    # "no" is a number-filler ONLY when immediately followed by a digit token.
    assert rg.fold_tokens("no") == ["no"]


def test_fold_tokens_empty():
    assert rg.fold_tokens("") == []
    assert rg.fold_tokens(None) == []


# --- the full KRI-172 scenario -------------------------------------------------

# A synthetic transcript mirroring a "guess the player" reaction video:
# a pre-existing "no" (word 0) precedes any mention of the first player, so
# the after-filter must ignore it. Words carry small 0.1s gaps so "starts
# strictly after" never lands on a same-instant boundary (Whisper words are
# rarely back-to-back with zero gap in practice either).
_WORDS = [
    _word("no", 0.0, 0.3),
    _word("number", 0.4, 0.7),
    _word("three", 0.8, 1.1),
    _word("mason", 1.2, 1.5),
    _word("greenwood", 1.6, 1.9),
    _word("no", 2.0, 2.3),
    _word("no", 2.4, 2.7),
    _word("rafael", 2.8, 3.1),
    _word("leao", 3.2, 3.5),
    _word("yes", 3.6, 3.9),
    _word("number", 4.0, 4.3),
    _word("two", 4.4, 4.7),
    _word("dusan", 4.8, 5.1),
    _word("vlahovic", 5.2, 5.5),
    _word("no", 5.6, 5.9),
    _word("leandro", 6.0, 6.3),
    _word("trossard", 6.4, 6.7),
    _word("yes", 6.8, 7.1),
    _word("number", 7.2, 7.5),
    _word("one", 7.6, 7.9),
    _word("mohamed", 8.0, 8.3),
    _word("salah", 8.4, 8.7),
    _word("the", 8.8, 9.1),
    _word("goat", 9.2, 9.5),
]

_DURATION_S = 12.0

_BEATS = [
    {
        "beat_id": "greenwood-photo",
        "trigger": "mason greenwood",
        "visual_id": "v-greenwood",
        "visual_role": "photo",
        "hold_s": 1.0,
    },
    {
        "beat_id": "greenwood-reject",
        "trigger": "no",
        "after": "mason greenwood",
        "visual_id": "v-x",
        "visual_role": "sticker",
        "sound": "buzzer",
        "hold_s": 0.6,
    },
    {
        "beat_id": "leao-photo",
        "trigger": "rafael leao",
        "visual_id": "v-leao",
        "visual_role": "photo",
        "hold_s": 1.0,
    },
    {
        "beat_id": "leao-approve",
        "trigger": "yes",
        "after": "rafael leao",
        "visual_id": "v-check",
        "visual_role": "sticker",
        "sound": "ding",
        "hold_s": 0.6,
    },
    {
        "beat_id": "badge-3",
        "trigger": "number three",
        "visual_id": "v-badge3",
        "visual_role": "sticker",
        "hold_s": 0.6,
    },
    {
        "beat_id": "vlahovic-reject",
        "trigger": "no",
        "after": "dusan vlahovic",
        "visual_id": "v-x",
        "visual_role": "sticker",
        "sound": "buzzer",
        "hold_s": 0.6,
    },
    {
        "beat_id": "badge-2",
        "trigger": "number two",
        "visual_id": "v-badge2",
        "visual_role": "sticker",
        "hold_s": 0.6,
    },
    {
        "beat_id": "badge-1",
        "trigger": "number one",
        "visual_id": "v-badge1",
        "visual_role": "sticker",
        "hold_s": 0.6,
    },
    {
        "beat_id": "salah-photo",
        "trigger": "mohamed salah",
        "visual_id": "v-salah",
        "visual_role": "photo",
    },
    {
        "beat_id": "icardi-never",
        "trigger": "Icardi",
        "visual_id": "v-badge1",
        "visual_role": "sticker",
    },
]

_CLOSING = {"visual_id": "v-salah", "badge_visual_id": "v-goat", "from_trigger": "salah"}

_POOL = [
    _asset("v-greenwood", filename="greenwood.jpg"),
    _asset("v-x", filename="x.png"),
    _asset("v-check", filename="check.png"),
    _asset("v-badge3", filename="badge3.png"),
    _asset("v-badge2", filename="badge2.png"),
    _asset("v-badge1", filename="badge1.png"),
    _asset("v-leao", filename="leao.jpg"),
    _asset("v-salah", filename="salah.jpg"),
    _asset("v-goat", filename="goat.png"),
]

_SFX = [
    _sfx("sfx-buzzer", "Wrong buzzer", ("buzzer", "wrong")),
    _sfx("sfx-ding", "Ding", ("ding",)),
]


def _run_scenario(monkeypatch):
    _patch(monkeypatch, assets=_POOL, sfx=_SFX)
    return rg.ground_phone_reaction_beats(
        _open_session,
        job_id="00000000-0000-0000-0000-000000000000",
        beats=_BEATS,
        closing=_CLOSING,
        words=_WORDS,
        duration_s=_DURATION_S,
        clip_path=None,
    )


def test_scenario_greenwood_photo_and_reject_after_pre_existing_no(monkeypatch):
    result = _run_scenario(monkeypatch)
    cards = {c.id: c for c in result.cards}

    photo = cards["beat-greenwood-photo-1"]
    assert (photo.media_id, photo.start_s, photo.end_s) == ("v-greenwood", 1.2, 2.2)
    # KRI-183: `_run_scenario` calls with clip_path=None -- face sampling
    # never ran even though there are cards to place (`face_sampling ==
    # "skipped"`), so the conservative fallback face box is protected and
    # both default slots (photo top-right, sticker top-left) shrink/move
    # into the opposite corner instead of sitting at their untouched
    # defaults.
    assert (photo.x_frac, photo.y_frac, photo.scale, photo.z) == (0.8, 0.14, 0.198, 0)

    reject = cards["beat-greenwood-reject-1"]
    # The FIRST "no" AFTER greenwood (2.0s) -- not the pre-existing "no" at 0.0s.
    assert (reject.media_id, reject.start_s, reject.end_s) == ("v-x", 2.0, 2.6)
    assert (reject.x_frac, reject.y_frac, reject.z) == (0.2, 0.14, 1)


def test_scenario_leao_photo_and_approve(monkeypatch):
    result = _run_scenario(monkeypatch)
    cards = {c.id: c for c in result.cards}

    photo = cards["beat-leao-photo-1"]
    assert (photo.media_id, photo.start_s, photo.end_s) == ("v-leao", 2.8, 3.8)

    approve = cards["beat-leao-approve-1"]
    assert (approve.media_id, approve.start_s, approve.end_s) == ("v-check", 3.6, 4.2)


def test_scenario_rank_badges(monkeypatch):
    result = _run_scenario(monkeypatch)
    cards = {c.id: c for c in result.cards}

    assert (cards["beat-badge-3-1"].media_id, cards["beat-badge-3-1"].start_s) == ("v-badge3", 0.8)
    assert (cards["beat-badge-2-1"].media_id, cards["beat-badge-2-1"].start_s) == ("v-badge2", 4.4)
    assert (cards["beat-badge-1-1"].media_id, cards["beat-badge-1-1"].start_s) == ("v-badge1", 7.6)


def test_scenario_vlahovic_reject_reuses_sticker_without_dedupe(monkeypatch):
    """The same "X" sticker (`v-x`) is legitimately used twice, at
    non-overlapping times -- `arbitrate_media_overlays`'s duplicate-asset
    guard only fires on OVERLAPPING windows for the same asset, so both
    survive (see the module-level finding in the PR description)."""

    result = _run_scenario(monkeypatch)
    cards = {c.id: c for c in result.cards}

    reject2 = cards["beat-vlahovic-reject-1"]
    assert reject2.media_id == "v-x"
    assert round(reject2.start_s, 3) == 5.6
    assert round(reject2.end_s, 3) == 6.2
    x_cards = [c for c in result.cards if c.media_id == "v-x"]
    assert len(x_cards) == 2


def test_scenario_salah_photo_merges_into_closing(monkeypatch):
    result = _run_scenario(monkeypatch)
    cards = {c.id: c for c in result.cards}

    # No separate "closing-photo" card -- the salah beat card was extended.
    assert "closing-photo" not in cards
    salah = cards["beat-salah-photo-1"]
    assert salah.media_id == "v-salah"
    assert salah.start_s == 8.0
    assert salah.end_s == _DURATION_S

    badge = cards["closing-badge"]
    assert (badge.media_id, badge.start_s, badge.end_s) == ("v-goat", 8.7, _DURATION_S)

    assert result.receipt["closing"] == {
        "status": "placed",
        "from_s": 8.7,
        "visual_label": "salah.jpg",
        "badge": "placed",
    }


def test_scenario_sound_effects_resolved_by_description(monkeypatch):
    result = _run_scenario(monkeypatch)
    by_catalog = {(s.catalog_id, round(s.at_s, 3)) for s in result.sound_effects}
    assert by_catalog == {("sfx-buzzer", 2.0), ("sfx-buzzer", 5.6), ("sfx-ding", 3.6)}


def test_scenario_icardi_never_heard(monkeypatch):
    result = _run_scenario(monkeypatch)
    assert result.receipt["unplaced"] == [
        {"beat_id": "icardi-never", "trigger": "Icardi", "reason": "never_heard"}
    ]
    assert not any(c.id.startswith("beat-icardi-never") for c in result.cards)


def test_scenario_placed_entries_cover_every_successful_beat(monkeypatch):
    result = _run_scenario(monkeypatch)
    placed_beat_ids = {p["beat_id"] for p in result.receipt["placed"]}
    assert placed_beat_ids == {
        "greenwood-photo",
        "greenwood-reject",
        "leao-photo",
        "leao-approve",
        "badge-3",
        "vlahovic-reject",
        "badge-2",
        "badge-1",
        "salah-photo",
    }


def test_scenario_receipt_has_no_gcs_or_generation(monkeypatch):
    result = _run_scenario(monkeypatch)
    dumped = json.dumps(result.receipt)
    assert "gcs_path" not in dumped
    assert "generation" not in dumped
    for asset in _POOL:
        assert asset["gcs_path"] not in dumped


def test_scenario_face_sampling_skipped_without_clip_path(monkeypatch):
    result = _run_scenario(monkeypatch)
    assert result.receipt["face_sampling"] == "skipped"
    assert result.receipt["matcher"] == "phrase"
    assert result.receipt["version"] == 1


# --- individual failure reasons -------------------------------------------------


def test_after_not_heard(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v1")])
    words = [_word("hello", 0.0, 0.3)]
    beats = [
        {
            "beat_id": "b1",
            "trigger": "hello",
            "after": "never spoken phrase",
            "visual_id": "v1",
            "visual_role": "sticker",
        }
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "after_not_heard"}
    ]


def test_beat_visual_id_accepts_manifest_asset_prefix(monkeypatch):
    """The creator manifest exposes a pool asset as `asset-<uuid>`
    (`creator_sessions.py`), while `_load_ready_pool_assets` keys rows by the
    raw id -- a strategy's `visual_id` normally arrives prefixed."""

    _patch(monkeypatch, assets=[_asset("11111111-1111-1111-1111-111111111111")])
    words = [_word("hello", 0.0, 0.3)]
    beats = [
        {
            "beat_id": "b1",
            "trigger": "hello",
            "visual_id": "asset-11111111-1111-1111-1111-111111111111",
            "visual_role": "photo",
        }
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert len(result.cards) == 1
    assert result.cards[0].media_id == "11111111-1111-1111-1111-111111111111"
    assert result.receipt["unplaced"] == []


def test_visual_not_in_pool(monkeypatch):
    _patch(monkeypatch, assets=[])
    words = [_word("hello", 0.0, 0.3)]
    beats = [
        {"beat_id": "b1", "trigger": "hello", "visual_id": "missing-asset", "visual_role": "photo"}
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "visual_not_in_pool"}
    ]


def test_visual_is_video(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v1", kind="video")])
    words = [_word("hello", 0.0, 0.3)]
    beats = [{"beat_id": "b1", "trigger": "hello", "visual_id": "v1", "visual_role": "photo"}]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "visual_is_video"}
    ]


def test_sound_not_found(monkeypatch):
    _patch(monkeypatch, assets=[], sfx=[])
    words = [_word("hello", 0.0, 0.3)]
    beats = [{"beat_id": "b1", "trigger": "hello", "sound": "a totally unmatched sound"}]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.sound_effects == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "sound_not_found"}
    ]


def test_same_slot_overlap_truncation_and_drop(monkeypatch):
    """Two sticker cards overlapping in time: the earlier is truncated at the
    later's start, and dropped outright when that leaves < 0.4s."""

    _patch(monkeypatch, assets=[_asset("vA"), _asset("vB")])
    words = [_word("alpha", 0.0, 0.1), _word("beta", 0.2, 0.3)]
    beats = [
        {
            "beat_id": "beatA",
            "trigger": "alpha",
            "visual_id": "vA",
            "visual_role": "sticker",
            "hold_s": 2.0,
        },
        {
            "beat_id": "beatB",
            "trigger": "beta",
            "visual_id": "vB",
            "visual_role": "sticker",
            "hold_s": 2.0,
        },
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    cards = {c.id: c for c in result.cards}
    assert "beat-beatA-1" not in cards
    assert result.receipt["unplaced"] == [
        {"beat_id": "beatA", "trigger": "alpha", "reason": "overlap"}
    ]
    survivor = cards["beat-beatB-1"]
    assert (survivor.start_s, survivor.end_s) == (0.2, 2.2)


def test_closing_wins_truncates_unrelated_beat_card(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-other"), _asset("v-closing")])
    words = [_word("hello", 0.0, 0.3)]
    beats = [
        {
            "beat_id": "b1",
            "trigger": "hello",
            "visual_id": "v-other",
            "visual_role": "photo",
            "hold_s": 4.0,
        }
    ]
    closing = {"visual_id": "v-closing"}  # no from_trigger -> last 3s
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=closing,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    cards = {c.id: c for c in result.cards}
    beat_card = cards["beat-b1-1"]
    assert (beat_card.start_s, beat_card.end_s) == (0.0, 2.0)  # truncated at closing start
    closing_card = cards["closing-photo"]
    assert (closing_card.media_id, closing_card.start_s, closing_card.end_s) == (
        "v-closing",
        2.0,
        5.0,
    )
    assert result.receipt["closing"]["status"] == "placed"
    assert result.receipt["closing"]["from_s"] == 2.0
    assert result.receipt["closing"]["badge"] == "none"
    assert "badge_reason" not in result.receipt["closing"]


def test_closing_visual_and_badge_accept_manifest_asset_prefix(monkeypatch):
    photo_id = "22222222-2222-2222-2222-222222222222"
    badge_id = "33333333-3333-3333-3333-333333333333"
    _patch(monkeypatch, assets=[_asset(photo_id), _asset(badge_id)])
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": f"asset-{photo_id}", "badge_visual_id": f"asset-{badge_id}"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    cards = {c.id: c for c in result.cards}
    assert cards["closing-photo"].media_id == photo_id
    assert cards["closing-badge"].media_id == badge_id
    assert result.receipt["closing"]["status"] == "placed"
    assert result.receipt["closing"]["badge"] == "placed"


def test_closing_badge_placed(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-closing"), _asset("v-badge")])
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing", "badge_visual_id": "v-badge"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    cards = {c.id: c for c in result.cards}
    assert "closing-badge" in cards
    assert cards["closing-badge"].media_id == "v-badge"
    assert result.receipt["closing"]["badge"] == "placed"
    assert "badge_reason" not in result.receipt["closing"]


def test_closing_badge_unplaced_visual_not_in_pool(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-closing")])
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing", "badge_visual_id": "missing-badge"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    assert not any(c.id == "closing-badge" for c in result.cards)
    assert result.receipt["closing"]["badge"] == "unplaced"
    assert result.receipt["closing"]["badge_reason"] == "visual_not_in_pool"


def test_closing_badge_unplaced_visual_is_video(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-closing"), _asset("v-badge", kind="video")])
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing", "badge_visual_id": "v-badge"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    assert result.receipt["closing"]["badge"] == "unplaced"
    assert result.receipt["closing"]["badge_reason"] == "visual_is_video"


def test_closing_badge_unplaced_no_safe_spot(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-closing"), _asset("v-badge")])

    def _fake_resolve(overlays, *, clip_path, job_id, footprints_by_id):  # noqa: ARG001
        resolved = {}
        reasons = {}
        for o in overlays:
            if o["id"] == "closing-badge":
                reasons[o["id"]] = "no_safe_spot"
            else:
                resolved[o["id"]] = o
        return resolved, reasons, "skipped"

    monkeypatch.setattr(rg, "resolve_phone_card_geometry", _fake_resolve)
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing", "badge_visual_id": "v-badge"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    cards = {c.id: c for c in result.cards}
    assert "closing-photo" in cards  # photo unaffected by the badge's failure
    assert "closing-badge" not in cards
    assert result.receipt["closing"]["status"] == "placed"
    assert result.receipt["closing"]["badge"] == "unplaced"
    assert result.receipt["closing"]["badge_reason"] == "no_safe_spot"


def test_closing_badge_unplaced_duplicate_maps_to_overlap(monkeypatch):
    """Geometry's own "duplicate" decision (the same asset colliding with
    another placement's overlapping time window) surfaces on the closing
    badge as "overlap" -- the fixed reason vocabulary has no "duplicate"."""

    _patch(monkeypatch, assets=[_asset("v-closing"), _asset("v-badge")])

    def _fake_resolve(overlays, *, clip_path, job_id, footprints_by_id):  # noqa: ARG001
        resolved = {}
        reasons = {}
        for o in overlays:
            if o["id"] == "closing-badge":
                reasons[o["id"]] = "duplicate"
            else:
                resolved[o["id"]] = o
        return resolved, reasons, "skipped"

    monkeypatch.setattr(rg, "resolve_phone_card_geometry", _fake_resolve)
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing", "badge_visual_id": "v-badge"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    assert result.receipt["closing"]["badge"] == "unplaced"
    assert result.receipt["closing"]["badge_reason"] == "overlap"


def test_closing_no_badge_requested_is_none(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v-closing")])
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=[],
        closing={"visual_id": "v-closing"},
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    assert result.receipt["closing"]["badge"] == "none"
    assert "badge_reason" not in result.receipt["closing"]


def test_closing_none_has_badge_none(monkeypatch):
    _patch(monkeypatch, assets=[])
    result = rg.ground_phone_reaction_beats(
        _open_session, job_id="j1", beats=[], closing=None, words=[], duration_s=5.0, clip_path=None
    )
    assert result.receipt["closing"] == {"status": "none", "badge": "none"}


def test_card_window_too_short_after_duration_clamp(monkeypatch):
    """A trigger spoken right near the end of the clip clamps its card window
    under 0.3s -- distinct from `overlap` (which is reserved for a window
    shortened by ANOTHER card)."""

    _patch(monkeypatch, assets=[_asset("v1")])
    words = [_word("hello", 4.9, 5.0)]
    beats = [
        {
            "beat_id": "b1",
            "trigger": "hello",
            "visual_id": "v1",
            "visual_role": "photo",
            "hold_s": 3.0,
        }
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "too_short"}
    ]


def test_no_safe_spot(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v1")])

    def _fake_resolve(overlays, *, clip_path, job_id, footprints_by_id):  # noqa: ARG001
        return {}, {o["id"]: "no_safe_spot" for o in overlays}, "skipped"

    monkeypatch.setattr(rg, "resolve_phone_card_geometry", _fake_resolve)
    words = [_word("hello", 0.0, 0.3)]
    beats = [{"beat_id": "b1", "trigger": "hello", "visual_id": "v1", "visual_role": "photo"}]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "hello", "reason": "no_safe_spot"}
    ]


def test_empty_words_reports_every_beat_never_heard(monkeypatch):
    _patch(monkeypatch, assets=[_asset("v1")])
    beats = [{"beat_id": "b1", "trigger": "anything", "visual_id": "v1", "visual_role": "sticker"}]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=[],
        duration_s=5.0,
        clip_path=None,
    )
    assert result.cards == []
    assert result.sound_effects == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b1", "trigger": "anything", "reason": "never_heard"}
    ]


# --- KRI-181: beat sound resolves to real KRI-173 catalog ids, never a substring --------


# Prod ids named in the ticket, reused verbatim for the base "buzzer"/"ding"
# pair so a regression against the real library would trip these too.
_WRONG_BUZZER_ID = "76516ffbeb9d45a9b00c4102f4e5ea7c"
_WRONG_BUZZER_LONG_ID = "a1e2b3c4d5f6a7b8c9d0e1f2a3b4c5d6"
_GAME_SHOW_BUZZER_ID = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a1"
_CORRECT_DING_ID = "f8524cb1d3df4cb08628282692d82769"
_ELEVATOR_DING_ID = "c3d4e5f6a7b8c9d0e1f2a3b4c5d6a1b2"
_WHOOSH_ID = "d4e5f6a7b8c9d0e1f2a3b4c5d6a1b2c3"
_BUZZING_BEES_ID = "e5f6a7b8c9d0e1f2a3b4c5d6a1b2c3d4"


def _sfx_entry(
    id_: str,
    name: str,
    *,
    search_terms: tuple[str, ...],
    catalog_rank: int,
    quality_tier: str = "library",
) -> SfxEntry:
    return SfxEntry(
        id=id_,
        name=name,
        category=None,
        search_terms=search_terms,
        quality_tier=quality_tier,
        catalog_rank=catalog_rank,
    )


# Base wins its own "long" variant AND the differently-specific "Game show
# buzzer" by prominence (catalog_rank 0 < 1 < 2) once word-score ties; same
# shape for the ding pair. "Buzzing bees" is a decoy: "buzz" is a substring
# of its name but never a whole matched word.
_LIBRARY_SFX = [
    _sfx_entry(_WRONG_BUZZER_ID, "Wrong buzzer", search_terms=("buzzer", "wrong"), catalog_rank=0),
    _sfx_entry(
        _WRONG_BUZZER_LONG_ID,
        "Wrong buzzer long",
        search_terms=("buzzer", "wrong", "long"),
        catalog_rank=1,
    ),
    _sfx_entry(
        _GAME_SHOW_BUZZER_ID,
        "Game show buzzer",
        search_terms=("buzzer", "game", "show"),
        catalog_rank=2,
    ),
    _sfx_entry(_CORRECT_DING_ID, "Correct ding", search_terms=("ding", "correct"), catalog_rank=0),
    _sfx_entry(
        _ELEVATOR_DING_ID, "Elevator ding", search_terms=("ding", "elevator"), catalog_rank=1
    ),
    _sfx_entry(_WHOOSH_ID, "Whoosh", search_terms=("whoosh",), catalog_rank=0),
    _sfx_entry(
        _BUZZING_BEES_ID,
        "Buzzing bees",
        search_terms=("bees", "insects"),
        catalog_rank=0,
        quality_tier="core",
    ),
]

_SOUND_TRIGGER_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")


def _sound_only_words() -> list[dict]:
    return [_word(t, i * 0.4, i * 0.4 + 0.3) for i, t in enumerate(_SOUND_TRIGGER_WORDS)]


def _sound_beat(beat_id: str, trigger: str, sound: str) -> dict:
    """A sound-only beat (no `visual_id`/`visual_role`) so only `_resolve_sound`
    is exercised -- the card/visual side of grounding never enters the
    picture."""

    return {"beat_id": beat_id, "trigger": trigger, "sound": sound}


class TestBeatSoundResolvesToLibraryIds:
    """KRI-181: a beat's `sound` must resolve to a real KRI-173 catalog id via
    exact-id match or whole-word description coverage -- never a substring
    match, and never a random pick among equally-scored variants (prominence
    must decide the tie)."""

    def _run(self, monkeypatch, beats: list[dict]):
        _patch(monkeypatch, assets=[], sfx=_LIBRARY_SFX)
        return rg.ground_phone_reaction_beats(
            _open_session,
            job_id="j-kri181",
            beats=beats,
            closing=None,
            words=_sound_only_words(),
            duration_s=10.0,
            clip_path=None,
        )

    def _catalog_ids_by_beat_card_id(self, result) -> dict[str, str]:
        return {s.id.rsplit("-sfx", 1)[0]: s.catalog_id for s in result.sound_effects}

    def test_bare_buzzer_prefers_base_over_variant_and_disambiguated_sibling(self, monkeypatch):
        """ "a buzzer" word-covers Wrong buzzer, Wrong buzzer long, AND Game
        show buzzer equally -- the base entry must win via the prominence
        tie-break (lowest catalog_rank), not list order."""

        result = self._run(monkeypatch, [_sound_beat("b-buzzer", "alpha", "a buzzer")])
        assert len(result.sound_effects) == 1
        assert result.sound_effects[0].catalog_id == _WRONG_BUZZER_ID

    def test_wrong_buzzer_and_game_show_buzzer_disambiguate(self, monkeypatch):
        beats = [
            _sound_beat("b-wrong", "alpha", "wrong buzzer"),
            _sound_beat("b-gameshow", "bravo", "game show buzzer"),
        ]
        ids = self._catalog_ids_by_beat_card_id(self._run(monkeypatch, beats))
        assert ids["beat-b-wrong-1"] == _WRONG_BUZZER_ID
        assert ids["beat-b-gameshow-1"] == _GAME_SHOW_BUZZER_ID

    def test_bare_ding_and_elevator_ding_disambiguate(self, monkeypatch):
        beats = [
            _sound_beat("b-ding", "alpha", "a ding"),
            _sound_beat("b-elevator", "bravo", "elevator ding"),
        ]
        ids = self._catalog_ids_by_beat_card_id(self._run(monkeypatch, beats))
        assert ids["beat-b-ding-1"] == _CORRECT_DING_ID
        assert ids["beat-b-elevator-1"] == _ELEVATOR_DING_ID

    def test_whoosh_resolves(self, monkeypatch):
        result = self._run(monkeypatch, [_sound_beat("b-whoosh", "alpha", "whoosh")])
        assert result.sound_effects[0].catalog_id == _WHOOSH_ID

    def test_bare_buzz_does_not_substring_match_buzzing_bees(self, monkeypatch):
        """ "buzz" (not "buzzer") is a whole-word MISS against every fixture
        entry, including the whole "Wrong buzzer" trio -- `_stem` never turns
        "buzzer" into "buzz", so word-coverage matching correctly refuses to
        guess rather than falling back to the "Buzzing bees" substring decoy.

        Actual outcome exercised here: no entry covers the query, so
        `_resolve_sound` returns None and the beat surfaces in the receipt
        with reason "sound_not_found" -- it never resolves to "Buzzing bees".
        """

        result = self._run(monkeypatch, [_sound_beat("b-buzz", "alpha", "buzz")])
        assert result.sound_effects == []
        assert result.receipt["unplaced"] == [
            {"beat_id": "b-buzz", "trigger": "alpha", "reason": "sound_not_found"}
        ]

    def test_exact_catalog_id_wins_over_what_description_matching_would_pick(self, monkeypatch):
        """Passing the exact catalog id of the "long" variant resolves to
        THAT entry, even though a free-text "buzzer" description would
        instead prefer the base "Wrong buzzer" by prominence -- exact-id
        lookup short-circuits `resolve_described_effect` entirely."""

        result = self._run(monkeypatch, [_sound_beat("b-exact", "alpha", _WRONG_BUZZER_LONG_ID)])
        assert result.sound_effects[0].catalog_id == _WRONG_BUZZER_LONG_ID

    def test_unmatched_description_surfaces_sound_not_found_with_real_field_names(
        self, monkeypatch
    ):
        result = self._run(monkeypatch, [_sound_beat("b-kazoo", "alpha", "a kazoo")])
        assert result.sound_effects == []
        assert result.receipt["unplaced"] == [
            {"beat_id": "b-kazoo", "trigger": "alpha", "reason": "sound_not_found"}
        ]


def test_beat_error_is_fail_open(monkeypatch):
    """A malformed beat (not even a dict) must not sink the whole job."""

    _patch(monkeypatch, assets=[_asset("v1")])
    words = [_word("hello", 0.0, 0.3)]
    beats = [
        None,
        {"beat_id": "b2", "trigger": "hello", "visual_id": "v1", "visual_role": "sticker"},
    ]
    result = rg.ground_phone_reaction_beats(
        _open_session,
        job_id="j1",
        beats=beats,
        closing=None,
        words=words,
        duration_s=5.0,
        clip_path=None,
    )
    reasons = {u["beat_id"]: u["reason"] for u in result.receipt["unplaced"]}
    assert reasons["beat-0"].startswith("error")
    assert any(p["beat_id"] == "b2" for p in result.receipt["placed"])
