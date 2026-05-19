"""nova.audio.song_sections — top-K edit-worthy sections for a music track.

Producer of ``SongSectionsOutput`` (see
``app/agents/_schemas/song_sections.py``). Runs once per track at
admin-analyze time alongside ``song_classifier``. The matcher (Phase 2)
and the variant orchestrator (Phase 6) consume ``best_sections`` to pick
a specific 15-60s window per ranked track instead of the mechanical
``auto_best_section`` window.

Inputs:
  - audio Gemini File API URI (already uploaded once by the orchestrator)
  - the structural ``AudioTemplateOutput`` dict (used as context; slot
    boundaries inform section boundaries — the model is asked to prefer
    slot-aligned cuts)
  - ``duration_s`` + ``beat_timestamps_s`` so the model can reason about
    where in the track it is

Output:
  - 1 to 3 ranked sections, each with ``start_s``/``end_s``/``label``/
    ``energy``/``suggested_use``/``rationale``, plus a forced
    ``section_version = CURRENT_SECTION_VERSION``.

The mechanical ``auto_best_section`` (in ``app/pipeline/music_recipe.py``)
stays in place as the fallback for the manual music-pick flow when the
agent has not run; auto-mode strictly requires ``best_sections IS NOT
NULL`` so a Gemini failure simply excludes the track from auto-mode until
``scripts/backfill_song_sections.py`` re-runs.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.song_sections import (
    CURRENT_SECTION_VERSION,
    SongSection,
    SongSectionsOutput,
)
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

# Section duration band — TikTok-shape constraint. Sections outside this
# band get dropped in parse() (not failed: an over-long section is the
# model's mistake, not a reason to throw away the other valid picks).
MIN_SECTION_DURATION_S = 15.0
MAX_SECTION_DURATION_S = 60.0

# Float tolerance on the upper bound. Gemini sometimes returns end_s
# slightly past duration_s due to rounding; 1s leeway avoids
# hair-trigger drops.
END_S_TOLERANCE_S = 1.0

# Two sections overlapping by more than this many seconds → drop the
# lower-ranked one (= higher rank number) rather than reject the whole
# response. Phase 6 variant diversity wants sections to be meaningfully
# distinct, so > 5s of overlap signals the picks are too similar.
MAX_OVERLAP_S = 5.0


class SongSectionsInput(BaseModel):
    file_uri: str
    file_mime: str = "audio/mp4"
    duration_s: float = Field(gt=0.0)
    beat_timestamps_s: list[float] = Field(default_factory=list)
    # The AudioTemplateOutput dict (audio_template's ``.model_dump()``).
    # Read-only context. The agent uses ``slots`` (beat-snapped slot
    # recipe) to align section boundaries when possible.
    audio_template_output: dict[str, Any] = Field(default_factory=dict)


class SongSectionsAgent(Agent[SongSectionsInput, SongSectionsOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.song_sections",
        prompt_id="identify_song_sections",
        prompt_version=CURRENT_SECTION_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = SongSectionsInput
    Output = SongSectionsOutput

    def media_uri(self, input: SongSectionsInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: SongSectionsInput) -> str:  # noqa: A002
        return input.file_mime or "audio/mp4"

    def required_fields(self) -> list[str]:
        return ["sections", "section_version"]

    def render_prompt(self, input: SongSectionsInput) -> str:  # noqa: A002
        at = input.audio_template_output or {}
        slots = at.get("slots") or []
        # Slot dicts produced by generate_music_recipe (and the merged
        # recipe) carry `position`, `target_duration_s`, and `energy` —
        # NOT `start_s`/`end_s`. Compute cumulative window-relative
        # offsets so the model has real numbers to align cuts to.
        # Window-relative: offsets are relative to the analyzed window
        # start (typically near beat_timestamps_s[0]), not to track 0.
        # Truncate to first 30 slots — a 3-minute song rarely has more,
        # and dumping 100+ slots into the prompt is not free.
        slot_lines: list[str] = []
        cum_s = 0.0
        for i, s in enumerate(slots[:30]):
            if not isinstance(s, dict):
                continue
            pos = s.get("position", i + 1)
            dur = float(s.get("target_duration_s", 0.0) or 0.0)
            energy = s.get("energy", "?")
            slot_lines.append(
                f"  slot {pos}: window-offset +{cum_s:.2f}s, duration={dur:.2f}s, energy={energy}"
            )
            cum_s += dur
        slot_block = "\n".join(slot_lines) if slot_lines else "  (no slot recipe available)"

        # Sample beat positions — give the model 20 spread evenly across
        # the beats list so it can pick beat-aligned boundaries without
        # dumping hundreds of timestamps into the prompt.
        beats = input.beat_timestamps_s
        if len(beats) > 20:
            step = len(beats) / 20.0
            sample = [beats[int(i * step)] for i in range(20)]
        else:
            sample = list(beats)
        beat_sample = ", ".join(f"{b:.2f}" for b in sample) if sample else "(no beats detected)"

        return load_prompt(
            "identify_song_sections",
            section_version=CURRENT_SECTION_VERSION,
            duration_s=f"{float(input.duration_s):.2f}",
            beat_count=str(len(input.beat_timestamps_s)),
            beat_sample=beat_sample,
            creative_direction=str(at.get("creative_direction", "") or ""),
            pacing_style=str(at.get("pacing_style", "") or ""),
            subject_niche=str(at.get("subject_niche", "") or ""),
            slot_block=slot_block,
            min_duration_s=f"{MIN_SECTION_DURATION_S:.0f}",
            max_duration_s=f"{MAX_SECTION_DURATION_S:.0f}",
            max_overlap_s=f"{MAX_OVERLAP_S:.0f}",
        )

    def parse(
        self,
        raw_text: str,
        input: SongSectionsInput,  # noqa: A002
    ) -> SongSectionsOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"song_sections: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("song_sections: response is not a JSON object")

        sections_raw = data.get("sections")
        if not isinstance(sections_raw, list):
            raise SchemaError("song_sections: missing/invalid 'sections' list")

        # Phase 1: per-section validation. Drop sections that fail the
        # cross-field checks rather than rejecting the whole response —
        # one bad section out of three is a worse outcome than two good
        # ones. The matcher would skip a NULL-sectioned track entirely.
        duration_s = float(input.duration_s)
        valid: list[SongSection] = []
        for i, raw in enumerate(sections_raw):
            if not isinstance(raw, dict):
                log.debug("song_sections_drop_non_dict", index=i)
                continue
            try:
                section = SongSection(**raw)
            except ValidationError as exc:
                # Pydantic-side rejection (enum miss, rank out of range,
                # negative start_s). Drop this one, keep going.
                log.debug("song_sections_drop_pydantic", index=i, error=str(exc))
                continue

            if section.start_s >= duration_s:
                log.debug(
                    "song_sections_drop_start_oob",
                    index=i,
                    start_s=section.start_s,
                    duration_s=duration_s,
                )
                continue
            if section.end_s <= section.start_s:
                log.debug(
                    "song_sections_drop_end_le_start",
                    index=i,
                    start_s=section.start_s,
                    end_s=section.end_s,
                )
                continue
            if section.end_s > duration_s + END_S_TOLERANCE_S:
                log.debug(
                    "song_sections_drop_end_oob",
                    index=i,
                    end_s=section.end_s,
                    duration_s=duration_s,
                )
                continue
            dur = section.end_s - section.start_s
            if dur < MIN_SECTION_DURATION_S or dur > MAX_SECTION_DURATION_S:
                log.debug(
                    "song_sections_drop_duration",
                    index=i,
                    duration_s=dur,
                )
                continue

            valid.append(section)

        # Phase 2: rank dedup. If two sections claim rank=1, keep the
        # first (the model's natural ordering is "best first" — trust
        # that prior over the rank field when they disagree).
        seen_ranks: set[int] = set()
        unique_rank: list[SongSection] = []
        for section in valid:
            if section.rank in seen_ranks:
                log.debug("song_sections_drop_dup_rank", rank=section.rank)
                continue
            seen_ranks.add(section.rank)
            unique_rank.append(section)

        # Phase 3: sort by rank ascending (best → worst), then walk
        # pairs and drop any later section overlapping an earlier (=
        # better-ranked) one by more than MAX_OVERLAP_S.
        unique_rank.sort(key=lambda s: s.rank)
        non_overlapping: list[SongSection] = []
        for section in unique_rank:
            overlaps = any(_overlap_s(prev, section) > MAX_OVERLAP_S for prev in non_overlapping)
            if overlaps:
                log.debug(
                    "song_sections_drop_overlap",
                    rank=section.rank,
                    start_s=section.start_s,
                    end_s=section.end_s,
                )
                continue
            non_overlapping.append(section)

        if not non_overlapping:
            raise RefusalError(
                "song_sections: no valid sections after filter "
                f"(received {len(sections_raw)}, all dropped)"
            )

        # Force section_version. The matcher trusts this exactly; if the
        # model echoes something else, we just produced these sections
        # under CURRENT_SECTION_VERSION's prompt, so that IS their version.
        try:
            return SongSectionsOutput(
                sections=non_overlapping,
                section_version=CURRENT_SECTION_VERSION,
            )
        except ValidationError as exc:
            # Pydantic max_length=3: more than 3 survived. Truncate to
            # top-3-by-rank (already sorted) and retry. min_length=1 is
            # guaranteed by the empty-list check above.
            if len(non_overlapping) > 3:
                return SongSectionsOutput(
                    sections=non_overlapping[:3],
                    section_version=CURRENT_SECTION_VERSION,
                )
            raise SchemaError(f"song_sections: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. "
            "`sections` MUST be a list of 1 to 3 entries, ranked 1 (best) "
            "to N. Each section MUST be 15-60 seconds long. Sections MUST "
            "NOT overlap by more than 5 seconds. Every categorical field "
            "MUST use one of the listed enum values verbatim."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def _overlap_s(a: SongSection, b: SongSection) -> float:
    """Seconds of overlap between two sections (0 if disjoint)."""
    return max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
