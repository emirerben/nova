"""The named gaps the end-to-end run found in code this lane does not own.

Each is a check that fails honestly: the cell reads ``gap`` in the report, the run stays green, and
the day the check passes the run FAILS with "gap closed, remove the marker" so a marker cannot
outlive the bug it names. The text is the repro; the owner is the file to fix.

GAP-1, 2, 3, 6, 7 and 8 were closed on the integration branch (the ring now holds the served cards,
a set of cards alone has an entry, Turkish taps get met receipts, a refused tap writes no brief,
Undo restores the filler setting, and the pinned-taps kill switch reaches a frozen set). Their
markers are gone, so each is now an ordinary failing check if it ever comes back. The numbers are
not reused.
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
FROZEN_KIND_SWITCH = Gap(
    "GAP-10",
    "A per-kind kill switch does not hide a card from a frozen set",
    "app/services/suggestion_serve.py (serve_suggestion_set / _serve_core: only the pinned-taps "
    "master flag is re-applied to a frozen set)",
    "Pulling KRIA_SUGGESTION_{HOOK_COPY,DUPLICATES,FILLER_WORDS,MUTE_CLIP}_ENABLED after a set was "
    "served leaves that kind's card on the phone for the rest of the 24 h freeze. A tap on it is "
    "refused in words with the typed signal and writes nothing (the safe half holds), and the "
    "refusal mints a new set without the card, so the cost is one dead tap per creator. The master "
    "switch (pinned taps) does strip a frozen set.",
)
TR_MUSIC_ACCUSATIVE = Gap(
    "GAP-11",
    "A Turkish music request in the accusative does not withhold the music card (KRI-129)",
    "app/services/suggestions/brief_asks.py (MUSIC.words has 'muzik', not 'muzig')",
    "A live requirement whose description says 'müziği çok kıs' (the music, accusative: the k "
    "softens to ğ) folds to 'muzigi cok kis'; the music dimension only knows 'muzik', 'sarki', "
    "'sesi kis' and 'sesini kis', so the card 'Müziği kıs' is still offered over a creator who "
    "just asked for exactly that. 'müzik olmasın' and 'sesi kıs lütfen' do withhold it.",
)
