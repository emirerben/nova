"""Runtime-v2 strategy policy: the same server checks v1 runs, before a draft.

The v1 Creator route compiles every proposed strategy through
``compile_strategy_to_plan`` -- repairs (reaction beats, story shapes, the
general sound-effect treatment, optional treatments the manifest can't render)
and refusals (a title on a Talking edit, per-shot text off the guided renderer,
an unavailable licensed sound effect). Runtime-v2 used to hand the model's
strategy straight to ``draft.apply_strategy``, so none of that ran and a phone
render silently dropped whatever it could not draw while the reply said "Done"
(KRI-142). This module is the v2 boundary: it returns either the repaired
strategy plus plain-language notices, or a question telling the creator what
can't be made and asking how to proceed.

Runtime-v2 also has no guided-proposal step, so ``dispatch_item_render_for``
(always called with ``bypass_guided_edit_gate=True``) refuses every
``guided_voiceover_v1`` strategy with ``proposal_replan_required`` -- a render
that can never start, however often the creator retries. Such a strategy is
downgraded here to the native voiceover edit over the project's own clips, with
a notice saying what was left out.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import (
    USER_SONG_CONTRACT_NOTICE,
    USER_SONG_MISSING_CODE,
    USER_SONG_MISSING_MESSAGE,
    USER_SONG_PHONE_ONLY_CODE,
    USER_SONG_PHONE_ONLY_MESSAGE,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    PhoneMediaUnavailableError,
)
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.services.creator_capabilities import (
    CreatorSfxUnavailableError,
    compile_strategy_to_plan,
)
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError

GUIDED_VOICEOVER_DOWNGRADE_NOTICE = (
    "Photos and videos from Visuals can't be timed to a voiceover here yet, so this "
    "edit uses your project's own clips."
)
CAPTIONS_KEPT_NOTICE = "This kind of edit always shows captions right now, so I kept them in."
TRANSCRIPT_LABELS_DROPPED_NOTICE = (
    "Words from your voiceover can't be shown on the clips in this edit yet, so I "
    "left those labels out."
)


_USER_SONG_REFUSALS = {
    USER_SONG_PHONE_ONLY_CODE: USER_SONG_PHONE_ONLY_MESSAGE,
    USER_SONG_MISSING_CODE: USER_SONG_MISSING_MESSAGE,
}


@dataclass(frozen=True)
class CheckedStrategy:
    strategy: CreativeStrategy
    notices: tuple[str, ...]


@dataclass(frozen=True)
class RefusedStrategy:
    question: str
    code: str


def _downgrade_guided_voiceover(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]] | RefusedStrategy:
    own_clips = [
        media.media_id
        for media in manifest.media
        if not media.media_id.startswith("asset-") and media.kind == "video"
    ]
    if not own_clips:
        return RefusedStrategy(
            question=(
                "A voiceover edit needs at least one video attached to this project. "
                "Add a clip and I'll time it to your voiceover."
            ),
            code="guided_voiceover_unavailable",
        )
    notices = [GUIDED_VOICEOVER_DOWNGRADE_NOTICE]
    intents = strategy.clip_intents or []
    kept_intents = [intent for intent in intents if intent.label_source != "transcript"]
    if len(kept_intents) != len(intents):
        notices.append(TRANSCRIPT_LABELS_DROPPED_NOTICE)
    selected = [media_id for media_id in strategy.selected_media_ids if media_id in own_clips]
    downgraded = strategy.model_copy(
        update={
            "execution_contract": None,
            "mixed_media_timing": None,
            "media_scope": "selected",
            "selected_media_ids": selected or own_clips,
            "clip_intents": kept_intents or None,
            "resolved_clip_intents": None,
            "render_program": "native",
        }
    )
    return downgraded, notices


def _refusal_question(exc: ValueError, strategy: CreativeStrategy) -> RefusedStrategy:
    """Plain words for a strategy the renderer can't draw, ending in a question."""

    if isinstance(exc, CreatorSfxUnavailableError):
        return RefusedStrategy(question=str(exc), code="licensed_sfx_unavailable")
    if isinstance(exc, CreatorStrategyError):
        message = str(exc)
        edit_format = exc.edit_format or strategy.edit_format
        if message.startswith("Narration labels"):
            # Same wording the planner's own clip-intent gate already uses.
            return RefusedStrategy(
                question="Those labels need a recorded voiceover with guided visuals.",
                code="transcript_labels_unavailable",
            )
        if message.startswith("opening_title"):
            question = (
                "Talking edits show your words as captions, so I can't add a title on "
                "top yet. Should I make it without the title?"
                if edit_format == "subtitled"
                else "This voiceover edit renders on your iPhone, which can't show a title "
                "yet. Should I make it without the title?"
            )
            return RefusedStrategy(question=question, code="title_unavailable")
        if message.startswith(("shot_labels", "closing_title")):
            if "exact photo/video cut timing" in message:
                return RefusedStrategy(
                    question=(
                        "I can't put text on each shot and keep exact photo and video "
                        "timing in the same edit. Which one matters more?"
                    ),
                    code="shot_text_unavailable",
                )
            return RefusedStrategy(
                question=(
                    "This kind of edit can't show your own text on each shot or at the end "
                    "yet. Should I make it without that text?"
                ),
                code="shot_text_unavailable",
            )
    if isinstance(exc, PhoneMediaUnavailableError):
        return RefusedStrategy(
            question=(
                "This edit renders on your iPhone, and it can't use some of the media you "
                "picked. Should I make it with just this project's clips?"
            ),
            code="phone_media_unavailable",
        )
    if isinstance(exc, MontageCadenceUnavailableError):
        return RefusedStrategy(
            question=(
                "I can't keep that exact alternating cut with a voiceover. Should I drop "
                "the voiceover, or change the cut?"
            ),
            code="montage_cadence_unavailable",
        )
    if isinstance(exc, MixedMediaTimingUnavailableError):
        return RefusedStrategy(
            question=(
                "I can't keep that exact photo and video timing in this edit. Should I "
                "make it without the exact timing?"
            ),
            code="mixed_media_timing_unavailable",
        )
    if isinstance(exc, CreatorCapabilityError) and exc.code in _USER_SONG_REFUSALS:
        # KRI-374: stable codes the app and evals key on; copy lives with the policy.
        return RefusedStrategy(question=_USER_SONG_REFUSALS[exc.code], code=exc.code)
    if isinstance(exc, CreatorCapabilityError):
        return RefusedStrategy(
            question=(
                "That kind of edit isn't available for this project yet. Should I make it "
                "as a different kind of edit?"
            ),
            code=exc.code,
        )
    return RefusedStrategy(
        question=(
            "I couldn't apply that exact direction to this edit. Could you say what "
            "matters most, and I'll build around it?"
        ),
        code="strategy_invalid",
    )


