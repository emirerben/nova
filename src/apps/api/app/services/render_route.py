"""One route resolver for an approved creator plan (KRI-470 PR-D).

``resolve_route`` is a pure function of the APPROVED plan: the pinned render
contract, the strategy fields the dispatchers branch on, media facts already in
the approval snapshot, and the platform.  It reads no request text -- no chat
message, no ``creator_request``, no brief prose -- so rewording a message can
never change a route.  ``tests/services/test_render_route.py`` guards that.

PR-D ships it SHADOW-FIRST.  For jobs carrying the plan-authority stamp the
dispatchers (``tasks/generative_build.py``) compute the same resolution from the
same persisted inputs and record a ``route_mismatch`` pipeline event when it
disagrees with the legacy decision; the LEGACY decision still renders.  PR-F
retires the legacy gates one at a time, using that log.

Three parts, top to bottom: the pure resolver; the job adapter that reads the
persisted job shape (and the stamp); and the shadow comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import structlog

from app.agents._schemas.edit_format import (
    GUIDED_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
    PHONE_RENDER_SUPPORTED_FORMATS,
    coerce_edit_format,
    render_program_for_intent,
)
from app.services.creator_execution_contract import requests_guided_voiceover
from app.services.creator_render_contract import (
    ADAPTER_DECLARATIONS,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_FIELD_PATHS,
    AdapterDeclaration,
    CreatorRenderContract,
    DeclineReason,
    read_render_contract,
    text_field_path,
)

log = structlog.get_logger()

Platform = Literal["phone", "cloud"]


class Route(StrEnum):
    """Every route the dispatchers can take today.

    Values are stable: they are stored in the contract (``route``) and in
    ``route_mismatch`` events.  ``subtitled`` / ``talking_head`` / ``narrated`` /
    ``guided_story`` exist on both platforms; the contract also stores the platform.
    """

    # Shared
    GUIDED_STORY = "guided_story"
    SUBTITLED = "subtitled"
    TALKING_HEAD = "talking_head"
    NARRATED = "narrated"
    # Phone montage family (``_run_phone_*``)
    SPEECH_MONTAGE = "speech_montage"
    VOICEOVER_MONTAGE = "voiceover_montage"
    UNIFIED_MONTAGE = "unified_montage"
    USER_SONG_MONTAGE = "user_song_montage"
    LIPSYNC_MONTAGE = "lipsync_montage"
    # Cloud classic archetypes (``_resolve_archetype``) and slides
    MONTAGE = "montage"
    VOICEOVER = "voiceover"
    DAY_VLOG = "day_vlog"
    SINGLE_HERO = "single_hero"
    SLIDES = "slides"


@dataclass(frozen=True)
class RouteCapabilities:
    """Rollout state the dispatchers consult (flags), as plain booleans.

    A capability is not part of the approved plan: it can change between approval
    and render.  A route the plan needs but a capability forbids is a typed
    refusal, never a silent reroute.  Defaults are "everything available" so the
    approval-time stamp records the plan's own route.
    """

    narrated: bool = True
    self_narration: bool = True
    subtitled: bool = True
    talking_head: bool = True
    day_vlog: bool = True
    single_hero: bool = True
    phone_talking_head: bool = True


def route_capabilities_from_settings() -> RouteCapabilities:
    """The live rollout flags the legacy dispatchers consult (read per call)."""

    from app.config import settings  # noqa: PLC0415
    from app.services.phone_rollout import phone_talking_head_supported  # noqa: PLC0415

    return RouteCapabilities(
        narrated=bool(settings.narrated_archetype_enabled),
        self_narration=bool(settings.narrated_self_narration_enabled),
        subtitled=bool(settings.subtitled_archetype_enabled),
        talking_head=bool(settings.edit_format_talking_head_enabled),
        day_vlog=bool(settings.edit_format_day_vlog_enabled),
        single_hero=bool(settings.edit_format_single_hero_enabled),
        phone_talking_head=bool(phone_talking_head_supported()),
    )


@dataclass(frozen=True)
class RouteInputs:
    """Everything the resolver may read.  There is no free-text field, on purpose."""

    platform: Platform
    contract: CreatorRenderContract
    edit_format: str | None
    strategy: Mapping[str, Any] | None = None
    guided_snapshot_present: bool = False
    voiceover_present: bool = False
    song_present: bool = False
    clip_count: int | None = None
    # Per clip in the approval snapshot: True / False, or None when never analysed.
    clip_has_speech: tuple[bool | None, ...] = ()
    capabilities: RouteCapabilities = RouteCapabilities()


@dataclass(frozen=True)
class RouteResolution:
    """A route, a typed refusal, or a choice the creator has to make.

    ``reason`` is the PR-A ``DeclineReason`` for a refusal (``needs_choice`` for a
    choice).  ``choice_kind`` names the open decision for PR-F's collector.
    ``drivers`` are the contract/strategy/media field paths the decision read.
    """

    outcome: Literal["route", "refusal", "needs_choice"]
    route: Route | None = None
    reason: DeclineReason | None = None
    field_path: str | None = None
    alternative: str | None = None
    choice_kind: str | None = None
    drivers: tuple[str, ...] = ()
    adapter: str | None = None

    @property
    def label(self) -> str:
        if self.outcome == "route":
            return str(self.route.value) if self.route else "route"
        return f"{self.outcome}:{self.reason}"


def _route(route: Route, adapter: str | None, drivers: list[str]) -> RouteResolution:
    return RouteResolution("route", route=route, adapter=adapter, drivers=tuple(drivers))


def _refuse(
    reason: DeclineReason, field_path: str | None, alternative: str, drivers: list[str]
) -> RouteResolution:
    return RouteResolution(
        "refusal",
        reason=reason,
        field_path=field_path,
        alternative=alternative,
        drivers=tuple(drivers),
    )


def _choice(kind: str, field_path: str | None, alternative: str, drivers: list[str]):
    return RouteResolution(
        "needs_choice",
        reason="needs_choice",
        choice_kind=kind,
        field_path=field_path,
        alternative=alternative,
        drivers=tuple(drivers),
    )


_PHONE_ADAPTER = {
    Route.GUIDED_STORY: "phone_guided_unified_montage",
    Route.UNIFIED_MONTAGE: "phone_guided_unified_montage",
    Route.USER_SONG_MONTAGE: "phone_guided_unified_montage",
    Route.LIPSYNC_MONTAGE: "phone_guided_unified_montage",
    Route.SPEECH_MONTAGE: "phone_speech_montage",
    Route.VOICEOVER_MONTAGE: "phone_voiceover_montage",
    Route.SUBTITLED: "phone_subtitled",
    Route.TALKING_HEAD: "phone_subtitled",
    Route.NARRATED: "phone_narrated",
}
_CLOUD_ADAPTER = {
    Route.GUIDED_STORY: "cloud_guided_story",
    Route.SLIDES: "cloud_slides",
    **{
        route: "cloud_classic"
        for route in (
            Route.MONTAGE,
            Route.VOICEOVER,
            Route.NARRATED,
            Route.SUBTITLED,
            Route.TALKING_HEAD,
            Route.DAY_VLOG,
            Route.SINGLE_HERO,
        )
    },
}

# Structural declines only: ``evidence_missing`` is the verifier's job after render.
_ROUTE_LEVEL_REASONS = ("capability_unavailable", "requirement_conflict")


def _declaration(platform: Platform, adapter: str) -> AdapterDeclaration:
    if platform == "phone":
        return ADAPTER_DECLARATIONS[adapter]
    from app.services.cloud_render_contract import CLOUD_ADAPTER_DECLARATIONS  # noqa: PLC0415

    return CLOUD_ADAPTER_DECLARATIONS[adapter]


def _present_requirements(contract: CreatorRenderContract) -> list[tuple[str, str | None]]:
    """(requirement, field path) for each requirement this contract actually carries."""

    rows: list[tuple[str, str | None]] = []
    if contract.duration_s is not None:
        rows.append(("duration_s", REQUIREMENT_FIELD_PATHS["duration_s"]))
    if contract.require_voiceover:
        rows.append(("require_voiceover", REQUIREMENT_FIELD_PATHS["require_voiceover"]))
    if contract.audio_source_ids:
        rows.append(("audio_source_ids", REQUIREMENT_FIELD_PATHS["audio_source_ids"]))
    if contract.original_audio is not None:
        rows.append(("original_audio", REQUIREMENT_FIELD_PATHS["original_audio"]))
    if contract.exact_texts:
        rows.append(("exact_texts", text_field_path(contract.exact_texts[0])))
    if contract.order_required:
        rows.append(("order_required", REQUIREMENT_FIELD_PATHS["order_required"]))
    return rows


def _adapter_gate(
    platform: Platform, route: Route, contract: CreatorRenderContract, drivers: list[str]
) -> RouteResolution:
    """The chosen route, unless its adapter structurally declines a pinned requirement."""

    adapter = (_PHONE_ADAPTER if platform == "phone" else _CLOUD_ADAPTER).get(route)
    if adapter is None:
        return _route(route, None, drivers)
    declines = _declaration(platform, adapter).declines
    for requirement, field_path in _present_requirements(contract):
        decline = declines.get(requirement)
        if decline is not None and decline.reason in _ROUTE_LEVEL_REASONS:
            driver = f"contract.{requirement}"
            return _refuse(
                decline.reason,
                field_path,
                decline.alternative,
                [*drivers, driver] if driver not in drivers else drivers,
            )
    return _route(route, adapter, drivers)


def _self_narration(
    inp: RouteInputs, fmt: str, drivers: list[str], *, cloud: bool
) -> RouteResolution:
    """No recorded voice: the footage's own speech spines the edit.

    One clip -> subtitled, several -> talking_head.  Speech facts only ever
    REFUSE (every clip analysed and none speaks); an unanalysed clip never blocks,
    because the renderer measures speech again at render time.
    """

    caps = inp.capabilities
    if fmt in NARRATED_EDIT_FORMATS and not caps.self_narration:
        return _refuse(
            "capability_unavailable",
            "edit_format",
            "Record your voice for this edit, or ask for a different format.",
            [*drivers, "capabilities.self_narration"],
        )
    drivers = [*drivers, "media.clip_count"]
    if inp.clip_has_speech and all(flag is False for flag in inp.clip_has_speech):
        return _refuse(
            "capability_unavailable",
            "edit_format",
            "None of your clips has speech to carry this edit. Record a voiceover, "
            "or ask for a montage.",
            [*drivers, "media.speech"],
        )
    multi = (inp.clip_count or 0) > 1
    route = Route.TALKING_HEAD if multi and fmt != "subtitled" else Route.SUBTITLED
    if route is Route.TALKING_HEAD and not (
        caps.talking_head if cloud else caps.phone_talking_head
    ):
        return _refuse(
            "capability_unavailable",
            "edit_format",
            "Use a single clip with your speech, or ask for a montage.",
            [*drivers, "capabilities.talking_head"],
        )
    if route is Route.SUBTITLED and fmt == "subtitled" and not caps.subtitled:
        return _refuse(
            "capability_unavailable",
            "edit_format",
            "Ask for a different format.",
            [*drivers, "capabilities.subtitled"],
        )
    return _adapter_gate("cloud" if cloud else "phone", route, inp.contract, drivers)


def _resolve_phone(inp: RouteInputs, fmt: str, drivers: list[str]) -> RouteResolution:
    contract = inp.contract
    voice = contract.require_voiceover
    if inp.guided_snapshot_present:
        drivers.append("guided_edit")
        return _adapter_gate("phone", Route.GUIDED_STORY, contract, drivers)
    drivers.append("edit_format")
    if fmt not in PHONE_RENDER_SUPPORTED_FORMATS:
        return _refuse(
            "capability_unavailable",
            "edit_format",
            "Ask for a montage, a subtitled clip, or a narrated edit.",
            drivers,
        )
    if fmt in GUIDED_EDIT_FORMATS:
        if voice:
            return _adapter_gate("phone", Route.VOICEOVER_MONTAGE, contract, drivers)
        if contract.audio_source_ids:
            return _adapter_gate("phone", Route.SPEECH_MONTAGE, contract, drivers)
        if (inp.strategy or {}).get("audio_strategy") == "user_song":
            lipsync = (inp.strategy or {}).get("song_sync") == "lipsync"
            drivers.append("song_sync")
            route = Route.LIPSYNC_MONTAGE if lipsync else Route.USER_SONG_MONTAGE
            return _adapter_gate("phone", route, contract, drivers)
        return _adapter_gate("phone", Route.UNIFIED_MONTAGE, contract, drivers)
    if fmt == "subtitled" and voice:
        return _refuse(
            "requirement_conflict",
            "edit_format",
            "A subtitled edit uses the clip's own audio. Drop the recorded voice, "
            "or ask for a narrated edit.",
            drivers,
        )
    if fmt == "subtitled" or (fmt in NARRATED_EDIT_FORMATS and not voice):
        return _self_narration(inp, fmt, drivers, cloud=False)
    if fmt in NARRATED_EDIT_FORMATS and voice:
        return _adapter_gate("phone", Route.NARRATED, contract, drivers)
    return _refuse("capability_unavailable", "edit_format", "Ask for a montage.", drivers)


def _resolve_cloud(inp: RouteInputs, fmt: str, drivers: list[str]) -> RouteResolution:
    contract = inp.contract
    caps = inp.capabilities
    voice = contract.require_voiceover
    strategy = inp.strategy or {}
    drivers.append("edit_format")
    if fmt == "slides":
        return _adapter_gate("cloud", Route.SLIDES, contract, drivers)
    if inp.guided_snapshot_present:
        drivers.append("guided_edit")
        program = strategy.get("render_program") or render_program_for_intent(
            fmt, has_voiceover=voice
        )
        drivers.append("render_program")
        if program == "guided" or requests_guided_voiceover(strategy):
            return _adapter_gate("cloud", Route.GUIDED_STORY, contract, drivers)
        return _refuse(
            "requirement_conflict",
            "edit_format",
            "This plan has a guided story but the edit format is audio-led. "
            "Pick one: the guided story, or the spoken/narrated edit.",
            drivers,
        )
    if fmt in GUIDED_EDIT_FORMATS:
        if fmt == "day_vlog" and not caps.day_vlog:
            return _refuse(
                "capability_unavailable",
                "edit_format",
                "Ask for a montage.",
                [*drivers, "capabilities.day_vlog"],
            )
        if fmt == "single_hero" and not caps.single_hero:
            return _refuse(
                "capability_unavailable",
                "edit_format",
                "Ask for a montage.",
                [*drivers, "capabilities.single_hero"],
            )
        if voice:
            return _adapter_gate("cloud", Route.VOICEOVER, contract, drivers)
        route = {"day_vlog": Route.DAY_VLOG, "single_hero": Route.SINGLE_HERO}.get(
            fmt, Route.MONTAGE
        )
        return _adapter_gate("cloud", route, contract, drivers)
    if fmt in NARRATED_EDIT_FORMATS:
        if voice:
            if not caps.narrated:
                return _refuse(
                    "capability_unavailable",
                    "edit_format",
                    "Ask for a montage with your voice, or a different format.",
                    [*drivers, "capabilities.narrated"],
                )
            return _adapter_gate("cloud", Route.NARRATED, contract, drivers)
        return _self_narration(inp, fmt, drivers, cloud=True)
    if voice:
        # subtitled / talking_head are spined by the clip's own audio.
        return _refuse(
            "requirement_conflict",
            "edit_format",
            "This format uses the clip's own audio. Drop the recorded voice, "
            "or ask for a narrated edit.",
            drivers,
        )
    if fmt == "subtitled":
        if not caps.subtitled:
            return _refuse(
                "capability_unavailable",
                "edit_format",
                "Ask for a different format.",
                [*drivers, "capabilities.subtitled"],
            )
        return _adapter_gate("cloud", Route.SUBTITLED, contract, drivers)
    if fmt == "talking_head":
        if not caps.talking_head:
            return _refuse(
                "capability_unavailable",
                "edit_format",
                "Ask for a different format.",
                [*drivers, "capabilities.talking_head"],
            )
        if inp.clip_has_speech and all(flag is False for flag in inp.clip_has_speech):
            return _refuse(
                "capability_unavailable",
                "edit_format",
                "None of your clips has speech to carry this edit. Ask for a montage.",
                [*drivers, "media.speech"],
            )
        return _adapter_gate("cloud", Route.TALKING_HEAD, contract, drivers)
    return _refuse("capability_unavailable", "edit_format", "Ask for a montage.", drivers)


def resolve_route(inp: RouteInputs) -> RouteResolution:
    """The one route an approved plan resolves to (pure; no request text)."""

    contract = inp.contract
    drivers: list[str] = []
    if contract.unresolved:
        return _choice(
            "unresolved_requirement",
            None,
            "Tell me which option you want and I'll continue.",
            ["contract.unresolved"],
        )
    strategy = inp.strategy or {}
    fmt = coerce_edit_format(inp.edit_format)
    song_strategy = strategy.get("audio_strategy") == "user_song"
    if song_strategy:
        drivers.append("audio_strategy")
    if contract.audio_source_ids and (contract.require_voiceover or song_strategy):
        return _refuse(
            "requirement_conflict",
            REQUIREMENT_FIELD_PATHS["audio_source_ids"],
            "Choose one soundtrack: the camera audio, your voice, or your song.",
            [
                "contract.audio_source_ids",
                "contract.require_voiceover" if contract.require_voiceover else "audio_strategy",
            ],
        )
    if contract.require_voiceover:
        drivers.append("contract.require_voiceover")
        if not inp.voiceover_present:
            return _choice(
                "voiceover_recording",
                REQUIREMENT_FIELD_PATHS["require_voiceover"],
                "Record or upload your voice, or tell me to use music instead.",
                [*drivers, "media.voiceover"],
            )
    if contract.audio_source_ids:
        drivers.append("contract.audio_source_ids")
    if song_strategy and not inp.song_present:
        return _choice(
            "song_upload",
            "audio_strategy",
            "Upload your song, or tell me to use library music instead.",
            [*drivers, "media.song"],
        )
    if inp.platform == "cloud" and song_strategy:
        return _refuse(
            "capability_unavailable",
            "audio_strategy",
            "Your own song works in a phone montage. Ask for that, or use library music.",
            drivers,
        )
    if inp.platform == "phone":
        return _resolve_phone(inp, fmt, drivers)
    return _resolve_cloud(inp, fmt, drivers)


# --- Job adapter -------------------------------------------------------------------
#
# Reads the persisted job shape (assembly_plan + all_candidates) -- the same inputs the
# dispatchers hold -- and nothing else.  The only strategy keys read are the plan keys
# named below; the approval snapshot contributes per-clip speech facts.


def platform_for(assembly: Mapping[str, Any]) -> Platform:
    from app.services.phone_sources import PHONE_SOURCES_FIELD  # noqa: PLC0415

    return "phone" if PHONE_SOURCES_FIELD in assembly else "cloud"


def _clip_speech_facts(assembly: Mapping[str, Any]) -> tuple[bool | None, ...]:
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    binding = assembly.get("creator_brief_binding")
    snapshot = binding.get("media_snapshot") if isinstance(binding, Mapping) else None
    rows = snapshot.get("clip_assignments") if isinstance(snapshot, Mapping) else None
    facts: list[bool | None] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        analysis = row.get("analysis")
        if not isinstance(analysis, Mapping) or not analysis:
            facts.append(None)
            continue
        facts.append(bool(clip_record(dict(analysis), kind="video").speech.has_speech))
    return tuple(facts)


def route_inputs_from_job(
    assembly: Mapping[str, Any],
    candidates: Mapping[str, Any],
    *,
    platform: Platform | None = None,
    capabilities: RouteCapabilities | None = None,
    contract: CreatorRenderContract | None = None,
) -> RouteInputs | None:
    """The resolver's inputs from a persisted job, or None when it has no contract."""

    contract = contract or read_render_contract(assembly)
    if contract is None:
        return None
    strategy = candidates.get("creator_strategy")
    song = candidates.get("user_song")
    song_dict = song if isinstance(song, Mapping) and song.get("gcs_path") else None
    return RouteInputs(
        platform=platform or platform_for(assembly),
        contract=contract,
        edit_format=candidates.get("declared_edit_format", candidates.get("edit_format")),
        strategy=strategy if isinstance(strategy, Mapping) else None,
        guided_snapshot_present=isinstance(assembly.get("guided_edit"), Mapping),
        voiceover_present=bool(candidates.get("voiceover_gcs_path")),
        song_present=song_dict is not None,
        clip_count=len(candidates.get("clip_paths") or []),
        clip_has_speech=_clip_speech_facts(assembly),
        capabilities=capabilities or RouteCapabilities(),
    )


