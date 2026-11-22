"""Chosen sound effects for a phone Talking / Voiceover edit (KRI-1561).

The worker hands this module what the edit has (timed words on the lane clock, the clock's
length, the sounds a named cue already placed) and gets back ordinary `SubtitledSoundEffect`
lane rows plus a receipt. It owns no model client and no database: the worker passes the
catalogue loader and the agent runner, so every branch runs in a unit test.

A general ask is served here; a named moment ("a whoosh when I say X") is a reaction beat and
keeps its own deterministic grounding. A chosen effect never lands within the spacing of a named
one, so a named cue always wins its slot.

The receipt (`variant["phone_sfx_choice_receipt"]`) says what happened in codes, never in a
language: ``declined`` is an `SfxLimit` value, and the reply line is written where it is read.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog
from pydantic import ValidationError

from app.agents._schemas.sfx_intent import SfxAskIntent
from app.services.sfx_autoplace import MIN_SPACING_S
from app.services.sfx_chooser import (
    AgentRunner,
    Declined,
    Placed,
    SfxAsk,
    SfxEvidence,
    SfxLimit,
    choose_sfx,
)
from app.services.speech_map import build_speech_map

log = structlog.get_logger()

RECEIPT_FIELD = "phone_sfx_choice_receipt"
RECEIPT_VERSION = 1
LANE_ID_PREFIX = "ask-sfx"


def read_sfx_ask(strategy: Mapping[str, Any] | None) -> SfxAskIntent | None:
    """The strategy's general sound-effect ask, or None (absent, empty or not valid)."""
    raw = strategy.get("sfx_ask") if isinstance(strategy, Mapping) else None
    if not raw:
        return None
    try:
        return SfxAskIntent.model_validate(raw)
    except ValidationError:
        log.warning("phone_sfx_choice.invalid_ask")
        return None


def _receipt(
    placed: Sequence[Mapping[str, Any]],
    declined: SfxLimit | None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": RECEIPT_VERSION,
        "asked": True,
        "placed": [dict(row) for row in placed],
        "declined": declined.value if declined is not None else None,
    }
    if error:
        out["error"] = error[:160]
    return out


def demote_receipt(receipt: dict[str, Any], dropped_ids: Sequence[str]) -> dict[str, Any]:
    """The receipt after some chosen rows were dropped (a catalogue row gone, a compile refusal):
    they are no longer placed, and nothing left means nothing fit."""
    gone = set(dropped_ids)
    out = copy.deepcopy(receipt)
    out["placed"] = [row for row in receipt.get("placed") or [] if row.get("id") not in gone]
    if not out["placed"] and out.get("declined") is None:
        out["declined"] = SfxLimit.nothing_fit.value
    return out


class PhoneSfxChoice:
    """``effects`` are `SubtitledSoundEffect` rows on the lane clock, still to be resolved."""

    __slots__ = ("effects", "receipt")

    def __init__(self, effects: tuple[Any, ...], receipt: dict[str, Any]) -> None:
        self.effects = effects
        self.receipt = receipt


def choose_for_phone(
    ask: SfxAskIntent,
    *,
    limit: SfxLimit | None,
    words: Sequence[Mapping[str, Any]] | None,
    duration_s: float,
    catalog_loader: Callable[[], list[dict]],
    run_agent: AgentRunner,
    taken_at_s: Sequence[float] = (),
) -> PhoneSfxChoice:
    """Choose lane rows for ``ask``, or a receipt that says why none were."""
    from app.pipeline.phone_subtitled_lanes import (  # noqa: PLC0415
        SubtitledSoundEffect,
        sfx_path_is_playable,
    )

    if limit is not None:
        return PhoneSfxChoice((), _receipt((), limit))
    speech = build_speech_map(list(words or []), duration_s, "task")
    if speech is None:
        return PhoneSfxChoice((), _receipt((), SfxLimit.no_timed_evidence))
    # The catalogue loads only now: a decline above never touches the database. A database
    # error is not a decline; it propagates so the task retries.
    catalog = [
        row
        for row in catalog_loader()
        if sfx_path_is_playable(str(row.get("audio_gcs_path") or ""))
        and str(row.get("audio_gcs_path") or "").startswith(f"sound-effects/{row.get('id')}/")
    ]
    try:
        choice = choose_sfx(
            SfxAsk(request_text=ask.request_text, max_placements=ask.max_placements),
            SfxEvidence(
                words=tuple(speech["words"]),
                pauses=tuple(speech["pauses"]),
                duration_s=float(duration_s),
            ),
            catalog,
            run_agent=run_agent,
        )
    except Exception as exc:  # noqa: BLE001 - optional effects never fail the render
        log.warning("phone_sfx_choice.agent_failed", error=str(exc)[:200])
        return PhoneSfxChoice((), _receipt((), SfxLimit.nothing_fit, error=str(exc)))
    if isinstance(choice, Declined):
        return PhoneSfxChoice((), _receipt((), choice.limit))
    assert isinstance(choice, Placed)
    kept = [
        placement
        for placement in choice.placements
        if all(abs(placement.at_s - float(taken)) >= MIN_SPACING_S for taken in taken_at_s)
    ]
    if not kept:
        return PhoneSfxChoice((), _receipt((), SfxLimit.nothing_fit))
    effects = tuple(
        SubtitledSoundEffect(
            id=f"{LANE_ID_PREFIX}-{index}",
            catalog_id=placement.effect_id,
            at_s=placement.at_s,
            volume=placement.gain,
        )
        for index, placement in enumerate(kept)
    )
    placed = [
        {"id": row.id, "effect_id": row.catalog_id, "at_s": row.at_s, "name": placement.name}
        for row, placement in zip(effects, kept, strict=True)
    ]
    return PhoneSfxChoice(effects, _receipt(placed, None))
