"""Pure Main Creator render-policy helpers shared by agent and compiler."""

import re
from typing import Any

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.agents._schemas.edit_format import (
    AUDIO_LED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
)
from app.schemas.edit_proposal import MontageAudioPlan

MAX_MAIN_CREATOR_SELECTED_MEDIA = 12

# Mirrors app.routes.creator_agent._MEDIA_COUNT_NOUN. Kept as a separate copy
# rather than a shared import: the route also uses it inline in its own
# negative/positive scope regexes, and threading a single regex-string
# constant back and forth across the route <-> agent boundary is not worth
# the coupling. Keep both copies identical if the noun list ever changes.
_MEDIA_COUNT_NOUN = r"(?:clips?|videos?|photos?|images?|pictures?|stills?|media|footage)"
_MEDIA_COUNT_TIME_SUFFIX = r"(?:s|secs?|seconds?|ms|milliseconds?|mins?|minutes?|hrs?|hours?)"

# A stated count only names the whole manifest when nothing else in the
# message narrows it: "I uploaded 30 clips, pick the best 5" and "30 clips is
# too many" both mention the manifest size while asking for LESS than all of
# it. Any reduction cue, or a second media count, keeps the legacy default --
# a missed "all" is recoverable, a forced "all" overrides the creator.
_STATED_COUNT_REDUCTION_CUE = (
    r"\b(?:too many|fewer|less|pick|choose|best|top|strongest|favou?rites?|only|just|"
    r"some of|leave out|left out|exclude|excluding|except|skip|remove|drop|cut out|"
    r"without|not all)\b"
)


def _stated_media_count(normalized: str) -> int | None:
    """Extract a creator-stated media quantity; never a duration or timestamp."""

    match = re.search(
        rf"\b(\d{{1,4}})\s+(?:of\s+(?:the|my|your)\s+)?{_MEDIA_COUNT_NOUN}\b",
        normalized,
    )
    if match:
        return int(match.group(1))
    # "all 30" without a trailing noun still names the whole manifest, as long
    # as the number is not immediately a duration ("all 30 seconds").
    match = re.search(
        rf"\ball\s+(\d{{1,4}})\b(?!\s*{_MEDIA_COUNT_TIME_SUFFIX}\b)",
        normalized,
    )
    if match:
        return int(match.group(1))
    return None


def _attached_media_count(manifest: Any) -> int:
    """Count of non-asset media (video/image) attached to this manifest."""

    return sum(
        1
        for ref in getattr(manifest, "media", None) or ()
        if getattr(ref, "kind", None) in {"video", "image"}
    )


def _stated_count_matches_manifest(normalized: str, manifest: Any | None) -> bool:
    if manifest is None:
        return False
    if re.search(_STATED_COUNT_REDUCTION_CUE, normalized):
        return False
    counts = set(
        re.findall(
            rf"\b(\d{{1,4}})\s+(?:of\s+(?:the|my|your)\s+)?{_MEDIA_COUNT_NOUN}\b", normalized
        )
    )
    if len(counts) > 1:
        return False
    stated_count = _stated_media_count(normalized)
    return stated_count is not None and stated_count == _attached_media_count(manifest)


def explicit_scope_from_stated_media_count(request: str, manifest: Any | None) -> bool:
    """True when a creator's stated clip count names their whole manifest.

    "Continue with 16 clips" naming an unambiguous manifest size, with no
    narrowing cue ("pick the best 5"), means "all" just as much as literally
    saying "all". Shared by the creator-agent route (post-model regex fence,
    KRI-129 part A/C) and the Main Creator agent's own parse-time scope
    resolution (KRI-129 part C) so both apply the same rule; kept here rather
    than in either module to avoid a route <-> agent import cycle.
    """

    normalized = " ".join(str(request or "").casefold().split())
    return _stated_count_matches_manifest(normalized, manifest)


def states_explicit_media_narrowing_cue(request: str) -> bool:
    """True when the message itself asks for LESS than the whole manifest.

    Shares ``_STATED_COUNT_REDUCTION_CUE`` (the same wording that keeps a
    stated clip count from being read as "all") so the creator-agent route
    can also use it to decide whether the creator's newest message overrides
    a stale historical answer (KRI-129 part 1): the newest explicit words
    must always win over a prior turn's recorded preference.
    """

    normalized = " ".join(str(request or "").casefold().split())
    return bool(re.search(_STATED_COUNT_REDUCTION_CUE, normalized))


