"""Did the creator ask for only their own voice? (KRI-1472)

"Only my voice, no music." is one sentence, and the brief writer usually files it as one
``audio`` requirement typed ``audio_intent: no_music`` (KRI-1164). Q1 run 3 (prod 2026-11-14,
thread 05aa3a87) filed it as two: "No music." with that fact, and "Only my voice." as an
``audio`` row whose only fact is ``voice_only: true``. No checker read that fact, so a phone
Talking draft answered "A Talking video can't do this, so it stays as filmed", was held, and the
simpler version listed the ask as left out, though a Talking edit with no music is exactly that.

What counts: a live whole-video ``audio`` requirement carrying the typed fact (a true
``voice_only`` under any of the spellings below, or ``audio_intent: voice_only``) and no other
typed ``audio_intent``. Only the fact is read, never the creator's words (KRI-667), so "louder
voice" or an untyped row keeps today's answer. It is judged like "No music"
(`brief_checks._check_voice_only`) and never mutes the footage's own sound.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement

VOICE_ONLY_INTENT = "voice_only"
# The keys a model files the boolean under when the prompt names none.
VOICE_ONLY_FACT_KEYS = ("voice_only", "only_voice", "only_my_voice")


def asks_voice_only(req: BriefRequirement) -> bool:
    """``req`` is the typed "only my voice" ask. Off (`VOICE_ONLY_ASK_ENABLED`): never."""
    if not settings.voice_only_ask_enabled or req.kind != "audio" or req.scope != "global":
        return False
    facts = req.facts if isinstance(req.facts, Mapping) else {}
    intent = facts.get("audio_intent")
    if intent is not None:
        # Another typed sound intent (no music, a mute, the camera sound) owns the row.
        return intent == VOICE_ONLY_INTENT
    return any(facts.get(key) is True for key in VOICE_ONLY_FACT_KEYS)


# `brief_checks.NO_MUSIC_INTENT` (that module imports this one).
_NO_MUSIC_INTENT = "no_music"


def _lead_clause(description: object) -> str | None:
    """The words before the first colon ("Only my voice: no music." -> "only my voice"), or
    None when there is no colon or nothing before it."""
    head, colon, _rest = str(description or "").partition(":")
    lead = " ".join(head.split()).casefold()
    return lead if colon and lead else None


def restated_voice_only_ids(requirements: Iterable[BriefRequirement]) -> frozenset[str]:
    """The untyped audio rows that restate a typed "only my voice" / "no music" row (KRI-1472).

    Q2 run 3 (prod 2026-11-15, job 77c63496) filed "Only my voice." twice: "Only my voice: no
    music." with ``audio_intent: no_music`` and "Only my voice: keep the speaker audible and
    mute the screen recordings." with no fact at all, which held the draft. Structure only, no
    word is read: a live whole-video ``audio`` row with NO facts, from the SAME source turn as
    a live whole-video ``audio`` sibling that carries the typed fact, whose description shares
    its lead clause (the words before the first colon) with that sibling. Such a row is judged
    as its sibling on a Talking edit. A lone untyped row, one from another turn, one with
    another lead clause or no colon, or one with a fact of its own keeps today's answer.
    """
    if not settings.voice_only_ask_enabled:
        return frozenset()
    rows = [
        (req, (req.source_turn_id, _lead_clause(req.description)))
        for req in requirements
        if req.live and req.kind == "audio" and req.scope == "global"
    ]
    rows = [(req, key) for req, key in rows if key[0] and key[1]]
    typed = {
        key
        for req, key in rows
        if req.facts.get("audio_intent") == _NO_MUSIC_INTENT or asks_voice_only(req)
    }
    return frozenset(req.id for req, key in rows if not req.facts and key in typed)
