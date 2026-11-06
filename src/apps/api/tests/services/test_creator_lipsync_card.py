"""KRI-1093: the Lip-sync picker card is a montage that defaults an attached song to lipsync.

Failure modes pinned here (each one is a way the card could silently do the wrong thing):

* the card flips an explicit "use it as background music" ask to lip-sync (the creator's words
  win, KRI-129);
* the card is ignored (a neutral turn still repairs to background);
* the Montage card (or a thread with no card) changes at all: manifest hashes, prompt bytes, repair;
* the hint leaks onto a non-montage edit or a manifest with no usable song;
* the thread's card never reaches the manifest, so the above passes only on hand-built manifests.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import (
    USER_SONG_BACKGROUND_NOTICE,
    USER_SONG_LIPSYNC_CARD_NOTICE,
)
from app.kria.strategy_policy import song_captions_refusal
from app.services import creator_capabilities as capabilities
from app.services import creator_sessions
from tests.services.test_creator_user_song import (
    CLIPS,
    _song,
    _song_strategy,
    golden_settings,
    song_on,
)

__all__ = ["golden_settings", "song_on"]


def _manifest(*, chosen_format=None, song=None, edit_format="montage"):
    return capabilities.resolve_creator_manifest(
        item_id="item-song",
        edit_format=edit_format,
        media=[{"media_id": media_id, "kind": "video", "duration_s": 20.0} for media_id in CLIPS],
        phone_source_media_ids=CLIPS,
        phone_rendering_allowed=True,
        user_song_item=song,
        chosen_format=chosen_format,
    )


def _compile(manifest, **strategy_fields):
    return capabilities.compile_strategy_to_plan(manifest, _song_strategy(**strategy_fields))


def test_card_defaults_an_unset_song_sync_to_lipsync(song_on) -> None:
    plan = _compile(_manifest(chosen_format="lipsync", song=_song()))

    assert plan.strategy.song_sync == "lipsync"
    assert plan.strategy.audio_strategy == "user_song"
    assert USER_SONG_LIPSYNC_CARD_NOTICE in plan.notices
    assert USER_SONG_BACKGROUND_NOTICE not in plan.notices


def test_an_explicit_background_ask_wins_over_the_card(song_on) -> None:
    plan = _compile(_manifest(chosen_format="lipsync", song=_song()), song_sync="background")

    assert plan.strategy.song_sync == "background"
    assert not any("background music" in notice for notice in plan.notices)
    assert USER_SONG_LIPSYNC_CARD_NOTICE not in plan.notices


def test_the_montage_card_still_repairs_to_background(song_on) -> None:
    for card in (None, "montage"):
        plan = _compile(_manifest(chosen_format=card, song=_song()))
        assert plan.strategy.song_sync == "background"
        assert USER_SONG_BACKGROUND_NOTICE in plan.notices


def test_the_hint_is_dropped_off_a_montage_family_edit(song_on) -> None:
    manifest = _manifest(chosen_format="lipsync", song=_song(), edit_format="subtitled")
    assert manifest.chosen_format is None


def test_a_card_with_no_song_default_is_not_a_hint(song_on) -> None:
    assert _manifest(chosen_format="montage").chosen_format is None
    assert _manifest(chosen_format="slides").chosen_format is None


def test_a_manifest_without_the_hint_is_byte_identical(song_on) -> None:
    plain = _manifest(song=_song())
    again = _manifest(chosen_format="montage", song=_song())
    assert (plain.context_hash, plain.manifest_hash) == (again.context_hash, again.manifest_hash)
    assert "chosen_format" not in plain.model_dump(mode="json")

    carded = _manifest(chosen_format="lipsync", song=_song())
    assert carded.model_dump(mode="json")["chosen_format"] == "lipsync"
    assert carded.context_hash != plain.context_hash
    assert carded.manifest_hash != plain.manifest_hash


def test_lipsync_card_without_a_song_still_asks_for_lyrics_song(song_on) -> None:
    """Captions asked on the Lip-sync card with no song: ask for the song, not "lip-sync" words."""
    strategy = CreativeStrategy(edit_format="montage", caption_style="auto")
    creator_text = "add lyrics captions"

    assert song_captions_refusal(_manifest(), strategy, creator_text=creator_text) is None
    refused = song_captions_refusal(
        _manifest(chosen_format="lipsync"), strategy, creator_text=creator_text
    )
    assert refused is not None
    assert refused.code == "song_captions_need_song"


async def _context(*, card):
    item = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        current_job_id=None,
        clip_gcs_paths=[],
        clip_assignments=[
            {"media_id": media_id, "gcs_path": f"users/u/{media_id}.mp4", "kind": "video"}
            for media_id in CLIPS
        ],
    )
    empty = MagicMock()
    empty.scalars.return_value = []
    db = AsyncMock()
    db.execute.side_effect = [empty, empty, empty]
    db.scalar.return_value = card
    manifest, _media = await creator_sessions.resolve_item_creator_context(
        db, item, persona=SimpleNamespace(user_id="00000000-0000-0000-0000-000000000002")
    )
    return manifest, db


@pytest.mark.asyncio
async def test_the_threads_card_reaches_the_manifest(song_on) -> None:
    lipsync, _ = await _context(card="lipsync")
    montage, _ = await _context(card="montage")
    none, _ = await _context(card=None)

    assert lipsync.chosen_format == "lipsync"
    assert montage.chosen_format is None
    assert (montage.context_hash, montage.manifest_hash) == (none.context_hash, none.manifest_hash)


@pytest.mark.asyncio
async def test_creator_songs_off_never_reads_the_card(song_on, monkeypatch) -> None:
    monkeypatch.setattr(song_on, "user_song_montage_enabled", False)
    manifest, db = await _context(card="lipsync")

    assert manifest.chosen_format is None
    db.scalar.assert_not_called()


def test_no_card_prompt_is_unchanged_and_the_card_adds_one_paragraph(song_on) -> None:
    from tests.agents.test_main_creator_user_song import _render, _song_input

    base = _song_input()
    carded = base.model_copy(
        update={
            "capability_manifest": base.capability_manifest.model_copy(
                update={"chosen_format": "lipsync"}
            )
        }
    )
    plain_prompt, card_prompt = _render(base), _render(carded)

    assert "Lip-sync video type" not in plain_prompt
    assert "Lip-sync video type" in card_prompt
    assert (
        hashlib.sha256(plain_prompt.encode()).hexdigest()
        != hashlib.sha256(card_prompt.encode()).hexdigest()
    )
    assert "just use it as background" in card_prompt
