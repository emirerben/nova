"""``generated_overlay``: one generated image, made only after the creator agrees to its price.

The action is ``AsyncGenerateAction``: a prompt the server derived from the creator's own brief
and the price the card shows first. A tap spends (``planner.plan_pinned_suggestion_turn`` ->
``generated_media.execute_generated_media``, idempotent on ``event_id``) and never edits the
video. The phone previews the image and the creator accepts it in the editor, so a wrong image
costs the quoted price and nothing else. There is no ``add_overlay`` compile here on purpose: a
phone Save refuses a photo Kria did not pin for the edit
(``phone_subtitled_editor._overlays_from_sections``), and placement belongs to the native editor.

**Loader input** (``ctx.kind_inputs["generated_overlay"]``, built in ``suggestion_serve``):

* ``allowed`` (required, else the kind never offers): the generated-images gate the route also
  asks (``generated_media.generated_images_available``: native client, ledger, budget, billing
  organisation, ``visualBlocks`` verified on the phone).
* ``subject`` (required, else the kind never offers): the plan item's idea, the one line the video
  is about. The image prompt is built from it and nothing the agent wrote.
* ``existing`` (optional): the item already has a generated image that was not rejected, so a
  second offer would only nag.

The card is offered only to clients that implement the consent sheet (``MIN_PROTOCOL``): an older
build would send the tap, and spend, on one touch.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.client_protocol import GENERATED_CARD_CONSENT_PROTOCOL
from app.services.suggestions.builders import PHONE
from app.services.suggestions.context import SuggestionContext, Target
from app.services.suggestions.models import (
    Action,
    AsyncGenerateAction,
    SuggestionKind,
    WitnessResult,
)

INPUT_KEY = "generated_overlay"
MIN_PROTOCOL = GENERATED_CARD_CONSENT_PROTOCOL
# What the card tells the creator it costs. A ceiling: one low-quality portrait image is metered
# from provider usage after the fact (`generated_usage_cost`) and has to stay under it.
QUOTED_COST_USD = 0.04
# The phone derives the same id (`GeneratedCardEvent.id`), so it can find the image without being
# told: the tap names the set and the card, and the operation is keyed on this.
EVENT_NAMESPACE = uuid.UUID("5b0c1f7e-2d3a-5c4b-9a61-7e2f0d8b4c19")
MAX_SUBJECT_CHARS = 200

_EVIDENCE = {
    "en": "You review it before it goes in",
    "tr": "Videoya girmeden önce sen onaylarsın",
}


def event_id(set_id: str, suggestion_id: str) -> uuid.UUID:
    """The client event id of the one image this card can make: a second tap finds the first."""
    return uuid.uuid5(EVENT_NAMESPACE, f"{set_id}:{suggestion_id}")


def _inputs(ctx: SuggestionContext) -> Mapping[str, Any]:
    raw = ctx.kind_inputs.get(INPUT_KEY)
    return raw if isinstance(raw, Mapping) else {}


def _prompt(ctx: SuggestionContext) -> str | None:
    raw = _inputs(ctx).get("subject")
    subject = " ".join(raw.split())[:MAX_SUBJECT_CHARS] if isinstance(raw, str) else ""
    if not subject:
        return None
    return f"A simple, bright, natural-looking image that fits a short video about: {subject}"


def _availability(ctx: SuggestionContext) -> bool:
    return (
        ctx.state == "ready"
        and ctx.client_protocol is not None
        and ctx.client_protocol >= MIN_PROTOCOL
        and _inputs(ctx).get("allowed") is True
        and _prompt(ctx) is not None
    )


def _present(ctx: SuggestionContext) -> bool:
    return _inputs(ctx).get("existing") is True


def _build_action(
    ctx: SuggestionContext, targets: Sequence[Target], copy: str | None
) -> Action | None:
    prompt = _prompt(ctx)
    if prompt is None or not _availability(ctx) or _present(ctx):
        return None
    return AsyncGenerateAction(
        spec={"capability": "image", "prompt": prompt}, cost_usd=QUOTED_COST_USD
    )


def _witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    """Re-prove the offer from the action itself: the spec is exactly one image prompt, the price
    is the one the card quoted, and the kind would still offer it on the facts as they are."""

    def refuse(reason: str) -> WitnessResult:
        return WitnessResult(ok=False, tier="spend", reason=reason)

    if not isinstance(action, AsyncGenerateAction):
        return refuse("not_async_generate")
    spec = action.spec
    if set(spec) != {"capability", "prompt"} or spec.get("capability") != "image":
        return refuse("spec_changed")
    if action.cost_usd != QUOTED_COST_USD:
        return refuse("price_changed")
    if not _availability(ctx):
        return refuse("not_available")
    if _present(ctx):
        return refuse("already_generated")
    if spec.get("prompt") != _prompt(ctx):
        return refuse("prompt_changed")
    return WitnessResult(ok=True, tier="spend")


def evidence_lines(ctx: SuggestionContext, targets: Sequence[Target]) -> list[str]:
    return [_EVIDENCE["tr" if ctx.language.casefold().startswith("tr") else "en"]]


KIND = SuggestionKind(
    kind="generated_overlay",
    states=frozenset({"ready"}),
    required_signals=(),
    availability=_availability,
    already_present=_present,
    build_action=_build_action,
    witness=_witness,
    surfaces=PHONE,
    flag="kria_suggestion_generated_overlay_enabled",
    min_protocol=MIN_PROTOCOL,
)
