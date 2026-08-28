"""Pure Main Creator render-policy helpers shared by agent and compiler."""

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.edit_format import (
    AUDIO_LED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
)

MAX_MAIN_CREATOR_SELECTED_MEDIA = 12
CAPABILITY_DRAFT_GUIDED_PROPOSAL = "draft_guided_proposal"


class MixedMediaTimingUnavailableError(ValueError):
    """The requested per-kind timing cannot be compiled by an available renderer."""


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
    intercut_comparison = strategy.intercut_comparison is not None
    native_required = (
        manifest.has_voiceover
        or (strategy.audio_strategy in {"original_audio", "voiceover"} and not intercut_comparison)
        or strategy_format in AUDIO_LED_EDIT_FORMATS
        or not guided_edit_applicable(strategy_format, has_voiceover=manifest.has_voiceover)
    )
    if native_required:
        return "native"
    guided = manifest.capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
    media_kinds = {media.kind for media in manifest.media}
    # Native montage only receives attached clips. A per-kind timing request
    # needs the guided specialist so pool photos are selected and compiled too.
    mixed_media_timing_requires_specialist = bool(
        strategy.mixed_media_timing is not None and {"image", "video"}.issubset(media_kinds)
    )
    intercut_comparison_requires_specialist = intercut_comparison
    if (mixed_media_timing_requires_specialist or intercut_comparison_requires_specialist) and not (
        guided and guided.available
    ):
        reason = (
            "mixed-media timing requires the guided proposal capability"
            if mixed_media_timing_requires_specialist
            else "the requested structured timing requires the guided proposal capability"
        )
        raise MixedMediaTimingUnavailableError(reason)
    if (
        guided
        and guided.available
        and (
            strategy.render_program == "guided"
            or mixed_media_timing_requires_specialist
            or intercut_comparison_requires_specialist
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
    if strategy.intercut_comparison is not None:
        intercut_ids = list(dict.fromkeys(strategy.intercut_comparison.source_media_ids))
        unknown_intercut = [media_id for media_id in intercut_ids if media_id not in manifest_ids]
        if unknown_intercut and not repair_model_output:
            raise ValueError("intercut comparison sources must reference manifest media")
        valid_intercut = [media_id for media_id in intercut_ids if media_id in manifest_ids]
        if repair_model_output and len(valid_intercut) < strategy.intercut_comparison.source_count:
            valid_intercut = [
                media.media_id
                for media in manifest.media
                if media.kind == "video" and not media.media_id.startswith("asset-")
            ][: strategy.intercut_comparison.source_count]
        strategy = strategy.model_copy(
            update={
                "intercut_comparison": strategy.intercut_comparison.model_copy(
                    update={"source_media_ids": valid_intercut}
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
    "MixedMediaTimingUnavailableError",
    "effective_render_program",
    "normalize_creator_strategy_media",
]
