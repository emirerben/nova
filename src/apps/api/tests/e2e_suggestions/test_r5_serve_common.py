"""Shared helpers of the R5 serve fixtures (no test lives here).

Every R5 serve fixture builds a real thread on real Postgres and drives the product's own path:
GET route -> ring -> ``submit_turn`` -> ``run_kria_turn`` -> ``_complete_draft_turn``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.database import sync_session
from app.models import PlanItem
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


async def get_at(phone: Phone, protocol: int | None):  # noqa: ANN201
    """``Phone.get`` from an app build that declares ``protocol`` (the header the server reads)."""
    from types import SimpleNamespace

    from app.auth import get_current_user
    from app.main import app
    from app.services import suggestion_chips as sc

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=phone.seeded.user_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://phone"
        ) as client:
            response = await client.get(
                f"/creation-threads/{phone.seeded.thread_id}/suggestions",
                headers={"X-Kria-Client-Protocol": str(protocol)} if protocol is not None else {},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    phone.last_wire = response.json()
    return sc.SuggestionSetOut.model_validate(phone.last_wire)


def into_phone_take(seeded: h.Seeded) -> str:
    """Rewrite the Talking take's path to an analysis proxy (what a phone-rendered item holds)."""
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        rows = [dict(r) for r in item.clip_assignments]
        proxy = f"users/e2e/analysis-proxy-{rows[0]['media_id']}.mp4"
        rows[0]["gcs_path"] = proxy
        item.clip_assignments = rows
        item.clip_gcs_paths = [proxy]
        db.commit()
        return str(rows[0]["media_id"])
