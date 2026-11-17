"""KRI-1470: go on without a Visual that is still being checked, and say which file.

The planner holds a turn while any creator-visible clip has no analysis yet
(`planner._plan_live_turn`, KRI-127). Q1 run 3 (2026-11-14): one overlay screenshot stayed
`queued` (KRI-1469), so every run of the prompt and of "Skip that clip and continue with the
rest" ended "I'm still checking 1 of your clips"; the take and nine Visuals were ready.

A pending Visual (a `PlanItemAsset`: screenshot, screen recording, photo) is left out of the
turn when the turn's last planning run still finds it pending, or when the creator's message
asks to go on without it. Pending footage (the phone clips the edit is cut from) still holds
the turn, and so does a turn that would have nothing else to plan with. The Visual stays in
the manifest (the approval fence hashes it); only this turn's clip matching skips it, and the
draft names the file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.config import settings
from app.kria.paid_call_scope import turn_run_epoch
from app.kria.reply_language import say
from app.services.choice_questions import loose_reply


def _phrases(*texts: str) -> frozenset[str]:
    return frozenset(loose_reply(text) for text in texts)


# Words around the answer that change nothing ("ok skip it please", "tamam onu atla").
_FILLERS = _phrases(
    "ok", "okay", "please", "pls", "just", "then", "yes", "yeah", "fine", "alright", "now",
    "tamam", "lütfen", "evet", "o zaman", "hadi", "bitte", "ja", "dann",
)  # fmt: skip
# A whole clause that drops the pending file or goes on with the rest. A plain "continue" /
# "go ahead" is NOT one: the still-checking reply asks for exactly that once the clip is ready.
_GO_WITHOUT = _phrases(
    # Each names the file or "the rest": a bare "skip it" / "drop it" may be about anything.
    "skip that clip", "skip this clip", "skip the clip", "skip that file", "skip the file",
    "skip that photo", "skip the photo", "skip that screenshot", "skip the screenshot",
    "skip that visual", "skip the visual", "skip that image", "skip the image",
    "continue with the rest", "continue with the others", "go on with the rest",
    "go ahead with the rest", "carry on with the rest", "use the rest", "just use the rest",
    "do it with the rest", "make it with the rest", "use the others",
    "continue without it", "go ahead without it", "go on without it", "carry on without it",
    "do it without it", "make it without it", "continue without that", "go without it",
    "o klibi atla", "bu klibi atla", "o dosyayı atla", "o görseli atla",
    "kalanlarla devam et", "diğerleriyle devam et", "geri kalanlarla devam et",
    "onsuz devam et", "onsuz yap", "onsuz hazırla",
    "mach mit dem rest weiter", "mit dem rest weitermachen", "ohne weitermachen",
    "mach ohne weiter", "überspring die datei", "überspring das bild",
)  # fmt: skip
_CLAUSE_BREAK = re.compile(r"[,.;!?\n]+|\s(?:and|then|ve|sonra|und|dann)\s", re.IGNORECASE)


def _unpadded(clause: str) -> str:
    words = clause.split()
    changed = True
    while words and changed:
        changed = False
        for size in (2, 1):
            if len(words) >= size and " ".join(words[:size]) in _FILLERS:
                words, changed = words[size:], True
                break
            if len(words) >= size and " ".join(words[-size:]) in _FILLERS:
                words, changed = words[:-size], True
                break
    return " ".join(words)


def asks_to_go_without(message: str | None) -> bool:
    """Every clause of the message drops the pending file or goes on with the rest."""
    clauses = [_unpadded(loose_reply(part)) for part in _CLAUSE_BREAK.split(str(message or ""))]
    clauses = [clause for clause in clauses if clause]
    return bool(clauses) and all(clause in _GO_WITHOUT for clause in clauses)


def last_planning_run() -> bool:
    """This is the turn's last run: the runtime answers instead of continuing it
    (`kria_runtime._continue_turn`: runs are counted by lease epoch, from 1)."""
    return turn_run_epoch.get() >= int(settings.kria_turn_auto_continue_max_runs)


def visuals_to_go_without(
    clips: Sequence, missing: Iterable[str], user_message: str | None
) -> tuple[list, str] | None:
    """The pending Visuals this turn goes on without, and why; None to keep waiting."""
    if not settings.kria_pending_visual_go_ahead_enabled:
        return None
    missing_ids = set(missing)
    pending = [clip for clip in clips if clip.media_id in missing_ids]
    if not pending or len(pending) != len(missing_ids):
        return None
    if any(getattr(clip, "asset_id", None) is None for clip in pending):
        return None  # footage still being analysed holds the turn
    if not any(clip.media_id not in missing_ids for clip in clips):
        return None  # nothing else to plan with
    if asks_to_go_without(user_message):
        return pending, "creator_skip"
    if last_planning_run():
        return pending, "last_run"
    return None


def left_out_notice(clips: Sequence) -> str:
    """One sentence naming each file this turn planned without."""
    names = [getattr(clip, "file_name", None) for clip in clips]
    quoted = ", ".join(f"“{name}”" for name in names if name)
    if len(clips) > 1:
        return say(
            en=(
                "I planned this without {names}: they're still being checked. "
                "Ask me to add them once they're ready."
            ),
            tr=(
                "Bunu {names} olmadan planladım: hâlâ kontrol ediliyorlar. "
                "Hazır olduklarında eklememi iste."
            ),
            names=quoted or say(en="some of your Visuals", tr="bazı görsellerin"),
        )
    return say(
        en=(
            "I planned this without {names}: it's still being checked. "
            "Ask me to add it once it's ready."
        ),
        tr="Bunu {names} olmadan planladım: hâlâ kontrol ediliyor. Hazır olunca eklememi iste.",
        names=quoted or say(en="one of your Visuals", tr="görsellerinden biri"),
    )