def stamp_route(assembly: Mapping[str, Any], candidates: Mapping[str, Any]) -> dict[str, Any]:
    """``assembly`` with the contract's ``route`` recorded -- plan-authority jobs only.

    Called where the contract is stamped (``generative_jobs``) and again once the
    guided snapshot is attached at dispatch (``content_plan_build``).  A job without
    the plan-authority stamp comes back as the SAME object (byte-identical).  Only a
    resolved route is recorded: a refusal or open choice leaves ``route`` unset, and
    the dispatcher's shadow check reports it.  Never raises: a resolver fault must not
    stop a job from being created, so it leaves the job unstamped.
    """

    from app.services.creator_render_contract import CONTRACT_FIELD  # noqa: PLC0415

    unchanged = assembly if isinstance(assembly, dict) else dict(assembly)
    if candidates.get(PLAN_AUTHORITY_FIELD) is None or CONTRACT_FIELD not in assembly:
        return unchanged
    try:
        inputs = route_inputs_from_job(assembly, candidates)
        if inputs is None:
            return unchanged
        resolution = resolve_route(inputs)
        if resolution.route is None:
            return unchanged
        stamped = inputs.contract.rebind(
            route=resolution.route.value, route_platform=inputs.platform
        )
        return {**assembly, CONTRACT_FIELD: stamped.model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001 -- the stamp is advisory; never block job creation
        log.warning("route_stamp_failed", error_class=type(exc).__name__)
        return unchanged


# --- Shadow comparison -------------------------------------------------------------

ROUTE_MISMATCH_EVENT = "route_mismatch"


def shadow_route_check(
    *,
    job_id: str,
    assembly: Mapping[str, Any],
    candidates: Mapping[str, Any],
    platform: Platform,
    legacy_route: str,
    point: str,
) -> None:
    """Record ``route_mismatch`` when the resolver disagrees with the legacy decision.

    Shadow only: never raises, never changes control flow.  Unstamped jobs return
    before reading anything.  Callers must hold no ``FOR UPDATE`` on the jobs row:
    ``record_pipeline_event`` writes that row from a separate connection.  The event
    carries typed values and field paths only (no request text, URLs or user ids).
    """

    if candidates.get(PLAN_AUTHORITY_FIELD) is None:
        return
    try:
        inputs = route_inputs_from_job(
            assembly,
            candidates,
            platform=platform,
            capabilities=route_capabilities_from_settings(),
        )
        if inputs is None:
            return
        resolution = resolve_route(inputs)
        if resolution.outcome == "route" and resolution.route.value == legacy_route:
            return
        from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

        record_pipeline_event(
            "assembly",
            ROUTE_MISMATCH_EVENT,
            {
                "point": point,
                "platform": platform,
                "legacy_route": legacy_route,
                "resolver_outcome": resolution.outcome,
                "resolver_route": resolution.route.value if resolution.route else None,
                "decline_reason": resolution.reason,
                "field_path": resolution.field_path,
                "choice_kind": resolution.choice_kind,
                "stamped_route": inputs.contract.route,
                "contract_digest": inputs.contract.digest,
                "drivers": list(resolution.drivers),
            },
        )
    except Exception as exc:  # noqa: BLE001 -- shadow mode must never affect a render
        log.warning(
            "route_shadow_check_failed",
            job_id=job_id,
            point=point,
            error_class=type(exc).__name__,
        )
