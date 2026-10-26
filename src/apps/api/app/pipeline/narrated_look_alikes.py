"""Look-alike clips on one named cue of a phone Voiceover edit (KRI-962).

Prod thread 99627FBC (Don Julio walk-in N1, job bdb0c798): "the DON JULIO sign while I say
'Don Julio'" resolved to two clips at the same confidence (0.9): the window sign seen from the
street, and Atakan at the brass plaque talking to the camera. Both reached the alignment agent
(``app.agents.narrated_clip_alignment``) with the same ``creator_label``, so nothing told them
apart. The agent opened the video on Atakan (silent under the voice) and put the real sign on
the unrelated last line "One question before we eat.", and nothing said so.

A *look-alike set* is two or more clips of the edit that carry exactly the same creator labels,
all from resolved ``include`` asks: the labels cannot tell the alignment agent which clip the
creator meant. A clip with any other label (a group, caption or order ask, a photo cue), a
pinned or sequenced clip, and a clip a clip-sound ask plays with its own sound are never in a
set: their seat is someone else's decision.

The tie-break reads recorded fields only, never the creator's words (KRI-667):

1. The resolver's confidence. A clip it rated higher on any shared label is never demoted.
2. Under a voiceover, a clip whose analysis says someone speaks to the camera
   (``people.speaks_to_camera``, ``speech.to_camera`` or ``content_type == "talking_head"``)
   is the weaker picture for a named thing: the person's mouth moves with no sound and the shot
   is about them. It yields the cue to a look-alike the analysis says shows nobody talking,
   rated at least as high on every shared label. Unknown analysis (none, degraded, failed)
   never leads and is never demoted.

Then, deterministically:

* ``labels_for_alignment``: a demoted clip loses the shared label in the agent's input, so the
  label names the lead alone; the clip is then placed by its description like any other clip.
* ``reseat``: if the agent still opened the set on a demoted clip, that clip and the earliest
  lead that outranks it swap seats, when both fill their new seat (``fits``: the phone can
  slow a short clip only where it retimes, and never below ``MIN_SWAP_RATE``). Every window
  and every other clip's seat stay exactly as the agent chose them.
* ``unbroken_ties``: a set nothing here could rank whose clips ended up apart on screen. It,
  and a swap the clip lengths blocked, get one render note each instead of a silent guess.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.kria.reply_language import say

TALKING_HEAD = "talking_head"
INCLUDE = "include"
# A swap never slows a clip below half speed to fill its new seat (the cloud narrated
# assembler already warns at 0.4x, ``narrated_assembler._SLOW_WARN_FACTOR``).
MIN_SWAP_RATE = 0.5
_NOTE_LABEL_CHARS = 80


@dataclass(frozen=True)
class CueClip:
    """One clip of the edit, as the look-alike tie-break sees it."""

    clip_id: str
    # The creator's labels for the clip, in brief order (what the alignment agent is shown).
    labels: tuple[str, ...]
    # True when every one of those labels comes from a resolved ``include`` ask.
    include_only: bool
    # Resolver confidence per label (0.0 when the assignment carried none).
    confidence: Mapping[str, float] = field(default_factory=dict)
    # From the clip's analysis: True / False, or None when the analysis is unknown.
    talks_to_camera: bool | None = None


@dataclass(frozen=True)
class LookAlikeSet:
    labels: tuple[str, ...]
    clips: tuple[CueClip, ...]

    @property
    def clip_ids(self) -> tuple[str, ...]:
        return tuple(clip.clip_id for clip in self.clips)

    def _rated_at_least(self, clip: CueClip, other: CueClip) -> bool:
        return all(
            clip.confidence.get(label, 0.0) >= other.confidence.get(label, 0.0)
            for label in self.labels
        )

    def _rated_above(self, clip: CueClip, other: CueClip) -> bool:
        return self._rated_at_least(clip, other) and any(
            clip.confidence.get(label, 0.0) > other.confidence.get(label, 0.0)
            for label in self.labels
        )

    def leads_over(self, clip: CueClip) -> list[CueClip]:
        """Look-alikes that outrank ``clip`` on the talking-to-camera rule, in input order:
        nobody talks in them and the resolver rated them at least as high. Empty unless
        ``clip`` is a known talking-to-camera clip."""
        if clip.talks_to_camera is not True:
            return []
        return [
            other
            for other in self.clips
            if other.talks_to_camera is False and self._rated_at_least(other, clip)
        ]

    @property
    def demoted(self) -> frozenset[str]:
        """The clips some look-alike outranks on the talking-to-camera rule."""
        return frozenset(clip.clip_id for clip in self.clips if self.leads_over(clip))

    def outranks(self, clip: CueClip, other: CueClip) -> bool:
        """``clip`` is the better match for the shared cue than ``other``."""
        return clip in self.leads_over(other) or self._rated_above(clip, other)


def look_alike_sets(clips: Iterable[CueClip]) -> list[LookAlikeSet]:
    """Clips with identical, include-only creator labels, grouped (sets of two or more)."""
    by_labels: dict[tuple[str, ...], list[CueClip]] = {}
    for clip in clips:
        if clip.labels and clip.include_only:
            by_labels.setdefault(clip.labels, []).append(clip)
    return [
        LookAlikeSet(labels=labels, clips=tuple(members))
        for labels, members in by_labels.items()
        if len(members) >= 2
    ]


def labels_for_alignment(
    labels_by_clip: Mapping[str, str], sets: Sequence[LookAlikeSet]
) -> dict[str, str]:
    """The alignment agent's ``creator_label`` per clip, with demoted clips' labels removed.

    A demoted clip carries exactly the set's labels, so it is left with none."""
    demoted = {clip_id for one in sets for clip_id in one.demoted}
    return {clip_id: label for clip_id, label in labels_by_clip.items() if clip_id not in demoted}


