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

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import (
    USER_SONG_BACKGROUND_NOTICE,
    USER_SONG_CONTRACT_NOTICE,
    USER_SONG_LIPSYNC_SHAPE_NOTICE,
    USER_SONG_MISSING_CODE,
    USER_SONG_MISSING_MESSAGE,
    USER_SONG_PHONE_ONLY_CODE,
    USER_SONG_PHONE_ONLY_MESSAGE,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    PhoneMediaUnavailableError,
)
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.reply_language import current_reply_language, say
from app.services.creator_capabilities import (
    TALKING_CLIP_INTENTS_DROPPED_NOTICE,
    CreatorSfxUnavailableError,
    compile_strategy_to_plan,
)
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError

# The English constants stay the canonical text (tests and callers compare them);
# ``_notice`` picks the Turkish twin when the chat is Turkish (KRI-520).
GUIDED_VOICEOVER_DOWNGRADE_NOTICE = (
    "Photos and videos from Visuals can't be timed to a voiceover here yet, so this "
    "edit uses your project's own clips."
)
CAPTIONS_KEPT_NOTICE = "This kind of edit always shows captions right now, so I kept them in."
TRANSCRIPT_LABELS_DROPPED_NOTICE = (
    "Words from your voiceover can't be shown on the clips in this edit yet, so I "
    "left those labels out."
)

_GUIDED_VOICEOVER_DOWNGRADE_NOTICE_TR = (
    "Visuals'taki fotoğraf ve videolar henüz seslendirmeye göre zamanlanamıyor, o yüzden "
    "bu düzenleme projenin kendi kliplerini kullanıyor."
)
_CAPTIONS_KEPT_NOTICE_TR = (
    "Bu tür düzenleme şu an her zaman altyazı gösteriyor, o yüzden altyazıları bıraktım."
)
_TRANSCRIPT_LABELS_DROPPED_NOTICE_TR = (
    "Seslendirmendeki sözler bu düzenlemede henüz kliplerin üzerinde gösterilemiyor, o "
    "yüzden bu etiketleri koymadım."
)


def _guided_voiceover_downgrade_notice() -> str:
    return say(
        en=GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
        tr=_GUIDED_VOICEOVER_DOWNGRADE_NOTICE_TR,
    )


def _captions_kept_notice() -> str:
    return say(en=CAPTIONS_KEPT_NOTICE, tr=_CAPTIONS_KEPT_NOTICE_TR)


def _transcript_labels_dropped_notice() -> str:
    return say(en=TRANSCRIPT_LABELS_DROPPED_NOTICE, tr=_TRANSCRIPT_LABELS_DROPPED_NOTICE_TR)


