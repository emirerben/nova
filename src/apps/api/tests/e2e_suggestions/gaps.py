"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell reads ``gap`` in the report, the run stays green, and
the day the check passes the run FAILS with "gap closed, remove the marker" so a marker cannot
outlive the bug it names. The text is the repro; the owner is the file to fix.

GAP-1 through 8 and 10 and 11 were closed on the integration and serve lanes (the ring holds the
served cards, a set of cards alone has an entry, Turkish taps get met receipts, a second tap on a
restating card whose approval is pending carries the typed refusal, a served restating card is one
the tap can apply, a refused tap writes no brief, Undo restores the filler setting, the pinned-taps
kill switch and the per-kind switches reach a frozen set, and a Turkish music ask in the accusative
withholds the music card). Their markers are gone, so each is now an ordinary failing check if it
ever comes back. The numbers are not reused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    id: str
    title: str
    owner: str
    text: str


MUTE_NO_REASON = Gap(
    "GAP-9",
    "A mute card carries no measured evidence line",
    "app/services/suggestion_chips.py _measured_evidence (no mute_clip branch)",
    "_measured_evidence has a branch for filler_words and duplicates only, so a mute card's "
    "evidence is empty (English) or just the clip label ('Klip 3'): the wind the signal measured "
    "is never shown. The reason line is whatever the agent wrote and passed the footage-word "
    "check ('Wind drowns out clip 3' is dropped, so the card reads 'Clip 3'; a Turkish reason that "
    "shares a footage word survives). The plan's card is 'Wind in clip 3'.",
)


SAVE_FENCE_LIFTED = Gap(
    "GAP-12",
    "An editor-op tap and its Undo lift the fence a native Save puts in front of the filler card",
    "app/tasks/kria_runtime.py (_complete_draft_turn: session.target_generation_id = ... on every "
    "editor-op draft) with services/suggestions/pinned.py save_unread / restate_base",
    "Talking take, then: the creator's native Save (editor-commit, caption size) -> the GET serves "
    "no filler card (the session has not read the Save) -> the creator taps ANY editor card, which "
    "re-syncs the session's target_generation_id to the saved video -> Undo (the head is the plan "
    "again) -> the GET serves the filler card and its tap is ACCEPTED: the plan it restates holds "
    "none of the Save's edits, which is what `_restate_video_moved` documents as the thing to "
    "refuse ('approving it renders from the plan and drops them'). The pointer says 'the agent "
    "read the saved video'; after Undo the draft that read it is gone. Expected: refused "
    "base_moved, or not served.",
)


WITHDRAWN_CARD_STAYS_HIDDEN = Gap(
    "GAP-13",
    "A tap that was taken back (deny, expiry, Undo) is not offered again once the chat was opened "
    "while its approval was pending",
    "app/services/suggestion_serve.py (fold_suppressed / the kept list) with "
    "app/kria/drafts.py _withdraw_footprint (unmark_consumed reads the ring only)",
    "Talking take: tap the filler card (a render approval is staged) -> the phone polls the "
    "suggestions route while the approval is pending (a new set is appended and the applied mark "
    "is folded into `thread.state['suggestion_suppressed']`) -> the creator denies the render "
    "(or lets it lapse, or taps Undo). The footprint withdrawal clears the card's mark on its ring "
    "entry, so the card the phone still holds taps again (L3 passes), but the folded fingerprint "
    "stays: the next GET serves hook, captions, music and sfx and never the filler card again. "
    "Nobody opening the chat in between (the same steps without the poll) gets the card back. The "
    "limit is written down for Undo (docs/pipelines/suggestion-blocks.md 'Handled cards never come "
    "back'); the poll-while-pending case is the ordinary one, so the withdrawal that G asked for "
    "restores the setting and the brief but not the offer.",
)
