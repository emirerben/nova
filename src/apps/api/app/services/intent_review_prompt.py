"""Builds the review prompt, response schema and server catalog from one frozen render packet.

Pure over the packet and the decoded frames: it opens no database session and reads nothing
the packet does not already hold, so the catalog it stores is exactly what was approved.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.agents._schemas.intent_review import response_schema
from app.agents.intent_review import PROMPT_VERSION, IntentReviewInput, render_review_prompt
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument
from app.kria.intent_review_catalog import FrameRef, build_review_catalog
from app.services.creator_render_contract import CreatorRenderContract


@dataclass(frozen=True)
class FrameView:
    frame_id: str
    jpeg: bytes
    sha256: str
    width: int
    height: int
    timestamp_s: float
    anchor_ref: str | None


@dataclass(frozen=True)
class BuiltReview:
    prompt: str
    response_schema: dict[str, Any]
    catalog: dict[str, Any]
    prompt_version: str


class NothingToReview(Exception):
    """No requirement the frames could judge: finish without spending anything."""


def build_review(
    packet: dict[str, Any],
    frames: Sequence[FrameView],
    *,
    rendered_duration_s: float | None = None,
) -> BuiltReview:
    catalog = build_review_catalog(
        packet,
        KriaDraftDocument.model_validate(json.loads(packet["approved_plan_json"])),
        CreatorRenderContract.model_validate_json(packet["contract_json"]),
        BriefBinding.model_validate_json(packet["binding_json"]),
        [
            FrameRef(
                frame_id=frame.frame_id,
                t_ms=round(frame.timestamp_s * 1000),
                sha256=frame.sha256,
                w=frame.width,
                h=frame.height,
                anchor_ref=frame.anchor_ref,
            )
            for frame in frames
        ],
        rendered_duration_s,
    )
    if not catalog.requirements:
        raise NothingToReview
    return BuiltReview(
        prompt=render_review_prompt(
            IntentReviewInput(original_request=packet["original_request"], catalog=catalog)
        ),
        response_schema=response_schema(catalog.requirement_ids, catalog.frame_ids),
        catalog=catalog.model_dump(mode="json"),
        prompt_version=PROMPT_VERSION,
    )