CAPABILITY_DRAFT_GUIDED_PROPOSAL = "draft_guided_proposal"
CAPABILITY_GUIDED_VOICEOVER = "guided_voiceover"
GUIDED_VOICEOVER_EXECUTION_CONTRACT = "guided_voiceover_v1"
CAPABILITY_PHONE_SOURCE_AUDIO = "phone_source_audio"
# Each is present only on phone manifests whose device engine is verified for
# that kind of Visuals media. The capability resolver reads that setting; this
# module never does.
CAPABILITY_PHONE_STILL_IMAGES = "phone_still_images"
CAPABILITY_PHONE_VISUAL_VIDEOS = "phone_visual_videos"


def _requires_guided_voiceover(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> bool:
    return bool(
        manifest.has_voiceover
        and strategy.execution_contract == GUIDED_VOICEOVER_EXECUTION_CONTRACT
        and (strategy.mixed_media_timing is not None or strategy.media_scope == "all")
    )


class MixedMediaTimingUnavailableError(ValueError):
    """The requested per-kind timing cannot be compiled by an available renderer."""


class PhoneMediaUnavailableError(MixedMediaTimingUnavailableError):
    """Phone rendering cannot use media the strategy puts in scope.

    Subclasses the mixed-media error so every existing handler keeps working;
    the route uses the subtype only to tell the creator what went wrong.
    """

    def __init__(
        self,
        message: str,
        *,
        still_images_available: bool = False,
        visual_videos_available: bool = False,
    ) -> None:
        super().__init__(message)
        self.still_images_available = still_images_available
        self.visual_videos_available = visual_videos_available


class PhoneFormatUnavailableError(MixedMediaTimingUnavailableError):
    """The iPhone has no renderer for this edit format or for voiceover audio.

    Subclassed for the same reason as ``PhoneMediaUnavailableError``.
    """

    def __init__(self, message: str, *, voiceover: bool = False) -> None:
        super().__init__(message)
        self.voiceover = voiceover


class MontageCadenceUnavailableError(ValueError):
    """The requested exact cadence conflicts with the available render path."""


def effective_render_program(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    allow_missing_format_capability: bool = False,
) -> str:
    """Resolve the renderer without importing runtime settings or services."""

    strategy_format = coerce_edit_format(strategy.edit_format)
    format_capability = manifest.capabilities.get(f"edit_format:{strategy_format}")
    if format_capability is None and not allow_missing_format_capability:
        raise ValueError(f"edit format {strategy_format!r} is unavailable")
    if format_capability is not None and not format_capability.available:
        raise ValueError(f"edit format {strategy_format!r} is unavailable")
    montage_audio_requires_guided = bool(
        strategy.montage_audio is not None
        and (
            strategy.montage_audio.preserve_source_audio
            or strategy.montage_audio.preview_source_beds
        )
    )
    montage_cadence_requires_guided = (
        strategy.montage_cadence is not None
        or strategy.video_reuse_policy in {"distinct_windows", "allow_repeat"}
    )
    phone = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    if phone is not None:
        if not phone.available:
            if phone.reason_code == "unsupported_phone_audio":
                raise PhoneFormatUnavailableError(
                    phone.reason or "phone rendering does not support voiceover audio",
                    voiceover=True,
                )
            raise MixedMediaTimingUnavailableError(phone.reason or "phone rendering is unavailable")
        # `phone.available` already reflects whether THIS project's recorded
        # voiceover may render on the phone (`CAPABILITY_PHONE_SOURCE_AUDIO`
        # in `app.services.creator_capabilities.resolve_creator_manifest`,
        # KRI-132's montage-family rollout flag + verified narrationAudio
        # gate; the guided-story narration lane stays hard-blocked via a
        # SEPARATE, unconditional `CAPABILITY_GUIDED_VOICEOVER` there) — so
        # reaching here with a voiceover already on the manifest is no
        # longer necessarily a hard block. A strategy asking to newly SWITCH
        # audio_strategy to "voiceover" with none attached yet is a
        # different, still-unsupported request the capability above never
        # considered, so that half of the check remains unconditional.
        if not manifest.has_voiceover and strategy.audio_strategy == "voiceover":
            raise PhoneFormatUnavailableError(
                "phone rendering does not support voiceover audio", voiceover=True
            )
        if not guided_edit_applicable(strategy_format, has_voiceover=False):
            raise PhoneFormatUnavailableError("phone sources require a guided edit format")
        source_ids: set[str] = set()
        if strategy.montage_cadence is not None:
            source_ids.update(strategy.montage_cadence.source_media_ids)
        if strategy.montage_audio is not None:
            source_ids.update(strategy.montage_audio.source_media_ids)
        selected_ids = set(strategy.selected_media_ids) | source_ids
        stills = manifest.capabilities.get(CAPABILITY_PHONE_STILL_IMAGES)
        still_images_available = bool(stills is not None and stills.available)
        videos = manifest.capabilities.get(CAPABILITY_PHONE_VISUAL_VIDEOS)
        visual_videos_available = bool(videos is not None and videos.available)

        def phone_renderable(media: CreatorMediaRef) -> bool:
            if not media.media_id.startswith("asset-"):
                return media.kind == "video"
            if media.kind == "video":
                # A Visuals video compiles exactly like bound footage and the
                # device mixes its sound the same way, so it may also drive
                # audio or cadence.
                return visual_videos_available
            # Visuals-pool photos compile to stills (fullscreen or a supporting
            # card). A still has no sound or source cuts, so it can never drive
            # audio or cadence.
            return (
                still_images_available
                and media.kind == "image"
                and media.media_id not in source_ids
            )

        if any(
            not phone_renderable(media)
            and (strategy.media_scope == "all" or media.media_id in selected_ids)
            for media in manifest.media
        ):
            raise PhoneMediaUnavailableError(
                "phone rendering requires bound video sources",
                still_images_available=still_images_available,
                visual_videos_available=visual_videos_available,
            )
    guided = manifest.capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
    guided_voiceover = manifest.capabilities.get(CAPABILITY_GUIDED_VOICEOVER)
    guided_voiceover_requested = _requires_guided_voiceover(manifest, strategy)
    if (
        manifest.has_voiceover
        and (strategy.mixed_media_timing is not None or strategy.media_scope == "all")
        and not guided_voiceover_requested
        and not (
            guided_voiceover is not None and guided_voiceover.reason_code == "disabled_by_setting"
        )
    ):
        raise MixedMediaTimingUnavailableError(
            "explicit mixed-media timing or all-media scope with a recorded voiceover "
            "requires the guided_voiceover_v1 execution contract"
        )
    if strategy.execution_contract is not None and not guided_voiceover_requested:
        raise MixedMediaTimingUnavailableError(
            "guided_voiceover_v1 requires a recorded voiceover and explicit mixed-media timing "
            "or all-media scope"
        )
    if guided_voiceover_requested:
        if manifest.narration is None:
            raise MixedMediaTimingUnavailableError(
                "guided voiceover requires a pinned narration identity"
            )
        if not (guided_voiceover and guided_voiceover.available):
            reason = (
                guided_voiceover.reason
                if guided_voiceover is not None
                else "guided voiceover capability is not advertised"
            )
            raise MixedMediaTimingUnavailableError(reason)
        return "guided"
    if strategy.media_scope == "all":
        if not (guided and guided.available):
            raise MixedMediaTimingUnavailableError(
                "all-media scope requires the guided proposal capability"
            )
        return "guided"
    if montage_cadence_requires_guided:
        if manifest.has_voiceover or strategy.audio_strategy == "voiceover":
            raise MontageCadenceUnavailableError(
                "exact montage cadence is unavailable with a recorded voiceover"
            )
        if not (guided and guided.available):
            raise MontageCadenceUnavailableError(
                "source-aware montage requires the guided proposal capability"
            )
        return "guided"
    if phone is not None:
        if manifest.has_voiceover:
            # KRI-132: reaching here with `manifest.has_voiceover` true
            # already means `phone.available` (checked at the top of this
            # function) implies the montage-family rollout flag is on,
            # narrationAudio is verified, and `strategy_format` is a
            # montage-family format (`guided_edit_applicable(strategy_format,
            # has_voiceover=False)`, checked a few lines above). We are also
            # past every earlier voiceover-specific guided-only gate above
            # (`guided_voiceover_requested`'s return, the has_voiceover +
            # mixed_media_timing/all-media-scope guided_voiceover_v1
            # requirement, and the plain `media_scope == "all"` branch), so
            # this is neither a guided-story voiceover request nor an
            # all-media/mixed-media-timing strategy. Resolve "native",
            # mirroring `_dispatch_item_render`'s own
            # `guided_applicable = guided_edit_applicable(strategy_format,
            # has_voiceover=True)` (always False for a voiceover item) and
            # `routes/creator_agent.py`'s `bypass_guided_edit_gate =
            # render_program == "native"` a few lines after this resolves.
            #
            # The montage-family phone compiler
            # (`app.pipeline.phone_montage_plan.compile_phone_montage_plan`)
            # only ever binds clip-lane sources bound to the device
            # (`PhoneSourceBinding`) — it has no Visuals-pool asset support
            # at all, unlike the guided-story compiler. An explicit
            # selection of Visuals-pool ("asset-*") media alongside a
            # voiceover must therefore fail closed here rather than silently
            # resolving to a native plan that drops it.
            selected_pool_media = {
                media.media_id
                for media in manifest.media
                if media.media_id.startswith("asset-") and media.media_id in selected_ids
            }
            if selected_pool_media:
                # Mirrors the wording `_phone_media_unavailable_message` in
                # `routes/creator_agent.py` renders for the no-capability
                # case — accurate here regardless of whether stillImages/
                # visualVideos happen to be verified, since THIS render
                # program (montage-family voiceover) cannot use Visuals
                # media either way.
                raise PhoneMediaUnavailableError(
                    "phone voiceover rendering only uses this project's own footage, not Visuals",
                    still_images_available=False,
                    visual_videos_available=False,
                )
            return "native"
        if not (guided and guided.available):
            raise MixedMediaTimingUnavailableError(
                "phone rendering requires the guided proposal capability"
            )
        return "guided"
    native_required = (
        manifest.has_voiceover
        or (
            strategy.audio_strategy in {"original_audio", "voiceover"}
            and not montage_audio_requires_guided
        )
        or strategy_format in AUDIO_LED_EDIT_FORMATS
        or not guided_edit_applicable(strategy_format, has_voiceover=manifest.has_voiceover)
    )
    if native_required:
        return "native"
    # Native montage only receives attached clips. A per-kind timing request
    # needs the guided specialist so pool photos are selected and compiled too.
    mixed_media_timing_requires_specialist = bool(
        strategy.mixed_media_timing is not None
        and {"image", "video"}.issubset({media.kind for media in manifest.media})
    )
    if (mixed_media_timing_requires_specialist or montage_audio_requires_guided) and not (
        guided and guided.available
    ):
        reason = (
            "mixed-media timing requires the guided proposal capability"
            if mixed_media_timing_requires_specialist
            else "source-aware montage requires the guided proposal capability"
        )
        raise MixedMediaTimingUnavailableError(reason)
    if (
        guided
        and guided.available
        and (
            strategy.render_program == "guided"
            or mixed_media_timing_requires_specialist
            or montage_audio_requires_guided
        )
    ):
        return "guided"
    return "native"


def normalize_creator_strategy_media(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    repair_model_output: bool = False,
) -> CreativeStrategy:
    """Bound exact refs; optional repair is reserved for the model boundary."""

    if strategy.video_reuse_policy in {"distinct_windows", "allow_repeat"}:
        strategy = strategy.model_copy(update={"direction": "fast_montage"})
    if (
        (
            strategy.montage_cadence is not None
            or strategy.video_reuse_policy in {"distinct_windows", "allow_repeat"}
        )
        and strategy.audio_strategy == "original_audio"
        and strategy.montage_audio is None
    ):
        strategy = strategy.model_copy(
            update={
                "montage_audio": MontageAudioPlan(
                    preserve_source_audio=True,
                    source_media_ids=(
                        strategy.montage_cadence.source_media_ids
                        if strategy.montage_cadence
                        else []
                    ),
                )
            }
        )
    if (
        manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO, None)
        and manifest.capabilities[CAPABILITY_PHONE_SOURCE_AUDIO].available
        and guided_edit_applicable(strategy.edit_format, has_voiceover=manifest.has_voiceover)
        and strategy.audio_strategy == "original_audio"
        and strategy.montage_audio is None
    ):
        strategy = strategy.model_copy(
            update={
                "montage_audio": MontageAudioPlan(
                    preserve_source_audio=True,
                )
            }
        )
    effective_program = effective_render_program(
        manifest,
        strategy,
        allow_missing_format_capability=repair_model_output,
    )
    manifest_ids = {media.media_id for media in manifest.media}
    unknown = [media_id for media_id in strategy.selected_media_ids if media_id not in manifest_ids]
    if unknown and not repair_model_output:
        raise ValueError("strategy selected_media_ids must reference manifest media")

    selected_media_ids: list[str] = []
    if strategy.montage_audio is not None:
        audio_ids = list(dict.fromkeys(strategy.montage_audio.source_media_ids))
        unknown_audio = [media_id for media_id in audio_ids if media_id not in manifest_ids]
        if unknown_audio and not repair_model_output:
            raise ValueError("montage audio sources must reference manifest media")
        valid_audio = [media_id for media_id in audio_ids if media_id in manifest_ids]
        if any(
            next(media for media in manifest.media if media.media_id == media_id).kind != "video"
            for media_id in valid_audio
        ):
            raise ValueError("montage audio sources must be videos")
        strategy = strategy.model_copy(
            update={
                "montage_audio": strategy.montage_audio.model_copy(
                    update={"source_media_ids": valid_audio}
                )
            }
        )
    if strategy.montage_cadence is not None:
        cadence_ids = list(dict.fromkeys(strategy.montage_cadence.source_media_ids))
        unknown_cadence = [media_id for media_id in cadence_ids if media_id not in manifest_ids]
        if unknown_cadence:
            raise ValueError("montage cadence sources must reference manifest media")
        by_id = {media.media_id: media for media in manifest.media}
        if any(by_id[media_id].kind != "video" for media_id in cadence_ids):
            raise ValueError("montage cadence sources must be videos")
        strategy = strategy.model_copy(
            update={
                "montage_cadence": strategy.montage_cadence.model_copy(
                    update={"source_media_ids": cadence_ids}
                )
            }
        )
    if effective_program == "native":
        native_ids = [
            media.media_id for media in manifest.media if not media.media_id.startswith("asset-")
        ]
        native_id_set = set(native_ids)
        selected_media_ids = list(
            dict.fromkeys(
                media_id for media_id in strategy.selected_media_ids if media_id in native_id_set
            )
        )[:MAX_MAIN_CREATOR_SELECTED_MEDIA]
        if not selected_media_ids and repair_model_output:
            selected_media_ids = native_ids[:MAX_MAIN_CREATOR_SELECTED_MEDIA]
        if not selected_media_ids:
            raise ValueError("native rendering requires at least one attached clip")
    elif effective_program == "guided" and strategy.media_scope == "selected":
        # KRI-129: this branch used to fall straight through to the "guided
        # takes every manifest media" default below, silently discarding the
        # creator's explicit subset for every non-native program. Unknown ids
        # are already rejected above unless repair_model_output is set, so
        # filtering to manifest_ids here is a bound, not a new trust
        # boundary. An empty result (e.g. every id was unknown and got
        # repaired away) intentionally falls back to today's "all" behavior.
        selected_media_ids = list(
            dict.fromkeys(
                media_id for media_id in strategy.selected_media_ids if media_id in manifest_ids
            )
        )
    # A confirmation must not promise more source time than a once-only
    # video montage can supply. Photos and recorded narration have separate
    # duration contracts; unknown video durations must be resolved downstream.
    duration_s = strategy.target_duration_s
    guided_takes_everything = effective_program == "guided" and not selected_media_ids
    selected = [
        media
        for media in manifest.media
        if guided_takes_everything or media.media_id in selected_media_ids
    ]
    if (
        strategy.video_reuse_policy == "once"
        and not manifest.has_voiceover
        and strategy.edit_format == "montage"
        and selected
        and all(media.kind == "video" and media.duration_s for media in selected)
    ):
        source_capacity_s = int(sum(media.duration_s for media in selected))
        if source_capacity_s >= 3:
            duration_s = min(duration_s, source_capacity_s)
    return strategy.model_copy(
        update={
            "target_duration_s": duration_s,
            "render_program": effective_program,
            "selected_media_ids": selected_media_ids,
        }
    )


__all__ = [
    "MAX_MAIN_CREATOR_SELECTED_MEDIA",
    "CAPABILITY_DRAFT_GUIDED_PROPOSAL",
    "CAPABILITY_GUIDED_VOICEOVER",
    "GUIDED_VOICEOVER_EXECUTION_CONTRACT",
    "MixedMediaTimingUnavailableError",
    "MontageCadenceUnavailableError",
    "PhoneFormatUnavailableError",
    "PhoneMediaUnavailableError",
    "effective_render_program",
    "explicit_scope_from_stated_media_count",
    "normalize_creator_strategy_media",
    "states_explicit_media_narrowing_cue",
]