def check_strategy_for_runtime_v2(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> CheckedStrategy | RefusedStrategy:
    """Run v1's plan compile over a v2 strategy; never raises a policy error."""

    notices: list[str] = []
    if strategy.caption_style == "none" and (
        strategy.edit_format == "subtitled" or strategy.edit_format in NARRATED_EDIT_FORMATS
    ):
        # Both renderers always caption (the item stores "none" as NULL, which
        # the worker reads as sentence captions), so say so instead of
        # approving a caption-free edit that renders with captions.
        strategy = strategy.model_copy(update={"caption_style": "auto"})
        notices.append(CAPTIONS_KEPT_NOTICE)
    if (
        strategy.audio_strategy == "user_song"
        and strategy.song_sync == "lipsync"
        and strategy.execution_contract is not None
    ):
        # KRI-374: a lip-sync edit follows the song, not a voiceover. Repair (clear the
        # contract, say so) before the voiceover downgrade below, which would otherwise
        # rewrite the selection and could refuse a project that has no video.
        strategy = strategy.model_copy(update={"execution_contract": None})
        notices.append(USER_SONG_CONTRACT_NOTICE)
    if strategy.execution_contract is not None:
        downgraded = _downgrade_guided_voiceover(manifest, strategy)
        if isinstance(downgraded, RefusedStrategy):
            return downgraded
        strategy, notices = downgraded
    try:
        edit_plan = compile_strategy_to_plan(manifest, strategy)
    except ValueError as exc:
        return _refusal_question(exc, strategy)
    checked = edit_plan.strategy
    if checked.render_program == "guided":
        # `compile_strategy_to_plan` widens a guided strategy's selection to the
        # whole manifest for the guided specialist. Runtime-v2 dispatches without
        # that specialist, so keep the selection the model boundary normalized.
        checked = checked.model_copy(update={"selected_media_ids": strategy.selected_media_ids})
    return CheckedStrategy(strategy=checked, notices=(*notices, *edit_plan.notices))


__all__ = [
    "GUIDED_VOICEOVER_DOWNGRADE_NOTICE",
    "TRANSCRIPT_LABELS_DROPPED_NOTICE",
    "CheckedStrategy",
    "RefusedStrategy",
    "check_strategy_for_runtime_v2",
]
