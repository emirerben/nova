"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell reads ``gap`` in the report, the run stays green, and
the day the check passes the run FAILS with "gap closed, remove the marker" so a marker cannot
outlive the bug it names. The text is the repro; the owner is the file to fix.

GAP-1 through 11 were closed on the integration and serve lanes (the ring holds the
served cards, a set of cards alone has an entry, Turkish taps get met receipts, a second tap on a
restating card whose approval is pending carries the typed refusal, a served restating card is one
the tap can apply, a refused tap writes no brief, Undo restores the filler setting, the pinned-taps
kill switch and the per-kind switches reach a frozen set, the mute card names the noise its signal
measured, and a Turkish music ask withholds the music card). Their markers are gone, so each is now
an ordinary failing check if it ever comes back. The numbers are not reused.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    id: str
    title: str
    owner: str
    text: str
