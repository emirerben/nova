"""Ground read-only overlay proposals before native review and placement."""

from __future__ import annotations

import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.agents.overlay_generation import (
    OverlayGenerationAgent,
    OverlayPlanInput,
    OverlayPlanOutput,
    OverlayProposal,
)


class OverlayPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID
    variant_id: str = Field(min_length=1, max_length=100)
    base_generation: str = Field(min_length=1, max_length=128)
    document_fingerprint: str = Field(min_length=1, max_length=64)
    mode: str = Field(pattern=r"^(prompt|automatic)$")
    prompt: str = Field(default="", max_length=1500)
    taste_notes: str = Field(default="", max_length=1500)
    editor_snapshot: dict

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.editor_snapshot).encode()) > 750_000:
            raise ValueError("Editor snapshot is too large")
        if self.mode == "prompt" and not self.prompt.strip():
            raise ValueError("Describe what to add")
        return self


class PlannedOverlay(OverlayProposal):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)


class OverlayPlanResponse(BaseModel):
    document_fingerprint: str
    base_generation: str
    time_basis: str = "editor_base"
    placement_review_required: bool = True
    overlays: list[PlannedOverlay] = Field(default_factory=list)
    message: str = ""


def _tokens(value: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold().replace("\u0307", ""))


def _words(cue: dict) -> list[dict]:
    result = []
    for word in cue.get("words") or []:
        if not isinstance(word, dict):
            continue
        text = str(word.get("word") or word.get("text") or "")
        start, end = word.get("start_s", word.get("start")), word.get("end_s", word.get("end"))
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start < end
            and text
        ):
            result.append(dict(quote=text, start_s=start, end_s=end))
    return result


def speech_evidence(original: list[dict], current: list[dict]) -> list[dict]:
    originals = {
        tuple((tuple(_tokens(w["quote"])), w["start_s"], w["end_s"]) for w in _words(row))
        for row in original
        if _words(row)
    }
    evidence = []
    for cue in current:
        words = _words(cue)
        text = str(cue.get("text") or "")
        identity = tuple((tuple(_tokens(w["quote"])), w["start_s"], w["end_s"]) for w in words)
        if not words or identity not in originals:
            continue
        if _tokens(" ".join(w["quote"] for w in words)) != _tokens(text):
            continue
        evidence.append(
            dict(
                kind="speech",
                reference_id=f"speech-{len(evidence)}",
                quote=text[:500],
                start_s=words[0]["start_s"],
                end_s=words[-1]["end_s"],
                words=words,
            )
        )
    return evidence


def _millis(value: float) -> float:
    return math.floor(value * 1000 + 0.5) / 1000


@dataclass(frozen=True)
class OverlayClock:
    windows: list[dict]
    base_duration: float
    output_duration: float
    insertion_base: float | None = None
    insertion_start: float | None = None
    shift: float = 0

    def to_base(self, output_s: float) -> float:
        if not 0 <= output_s <= self.output_duration:
            raise ValueError("The requested time is outside this video's timeline.")
        if self.insertion_base is None or self.shift <= 0:
            return _millis(output_s)
        before = min(self.insertion_base, self.insertion_start)
        after = self.insertion_base + self.shift
        if before <= output_s < after:
            raise ValueError("That time is inside the inserted Carousel. Choose a footage moment.")
        return _millis(output_s if output_s < before else output_s - self.shift)


