"""Pure Main Creator render-policy helpers shared by agent and compiler."""

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.edit_format import (
    AUDIO_LED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
)
from app.schemas.edit_proposal import MontageAudioPlan

MAX_MAIN_CREATOR_SELECTED_MEDIA = 12
CAPABILITY_DRAFT_GUIDED_PROPOSAL = "draft_guided_proposal"
CAPABILITY_GUIDED_VOICEOVER = "guided_voiceover"
GUIDED_VOICEOVER_EXECUTION_CONTRACT = "guided_voiceover_v1"


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
    montage_cadence_requires_guided = strategy.montage_cadence is not None
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

    if (
        strategy.montage_cadence is not None
        and strategy.audio_strategy == "original_audio"
        and strategy.montage_audio is None
    ):
        strategy = strategy.model_copy(
            update={
                "montage_audio": MontageAudioPlan(
                    preserve_source_audio=True,
                    source_media_ids=strategy.montage_cadence.source_media_ids,
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
    return strategy.model_copy(
        update={
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
    "effective_render_program",
    "normalize_creator_strategy_media",
]
