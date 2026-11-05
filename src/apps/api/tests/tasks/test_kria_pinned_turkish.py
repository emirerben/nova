"""A Turkish card tap gets the same receipts an English tap does (GAP-3).

Failure catalogue, written before any code. The seam binds each requirement of an editor draft to
the diff the ops made (``kria/editor_receipts``), and a card tap writes the card's own closed
sentence as that requirement. Turkish sentences fold through ``loose_text`` (diacritics and the
dotless i dropped), so a cue written for the English word can miss the Turkish one:

  T1  ``Müziği kıs`` folds to ``muzigi kis``. The cue was ``muzik\\w*``, but Turkish softens the
      final k before a vowel (muzik -> muzigi), so the ask named no dimension and the receipt was
      "partly, have a look" with the change listed ("Muzik -> 30%") instead of Done.
  T2  ``Ses efektleri ekle`` (add sound effects) also holds ``ses``, the Turkish word for volume.
      The ask then needed an audio-level change AND a sound effect, and a tap that did exactly what
      the card promised read as half done.
  T3  ``2. klibi kaldır`` (remove clip 2, the duplicates card's requirement) folds to
      ``2 klibi kaldir``. ``klip`` softens to ``klib`` before a vowel too, so no clip noun was
      found: no dimension, and no ordinal to hold the removal to clip 2.
  T4  The fix must not loosen the English binding or the Turkish asks that already bound (captions,
      transitions, hook), and an ask that really names the volume ("Müziğin sesini kıs") must still
      be held to a level change, so a sound effect alone is not "Done" for it.
  T5  A Turkish tap that changes the WRONG clip is not Done (the ordinal now binds): the receipt
      cannot be satisfied by any removal.

Every row drives submit -> claim -> plan -> row-locked commit through ``run_kria_turn`` on real
Postgres (the harness in ``kria_pinned_suggestion_harness``), with the thread's reply language set
to Turkish so the cards, the requirement and the receipt are all Turkish.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.database import engine as async_engine
from app.database import sync_session
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h

# The same CI-aware database guard as the other pinned suites: importing this module fails under
# CI, and skips elsewhere, when there is no migrated scratch database.
from tests.tasks.test_kria_pinned_kinds import (  # noqa: F401 - the guard runs on import
    _PROBLEM,
    head,
    pin_record,
    tap_and_run,
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    world = h.install_world(monkeypatch)
    k.enable_kinds(monkeypatch)
    return world


def speak_turkish(seeded) -> None:  # noqa: ANN001
    """The chat is in Turkish: the cards are served in it and the turn replies in it."""
    with sync_session() as db:
        thread = db.get(h.CreationThread, seeded.thread_id, with_for_update=True)
        thread.state = {**(thread.state or {}), "reply_language": "tr"}
        db.commit()


def receipts_of(thread_id) -> list[dict]:  # noqa: ANN001
    return h.events(thread_id, "draft_applied")[-1].payload["requirement_receipts"]


def assert_done(receipts: list[dict], what: str) -> None:
    [receipt] = receipts
    assert (receipt["status"], receipt["verification"]) == ("met", "checked"), (what, receipt)


# kind -> (English prompt, Turkish prompt): the closed sentences the cards send.
CASES = {
    "music": ("Turn the music down", "Müziği kıs"),
    "sfx": ("Add sound effects", "Ses efektleri ekle"),
    "captions": ("Add captions", "Altyazı ekle"),
    "transition": ("Add transitions between the clips", "Klipler arasına geçişler ekle"),
}


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("kind", list(CASES))
@pytest.mark.asyncio
async def test_a_card_tap_is_done_and_checked_in_both_languages(world, kind, language):
    """T1/T2/T4: music, sound effects, captions and transitions are Done in English and Turkish."""
    seeded = h.seed_ready_thread()
    if language == "tr":
        speak_turkish(seeded)
    await h.serve(seeded)
    card = seeded.cards[kind]
    assert card.prompt == CASES[kind][0 if language == "en" else 1]

    _, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed", result
    assert_done(receipts_of(seeded.thread_id), f"{kind}/{language}")


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.asyncio
async def test_a_duplicates_tap_is_done_and_checked_in_both_languages(world, monkeypatch, language):
    """T3: the requirement a duplicates tap writes is ``Remove clip N`` / ``N. klibi kaldır``."""
    seeded = h.seed_ready_thread()
    if language == "tr":
        speak_turkish(seeded)
    k.install_inputs(monkeypatch, needed=[])
    k.seed_duplicate_pair(seeded)
    await k.serve_cards(seeded, k.pick("duplicates", targets=("c1", "c2")))

    _, result = await tap_and_run(seeded, "duplicates")

    assert result["status"] == "completed", result
    assert_done(receipts_of(seeded.thread_id), f"duplicates/{language}")
    version = h.briefs(seeded.thread_id)[-1]
    assert version.requirements[-1]["description"] == (
        "Remove clip 1" if language == "en" else "1. klibi kaldır"
    )


def test_a_duplicates_removal_of_another_clip_is_not_done_in_turkish():
    """T5, at the binder: ``2. klibi kaldır`` is held to clip 2, so removing clip 1 is not Done.

    The English ask has always been held to its clip (``Remove clip 2``); the Turkish one found no
    ordinal at all and accepted any removal."""
    from app.kria.brief import BriefRequirement
    from app.kria.editor_receipts import bind_editor_receipts
    from app.services.kria_editor_ops_diff import DiffEntry, DiffTarget, EditorDiff

    def removal_of(clip: int) -> EditorDiff:
        target = DiffTarget(f"slot-{clip - 1}", "clip", None, f"clip {clip}", None, None, 3)
        return EditorDiff(entries=(DiffEntry("timeline", "removed", (target,)),))

    verdicts = {}
    for language, description in (("en", "Remove clip 2"), ("tr", "2. klibi kaldır")):
        req = BriefRequirement(id="r1", kind="select", scope="clip:clip-b", description=description)
        for clip in (1, 2):
            [receipt], _ = bind_editor_receipts([req], [], removal_of(clip), [])
            verdicts[(language, clip)] = receipt.status
    assert verdicts[("en", 2)] == "met" and verdicts[("tr", 2)] == "met"
    assert verdicts[("en", 1)] != "met", "English is held to its clip"
    assert verdicts[("tr", 1)] != "met", "so is Turkish"


def test_an_ask_that_names_the_volume_is_still_held_to_a_level_change():
    """T4, at the binder: ``Ses efektleri ekle`` is a sound-effect ask, but ``Müziğin sesini kıs``
    names the volume, so a sound effect alone is not Done for it."""
    from app.kria.brief import BriefRequirement
    from app.kria.editor_receipts import ask_shape

    effects = ask_shape(
        BriefRequirement(id="r1", kind="audio", scope="global", description="Ses efektleri ekle"),
        [],
    )
    assert [g.label for g in effects.groups] == ["sfx"]
    volume = ask_shape(
        BriefRequirement(id="r2", kind="audio", scope="global", description="Müziğin sesini kıs"),
        [],
    )
    assert "volume" in [g.label for g in volume.groups]
    both = ask_shape(
        BriefRequirement(
            id="r3",
            kind="audio",
            scope="global",
            description="Ses efektleri ekle ve sesi kıs",
        ),
        [],
    )
    assert {g.label for g in both.groups} == {"volume", "sfx"}