def overlay_clock(slots: list[dict], carousel: dict | None) -> OverlayClock:
    from app.services.authored_editor import _canonical_output_duration
    from app.services.guided_timeline import (
        carousel_boundary_duration,
        carousel_ripple_duration,
        effective_transitions,
        layout_frame_segments,
    )

    rows = [dict(row) for row in slots if not row.get("removed")]
    speech = any(
        str(row.get("slot_id") or "").startswith("native-phone-talking-source") for row in slots
    )
    frame_clock = not speech and any(row.get("source_end_s") is not None for row in rows)
    durations = []
    for row in rows:
        length = float(row.get("duration_s") or 0)
        if not speech and row.get("source_end_s") is not None:
            rate = float(row.get("playback_rate") or 1)
            span = float(row["source_end_s"]) - float(row.get("in_s") or 0)
            if not math.isfinite(rate) or rate <= 0 or not math.isfinite(span) or span <= 0:
                raise ValueError("This timeline has an invalid source window.")
            length = max(0.1, _canonical_output_duration(span, rate))
        if not math.isfinite(length) or length <= 0:
            raise ValueError("Save and reopen this timeline before planning overlays.")
        durations.append(round(length * 30) / 30 if frame_clock else length)
    transitions = [
        (
            str(row.get("transition_after") or "cut"),
            float(
                row.get("transition_duration_s")
                if row.get("transition_duration_s") is not None
                else 0.3
            ),
        )
        for row in rows
    ]
    effective = effective_transitions(durations, transitions)
    cursor = 0.0
    for index, row in enumerate(rows):
        row["duration_s"] = durations[index]
        row["transition_after"], row["transition_duration_s"] = effective[index]
        overlap = effective[index - 1][1] if index else 0
        if speech and index and transitions[index - 1][0] != "cut":
            overlap = min(
                0.3, transitions[index - 1][1], durations[index - 1] * 0.3, durations[index] * 0.3
            )
            if overlap < 0.1:
                overlap = 0
        start = cursor - overlap
        end = start + durations[index]
        row["output_start_s"] = start if speech else _millis(start)
        row["output_end_s"] = end if speech else _millis(row["output_start_s"] + durations[index])
        cursor = row["output_end_s"]
    if frame_clock:
        layout_frame_segments(rows)
    base_duration = rows[-1]["output_end_s"] if rows else 0.0
    if not carousel:
        return OverlayClock(rows, base_duration, base_duration)
    duration = carousel.get("duration_s", carousel.get("duration"))
    if duration is None and "start_s" in carousel and "end_s" in carousel:
        duration = float(carousel["end_s"]) - float(carousel["start_s"])
    if duration is None:
        raise ValueError("The Carousel duration is unavailable. Reopen the editor before planning.")
    duration = float(duration)
    if not math.isfinite(duration) or not 0 < duration <= 60:
        raise ValueError("The Carousel duration is invalid.")
    position = carousel.get("position", "intro")
    index = len(rows) if position == "outro" else len(rows) // 2 if position == "middle" else 0
    before = rows[index - 1] if index else None
    after = rows[index] if index < len(rows) else None
    insertion = after["output_start_s"] if after else before["output_end_s"] if before else 0

    def boundary(key: str, left: float | None, right: float | None) -> float:
        if carousel.get(key) != "crossfade" or left is None or right is None:
            return 0.0
        raw = carousel.get(key + "_duration_s", 0.4)
        requested = math.floor(float(raw) * 10 + 0.5) / 10
        return carousel_boundary_duration(requested, left, right)

    incoming = boundary("transition_in", before["duration_s"] if before else None, duration)
    outgoing = boundary("transition_out", duration, after["duration_s"] if after else None)
    old_overlap = before["output_end_s"] - after["output_start_s"] if before and after else 0
    start = _millis(before["output_end_s"] - incoming if before else 0)
    shift = _millis(carousel_ripple_duration(duration, old_overlap, incoming, outgoing))
    end = _millis(start + duration)
    total = max(end, _millis(base_duration + shift) if after else base_duration)
    return OverlayClock(rows, base_duration, total, insertion, start, shift)


def explicit_time_evidence(
    prompt: str, duration_s: float, clock: OverlayClock | None = None
) -> list[dict]:
    matches = list(re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?:\.(\d+))?(?!\d)", prompt))
    facts = []
    for match in matches:
        if int(match[2]) >= 60:
            continue
        time = int(match[1]) * 60 + int(match[2]) + float("0." + (match[3] or "0"))
        if 0 <= time < duration_s:
            facts.append(
                dict(
                    kind="explicit_time",
                    reference_id=f"time-{len(facts)}",
                    quote=match[0],
                    start_s=clock.to_base(time) if clock else time,
                    output_time_s=time,
                    end_s=clock.base_duration if clock else duration_s,
                )
            )
    for match in re.finditer(
        r"(?<![\d:])(\d+(?:[.,]\d+)?)\.?\s*(?:seconds?|secs?|saniyede|sn)\b", prompt, re.I
    ):
        prefix = prompt[max(0, match.start() - 8) : match.start()].casefold()
        if "saniyede" not in match[0].casefold() and not re.search(r"\bat\s*$", prefix):
            continue
        time = float(match[1].replace(",", "."))
        if 0 <= time < duration_s:
            facts.append(
                dict(
                    kind="explicit_time",
                    reference_id=f"time-{len(facts)}",
                    quote=match[0],
                    start_s=clock.to_base(time) if clock else time,
                    output_time_s=time,
                    end_s=clock.base_duration if clock else duration_s,
                )
            )
    return facts


