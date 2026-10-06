"""Decide WHERE in a voiceover each clip should come on screen (KRI-456).

Phone narrated edits without a filming guide used to split the transcript into
equal-duration buckets and give clip *i* to bucket *i*. Nothing compared what the
voice says with what each clip shows, so every clip played a step or two before
the voice reached it. This agent reads the timed words plus a short description
of every clip and answers one question per clip: at which word does it start?

The model never owns timestamps. It names a ``start_word_id`` from the supplied
transcript; the worker (``app.pipeline.narrated_alignment``) turns that into
step boundaries, enforces a minimum step length, and falls back to the legacy
bucket split whenever the answer is unusable. ``parse`` is strict on purpose: a
half-right placement list is worse than the fallback.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")


class NarratedAlignmentClip(BaseModel):
    clip_id: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=600)
    # What the creator called this shot in their brief ("grating the pecorino").
    creator_label: str = Field(default="", max_length=240)
    duration_s: float | None = Field(default=None, ge=0)


class NarratedClipAlignmentInput(BaseModel):
    words: list[dict] = Field(min_length=1, max_length=600)
    # Current clip order. When ``order_locked`` it is the creator's order.
    clips: list[NarratedAlignmentClip] = Field(min_length=2, max_length=50)
    creator_request: str = Field(default="", max_length=12_000)
    order_locked: bool = False
    language: str = Field(default="", max_length=20)


class NarratedClipPlacement(BaseModel):
    clip_id: str = Field(min_length=1, max_length=160)
    start_word_id: str = Field(min_length=7, max_length=7)
    reason: str = Field(default="", max_length=240)


class NarratedClipAlignmentOutput(BaseModel):
    placements: list[NarratedClipPlacement] = Field(default_factory=list, max_length=50)


class NarratedClipAlignmentAgent(Agent[NarratedClipAlignmentInput, NarratedClipAlignmentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.narrated_clip_alignment",
        prompt_id="narrated_clip_alignment",
        prompt_version="2026-10-06.1",
        model="gemini-2.5-flash",
        thinking_budget=1024,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = NarratedClipAlignmentInput
    Output = NarratedClipAlignmentOutput

    def required_fields(self) -> list[str]:
        return ["placements"]

    def render_prompt(self, input: NarratedClipAlignmentInput) -> str:  # noqa: A002
        return load_prompt(
            "narrated_clip_alignment",
            words_json=json.dumps(input.words, ensure_ascii=False),
            clips_json=json.dumps(
                [clip.model_dump(mode="json") for clip in input.clips], ensure_ascii=False
            ),
            creator_request=input.creator_request or "(none)",
            order_mode=(
                "LOCKED: keep the clips in exactly the order listed; only choose where each starts."
                if input.order_locked
                else "FREE: you may reorder the clips so they follow the narration."
            ),
            language=input.language or "(unknown)",
        )

    def parse(
        self,
        raw_text: str,
        input: NarratedClipAlignmentInput,  # noqa: A002
    ) -> NarratedClipAlignmentOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"narrated_clip_alignment: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("placements"), list):
            raise SchemaError("narrated_clip_alignment: response must contain a placements list")

        word_index = {str(word.get("word_id")): index for index, word in enumerate(input.words)}
        word_start = {
            str(word.get("word_id")): float(word.get("start_s") or 0.0) for word in input.words
        }
        input_order = [clip.clip_id for clip in input.clips]
        known_clips = set(input_order)
        if len(payload["placements"]) != len(input_order):
            raise SchemaError(
                f"narrated_clip_alignment: expected {len(input_order)} placements, "
                f"got {len(payload['placements'])}"
            )

        placements: list[NarratedClipPlacement] = []
        seen_clips: set[str] = set()
        previous_index = -1
        previous_start = -1.0
        for position, raw in enumerate(payload["placements"]):
            if not isinstance(raw, dict):
                raise SchemaError(f"narrated_clip_alignment: placement {position} is not an object")
            clip_id = str(raw.get("clip_id") or "")
            word_id = str(raw.get("start_word_id") or "")
            if clip_id not in known_clips:
                raise SchemaError(f"narrated_clip_alignment: unknown clip {clip_id!r}")
            if clip_id in seen_clips:
                raise SchemaError(f"narrated_clip_alignment: duplicate clip {clip_id!r}")
            if not _WORD_ID_RE.fullmatch(word_id) or word_id not in word_index:
                raise SchemaError(f"narrated_clip_alignment: unknown start word {word_id!r}")
            index = word_index[word_id]
            start = word_start[word_id]
            if index <= previous_index or start <= previous_start:
                raise SchemaError(
                    "narrated_clip_alignment: start words must be strictly increasing "
                    f"(placement {position} starts at {word_id})"
                )
            seen_clips.add(clip_id)
            previous_index = index
            previous_start = start
            placements.append(
                NarratedClipPlacement(
                    clip_id=clip_id,
                    start_word_id=word_id,
                    reason=" ".join(str(raw.get("reason") or "").split())[:240],
                )
            )

        if seen_clips != known_clips:
            missing = sorted(known_clips - seen_clips)
            raise SchemaError(f"narrated_clip_alignment: missing clips {missing}")
        if input.order_locked and [p.clip_id for p in placements] != input_order:
            raise SchemaError(
                "narrated_clip_alignment: clip order is locked but the response reordered it"
            )
        return NarratedClipAlignmentOutput(placements=placements)
