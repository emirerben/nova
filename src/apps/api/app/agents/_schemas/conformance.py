"""Schemas for nova.plan.conformance_feedback — conformance verdict at clip-attach time."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ConformanceInput(BaseModel):
    """What was asked vs what was uploaded.

    All inputs are text-only (pre-computed digests) — no new Gemini file upload.
    """

    # The structured shot list the creator was given at plan time.
    # Each dict: {what, how, duration_s}. Caller passes raw list-of-dict from filming_guide.
    filming_guide: list[dict]

    # Subset of ClipMetadataOutput for the single attached clip.
    # Keys used: detected_subject, content_type, audio_type, hook_text,
    # transcript (first 300 chars), visual_density, composition_note.
    clip_digest: dict

    # The plan item's top-level context (theme + idea) so the agent knows
    # what "on-brief" means even if the shot list is empty.
    theme: str
    idea: str
    # Optional creator-provided note about the analyzed clip ("this is a famous
    # vegan restaurant in Buenos Aires"). UNTRUSTED user free-text — rendered as
    # DATA; it can legitimately upgrade a verdict. "" = no note.
    user_context: str = ""


class ConformanceOutput(BaseModel):
    """The agent's verdict on whether the attached clip matches the shot list."""

    verdict: Literal["on_track", "minor_drift", "off_brief"]
    # 0.0 = wild guess, 1.0 = certain.
    confidence: float = Field(..., ge=0.0, le=1.0)
    # One-liner: "this looks like X instead of Y; engagement risk Z".
    summary: str
    # Max 3 items — specific mismatches between shooting guide and uploaded clip.
    mismatches: list[str] = Field(default_factory=list, max_length=3)
    # Max 3 items — actionable re-shoot tips.
    suggestions: list[str] = Field(default_factory=list, max_length=3)
    # Echo-back guard (prod wrong-brief incident): the agent copies the theme it
    # actually evaluated. The task asserts it matches the item's theme before
    # persisting — a mismatch means contaminated inputs, and the verdict is
    # discarded rather than shown. Also rendered in the UI as the evidence line
    # (READ AGAINST: "<theme>").
    evaluated_theme: str = ""