def _speech_start(fact: dict, quote: str) -> float:
    wanted = _tokens(quote)
    words = fact.get("words") or [fact]
    stream = [(token, word["start_s"]) for word in words for token in _tokens(word["quote"])]
    matches = [
        stream[i][1]
        for i in range(len(stream) - len(wanted) + 1)
        if wanted and [t for t, _ in stream[i : i + len(wanted)]] == wanted
    ]
    if len(matches) != 1:
        raise ValueError(
            "The spoken trigger is missing or ambiguous. Choose a specific occurrence."
        )
    return matches[0]


LOGO_NOTICE = "Upload the real logo or choose it from your visual library."


def materialize_overlays(
    raw: OverlayPlanOutput,
    evidence: list[dict],
    assets: list[dict],
    *,
    duration_s: float,
    prompt: str,
    existing_overlays: list[dict] | None = None,
    insertion_base: float | None = None,
    notices: list[str] | None = None,
) -> list[PlannedOverlay]:
    by_ref = {(row["kind"], row["reference_id"]): row for row in evidence}
    by_asset = {row["asset_id"]: row for row in assets}
    result = []
    occupied = [(row["start_s"], row["end_s"]) for row in existing_overlays or []]
    for row in raw.overlays:
        if row.kind == "image" and row.depicts != "generic_subject":
            # Only this row is refused; the rest of a mixed request still gets planned.
            # Anything the model did not explicitly call generic fails safe.
            if notices is not None and LOGO_NOTICE not in notices:
                notices.append(LOGO_NOTICE)
            continue
        if all(ref.kind == "visual" for ref in row.evidence) and re.search(
            r"\b(?:when|once|as soon as|exactly when)\b|(?:dığımda|diğimde|duğumda|düğümde)",
            prompt,
            re.I,
        ):
            raise ValueError("A shot description cannot time that action. Name an exact time.")
        references = []
        anchors = []
        for reference in row.evidence:
            fact = by_ref.get((reference.kind, reference.reference_id))
            if fact is None:
                raise ValueError(
                    "The suggested timing has no source evidence. Try a more specific request."
                )
            references.append((reference, fact))
            if reference.kind == "speech":
                anchors.append(_speech_start(fact, reference.quote))
        start = anchors[0] if anchors else row.start_s
        if any(abs(anchor - start) > 0.001 for anchor in anchors):
            raise ValueError("The spoken triggers name different moments. Choose one occurrence.")
        for reference, fact in references:
            if reference.kind == "visual":
                if not (fact["start_s"] <= start < row.end_s <= fact["end_s"]):
                    raise ValueError("The suggestion extends beyond the pictured source moment.")
                if reference.quote and _tokens(reference.quote) != _tokens(fact["quote"]):
                    raise ValueError(
                        "The suggestion names a visual moment not supported by the source."
                    )
            elif reference.kind == "explicit_time" and abs(start - fact["start_s"]) > 0.001:
                raise ValueError("The suggestion does not match the requested time.")
        if not 0 <= start < row.end_s <= duration_s:
            raise ValueError("The suggestion is outside this video's timeline.")
        if insertion_base is not None and start < insertion_base <= row.end_s:
            raise ValueError(
                "That overlay would stretch across the Carousel. "
                "Choose a footage interval on one side."
            )
        if any(start < end and row.end_s > begin for begin, end in occupied):
            raise ValueError(
                "The suggested overlays overlap. "
                "Choose a free moment or remove an existing overlay."
            )
        occupied.append((start, row.end_s))
        if row.kind == "library_asset":
            if row.asset_id not in by_asset:
                raise ValueError("Choose an available visual from this video's library.")
            asset = by_asset[row.asset_id]
            if asset["kind"] != "image":
                raise ValueError("Choose a still image for this overlay.")
        elif row.asset_id:
            raise ValueError("Only library overlays may reference an existing asset.")
        if row.kind == "image":
            if not row.prompt.strip() or row.text:
                raise ValueError(
                    "Generated pictures require a description; use native text for lettering."
                )
        if row.kind == "text" and not row.text.strip():
            raise ValueError("A text overlay needs readable text.")
        result.append(PlannedOverlay(**{**row.model_dump(exclude={"depicts"}), "start_s": start}))
    return result


