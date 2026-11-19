"""KRI-1470: go on without a Visual that is still being checked, and say which file.

The planner holds a turn while any creator-visible clip has no analysis yet
(`planner._plan_live_turn`, KRI-127). Q1 run 3 (2026-11-14): one overlay screenshot stayed
`queued` (KRI-1469), so every run of the prompt and of "Skip that clip and continue with the
rest" ended "I'm still checking 1 of your clips"; the take and nine Visuals were ready.

A pending Visual (a `PlanItemAsset`: screenshot, screen recording, photo) is left out of the
turn when the turn's last planning run still finds it pending; the earlier runs give it time
to land. No words are read to decide this (KRI-667: a skip phrase list would guess meaning):
any message's last run goes on, a "skip that" included. Pending footage (the phone clips the
edit is cut from) still holds the turn, and so does a turn that would have nothing else to
plan with. The Visual stays in the manifest (the approval fence hashes it); only this turn's
clip matching skips it, and the draft names the file.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.config import settings
from app.kria.paid_call_scope import turn_run_epoch
from app.kria.reply_language import say


def last_planning_run() -> bool:
    """This is the turn's last run: the runtime answers instead of continuing it
    (`kria_runtime._continue_turn`: runs are counted by lease epoch, from 1)."""
    return turn_run_epoch.get() >= int(settings.kria_turn_auto_continue_max_runs)


def visuals_to_go_without(clips: Sequence, missing: Iterable[str]) -> list | None:
    """The pending Visuals this turn goes on without; None to keep waiting."""
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
    return pending if last_planning_run() else None


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