# Notices and messages other modules write in English (the strategy compiler, the
# user-song policy). They reach the creator through this module's replies, so a
# Turkish chat gets them translated here by exact text; a module that already
# writes Turkish passes through untouched. Anything unknown stays as written.
_KNOWN_TR: dict[str, str] = {
    USER_SONG_CONTRACT_NOTICE: (
        "Dudak senkronlu düzenleme şarkını izler, o yüzden kaydedilmiş seslendirmeye göre "
        "zamanlamıyorum."
    ),
    USER_SONG_BACKGROUND_NOTICE: ("Şarkını bu düzenlemede arka plan müziği olarak kullanacağım."),
    USER_SONG_LIPSYNC_SHAPE_NOTICE: (
        "Dudak senkronlu düzenleme şarkını izler, o yüzden günlük vlog ya da tek kahraman "
        "kurgusu kullanmıyorum."
    ),
    USER_SONG_PHONE_ONLY_MESSAGE: (
        "Kendi şarkın sadece iPhone'unda hazırlanan düzenlemelerde çalışıyor. Bunu "
        "lisanslı müzikle yapayım mı?"
    ),
    USER_SONG_MISSING_MESSAGE: (
        "Önce şarkıyı yükle, düzenlemeyi ona göre kurarım. Ya da lisanslı müzik kullanayım mı?"
    ),
    TALKING_CLIP_INTENTS_DROPPED_NOTICE: (
        "Konuşmalı düzenlemeler söylediklerini altyazıya çevirir, o yüzden tek tek "
        "kliplere ayrı etiket ya da altyazı eklemedim."
    ),
    (
        "Sound effects on iPhone are placed at the moments you named; the general "
        "sound-effect treatment was left out."
    ): (
        "iPhone'da ses efektleri senin söylediğin anlara konuyor; genel ses efekti "
        "uygulamasını koymadım."
    ),
    (
        "Sound and photo pop-ins timed to your words aren't available for this "
        "edit yet; left them out."
    ): "Sözlerine göre çıkan ses ve fotoğraflar bu düzenleme için henüz yok; koymadım.",
    "Couldn't find the closing photo you named; kept the normal ending.": (
        "Söylediğin kapanış fotoğrafını bulamadım; normal bitişi bıraktım."
    ),
    "Couldn't find the closing badge you named; left it off.": (
        "Söylediğin kapanış rozetini bulamadım; koymadım."
    ),
    (
        "Sound effects can't render on your iPhone yet. Ask for this edit without the sound effect."
    ): "Ses efektleri iPhone'unda henüz hazırlanamıyor. Bu düzenlemeyi ses efektsiz iste.",
    "The requested licensed sound effect is unavailable. Choose another effect.": (
        "İstediğin lisanslı ses efekti yok. Başka bir efekt seç."
    ),
}
_SHAPE_NAMES_TR = {"Day vlog": "Günlük vlog", "Single-hero": "Tek kahraman"}
_KNOWN_TR_PATTERNS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(r"Couldn't find \"(.+)\"'s photo/sticker; left it out\."),
        lambda m: f'"{m.group(1)}" için fotoğraf ya da çıkartma bulamadım; koymadım.',
    ),
    (
        re.compile(r"(Day vlog|Single-hero) shape needs music; kept a regular montage\."),
        lambda m: (
            f"{_SHAPE_NAMES_TR[m.group(1)]} kurgusu için müzik gerekiyor; normal bir montaj yaptım."
        ),
    ),
    (
        re.compile(r"Reading this as a montage in the (day-vlog|single-hero) style\."),
        lambda m: (
            "Bunu "
            + ("günlük vlog" if m.group(1) == "day-vlog" else "tek kahraman")
            + " tarzında bir montaj olarak okudum."
        ),
    ),
    (
        re.compile(r"The requested licensed sound effect (.+) is unavailable\."),
        lambda m: f"İstediğin lisanslı ses efekti ({m.group(1)}) yok.",
    ),
)


