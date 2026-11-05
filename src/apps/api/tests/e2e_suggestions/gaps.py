"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell or negative reads ``gap`` in the report, the run
stays green, and the day the check passes the run FAILS with "gap closed, remove the marker" so a
marker cannot outlive the bug it names. The text is the repro; the owner is the file to fix.

Open now: GAP-9 (a mute card shows no measured evidence), GAP-12 (an editor tap and its Undo lift
the fence a native Save puts up in front of the filler card), GAP-13 (a tap taken back is not
offered again once the chat was opened while its approval was pending), GAP-14 (a capability lever
pulled after the serve leaves a dead filler card), GAP-15 (a Turkish volume ask with a word in the
middle does not withhold the music card), GAP-16 (a kind switched on after a set was frozen is
served under the same set id with no ring entry: a dead first tap for every idle thread, at every
staged rollout of a per-kind flag).

Closed upstream, markers gone, so each is an ordinary failing check if it comes back: GAP-1 to 8
(the ring holds the served cards, a set of cards alone has an entry, Turkish taps get met receipts,
a second tap on a restating card whose approval is pending carries the typed refusal, a served
restating card is one the tap can apply, a refused tap writes no brief, Undo restores the filler
setting), GAP-10 (a per-kind switch reaches a frozen set) and GAP-11 (a Turkish music ask in the
accusative withholds the music card). The numbers are not reused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    id: str
    title: str
    owner: str
    text: str
    # The kinds the defect is about, and whether it lets a card be SERVED whose tap is refused (the
    # rule under test is that every served card executes): ``kinds_proven`` reads both.
    kinds: tuple[str, ...] = ()
    serves_dead_card: bool = False

    def wire(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "owner": self.owner,
            "text": self.text,
            "kinds": list(self.kinds),
            "serves_dead_card": self.serves_dead_card,
        }


MUTE_NO_REASON = Gap(
    "GAP-9",
    "A mute card carries no measured evidence line",
    "app/services/suggestion_chips.py _measured_evidence (no mute_clip branch)",
    "_measured_evidence has a branch for filler_words and duplicates only, so a mute card's "
    "evidence is empty (English) or just the clip label ('Klip 3'): the wind the signal measured "
    "is never shown. The reason line is whatever the agent wrote and passed the footage-word "
    "check ('Wind drowns out clip 3' is dropped, so the card reads 'Clip 3'; a Turkish reason that "
    "shares a footage word survives). The plan's card is 'Wind in clip 3'.",
    kinds=("mute_clip",),
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
    kinds=("filler_words",),
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
    kinds=("filler_words", "mute_clip"),
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
    kinds=("filler_words",),
    serves_dead_card=True,
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
    kinds=("music",),
)


NEW_KIND_UNRESOLVED_CARD = Gap(
    "GAP-16",
    "A kind switched on after a set was frozen is served under the same set id with no ring entry",
    "app/services/suggestion_serve.py (attach_cards returns None when the entry already has cards; "
    "the set id follows the digest, which has no enabled-kinds part)",
    "Rollout: `KRIA_SUGGESTION_HOOK_COPY_ENABLED` (or DUPLICATES / FILLER_WORDS / MUTE_CLIP) is "
    "off; an idle thread's GET freezes a set without that card (set id X, its ring entry holds "
    "the other cards). The switch is turned on. Nothing in the digest changed, so for 24 h the "
    "frozen set is served as before; once the freeze lapses the same digest is rebuilt WITH the "
    "new card, still under set id X. `attach_cards` refuses to touch an entry that already has "
    "cards, so the ring never learns the card: the GET serves it and its tap answers "
    "`unresolved` ('no longer fits'). The refusal mints a new set that leaves the card out and the "
    "refused mark keeps it out, so a creator who never edited gets a dead card and then none. "
    "Edited threads are unaffected (the digest moves). Anything else that decides a card at build "
    "time but is not in the digest (a capability lever such as SILENCE_CUT_ENABLED turned on) "
    "does the same.",
    kinds=("hook", "duplicates", "filler_words", "mute_clip"),
    serves_dead_card=True,
)
