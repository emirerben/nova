"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell or negative reads ``gap`` in the report, the run
stays green, and the day the check passes the run FAILS with "gap closed, remove the marker" so a
marker cannot outlive the bug it names. The text is the repro; the owner is the file to fix.

None is open. GAP-1 to 11 were closed on the integration and serve lanes (the ring holds the served
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
