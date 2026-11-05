"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell reads ``gap`` in the report, the run stays green, and
the day the check passes the run FAILS with "gap closed, remove the marker" so a marker cannot
outlive the bug it names. The text is the repro; the owner is the file to fix.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gap:
    id: str
    title: str
    owner: str
    text: str


TR_RECEIPT = Gap(
    "GAP-3",
    "Turkish card taps get a partial receipt",
    "app/kria/editor_receipts.py (_bind_by_diff, shape.cued)",
    "A Turkish card's closed prompt ('Müziği kıs', 'Ses efektleri ekle') names no dimension "
    "the receipt judge recognises, so its requirement is un-cued and the receipt reads "
    "partial/unchecked with the change listed ('Müzik → 30%') instead of met/checked. The "
    "same English cards read met.",
)
RESTATE_DOUBLE_TAP = Gap(
    "GAP-4",
    "Second tap on a restating card is answered without a typed signal",
    "app/kria/duplicate_send.py (answers at submit)",
    "A second tap on a filler or mute card while its approval is pending is answered by the "
    "duplicate-send guard at submit ('That's the same message as the draft waiting for "
    "you'): nothing is written, but there is no suggestion_pin signal, no pinned_stale "
    "refresh, and the card is not marked.",
)
RESTATE_NOT_READY = Gap(
    "GAP-5",
    "A served restating card is refused at the tap",
    "app/services/suggestions/kinds/filler_words.py (availability) / "
    "app/tasks/kria_runtime.py (approval readiness)",
    "On the east-run-brief replay (live requirements the recorded plan does not cover: "
    "plan_shape.has_per_clip_text_lane is false) the filler card is served and its tap is "
    "refused requirements_not_ready with a choice question about an unrelated requirement "
    "('Bunlar olmadan hazırla'). A served card must be one the tap can apply.",
)
REFUSED_BRIEF = Gap(
    "GAP-6",
    "A refused restating tap still commits its brief update",
    "app/tasks/kria_runtime.py (~3160, "
    "_complete_response_turn(brief_updates=planned.brief_updates))",
    "The requirements_not_ready refusal of a pinned tap commits planned.brief_updates, so a "
    "refused filler card leaves a live 'speech_cleanup: true' requirement (r5) in the brief "
    "although nothing was applied (speech_cleanup_enabled stays false). KRI-129 then treats "
    "it as something the creator asked for.",
)
MUTE_NO_REASON = Gap(
    "GAP-9",
    "A mute card carries no measured reason",
    "app/services/suggestion_chips.py _measured_evidence (no mute_clip branch)",
    "_measured_evidence has a branch for filler_words and duplicates only, so a mute card reads "
    "'Clip 3' as its reason with no evidence line: the wind the signal measured is never shown, "
    "and an agent reason about wind is dropped by validate_why (no footage word). The plan's "
    "card is 'Wind in clip 3'.",
)