def safe_zone(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    values = {key: raw.get(key) for key in ("x", "y", "w", "h")}
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in values.values()
    ):
        return None
    x, y, w, h = (values[key] for key in ("x", "y", "w", "h"))
    return values if 0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x and 0 < h <= 1 - y else None


def occupied_intervals(variant: dict) -> list[dict]:
    from app.services.kria_editor_ops import is_caption_text_bar

    intervals = []
    for lane in ("visual_blocks", "media_overlays", "text_elements", "motion_scenes"):
        for row in variant.get(lane) or []:
            if not isinstance(row, dict) or row.get("removed") or is_caption_text_bar(row):
                continue
            start, end = row.get("start_s"), row.get("end_s")
            if lane == "motion_scenes":
                first, last = row.get("start_frame"), row.get("end_frame_exclusive")
                if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
                    continue
                start, end = first / 30, last / 30
            if (
                isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and math.isfinite(start)
                and math.isfinite(end)
                and 0 <= start < end
            ):
                intervals.append(
                    dict(id=str(row.get("id") or ""), kind=lane, start_s=start, end_s=end)
                )
    return intervals


def plan_context(
    job,
    variant: dict,
    item,
    body: OverlayPlanRequest,
    assets: list[dict],
    *,
    locale: str | None = None,
) -> OverlayPlanInput:
    from app.kria.api_schemas import EditorStateIn, EditorStateLanes
    from app.kria.reply_language import resolve_reply_language
    from app.services.kria_editor_ops import (
        build_editor_snapshot,
        clip_seen_by_media_id,
        clip_visible_text_by_media_id,
        resolve_editor_base,
    )

    payload = body.editor_snapshot.get("editor_payload", body.editor_snapshot)
    sections = payload.get("sections", payload)
    if not isinstance(sections, dict):
        raise ValueError("The editor snapshot is invalid")
    snapshot_base = payload.get("base_generation", body.base_generation)
    if snapshot_base != body.base_generation:
        raise ValueError("The editor snapshot is stale")
    lanes = {k: v for k, v in sections.items() if k in EditorStateLanes.model_fields}
    state = EditorStateIn(
        base_generation=body.base_generation, client_state_id=body.document_fingerprint, lanes=lanes
    )
    base = resolve_editor_base(job, variant, None, state)
    if base.fallback_reason:
        raise ValueError("The current editor timeline cannot be read. Reload before planning.")
    assignments = list(item.clip_assignments or [])
    context = dict(
        seen=clip_seen_by_media_id(job, variant, assignments),
        visible_text=clip_visible_text_by_media_id(job, variant, assignments),
    )
    zones_by_media = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        analysis = assignment.get("analysis") or {}
        zone = safe_zone(analysis.get("text_safe_zone")) if isinstance(analysis, dict) else None
        if zone:
            for media_id in clip_seen_by_media_id(job, variant, [assignment]):
                zones_by_media[media_id] = zone
    snapshot = build_editor_snapshot(job, base.projected, clip_context=context)
    carousel = sections.get("carousel_moment", base.projected.get("carousel_moment"))
    clock = overlay_clock(snapshot["slots"], carousel)
    slots = clock.windows
    duration = clock.base_duration or float(snapshot["total_duration_s"])
    if not clock.windows and carousel:
        raise ValueError("Add footage before planning overlays around the Carousel.")
    if not math.isfinite(duration) or not 0 < duration <= 600:
        raise ValueError("This edit has no supported timeline to plan against")
    saved_snapshot = build_editor_snapshot(job, variant, clip_context=context)
    timing_keys = (
        "slot_id",
        "clip_index",
        "in_s",
        "duration_s",
        "playback_rate",
        "removed",
        "transition_after",
        "transition_duration_s",
    )
    current_timing = [tuple(row.get(k) for k in timing_keys) for row in snapshot["slots"]]
    saved_timing = [tuple(row.get(k) for k in timing_keys) for row in saved_snapshot["slots"]]
    originals = list(variant.get("caption_cues") or [])
    current = list(base.projected.get("caption_cues") or [])
    evidence = speech_evidence(originals, current) if current_timing == saved_timing else []
    evidence.extend(
        explicit_time_evidence(
            body.prompt, clock.output_duration or duration, clock if clock.windows else None
        )
    )
    for slot in slots:
        source = next(
            (
                row
                for row in saved_snapshot["slots"]
                if row.get("clip_index") == slot.get("clip_index")
                and row.get("media_id") == slot.get("media_id")
            ),
            {},
        )
        seen = source.get("seen") or {}
        description = seen.get("text") or (
            "Visible text: " + source["visible_text"] if source.get("visible_text") else ""
        )
        start, end = slot.get("output_start_s"), slot.get("output_end_s")
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and 0 <= start < end <= duration
            and description
        ):
            evidence.append(
                dict(
                    kind="visual",
                    reference_id=str(slot["key"]),
                    quote=description,
                    visible_text=source.get("visible_text") or "",
                    text_safe_zone=zones_by_media.get(source.get("media_id")),
                    placement_review_required=True,
                    start_s=start,
                    end_s=end,
                )
            )
    return OverlayPlanInput(
        language=resolve_reply_language(body.prompt, locale=locale) or "en",
        insertion_base=clock.insertion_base if clock.shift > 0 else None,
        mode=body.mode,
        prompt=body.prompt,
        taste_notes=body.taste_notes,
        duration_s=duration,
        evidence=evidence[:600],
        assets=assets,
        existing_overlays=occupied_intervals(
            {
                **base.projected,
                "motion_scenes": sections.get("motion_scenes", base.projected.get("motion_scenes")),
            }
        ),
    )


