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

from pydantic import BaseModel, Field, model_validator

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
    # Whole order fixed by a filming guide: the listed order is kept verbatim.
    order_locked: bool = False
    # PINNED mode (KRI-532): the creator fixed only how the video opens and/or
    # closes ("end on the sunset valley"). Clip ids that must open the video in
    # that order / close it in that order; every other clip follows the narration.
    # Ignored when ``order_locked``.
    pinned_first: list[str] = Field(default_factory=list, max_length=50)
    pinned_last: list[str] = Field(default_factory=list, max_length=50)
    # The creator's described sequence ("then the pasta, then the cheese"): clip ids per
    # group, in the creator's listed order. Every clip of a group must come before every
    # clip of the next group; clips in no group are free. Ignored when ``order_locked``.
    ordered_groups: list[list[str]] = Field(default_factory=list, max_length=50)
    language: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def _check_pins(self) -> NarratedClipAlignmentInput:
        known = {clip.clip_id for clip in self.clips}
        pins = [*self.pinned_first, *self.pinned_last]
        unknown = [clip_id for clip_id in pins if clip_id not in known]
        if unknown:
            raise ValueError(f"pinned clips are not in clips: {unknown}")
        if len(set(pins)) != len(pins):
            raise ValueError("a clip may be pinned only once")
        grouped = [clip_id for group in self.ordered_groups for clip_id in group]
        unknown = [clip_id for clip_id in grouped if clip_id not in known]
        if unknown:
            raise ValueError(f"ordered_groups clips are not in clips: {unknown}")
        if len({*grouped, *pins}) != len(grouped) + len(pins):
            raise ValueError("a clip may be pinned or grouped only once")
        return self

    @property
    def pinned(self) -> bool:
        """True in PINNED mode: not fully locked, but the creator fixed part of the order."""
        return not self.order_locked and bool(
            self.pinned_first or self.pinned_last or self.ordered_groups
        )


class NarratedClipPlacement(BaseModel):
    clip_id: str = Field(min_length=1, max_length=160)
    start_word_id: str = Field(min_length=7, max_length=7)
    reason: str = Field(default="", max_length=240)


class NarratedClipAlignmentOutput(BaseModel):
    placements: list[NarratedClipPlacement] = Field(default_factory=list, max_length=50)
    # PINNED mode only: the model listed the placements out of screen order and parse
    # re-sorted them by start word (KRI-532). Recorded on the pipeline trace.
    resorted: bool = False


def _order_mode_text(input: NarratedClipAlignmentInput) -> str:  # noqa: A002
    if input.order_locked:
        return "LOCKED: keep the clips in exactly the order listed; only choose where each starts."
    if not input.pinned:
        return "FREE: you may reorder the clips so they follow the narration."
    label_by_id = {clip.clip_id: clip.creator_label for clip in input.clips}

    def _describe(clip_ids: list[str]) -> str:
        return ", then ".join(
            f"`{clip_id}` ({label_by_id[clip_id]})" if label_by_id[clip_id] else f"`{clip_id}`"
            for clip_id in clip_ids
        )

    def _group(clip_ids: list[str]) -> str:
        return " and ".join(_describe([clip_id]) for clip_id in clip_ids)

    parts: list[str] = []
    if input.pinned_first:
        parts.append(
            f"{_describe(input.pinned_first)} must open the video, in exactly that order"
            if len(input.pinned_first) > 1
            else f"{_describe(input.pinned_first)} must be the FIRST clip"
        )
    if input.pinned_last:
        parts.append(
            f"{_describe(input.pinned_last)} must close the video, in exactly that order"
            if len(input.pinned_last) > 1
            else f"{_describe(input.pinned_last)} must be the LAST clip"
        )
    if len(input.ordered_groups) > 1:
        parts.append(
            "the creator described this sequence: "
            + ", then ".join(_group(group) for group in input.ordered_groups)
            + " (every clip of a group is shown before every clip of the next group)"
        )
    if not parts:
        return (
            "PINNED: no clip is fixed. Place every clip where the narration describes it and "
            "list the placements in on-screen order."
        )
    return (
        "PINNED: the creator fixed only the clips named here. "
        + "; ".join(parts)
        + ". Every other clip is free: place it where the narration describes it and list the "
        "placements in on-screen order."
    )


class NarratedClipAlignmentAgent(Agent[NarratedClipAlignmentInput, NarratedClipAlignmentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.narrated_clip_alignment",
        prompt_id="narrated_clip_alignment",
        prompt_version="2026-10-08.1",
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
            order_mode=_order_mode_text(input),
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

        entries: list[tuple[str, str, str]] = []  # (clip_id, word_id, reason)
        seen_clips: set[str] = set()
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
            seen_clips.add(clip_id)
            entries.append((clip_id, word_id, " ".join(str(raw.get("reason") or "").split())[:240]))
        if seen_clips != known_clips:
            missing = sorted(known_clips - seen_clips)
            raise SchemaError(f"narrated_clip_alignment: missing clips {missing}")

        resorted = False
        if input.pinned:
            # The model owns WHERE each clip starts; screen order follows from that. A
            # model that listed the clips in input order is re-sorted by start word
            # (stable: ties keep the listed order and are then rejected below).
            by_start = sorted(entries, key=lambda entry: word_index[entry[1]])
            resorted = by_start != entries
            entries = by_start

        placements: list[NarratedClipPlacement] = []
        previous_index = -1
        previous_start = -1.0
        for position, (clip_id, word_id, reason) in enumerate(entries):
            if (input.order_locked or input.pinned) and position == 0:
                # In a LOCKED/PINNED order the first clip owns the opening of the voiceover: the
                # worker pins the first step to 0.0 anyway, and a model that placed it
                # later (its subject is narrated later) must not trip the monotonic check.
                word_id = str(input.words[0]["word_id"])
            index = word_index[word_id]
            start = word_start[word_id]
            if index <= previous_index or start <= previous_start:
                raise SchemaError(
                    "narrated_clip_alignment: start words must be strictly increasing "
                    f"(placement {position} starts at {word_id})"
                )
            previous_index = index
            previous_start = start
            placements.append(
                NarratedClipPlacement(clip_id=clip_id, start_word_id=word_id, reason=reason)
            )

        if input.order_locked and [p.clip_id for p in placements] != input_order:
            raise SchemaError(
                "narrated_clip_alignment: clip order is locked but the response reordered it"
            )
        if input.pinned:
            listed = [p.clip_id for p in placements]
            head = input.pinned_first
            tail = input.pinned_last
            if listed[: len(head)] != head:
                raise SchemaError(
                    f"narrated_clip_alignment: pinned opening clips {head} must come first "
                    "in that order"
                )
            if tail and listed[len(listed) - len(tail) :] != tail:
                raise SchemaError(
                    f"narrated_clip_alignment: pinned closing clips {tail} must come last "
                    "in that order"
                )
            screen_pos = {clip_id: i for i, clip_id in enumerate(listed)}
            for before, after in zip(input.ordered_groups, input.ordered_groups[1:]):
                if max(screen_pos[c] for c in before) > min(screen_pos[c] for c in after):
                    raise SchemaError(
                        f"narrated_clip_alignment: clips {before} must all come before {after}"
                    )
        return NarratedClipAlignmentOutput(placements=placements, resorted=resorted)
