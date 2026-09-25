"""Clip facts: when and where a clip was filmed, with provenance (KRI-189).

Three writers feed one reader:

* the phone sends ``capture`` (capture time, coarse location, reverse-geocoded
  place) when it attaches a clip -> ``capture_from_assignment`` /
  ``capture_facts`` (provenance ``exif`` / ``geocode``);
* the landmark agent guesses a landmark from the frames + place ->
  ``landmark_fact`` (provenance ``inferred``), persisted on the clip's stored
  understanding record (``analysis["understanding"]["facts"]``);
* every reader (Main Creator prompt, edit planner prompt, ordering) calls
  ``assignment_facts`` and never re-derives anything.

Everything here is gated by ``settings.clip_facts_for(user_id)``; callers check
it before using any of this, so the flag-off path never sees a fact.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from app.kria.media_sources import (
    ClipCapture,
    MediaUploadContract,
)
from app.schemas.clip_understanding import (
    FACTS_KEY,
    ClipFact,
)

log = structlog.get_logger()

CAPTURE_KEY = "capture"

_KIND_ORDER = {
    kind: rank
    for rank, kind in enumerate(("capture_time", "place", "landmark", "visible_text", "creator"))
}

# Records which storage generation the landmark agent already answered for
# (even with "unknown"), so a retry or re-plan never pays for the same guess.
LANDMARK_GENERATION_KEY = "clip_facts_landmark_generation"

# Landmark guessing is best-effort context, never a reason to fail a plan.
_LANDMARK_CONCURRENCY = 3
# Wall-clock cap for the whole enrichment step. It sits inline on the draft's critical
# path, so a slow provider must cost bounded time: whatever has not answered by the
# deadline is skipped (and not recorded as attempted) and the plan continues.
LANDMARK_BUDGET_S = 45.0


def capture_from_assignment(assignment: Mapping[str, Any]) -> ClipCapture | None:
    """The stored capture context for one clip assignment, or None.

    Reads the explicit ``capture`` key first, then the phone proxy receipt's
    original descriptor. Never raises: a malformed stored value is "no capture".
    """
    raw = assignment.get(CAPTURE_KEY)
    if isinstance(raw, dict) and raw:
        try:
            capture = ClipCapture.model_validate(raw)
        except ValidationError:
            capture = None
        if capture is not None and not capture.is_empty():
            return capture
    contract_raw = assignment.get("upload_contract")
    if isinstance(contract_raw, dict) and contract_raw:
        try:
            contract = MediaUploadContract.model_validate(contract_raw)
        except ValidationError:
            return None
        capture = contract.proxy.original.capture if contract.proxy is not None else None
        if capture is not None and not capture.is_empty():
            return capture
    return None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def capture_facts(capture: ClipCapture | None) -> list[ClipFact]:
    """Facts the phone read from Photos: capture time (exif) and place (geocode)."""
    if capture is None:
        return []
    facts: list[ClipFact] = []
    if capture.capture_time is not None:
        with suppress(ValidationError):
            facts.append(
                ClipFact(
                    kind="capture_time", value=_iso_utc(capture.capture_time), provenance="exif"
                )
            )
    # A country alone ("Türkiye") says nothing about where the clip was filmed, so it is
    # not a place fact and can never caption a clip (KRI-190 simulator test). Decided
    # here, where the parts are still separate: a lone "Singapore" from a locality that
    # shares its country's name is a real place and is kept.
    if capture.place is not None and (capture.place.sub_locality or capture.place.locality):
        label = capture.place.label()
        if label:
            # A label made only of characters the fact cleaner strips is "no place".
            with suppress(ValidationError):
                facts.append(ClipFact(kind="place", value=label, provenance="geocode"))
    return facts


def understanding_facts(analysis: object) -> list[ClipFact]:
    """Facts stored on a clip's analysis (capture copies, landmark, ...)."""
    if not isinstance(analysis, dict):
        return []
    raw = analysis.get(FACTS_KEY)
    if not isinstance(raw, list):
        return []
    facts: list[ClipFact] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            facts.append(ClipFact.model_validate(row))
        except ValidationError:
            continue
    return facts