def localize_known_message(text: str) -> str:
    """A message other modules wrote in English, in the chat's language.

    English chats (and any text this table doesn't know) come back unchanged.
    """
    if current_reply_language() != "tr":
        return text
    known = _KNOWN_TR.get(text)
    if known is not None:
        return known
    for pattern, render in _KNOWN_TR_PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return render(match)
    return text


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
            question=say(
                en=(
                    "A voiceover edit needs at least one video attached to this project. "
                    "Add a clip and I'll time it to your voiceover."
                ),
                tr=(
                    "Seslendirmeli bir düzenleme için projeye en az bir video eklemen "
                    "lazım. Bir klip ekle, onu seslendirmene göre ayarlarım."
                ),
            ),
            code="guided_voiceover_unavailable",
        )
    notices = [_guided_voiceover_downgrade_notice()]
    intents = strategy.clip_intents or []
    kept_intents = [intent for intent in intents if intent.label_source != "transcript"]
    if len(kept_intents) != len(intents):
        notices.append(_transcript_labels_dropped_notice())
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
        return RefusedStrategy(
            question=localize_known_message(str(exc)), code="licensed_sfx_unavailable"
        )
    if isinstance(exc, CreatorStrategyError):
        message = str(exc)
        edit_format = exc.edit_format or strategy.edit_format
        if message.startswith("Narration labels"):
            # Same wording the planner's own clip-intent gate already uses.
            return RefusedStrategy(
                question=say(
                    en="Those labels need a recorded voiceover with guided visuals.",
                    tr=(
                        "Bu etiketler için kaydedilmiş bir seslendirme ve rehberli "
                        "görseller gerekiyor."
                    ),
                ),
                code="transcript_labels_unavailable",
            )
        if message.startswith("opening_title"):
            question = (
                say(
                    en=(
                        "Talking edits show your words as captions, so I can't add a title on "
                        "top yet. Should I make it without the title?"
                    ),
                    tr=(
                        "Konuşmalı düzenlemeler sözlerini altyazı olarak gösterir, o yüzden "
                        "üstüne henüz başlık ekleyemiyorum. Başlıksız yapayım mı?"
                    ),
                )
                if edit_format == "subtitled"
                else say(
                    en=(
                        "This voiceover edit renders on your iPhone, which can't show a title "
                        "yet. Should I make it without the title?"
                    ),
                    tr=(
                        "Bu seslendirmeli düzenleme iPhone'unda hazırlanıyor ve orada henüz "
                        "başlık gösterilemiyor. Başlıksız yapayım mı?"
                    ),
                )
            )
            return RefusedStrategy(question=question, code="title_unavailable")
        if message.startswith("pinned_texts"):
            return RefusedStrategy(
                question=(
                    "This kind of edit can't keep your text in a corner for the whole video "
                    "yet. Should I make it without that text?"
                ),
                code="pinned_text_unavailable",
            )
        if message.startswith(("shot_labels", "closing_title")):
            if "exact photo/video cut timing" in message:
                return RefusedStrategy(
                    question=say(
                        en=(
                            "I can't put text on each shot and keep exact photo and video "
                            "timing in the same edit. Which one matters more?"
                        ),
                        tr=(
                            "Aynı düzenlemede hem her çekime yazı koyup hem de fotoğraf ve "
                            "videoların zamanlamasını tam tutamıyorum. Hangisi daha önemli?"
                        ),
                    ),
                    code="shot_text_unavailable",
                )
            # KRI-514: name only what was asked for, and say the rest is kept. A
            # lone closing text is quoted back ("MY PICK") instead of a generic
            # "on each shot or at the end" the creator never asked for.
            if strategy.shot_labels:
                missing = "your own text on each shot" + (
                    " or at the end" if strategy.closing_title else ""
                )
                missing_tr = (
                    "her çekimde ya da sonda senin yazını"
                    if strategy.closing_title
                    else "her çekimde senin yazını"
                )
            else:
                missing = f'"{strategy.closing_title}" at the end'
                missing_tr = f'sonda "{strategy.closing_title}" yazısını'
            return RefusedStrategy(
                question=say(
                    en=(
                        f"This kind of edit can't show {missing} yet. Should I make everything "
                        "else and leave that text out?"
                    ),
                    tr=(
                        f"Bu tür düzenleme {missing_tr} henüz gösteremiyor. Geri kalan her şeyi "
                        "yapıp o yazıyı dışarıda bırakayım mı?"
                    ),
                ),
                code="shot_text_unavailable",
            )
    if isinstance(exc, PhoneMediaUnavailableError):
        return RefusedStrategy(
            question=say(
                en=(
                    "This edit renders on your iPhone, and it can't use some of the media you "
                    "picked. Should I make it with just this project's clips?"
                ),
                tr=(
                    "Bu düzenleme iPhone'unda hazırlanıyor ve seçtiğin bazı medyayı "
                    "kullanamıyor. Sadece bu projenin kliplerini kullanarak yapayım mı?"
                ),
            ),
            code="phone_media_unavailable",
        )
    if isinstance(exc, MontageCadenceUnavailableError):
        return RefusedStrategy(
            question=say(
                en=(
                    "I can't keep that exact alternating cut with a voiceover. Should I drop "
                    "the voiceover, or change the cut?"
                ),
                tr=(
                    "Bu tam dönüşümlü kesimi seslendirmeyle birlikte koruyamıyorum. "
                    "Seslendirmeyi çıkarayım mı, yoksa kesimi mi değiştireyim?"
                ),
            ),
            code="montage_cadence_unavailable",
        )
    if isinstance(exc, MixedMediaTimingUnavailableError):
        return RefusedStrategy(
            question=say(
                en=(
                    "I can't keep that exact photo and video timing in this edit. Should I "
                    "make it without the exact timing?"
                ),
                tr=(
                    "Bu düzenlemede o tam fotoğraf ve video zamanlamasını koruyamıyorum. "
                    "Tam zamanlama olmadan yapayım mı?"
                ),
            ),
            code="mixed_media_timing_unavailable",
        )
    if isinstance(exc, CreatorCapabilityError) and exc.code in _USER_SONG_REFUSALS:
        # KRI-374: stable codes the app and evals key on; copy lives with the policy.
        return RefusedStrategy(
            question=localize_known_message(_USER_SONG_REFUSALS[exc.code]), code=exc.code
        )
    if isinstance(exc, CreatorCapabilityError):
        return RefusedStrategy(
            question=say(
                en=(
                    "That kind of edit isn't available for this project yet. Should I make it "
                    "as a different kind of edit?"
                ),
                tr=(
                    "Bu tür düzenleme bu proje için henüz yok. Başka türde bir düzenleme "
                    "olarak yapayım mı?"
                ),
            ),
            code=exc.code,
        )
    return RefusedStrategy(
        question=say(
            en=(
                "I couldn't apply that exact direction to this edit. Could you say what "
                "matters most, and I'll build around it?"
            ),
            tr=(
                "İstediğin yönü bu düzenlemeye aynen uygulayamadım. En çok neyin önemli "
                "olduğunu söyler misin, ona göre kurayım?"
            ),
        ),
        code="strategy_invalid",
    )


