"""nova.plan.clip_plan_matcher — assign a user's seed clips to plan items.

The content-plan activation seed runs this once: after the plan is ready the user
uploads one batch of recent clips, the activation task analyzes each with
``clip_metadata``, and this agent decides which clip best serves which plan item.
The orchestrator auto-generates the top picks so the user gets an instant first
video before any per-item themed upload.

Text-only (no vision re-run): consumes the ``clip_metadata`` digests plus each
plan item's theme/idea/filming_suggestion, returns scored assignments.

Hard rules enforced in ``parse()``:
  - every ``clip_gcs_path`` MUST appear verbatim in the input clips (echoed, not
    sanitized — it is a path we have to use as-is; hallucinated paths are dropped)
  - every ``item_id`` MUST appear in the input items (hallucinations dropped)
  - duplicate ``(item_id, clip_gcs_path)`` pairs are deduped, keeping higher score
  - scores clamped to [0, 10]; ``rationale`` must be non-empty per kept entry
  - kept assignments are sorted by score desc and capped at ``max_assignments``

Unlike ``music_matcher``, an EMPTY result is valid (best-effort: a clip batch
that fits nothing leaves the plan untouched). ``parse()`` raises only when the
JSON itself is malformed, never on an empty-but-valid list.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.clip_plan_match import (
    Assignment,
    ClipPlanMatcherInput,
    ClipPlanMatcherOutput,
    ClipSummary,
    PlanItemSummary,
)
from app.pipeline.prompt_loader import load_prompt

# ── Prompt-injection sanitization ────────────────────────────────────────────
# Clip transcripts/subjects are third-party text fed into the matcher prompt; the
# theme/idea fields originate from another LLM. Defang role markers / fences as a
# belt-and-suspenders alongside the prompt's "treat as data" framing. NOTE: the
# clip_gcs_path is NEVER passed through here — it must round-trip verbatim and is
# validated by set membership instead.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?im)^\s*(system|assistant|user|tool|developer)\s*[:>]\s*")
_FENCE = re.compile(r"```+")
_MAX_FIELD_CHARS = 400
_MAX_TRANSCRIPT_CHARS = 300


def _sanitize_text(s: str, *, limit: int = _MAX_FIELD_CHARS) -> str:
    if not s:
        return ""
    s = _CONTROL_CHARS.sub(" ", s)
    s = _ROLE_MARKERS.sub("[role-marker-stripped] ", s)
    s = _FENCE.sub("'''", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _format_clip(idx: int, c: ClipSummary) -> str:
    # Index the clip in the prompt; the model echoes clip_gcs_path verbatim. The
    # path is shown plainly (not quoted/escaped) so the model copies it exactly.
    return (
        f"- clip[{idx}] path={c.clip_gcs_path} | dur={c.duration_s:.1f}s | "
        f"hook_score={c.hook_score:.1f} | "
        f'subject="{_sanitize_text(c.detected_subject)}" | '
        f'hook="{_sanitize_text(c.hook_text)}" | '
        f'transcript="{_sanitize_text(c.transcript_excerpt, limit=_MAX_TRANSCRIPT_CHARS)}"'
    )


def _format_item(it: PlanItemSummary) -> str:
    return (
        f"- item_id={it.item_id} | "
        f'theme="{_sanitize_text(it.theme)}" | '
        f'idea="{_sanitize_text(it.idea)}" | '
        f'filming="{_sanitize_text(it.filming_suggestion)}"'
    )


# ── Agent ─────────────────────────────────────────────────────────────────────


class ClipPlanMatcherAgent(Agent[ClipPlanMatcherInput, ClipPlanMatcherOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.clip_plan_matcher",
        prompt_id="match_clip_plan",
        prompt_version="2026-05-29",
        # Text-only assignment from pre-computed digests; flash + a small thinking
        # budget is plenty (mirrors music_matcher's measured 256-token cap).
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=256,
    )
    Input = ClipPlanMatcherInput
    Output = ClipPlanMatcherOutput

    def required_fields(self) -> list[str]:
        return ["assignments"]

    def render_prompt(self, input: ClipPlanMatcherInput) -> str:  # noqa: A002
        clip_lines = "\n".join(_format_clip(i, c) for i, c in enumerate(input.clips))
        item_lines = "\n".join(_format_item(it) for it in input.items)
        valid_paths = "\n".join(c.clip_gcs_path for c in input.clips)
        valid_items = ", ".join(it.item_id for it in input.items)
        return load_prompt(
            "match_clip_plan",
            max_assignments=str(input.max_assignments),
            clip_count=str(len(input.clips)),
            item_count=str(len(input.items)),
            clip_lines=clip_lines,
            item_lines=item_lines,
            valid_clip_paths=valid_paths,
            valid_item_ids=valid_items,
        )

    def parse(
        self,
        raw_text: str,
        input: ClipPlanMatcherInput,  # noqa: A002
    ) -> ClipPlanMatcherOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"clip_plan_matcher: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("clip_plan_matcher: response is not a JSON object")

        raw = data.get("assignments")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise SchemaError("clip_plan_matcher: 'assignments' must be a list")

        valid_paths = {c.clip_gcs_path for c in input.clips}
        valid_items = {it.item_id for it in input.items}
        seen: set[tuple[str, str]] = set()
        kept: list[Assignment] = []

        for entry in raw:
            if not isinstance(entry, dict):
                continue
            path = entry.get("clip_gcs_path")
            item_id = entry.get("item_id")
            if not isinstance(path, str) or path not in valid_paths:
                # Hallucinated / mangled path — silent drop (the verbatim echo
                # contract failed). Membership check is the only path validation.
                continue
            if not isinstance(item_id, str) or item_id.strip() not in valid_items:
                continue
            item_id = item_id.strip()
            key = (item_id, path)
            if key in seen:
                continue
            try:
                score = float(entry.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            score = max(0.0, min(10.0, score))
            rationale = str(entry.get("rationale", "") or "").strip()
            if not rationale:
                continue
            try:
                kept.append(
                    Assignment(
                        item_id=item_id,
                        clip_gcs_path=path,
                        score=score,
                        rationale=rationale,
                    )
                )
            except ValidationError:
                continue
            seen.add(key)

        # Sort by score desc (stable — ties keep model order) and cap. Empty is OK.
        kept.sort(key=lambda a: a.score, reverse=True)
        kept = kept[: input.max_assignments]

        try:
            return ClipPlanMatcherOutput(assignments=kept)
        except ValidationError as exc:
            raise SchemaError(f"clip_plan_matcher: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. Every "
            "`clip_gcs_path` MUST be copied verbatim from the provided clip list "
            "and every `item_id` MUST be one of the listed item_ids — do not "
            "invent values. If no clip genuinely fits any item, return "
            '`{"assignments": []}` rather than forcing a weak match.'
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


# Re-exported for callers/tests that import the schema names from the agent module
# (mirrors music_matcher's surface).
__all__ = [
    "Assignment",
    "ClipPlanMatcherAgent",
    "ClipPlanMatcherInput",
    "ClipPlanMatcherOutput",
    "ClipSummary",
    "PlanItemSummary",
]