def _placed(one: LookAlikeSet, position: Mapping[str, int]) -> list[CueClip]:
    return sorted(
        (clip for clip in one.clips if clip.clip_id in position),
        key=lambda clip: position[clip.clip_id],
    )


def reseat(
    order: Sequence[str],
    sets: Sequence[LookAlikeSet],
    *,
    fits: Callable[[str, int], bool],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """``order`` (one clip id per seat, screen order) with each set opened by a lead.

    ``fits(clip_id, seat)`` says whether that clip can fill that seat. Returns the new order,
    one row per swap (only the two clips trade seats), and one row per set whose swap no lead
    could make because of clip lengths (1-based screen positions, for the render note).
    """
    seats = list(order)
    swaps: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for one in sets:
        position = {clip_id: i for i, clip_id in enumerate(seats)}
        placed = _placed(one, position)
        if len(placed) < 2:
            continue
        first = placed[0]
        leads = sorted(
            (clip for clip in one.leads_over(first) if clip.clip_id in position),
            key=lambda clip: position[clip.clip_id],
        )
        if not leads:
            continue
        a = position[first.clip_id]
        lead = next(
            (
                clip
                for clip in leads
                if fits(clip.clip_id, a) and fits(first.clip_id, position[clip.clip_id])
            ),
            None,
        )
        if lead is None:
            blocked.append(
                {
                    "labels": list(one.labels),
                    "lead": leads[0].clip_id,
                    "first": first.clip_id,
                    "lead_position": position[leads[0].clip_id] + 1,
                    "first_position": a + 1,
                }
            )
            continue
        b = position[lead.clip_id]
        seats[a], seats[b] = seats[b], seats[a]
        swaps.append(
            {
                "labels": list(one.labels),
                "seat": a,
                "lead": lead.clip_id,
                "moved": first.clip_id,
                "moved_to_seat": b,
            }
        )
    return seats, swaps, blocked


def unbroken_ties(order: Sequence[str], sets: Sequence[LookAlikeSet]) -> list[dict[str, Any]]:
    """Sets whose first clip on screen nothing here ranks above the others, placed apart.

    Each row carries the labels and the clips' 1-based screen positions. A set whose clips sit
    next to each other shows all of them around the same words, so it needs no note. A set
    opened by a demoted clip is ``reseat``'s (swapped, or blocked with its own note).
    """
    position = {clip_id: i for i, clip_id in enumerate(order)}
    rows: list[dict[str, Any]] = []
    for one in sets:
        placed = _placed(one, position)
        if len(placed) < 2 or placed[0].clip_id in one.demoted:
            continue
        first = placed[0]
        if all(one.outranks(first, other) for other in placed[1:]):
            continue
        seats = [position[clip.clip_id] for clip in placed]
        if seats[-1] - seats[0] + 1 == len(seats):
            continue
        rows.append(
            {
                "labels": list(one.labels),
                "clip_ids": [clip.clip_id for clip in placed],
                "positions": [seat + 1 for seat in seats],
            }
        )
    return rows


def _label(labels: Sequence[str]) -> str:
    return " / ".join(labels)[:_NOTE_LABEL_CHARS].strip()


def _numbers(positions: Sequence[int], conjunction: str, *, suffix: str = "") -> str:
    shown = [f"{p}{suffix}" for p in positions]
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} {conjunction} {shown[-1]}"


def tie_note(labels: Sequence[str], positions: Sequence[int]) -> str:
    """The render note for an unbroken tie, in the chat's language."""
    label = _label(labels)
    first = positions[0]
    if len(positions) == 2:
        return say(
            en=(
                f'Clips {positions[0]} and {positions[1]} both look like "{label}", and I '
                f"couldn't tell which one you meant, so clip {first} comes first."
            ),
            tr=(
                f'{positions[0]}. ve {positions[1]}. klibin ikisi de "{label}" gibi görünüyor; '
                f"hangisini kastettiğini anlayamadım, bu yüzden önce {first}. klip geliyor."
            ),
        )
    return say(
        en=(
            f'Clips {_numbers(positions, "and")} all look like "{label}", and I couldn\'t '
            f"tell which one you meant, so clip {first} comes first."
        ),
        tr=(
            f'{_numbers(positions, "ve", suffix=".")} klipler "{label}" gibi görünüyor; '
            f"hangisini kastettiğini anlayamadım, bu yüzden önce {first}. klip geliyor."
        ),
    )


def blocked_note(labels: Sequence[str], *, lead_position: int, first_position: int) -> str:
    """The render note for a lead too short to take the cue's seat, in the chat's language."""
    label = _label(labels)
    return say(
        en=(
            f'Clip {lead_position} looks most like "{label}", but it is too short to fill '
            f"clip {first_position}'s place, so clip {first_position} comes first."
        ),
        tr=(
            f'{lead_position}. klip "{label}" ile en çok eşleşiyor, ama {first_position}. '
            f"klibin yerini dolduracak kadar uzun değil; bu yüzden önce {first_position}. "
            "klip geliyor."
        ),
    )


__all__ = [
    "INCLUDE",
    "MIN_SWAP_RATE",
    "TALKING_HEAD",
    "CueClip",
    "LookAlikeSet",
    "blocked_note",
    "labels_for_alignment",
    "look_alike_sets",
    "reseat",
    "tie_note",
    "unbroken_ties",
]