def _drops_requested_action(
    before: CreativeStrategy, after: CreativeStrategy, *, stated_settings: bool = False
) -> bool:
    """Semantic losses need consent; styling normalization can still recover."""
    prior = before.model_dump(mode="json")
    next_values = after.model_dump(mode="json")
    fields = (
        "clip_intents",
        "reaction_beats",
        "closing_visual_id",
        "closing_badge_id",
        "opening_title",
        "shot_labels",
        "closing_title",
        "pinned_texts",
        "execution_contract",
        "mixed_media_timing",
        "licensed_sfx",
        "target_duration_s",
    )
    if stated_settings:
        # KRI-476: the creator's own seconds for the title and their explicit ask for
        # full-screen Visuals are creator-stated; a renderer that cannot honour them used
        # to drop them silently inside `compile_strategy_to_plan`.
        fields = (*fields, "opening_title_duration_s", "overlay_display")
    if any(
        prior.get(key) not in (None, [], "") and prior.get(key) != next_values.get(key)
        for key in fields
    ):
        return True
    return prior.get("caption_style") == "none" and next_values.get("caption_style") != "none"


def _repair_detail(before: CreativeStrategy, after: CreativeStrategy) -> str:
    """Plain words for the creator-stated settings a silent repair would have dropped."""

    lines: list[str] = []
    if before.opening_title_duration_s is not None and (
        after.opening_title_duration_s != before.opening_title_duration_s
    ):
        lines.append(
            say(
                en=(
                    f"This edit can't hold your title for exactly "
                    f"{before.opening_title_duration_s:g} seconds."
                ),
                tr=(
                    f"Bu düzenleme başlığını tam {before.opening_title_duration_s:g} saniye "
                    "tutamıyor."
                ),
            )
        )
    if before.overlay_display == "fullscreen" and after.overlay_display != "fullscreen":
        lines.append(
            say(
                en="Full-screen Visuals aren't available for this edit.",
                tr="Tam ekran Visuals bu düzenleme için yok.",
            )
        )
    return " ".join(lines)


def check_strategy_for_runtime_v2(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    ask_before_simplifying: bool = False,
    ask_about_stated_settings: bool = False,
) -> CheckedStrategy | RefusedStrategy:
    """Run v1's plan compile over a v2 strategy; never raises a policy error."""

    original = strategy
    notices: list[str] = []
    if strategy.caption_style == "none" and (
        strategy.edit_format == "subtitled" or strategy.edit_format in NARRATED_EDIT_FORMATS
    ):
        # Both renderers always caption (the item stores "none" as NULL, which
        # the worker reads as sentence captions), so say so instead of
        # approving a caption-free edit that renders with captions.
        strategy = strategy.model_copy(update={"caption_style": "auto"})
        notices.append(_captions_kept_notice())
    if (
        strategy.audio_strategy == "user_song"
        and strategy.song_sync == "lipsync"
        and strategy.execution_contract is not None
    ):
        # KRI-374: a lip-sync edit follows the song, not a voiceover. Repair (clear the
        # contract, say so) before the voiceover downgrade below, which would otherwise
        # rewrite the selection and could refuse a project that has no video.
        strategy = strategy.model_copy(update={"execution_contract": None})
        notices.append(localize_known_message(USER_SONG_CONTRACT_NOTICE))
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
    all_notices = (*notices, *(localize_known_message(n) for n in edit_plan.notices))
    if ask_before_simplifying and _drops_requested_action(
        original, checked, stated_settings=ask_about_stated_settings
    ):
        details = (
            " ".join(all_notices)
            or (_repair_detail(original, checked) if ask_about_stated_settings else "")
            or say(
                en="This edit cannot carry out that exact combination of requests.",
                tr="Bu düzenleme istediklerinin bu birleşimini aynen yapamıyor.",
            )
        )
        tail = say(
            en="Your current draft is unchanged. Should I make a simpler version?",
            tr="Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı?",
        )
        return RefusedStrategy(
            question=f"{details} {tail}",
            code="simplification_requires_choice",
        )
    return CheckedStrategy(strategy=checked, notices=all_notices)


__all__ = [
    "GUIDED_VOICEOVER_DOWNGRADE_NOTICE",
    "TRANSCRIPT_LABELS_DROPPED_NOTICE",
    "CheckedStrategy",
    "RefusedStrategy",
    "check_strategy_for_runtime_v2",
    "localize_known_message",
]
