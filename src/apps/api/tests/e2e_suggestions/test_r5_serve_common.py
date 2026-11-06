"""Shared helpers of the R5 serve fixtures (no test lives here).

Every R5 serve fixture builds a real thread on real Postgres and drives the product's own path:
GET route -> ring -> ``submit_turn`` -> ``run_kria_turn`` -> ``_complete_draft_turn``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import NO_LONGER_FITS, NO_LONGER_FITS_TR, Phone
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"
KEYS = [KEY, f"{KEY}/tr"]

# kind -> (its own switch, the footage that serves it)
PER_KIND = {
    "hook": ("kria_suggestion_hook_copy_enabled", EDITOR),
    "duplicates": ("kria_suggestion_duplicates_enabled", SIGNALS),
    "filler_words": ("kria_suggestion_filler_words_enabled", TALKING),
    "mute_clip": ("kria_suggestion_mute_clip_enabled", SIGNALS),
}
RESTATING = frozenset({"filler_words", "mute_clip"})


def prefs(key: str = KEY) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


@dataclass
class Scene:
    scn: Any
    seeded: h.Seeded
    phone: Phone
    key: str
    profile: str


def build(world, key: str, profile: str, *, scn=None) -> Scene:  # noqa: ANN001
    """A seeded thread (a Talking take also gets its ready cleanup analysis) and its phone."""
    scn = scn or threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    return Scene(scn, seeded, Phone(seeded, world, prefs(key), language=scn.language), key, profile)


def keep_mute_card(monkeypatch, kind: str) -> None:  # noqa: ANN001
    """The mute kind ships dark: its pinned tap applies only with this constant on."""
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)


def kinds_of(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


def card_of(out, kind: str):  # noqa: ANN001, ANN201
    return next((c for c in out.suggestions if c.kind == kind), None)


def executes(tap) -> bool:  # noqa: ANN001
    """The tap was PLANNED and wrote its draft, with no typed refusal."""
    return bool(tap.planned and tap.refused is None and tap.new_drafts == 1)


def lang_reply(key: str) -> str:
    return NO_LONGER_FITS_TR if key.endswith("/tr") else NO_LONGER_FITS


def stored_ids(phone: Phone, set_id: str) -> set[str]:
    entry = phone.ring_entry(set_id) or {}
    return {str(c.get("id")) for c in entry.get("suggestions") or []}


def unstored(phone: Phone, out) -> list[str]:  # noqa: ANN001
    """The served cards the ring entry of this set does not hold (each one is a dead tap)."""
    held = stored_ids(phone, out.set_id)
    return [c.kind for c in out.suggestions if c.id not in held]
