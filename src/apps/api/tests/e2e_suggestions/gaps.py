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


CAPABILITY_LEVER_DEAD_CARD = Gap(
    "GAP-14",
    "A capability lever pulled after the serve leaves a filler card on the phone for a refused tap",
    "app/services/suggestion_serve.py (_strip_dead_cards asks the kind switch, the consent gate, "
    "approval readiness and the plan; not the cleanup capability the card is built on)",
    "Talking take, filler card served; then `SILENCE_CUT_ENABLED`, `SUBTITLED_ARCHETYPE_ENABLED` "
    "or `SPEECH_CLEANUP_MODE=off` is switched off (an operator's rollout lever, no code change). "
    "The next GET still serves the card from the frozen set for the rest of the 24 h, and its tap "
    "is refused `base_moved` (`pinned.rebuild_action` finds `capability_for_item` closed). Nothing "
    "is written and the refusal mints a new set that drops the card, so the cost is one dead tap "
    "per held card; the same class as GAP-10 (the per-kind switch), which the strip now answers.",
)


TURKISH_VOLUME_ASK_LEAKS = Gap(
    "GAP-15",
    "A Turkish volume ask with a word between noun and verb does not withhold the music card",
    "app/services/suggestions/brief_asks.py (MUSIC.words: 'sesi kis' ... are adjacent phrases)",
    "A live requirement whose description is 'sesi biraz kıs', 'sesi çok kıs', 'sesi tamamen "
    "kapat', 'sesi daha düşük yap', 'ses çok yüksek' (and the like) carries no typed audio fact, "
    "so "
    "the words decide. MUSIC.words lists `f'{sesi|sesini} {verb}'` as adjacent phrases, so one "
    "adverb in between ('biraz', 'çok', 'tamamen', 'daha') makes it another phrase and the card "
    "'Müziği kıs' is offered over the creator's own 'sesi biraz kıs' (KRI-129). 'sesi kıs', "
    "'sesini azalt' and every softened-k form of 'müzik' ('müziği', 'müziğin', 'müziğe') are read.",
)
