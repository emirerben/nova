"""The one sound-effect moment chooser (KRI-1561).

`choose_sfx` takes a general ask ("add sound effects where they fit") plus the
timed evidence an edit has, and returns either placements or a named reason it
declined. It never invents a time: the agent proposes, `resolve_sfx_suggestions`
disposes (known effect, voice-free, on a supplied mark, spaced, capped).

Evidence rules:

  - words and visual moments are evidence; pauses ride along with words;
  - beats are only a preference hint to the agent. They are never a mark, so a
    music grid with no speech and no visual moments declines instead of
    producing evenly spaced effects;
  - a named cue ("a whoosh when I say X") is NOT handled here. It stays on
    `strategy.reaction_beats` and its deterministic word grounding.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.agents.sfx_placement import (
    _MAX_EFFECTS,
    MAX_SFX_PLACEMENTS,
    SfxCatalogEffect,
    SfxPlacementInput,
    SfxPlacementOutput,
)
from app.services.sfx_autoplace import resolve_sfx_suggestions
from app.services.sfx_catalog import placement_catalog


class SfxLimit(StrEnum):
    """Why effects were not placed. Each member gets its TR/EN line in the copy layer."""

    flag_off = "flag_off"
    feature_unverified = "feature_unverified"
    empty_catalog = "empty_catalog"
    multi_speaker = "multi_speaker"
    format_unsupported = "format_unsupported"
    no_timed_evidence = "no_timed_evidence"
    creator_said_no = "creator_said_no"
    # KRI-1598: a guided montage plays the effect the creator named and adds no chosen ones
    # (its named-effect check refuses any other row on the next Save).
    named_effect = "named_effect"
    nothing_fit = "nothing_fit"


@dataclass(frozen=True)
class SfxAsk:
    request_text: str
    max_placements: int = MAX_SFX_PLACEMENTS

    def __post_init__(self) -> None:
        if not 1 <= self.max_placements <= MAX_SFX_PLACEMENTS:
            raise ValueError(f"max_placements must be 1..{MAX_SFX_PLACEMENTS}")


@dataclass(frozen=True)
class SfxEvidence:
    """Timed facts about the edit, in output-timeline seconds."""

    words: tuple[Mapping[str, Any], ...] = ()
    pauses: tuple[Mapping[str, Any], ...] = ()
    moments: tuple[Mapping[str, Any], ...] = ()
    beats_s: tuple[float, ...] = ()
    duration_s: float = 0.0


@dataclass(frozen=True)
class SfxPlacement:
    id: str
    effect_id: str
    name: str
    at_s: float
    gain: float
    anchor: str
    reason: str


@dataclass(frozen=True)
class Placed:
    placements: tuple[SfxPlacement, ...]


@dataclass(frozen=True)
class Declined:
    limit: SfxLimit


SfxChoice = Placed | Declined

AgentRunner = Callable[[SfxPlacementInput], SfxPlacementOutput]


def _marks(evidence: SfxEvidence) -> list[float]:
    """Every time a placement may land on. Beats are deliberately absent."""
    marks: list[float] = []
    for w in evidence.words:
        marks.append(float(w.get("end_s", w.get("e", 0.0))))
        marks.append(float(w.get("start_s", w.get("s", 0.0))))
    for p in evidence.pauses:
        marks.append(float(p.get("s", 0.0)))
    for m in evidence.moments:
        if str(m.get("description") or "").strip():
            marks.extend((float(m.get("start_s", 0.0)), float(m.get("end_s", 0.0))))
    return [m for m in marks if math.isfinite(m)]


def choose_sfx(
    ask: SfxAsk,
    evidence: SfxEvidence,
    catalog: Sequence[Mapping[str, Any]],
    *,
    run_agent: AgentRunner,
) -> SfxChoice:
    """Place effects for a general ask, or say why not.

    `catalog` rows are the `_load_glossary` shape (id, name, audio_gcs_path,
    contains_voice, role_tags, duration_s). `run_agent` runs the placement
    agent, so callers own the model client and the run context.
    """
    playable = [row for row in catalog if row.get("audio_gcs_path")]
    entries = placement_catalog(playable, _MAX_EFFECTS)
    if not entries:
        return Declined(SfxLimit.empty_catalog)
    marks = _marks(evidence)
    if evidence.duration_s <= 0 or not marks:
        return Declined(SfxLimit.no_timed_evidence)

    agent_out = run_agent(
        SfxPlacementInput(
            words=[dict(w) for w in evidence.words],
            pauses=[dict(p) for p in evidence.pauses],
            moments=[dict(m) for m in evidence.moments],
            effects=[
                SfxCatalogEffect(
                    effect_id=e.id,
                    name=e.name,
                    role_tags=list(e.role_tags),
                    duration_s=e.duration_s,
                )
                for e in entries
            ],
            duration_s=evidence.duration_s,
            beats_s=list(evidence.beats_s),
            request_text=ask.request_text,
        )
    )
    accepted = resolve_sfx_suggestions(
        agent_out.placements,
        [dict(row) for row in playable],
        evidence.duration_s,
        "",
        allowed_marks=marks,
    )[: ask.max_placements]
    if not accepted:
        return Declined(SfxLimit.nothing_fit)
    return Placed(
        tuple(
            SfxPlacement(
                id=row["id"],
                effect_id=row["effect_id"],
                name=row["name"],
                at_s=row["at_s"],
                gain=row["gain"],
                anchor=row["anchor"],
                reason=row["reason"],
            )
            for row in accepted
        )
    )