def assignment_facts(assignment: Mapping[str, Any]) -> list[ClipFact]:
    """Every fact known about one clip assignment, capture facts first.

    A stored fact never duplicates a capture-derived one of the same kind and
    provenance (the capture on the assignment is the source of truth).
    """
    facts = capture_facts(capture_from_assignment(assignment))
    seen = {(fact.kind, fact.provenance) for fact in facts}
    for fact in understanding_facts(assignment.get("analysis")):
        if (fact.kind, fact.provenance) in seen:
            continue
        seen.add((fact.kind, fact.provenance))
        facts.append(fact)
    return facts


def facts_for_prompt(facts: Iterable[ClipFact]) -> list[dict[str, Any]]:
    return [fact.prompt_dict() for fact in facts]


def capture_time_from_facts(facts: Iterable[Mapping[str, Any]]) -> datetime | None:
    """The capture-time fact as an aware UTC datetime, if one is present."""
    for fact in facts:
        if fact.get("kind") != "capture_time":
            continue
        try:
            value = datetime.fromisoformat(str(fact.get("value") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            continue
        return value.astimezone(UTC)
    return None


# ── Ordering ──────────────────────────────────────────────────────────────────

OrderingBasis = Literal["capture_time", "attachment"]


@dataclass(frozen=True)
class CaptureOrdering:
    ordered_ids: list[str]
    basis: OrderingBasis
    # Clips with no capture time. They keep their attachment-order slot, and
    # the receipt must say the order for them is the order they were attached.
    fallback_ids: list[str] = field(default_factory=list)

    def diagnostics(self) -> dict[str, Any]:
        return {"ordering_basis": self.basis, "ordering_fallback_clip_ids": self.fallback_ids}


def order_by_capture_time(
    attachment_ids: list[str], capture_times: Mapping[str, datetime]
) -> CaptureOrdering:
    """Order clips chronologically by capture time where it is known.

    Clips without a capture time keep their attachment-order position (their
    slot is fixed); the clips that do have one are sorted, stably, into the
    remaining slots. With fewer than two timed clips there is nothing to sort
    and the basis is ``attachment``. ``by_route`` uses the same order for now
    (the route a creator walked is the order they filmed it in).
    """
    timed = [media_id for media_id in attachment_ids if media_id in capture_times]
    untimed = [media_id for media_id in attachment_ids if media_id not in capture_times]
    if len(timed) < 2:
        return CaptureOrdering(list(attachment_ids), "attachment", untimed)
    attach_index = {media_id: index for index, media_id in enumerate(attachment_ids)}
    ranked = sorted(timed, key=lambda media_id: (capture_times[media_id], attach_index[media_id]))
    slots = [index for index, media_id in enumerate(attachment_ids) if media_id in capture_times]
    ordered = list(attachment_ids)
    for slot, media_id in zip(slots, ranked, strict=True):
        ordered[slot] = media_id
    return CaptureOrdering(ordered, "capture_time", untimed)


# ── Landmark enrichment (best guess, inferred) ────────────────────────────────


def landmark_fact_for_assignment(assignment: Mapping[str, Any]) -> ClipFact | None:
    for fact in understanding_facts(assignment.get("analysis")):
        if fact.kind == "landmark":
            return fact
    return None


def _generation(assignment: Mapping[str, Any]) -> str:
    """The pinned storage generation. Attach stores it as ``storage_generation``."""
    return str(assignment.get("generation") or assignment.get("storage_generation") or "")


def _landmark_key(assignment: Mapping[str, Any], language: str = "") -> str:
    """The cache key of one landmark answer: the storage generation plus the language the
    creator wrote in, so a guess made for another language (or before names followed the
    creator's language) is never reused. No creator text keeps the bare generation."""
    generation = _generation(assignment)
    return f"{generation}|{language}" if language else generation


def _landmark_attempted(assignment: Mapping[str, Any], language: str = "") -> bool:
    """True once a guess (even an empty one) was recorded for this generation + language."""
    analysis = assignment.get("analysis")
    if not isinstance(analysis, dict):
        return False
    return analysis.get(LANDMARK_GENERATION_KEY) == _landmark_key(assignment, language)


def _landmark_prompt_place(assignment: Mapping[str, Any]) -> tuple[str, float | None, float | None]:
    capture = capture_from_assignment(assignment)
    if capture is None:
        return "", None, None
    place = capture.place.label() if capture.place is not None else ""
    loc = capture.coarse_location
    return place, (loc.lat if loc else None), (loc.lon if loc else None)


def _has_landmark_context(assignment: Mapping[str, Any]) -> bool:
    """A place name or coordinate to narrow the guess.

    Without one the model would name a landmark from pixels and world knowledge alone,
    and a wrong `inferred` name flows straight into the planner. It also covers a creator
    who turned "use when and where clips were filmed" off: no where, no landmark call.
    """
    place, lat, lon = _landmark_prompt_place(assignment)
    return bool(place) or (lat is not None and lon is not None)


def _guess_landmark(
    assignment: dict[str, Any], *, ctx: Any, creator_text: str = ""
) -> ClipFact | None:
    """Download the pinned clip, upload it once, ask the landmark agent."""
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.landmark_guess import LandmarkGuessAgent, LandmarkGuessInput  # noqa: PLC0415
    from app.storage import download_generation_to_file  # noqa: PLC0415

    path = str(assignment.get("gcs_path") or "")
    generation = _generation(assignment)
    if not path or not generation:
        return None
    place, lat, lon = _landmark_prompt_place(assignment)
    with tempfile.TemporaryDirectory(prefix="clip-landmark-") as tmpdir:
        local = os.path.join(tmpdir, Path(path).name or "clip.mp4")
        download_generation_to_file(path, local, generation=generation)
        uploaded = default_client().upload_media(local)
        output = LandmarkGuessAgent(default_client()).run(
            LandmarkGuessInput(
                file_uri=uploaded.uri,
                file_mime=getattr(uploaded, "mime_type", None) or "video/mp4",
                place=place,
                lat=lat,
                lon=lon,
                creator_text=creator_text[:400],
            ),
            ctx=ctx,
        )
    if output.is_unknown():
        return None
    # D4: used directly, no confidence gate. Provenance `inferred` is what makes
    # every guess correctable downstream.
    return ClipFact(
        kind="landmark", value=output.name, provenance="inferred", confidence=output.confidence
    )


def _merge_stored_facts(
    analysis: dict[str, Any], new: Iterable[ClipFact], *, replace_kind: str | None = None
) -> list[dict[str, Any]]:
    """Stored fact rows with ``new`` merged in (same kind+provenance is replaced)."""
    incoming = list(new)
    replaced = {(fact.kind, fact.provenance) for fact in incoming}
    kept = [
        row
        for row in (analysis.get(FACTS_KEY) or [])
        if isinstance(row, dict)
        and (row.get("kind"), row.get("provenance")) not in replaced
        and row.get("kind") != replace_kind
    ]
    rows = [*kept, *(fact.model_dump(mode="json", exclude_none=True) for fact in incoming)]
    # Canonical order so a re-merge of unchanged facts compares equal.
    return sorted(rows, key=lambda row: _KIND_ORDER.get(str(row.get("kind")), len(_KIND_ORDER)))


def with_capture_facts(assignment: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``assignment`` with its capture-derived facts stored on the analysis.

    Idempotent. This is what lets every ``clip_record`` reader (chat evidence,
    planner, matchers, later the editor-op tool) see when/where a clip was filmed
    without knowing about assignments.
    """
    facts = capture_facts(capture_from_assignment(assignment))
    if not facts:
        return assignment
    analysis = dict(assignment.get("analysis") or {})
    merged = _merge_stored_facts(analysis, facts)
    if merged == analysis.get(FACTS_KEY):
        return assignment
    analysis[FACTS_KEY] = merged
    return {**assignment, "analysis": analysis}


def with_landmark_fact(
    assignment: dict[str, Any], fact: ClipFact | None, *, language: str = ""
) -> dict[str, Any]:
    """Copy of ``assignment`` with the guess (or the "asked, no answer") recorded."""
    entry = dict(assignment)
    analysis = dict(entry.get("analysis") or {})
    analysis[FACTS_KEY] = _merge_stored_facts(
        analysis, [fact] if fact is not None else [], replace_kind="landmark"
    )
    analysis[LANDMARK_GENERATION_KEY] = _landmark_key(entry, language)
    entry["analysis"] = analysis
    return entry


def _ref_with_analysis(ref: Any, analysis: dict[str, Any]) -> Any:
    model_copy = getattr(ref, "model_copy", None)
    return model_copy(update={"analysis": analysis}) if callable(model_copy) else ref


def enrich_clip_facts(
    results: list[tuple[dict[str, Any], Any]],
    *,
    make_ctx: Any,
    on_updated: Any = None,
    budget_s: float = LANDMARK_BUDGET_S,
    creator_text: str = "",
) -> list[tuple[dict[str, Any], Any]]:
    """Store every clip's facts on its analysis: capture copies, then a landmark guess.

    * Capture-derived facts (exif/geocode) are copied from the assignment's
      ``capture`` onto ``analysis["clip_facts"]``: cheap, deterministic.
    * Every analyzed VIDEO clip that has a place or coordinate and no recorded
      landmark attempt gets one best-guess landmark (provenance ``inferred``). A clip
      with neither is never sent to the model. Fail-open per clip: a provider error
      leaves the clip without one and the plan proceeds, and the whole step is capped
      at ``budget_s`` seconds.

    ``on_updated(entry, ref)`` lets the caller checkpoint each enriched
    assignment so a Celery retry does not pay for the guess twice.
    ``make_ctx(media_id)`` builds the per-call RunContext. ``creator_text`` (the creator's
    own words, when known) makes the landmark names follow their language, and its language
    is part of each guess's cache key (KRI-210). Returns the results
    with entries AND refs updated (the ref carries the new analysis).
    """
    from app.agents.landmark_guess import creator_language  # noqa: PLC0415

    language = creator_language(creator_text)
    updated: list[tuple[dict[str, Any], Any]] = []
    for entry, ref in results:
        new_entry = with_capture_facts(entry)
        if new_entry is not entry:
            ref = _ref_with_analysis(ref, new_entry["analysis"])
            if on_updated is not None:
                try:
                    on_updated(new_entry, ref)
                except Exception as exc:  # noqa: BLE001 — checkpointing must not fail the plan
                    log.warning("clip_facts.checkpoint_failed", error=str(exc)[:240])
        updated.append((new_entry, ref))

    final = list(updated)

    def _record(index: int, fact: ClipFact | None) -> None:
        entry, ref = updated[index]
        new_entry = with_landmark_fact(entry, fact, language=language)
        ref = _ref_with_analysis(ref, new_entry["analysis"])
        final[index] = (new_entry, ref)
        if on_updated is not None:
            try:
                on_updated(new_entry, ref)
            except Exception as exc:  # noqa: BLE001 — checkpointing must not fail the plan
                log.warning("clip_facts.checkpoint_failed", error=str(exc)[:240])

    todo: list[int] = []
    for index, (entry, ref) in enumerate(updated):
        if getattr(ref, "kind", "video") != "video" or _landmark_attempted(entry, language):
            continue
        if not _has_landmark_context(entry):
            # Nothing to narrow a guess with: record "asked, no answer" and never call.
            _record(index, None)
            continue
        todo.append(index)
    if not todo:
        return final

    def run(index: int) -> ClipFact | None:
        entry, _ref = updated[index]
        try:
            # With no creator text the call is exactly the pre-KRI-210 one.
            extra = {"creator_text": creator_text} if language else {}
            return _guess_landmark(entry, ctx=make_ctx(str(entry.get("media_id"))), **extra)
        except Exception as exc:  # noqa: BLE001 — best-effort context, never fatal
            log.warning(
                "clip_facts.landmark_failed",
                media_id=str(entry.get("media_id")),
                error=str(exc)[:240],
            )
            raise

    pool = ThreadPoolExecutor(
        max_workers=min(_LANDMARK_CONCURRENCY, len(todo)), thread_name_prefix="clip-landmark"
    )
    try:
        futures = {pool.submit(run, index): index for index in todo}
        try:
            for future in as_completed(futures, timeout=budget_s):
                try:
                    fact = future.result()
                except Exception:  # noqa: BLE001 — already logged; leave the clip unenriched
                    continue
                _record(futures[future], fact)
        except FutureTimeoutError:
            log.warning(
                "clip_facts.landmark_budget_exceeded",
                budget_s=budget_s,
                skipped=sum(1 for f in futures if not f.done()),
            )
    finally:
        # Never wait on a slow provider: anything still pending is abandoned.
        pool.shutdown(wait=False, cancel_futures=True)
    return final