def generate_overlay_plan(
    db, *, owner_id, item_id, body: OverlayPlanRequest, ctx, agent=None, locale: str | None = None
):
    from app.agents._model_client import default_client
    from app.models import Job, PlanItemAsset
    from app.routes.generative_jobs import variant_render_baseline
    from app.services.generated_media import owned_item
    from app.services.kria_editor_ops import _seen_text

    item = owned_item(db, owner_id, item_id)
    if item.current_job_id != body.job_id:
        raise ValueError("The video's current edit changed. Reopen it before planning.")
    job = db.get(Job, body.job_id)
    if job is None or job.user_id != owner_id or job.status == "cancelled":
        raise ValueError("Video not found")
    variant = next(
        (
            row
            for row in (job.assembly_plan or {}).get("variants") or []
            if row.get("variant_id") == body.variant_id
        ),
        None,
    )
    if variant is None:
        raise ValueError("Variant not found")
    if body.base_generation != variant_render_baseline(variant):
        raise ValueError("The editor snapshot is stale")
    pool = db.scalars(
        select(PlanItemAsset)
        .where(
            PlanItemAsset.plan_item_id == item_id,
            PlanItemAsset.user_id == owner_id,
            PlanItemAsset.status == "ready",
            PlanItemAsset.kind == "image",
        )
        .limit(40)
    ).all()
    assets = [
        dict(
            asset_id=str(row.id),
            kind=row.kind,
            title=(row.source_filename or "Visual")[:200],
            duration_s=row.duration_s,
            description=_seen_text(row.analysis, kind=row.kind),
        )
        for row in pool
    ]
    context = plan_context(job, variant, item, body, assets, locale=locale)
    response = dict(
        document_fingerprint=body.document_fingerprint, base_generation=body.base_generation
    )
    if not context.evidence:
        return OverlayPlanResponse(
            **response,
            message=(
                "I need timed speech or analyzed visual moments to place overlays. "
                "Save and reopen recent timeline edits, or name an exact time."
            ),
        )
    raw = (agent or OverlayGenerationAgent(default_client())).run(context, ctx=ctx)
    notices: list[str] = []
    try:
        overlays = materialize_overlays(
            raw,
            context.evidence,
            assets,
            duration_s=context.duration_s,
            prompt=body.prompt,
            existing_overlays=context.existing_overlays,
            insertion_base=context.insertion_base,
            notices=notices,
        )
    except ValueError as exc:
        return OverlayPlanResponse(**response, message=" ".join((str(exc), *notices)))
    message = raw.message
    if overlays:
        message = (message + " Review placement over your footage.").strip()
    if notices:
        message = " ".join(part for part in (message, *notices) if part).strip()
    return OverlayPlanResponse(**response, overlays=overlays, message=message)
