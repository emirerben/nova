"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell or negative reads ``gap`` in the report, the run
stays green, and the day the check passes the run FAILS with "gap closed, remove the marker" so a
marker cannot outlive the bug it names. The text is the repro; the owner is the file to fix.

Open now: GAP-17 (a hook card is served again after its own tap was undone, on an account without
the creative brief, and its tap is refused).

GAP-1 to 11 were closed on the integration and serve lanes (the ring holds the served
cards, a set of cards alone has an entry, Turkish taps get met receipts, a second tap on a restating
card whose approval is pending carries the typed refusal, a served restating card is one the tap
can apply, a refused tap writes no brief, Undo restores the filler setting, the pinned-taps kill
switch and the per-kind switches reach a frozen set, the mute card names the noise its signal
measured, and a Turkish music ask withholds the music card). GAP-12 to 16 were closed on the round-5
and round-6 lanes and their markers came out in the round-6 proof run: an editor tap and its Undo no
longer lift the fence a native Save puts in front of the filler card (12), a tap taken back is
offered again even when the chat was opened while its approval was pending (13), a capability lever
pulled after the serve takes the filler card off the phone (14), a Turkish volume ask is a typed
``audio`` requirement whatever its wording (15), and a kind switched on after a set was frozen is a
card the ring holds (16). Their markers are gone, so each is now an ordinary failing check if it
ever comes back. The numbers are not reused.

A new gap is declared here, passed as ``gap=`` to the check that fails because of it, and listed
with the kinds it is about; ``serves_dead_card`` is set only when the defect lets a card be SERVED
whose tap is refused (the rule under test is that every served card executes), which makes
``kinds_proven`` report ``tap_executes: false`` for those kinds whatever the other cells did.
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


HOOK_RESERVED_AFTER_UNDO = Gap(
    "GAP-17",
    "On an account without the creative brief, Undo of a hook tap serves the hook card again and "
    "its tap is refused",
    "app/services/suggestion_serve.py (_strip_dead_cards, consent step) calls "
    "creative_copy_gate.creative_copy_problem, which folds the thread's title decisions WITHOUT "
    "the tapped cards' own (fold_creative_copy include_suggestions=False); the kinds/hook.py "
    "witness reads them WITH (copy_decided)",
    "KRIA_CREATIVE_BRIEF_ENABLED off (or the creator not on KRIA_CREATIVE_BRIEF_USER_IDS), pinned "
    "taps and chips on, nermin-matcha-update editor footage (a voice, no title). 1. GET: set S "
    "holds a hook card H. 2. Tap H: it applies, writes the title and a creative-copy decision "
    "(source suggestion). 3. Undo the draft. 4. GET: the set id is S again (Undo returned the "
    "digest to its first value and nothing else moved it, so the 24 h freeze serves S) and H is "
    "served. 5. Tap H: refused base_moved ('no longer fits'), nothing written; the refusal "
    "mints a set without the hook and the refused mark keeps it out. With the brief on, step 4 "
    "serves a NEW set without the hook (the requirement the tap wrote and Undo withdrew moved "
    "the brief version, so the digest differs): the documented 'a hook stays spent after Undo' "
    "holds there by that side effect, not by the serve asking the question the tap asks. Cost: "
    "one dead tap on a card the creator already used and undid.",
    kinds=("hook",),
    serves_dead_card=True,
)
