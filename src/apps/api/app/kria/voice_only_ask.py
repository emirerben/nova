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

from collections.abc import Mapping
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
