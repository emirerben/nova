"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell reads ``gap`` in the report, the run stays green, and
the day the check passes the run FAILS with "gap closed, remove the marker" so a marker cannot
outlive the bug it names. The text is the repro; the owner is the file to fix.

GAP-1, 2, 3, 6, 7 and 8 were closed on the integration branch (the ring now holds the served cards,
a set of cards alone has an entry, Turkish taps get met receipts, a refused tap writes no brief,
Undo restores the filler setting, and the pinned-taps kill switch reaches a frozen set). GAP-5, 10
and 11 were closed on the serve lane (a served restating card is one the tap can apply, a per-kind
switch strips a frozen set, and a Turkish music ask in the accusative withholds the music card).
Their markers are gone, so each is now an ordinary failing check if it ever comes back. The numbers
are not reused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    id: str
    title: str
    owner: str
    text: str


RESTATE_DOUBLE_TAP = Gap(
    "GAP-4",
    "Second tap on a restating card is answered without a typed signal",
    "app/kria/duplicate_send.py (answers at submit)",
    "A second tap on a filler or mute card while its approval is pending is answered by the "
    "duplicate-send guard at submit ('That's the same message as the draft waiting for "
    "you'): nothing is written, but there is no suggestion_pin signal, no pinned_stale "
    "refresh, and the card is not marked.",
)
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
