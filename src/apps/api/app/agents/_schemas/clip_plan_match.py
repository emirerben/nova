"""Schemas for nova.plan.clip_plan_matcher — assign seed clips to plan items.

The activation seed runs this once on a user's uploaded clip batch (already
analyzed by ``clip_metadata``) against their content-plan items. The matcher
returns best-fit ``(item_id, clip_gcs_path)`` assignments; the orchestrator
auto-generates the top picks so the user sees a finished video before any
per-item themed-upload "homework."

Differences from ``music_matcher`` (the sibling ranking agent), all deliberate:
  - The join key is ``clip_gcs_path`` (the seed clip's real GCS path), not an
    opaque id. At matcher time the seed clips have paths, not Gemini file refs,
    so using the path directly removes a lookup table AND lets ``parse()``
    cross-validate against the input set. The path must round-trip VERBATIM —
    it is never sanitized (it is data we must echo exactly), only validated by
    membership.
  - ``assignments`` may be EMPTY. A clip batch that fits no plan item is a
    legitimate best-effort outcome (the user keeps their full plan and does
    per-item uploads). Empty is NOT a refusal here — ``parse()`` raises only on
    unparseable JSON, never on an empty-but-valid list.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClipSummary(BaseModel):
    """Per-clip digest the matcher sees. Derived from ``clip_metadata`` output by
    the activation task — the matcher never re-runs vision."""

    # The join key — echoed verbatim in assignments, validated by membership.
    clip_gcs_path: str = Field(min_length=1)
    hook_text: str = ""
    hook_score: float = Field(default=0.0, ge=0.0, le=10.0)
    detected_subject: str = ""
    transcript_excerpt: str = ""
    duration_s: float = Field(default=0.0, ge=0.0)


class PlanItemSummary(BaseModel):
    """One content-plan item the matcher can assign a clip to."""

    item_id: str = Field(min_length=1)
    theme: str = ""
    idea: str = ""
    filming_suggestion: str = ""


class ClipPlanMatcherInput(BaseModel):
    clips: list[ClipSummary] = Field(min_length=1)
    items: list[PlanItemSummary] = Field(min_length=1)
    # Upper bound on how many items the seed should activate. The matcher returns
    # at most this many assignments (best, highest-scoring first).
    max_assignments: int = Field(default=2, ge=1, le=7)


class Assignment(BaseModel):
    item_id: str = Field(min_length=1)
    clip_gcs_path: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=10.0)
    rationale: str = Field(min_length=1)


class ClipPlanMatcherOutput(BaseModel):
    # May be empty — a seed batch that fits nothing is a valid non-refusal result.
    assignments: list[Assignment] = Field(default_factory=list)
