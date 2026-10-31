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
can't be made and asking how to proceed. The runtime planner opts into
``simplify_unsupported``: known unavailable text and SFX fields are omitted with
an exact notice and the supported strategy is compiled again before approval.

Runtime-v2 also has no guided-proposal step, so ``dispatch_item_render_for``
(always called with ``bypass_guided_edit_gate=True``) refuses every
``guided_voiceover_v1`` strategy with ``proposal_replan_required`` -- a render
that can never start, however often the creator retries. Such a strategy is
downgraded here to the native voiceover edit over the project's own clips, with
a notice saying what was left out; it asks first only when the downgrade loses
something the creator asked for (KRI-594).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.agents._schemas.creator_agent import (
    FULLSCREEN_OVERLAY_DISPLAYS,
    CreativeStrategy,
    ResolvedCreatorManifest,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_USER_SONG,
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
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
    latest_scope_counts_own_clips,
    native_voiceover_only,
)
from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS, NARRATED_EDIT_FORMATS
from app.kria.reply_language import current_reply_language, say
from app.kria.voiceover_scope import lost_visual_asks, resolve_native_all_scope
from app.pipeline.narrated_title import TITLE_MAX_CHARS
from app.schemas.clip_intents import ClipIntent
from app.schemas.edit_proposal import MAX_CREATOR_PINNED_TEXTS, PINNED_TEXT_MAX_CHARS
from app.services.creator_capabilities import (
    CAPABILITY_REACTION_BEATS,
    ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE,
    ENDING_CLIP_NOTICES,
    SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE,
    SPEAKER_BACKGROUND_NOTICES,
    TALKING_CLIP_INTENTS_DROPPED_NOTICE,
    CreatorSfxUnavailableError,
    compile_strategy_to_plan,
    phone_talking_title_lane_open,
    resolve_creator_image_media_ref,
    resolve_creator_visual_video_media_ref,
)
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError

# The English constants stay the canonical text (tests and callers compare them);
# ``_notice`` picks the Turkish twin when the chat is Turkish (KRI-520).
# Said only where the edit has no word-timed photos (no `reaction_beats`): there it is true.
GUIDED_VOICEOVER_DOWNGRADE_NOTICE = (
    "Photos and videos from Visuals can't be timed to a voiceover here yet, so this "
    "edit uses your project's own clips."
)
# KRI-594: where photos pop in on a spoken word (KRI-519), say that instead.
VISUALS_NEED_A_WORD_NOTICE = (
    "In a voiceover edit, photos from Visuals show up on a word you say, so I left out "
    "the ones that aren't tied to a word."
)
VOICEOVER_CLIP_CAP_NOTICE = (
    f"A voiceover edit can use up to {MAX_MAIN_CREATOR_SELECTED_MEDIA} of your clips, so "
    "I left out the rest."
)
VOICEOVER_MEDIA_TIMING_NOTICE = (
    "I can't keep that exact photo and video timing in a voiceover edit."
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
_VISUALS_NEED_A_WORD_NOTICE_TR = (
    "Seslendirmeli düzenlemede Visuals'taki fotoğraflar söylediğin bir kelimede beliriyor, "
    "o yüzden bir kelimeye bağlı olmayanları koymadım."
)
_VOICEOVER_CLIP_CAP_NOTICE_TR = (
    f"Seslendirmeli düzenleme kliplerinden en fazla {MAX_MAIN_CREATOR_SELECTED_MEDIA} "
    "tanesini kullanabiliyor, o yüzden gerisini koymadım."
)
_VOICEOVER_MEDIA_TIMING_NOTICE_TR = (
    "Seslendirmeli düzenlemede o tam fotoğraf ve video zamanlamasını koruyamıyorum."
)
_CAPTIONS_KEPT_NOTICE_TR = (
    "Bu tür düzenleme şu an her zaman altyazı gösteriyor, o yüzden altyazıları bıraktım."
)
_TRANSCRIPT_LABELS_DROPPED_NOTICE_TR = (
    "Seslendirmendeki sözler bu düzenlemede henüz kliplerin üzerinde gösterilemiyor, o "
    "yüzden bu etiketleri koymadım."
)


def _guided_voiceover_downgrade_notice(manifest: ResolvedCreatorManifest) -> str:
    """Why Visuals the guided plan would have shown are not in the voiceover edit."""

    beats = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
    if beats is not None and beats.available:
        return say(en=VISUALS_NEED_A_WORD_NOTICE, tr=_VISUALS_NEED_A_WORD_NOTICE_TR)
    return say(
        en=GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
        tr=_GUIDED_VOICEOVER_DOWNGRADE_NOTICE_TR,
    )


def _asked_media_left_out_notice(kind: str | None, ask: str) -> str:
    """KRI-594: a photo or video the creator's brief asks for, named in their own words."""

    noun_en, noun_tr = ("photo", "fotoğraf") if kind == "image" else ("video", "video")
    return say(
        en=f'This version leaves out the {noun_en} you asked for: "{ask}".',
        tr=f'Bu sürümde istediğin {noun_tr} yok: "{ask}".',
    )


def _captions_kept_notice() -> str:
    return say(en=CAPTIONS_KEPT_NOTICE, tr=_CAPTIONS_KEPT_NOTICE_TR)


def transcript_labels_dropped_notice() -> str:
    return say(en=TRANSCRIPT_LABELS_DROPPED_NOTICE, tr=_TRANSCRIPT_LABELS_DROPPED_NOTICE_TR)


def renderable_clip_intents(
    manifest: ResolvedCreatorManifest, intents: Sequence[ClipIntent]
) -> list[ClipIntent] | None:
    """KRI-691: ``intents`` without the narration labels this chat can never draw, or None
    when there is nothing to leave out.

    Only the guided contract draws a transcript label, and a runtime-v2 manifest with a
    recorded voiceover never offers it (KRI-598, `native_voiceover_only`). Asking for "a
    recorded voiceover with guided visuals" there asked for a voiceover the creator had
    already recorded, on every turn (prod thread 2ea478ec), so the label is left out with
    `transcript_labels_dropped_notice` instead. Where the contract is still offered, or no
    voiceover is recorded yet, the question stays: the creator can act on it.
    """

    if not native_voiceover_only(manifest):
        return None
    kept = [intent for intent in intents if intent.label_source != "transcript"]
    return kept if len(kept) != len(intents) else None


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
    "Small cards at an exact time aren't available for this edit yet; left them out.": (
        "Tam zamanlı küçük kartlar bu düzenleme için henüz yok; koymadım."
    ),
    "Couldn't find the clip the small card should follow; left that card out.": (
        "Küçük kartın takip edeceği klibi bulamadım; o kartı koymadım."
    ),
    "Couldn't find the photo or video you named for a small card; left it out.": (
        "Küçük kart için söylediğin fotoğrafı ya da videoyu bulamadım; koymadım."
    ),
    "Videos can't show as small cards in this edit yet; left that card out.": (
        "Bu düzenlemede videolar henüz küçük kart olarak çıkamıyor; o kartı koymadım."
    ),
    "Couldn't find the closing photo you named; kept the normal ending.": (
        "Söylediğin kapanış fotoğrafını bulamadım; normal bitişi bıraktım."
    ),
    "Couldn't find the closing badge you named; left it off.": (
        "Söylediğin kapanış rozetini bulamadım; koymadım."
    ),
    # KRI-841: a video appended after the talk with its own sound.
    ENDING_CLIP_NOTICES["not_talking"]: (
        "Bu videoyu kendi sesiyle sona ekleyemedim: sadece iPhone'daki konuşmalı düzenlemeler "
        "başka bir videoyla bitebilir."
    ),
    ENDING_CLIP_NOTICES["not_found"]: (
        "Son için söylediğin videoyu bulamadım; normal bitişi bıraktım."
    ),
    ENDING_CLIP_NOTICES["not_video"]: (
        "Bunu kendi sesiyle sona ekleyemedim: bu bir fotoğraf, video değil. Fotoğraf kapanış "
        "fotoğrafı olarak girebilir."
    ),
    ENDING_CLIP_NOTICES["visual_videos_off"]: (
        "Bu videoyu sona ekleyemedim: bu iPhone Visuals'taki videoları henüz oynatamıyor."
    ),
    ENDING_CLIP_NOTICES["media_lanes_disabled"]: (
        "Bu videoyu sona ekleyemedim: bu iPhone oluşturması konuşmanın çevresine resim ve "
        "video koyamıyor henüz."
    ),
    ENDING_CLIP_NOTICES["audio_mix_not_verified"]: (
        "Bunu kendi sesiyle sona ekleyemedim: bu iPhone sesini videoya karıştıramıyor henüz."
    ),
    ENDING_CLIP_NOTICES["muted_footage_ending_unsupported"]: (
        "Bu klibi sessiz olarak sona ekleyemedim: kendi kliplerinden biri videoyu ancak kendi "
        "sesiyle bitirebilir."
    ),
    ENDING_CLIP_NOTICES["not_selected"]: (
        "Bu klibi sona ekleyemedim: bu düzenlemeden çıkarmamı istemiştin."
    ),
    ENDING_CLIP_NOTICES["only_clip"]: ("Bu klibi sona ekleyemedim: bu düzenlemedeki tek klip bu."),
    ENDING_CLIP_NOTICES["multi_clip_off"]: (
        "Bu klibi sona ekleyemedim: başka bir klibinle bitirmek için birden çok klipli "
        "konuşmalı düzenleme gerekiyor ve o henüz yok."
    ),
    ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE: (
        "Sonda istediğin video sadece orada oynuyor, söylediğin bir kelimede ayrıca çıkmıyor."
    ),
    # KRI-1117: a photo or looping video behind the speaker.
    SPEAKER_BACKGROUND_NOTICES["unavailable"]: (
        "Bunu arkana koyamadım: fotoğraf ya da video arka planı sadece iPhone'da hazırlanan "
        "konuşmalı düzenlemelerde çalışıyor ve şu an burada kullanılamıyor."
    ),
    SPEAKER_BACKGROUND_NOTICES["not_found"]: (
        "Arka plan için söylediğin fotoğraf ya da videoyu bulamadım; kendi arka planını bıraktım."
    ),
    SPEAKER_BACKGROUND_NOTICES["pick_not_found"]: (
        "Kamerada seçtiğin arka planı kullanamadım: artık bu düzenlemenin fotoğraf ve "
        "videoları arasında değil; kendi arka planını bıraktım."
    ),
    SPEAKER_BACKGROUND_NOTICES["kind_off"]: (
        "Bunu arka plan yapamadım: bu iPhone onu henüz arkanda çizemiyor."
    ),
    SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE: (
        "Arkandaki fotoğraf ya da video sadece arka plan, söylediğin bir kelimede ayrıca çıkmıyor."
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
        re.compile(  # kri667: matches our own fixed English notice, not creator text
            r"Full screen isn't available for this edit yet, so \"(.+)\" shows as a small card "
            r"in the corner instead\."
        ),
        lambda m: (
            f'Bu düzenlemede tam ekran henüz yok; "{m.group(1)}" köşede küçük bir kart olarak '
            "çıkacak."
        ),
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
    omitted_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefusedStrategy:
    question: str
    code: str
    # KRI-759 (only a `simplification_requires_choice` refusal sets these): the strategy
    # fields the check would drop, and, when the server rewrote them silently, what was
    # dropped in the creator's words (`agreed` is the same as a done-deal line, for the
    # reply that follows when the creator accepts the simpler version).
    fields: tuple[str, ...] = ()
    agreed: str = ""


SONG_CAPTIONS_NEED_SONG_CODE = "song_captions_need_song"


def song_captions_refusal(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy, *, creator_text: str
) -> RefusedStrategy | None:
    """Ask for the song before a lip-sync edit that was asked to show lyrics (KRI-569).

    The lyrics are the uploaded song's: without the song there is nothing to caption, and
    the room audio in the clips is not a lyrics source. Judged on the creator's own words
    ("lip-sync" and "captions"), never on the model's caption default, so an ordinary
    montage is never asked for a song. The caller owns the ``SONG_CAPTIONS_ENABLED`` fence.
    """

    if (
        not creator_text
        or strategy.audio_strategy in ("user_song", "voiceover")
        or strategy.edit_format not in GUIDED_EDIT_FORMATS
    ):
        return None
    from app.pipeline.song_captions import captions_requested, mentions_lipsync  # noqa: PLC0415

    asked = captions_requested([creator_text], strategy.caption_style)
    if not (asked and mentions_lipsync(creator_text)):
        return None
    if getattr(manifest, "has_user_song", False) or getattr(manifest, "has_voiceover", False):
        return None
    # A song that IS attached but cannot be used (phone-only, switched off, wrong format)
    # leaves `has_user_song` False and marks the capability unavailable; the user-song
    # messaging owns that case. Only a manifest with no song capability at all is "no song".
    if CAPABILITY_USER_SONG in (getattr(manifest, "capabilities", None) or {}):
        return None
    return RefusedStrategy(
        question=say(
            en="Add the song you lip-synced to and I'll caption its lyrics.",
            tr="Dudak senkronu yaptığın şarkıyı ekle, sözlerini altyazı olarak ekleyeyim.",
        ),
        code=SONG_CAPTIONS_NEED_SONG_CODE,
    )


# The fields `_downgrade_guided_voiceover` rewrites. When the downgrade keeps every request,
# a dropped request is judged against the creator's strategy with these already swapped.
_DOWNGRADE_FIELDS = (
    "execution_contract",
    "mixed_media_timing",
    "media_scope",
    "selected_media_ids",
    "clip_intents",
    "resolved_clip_intents",
    "render_program",
)


@dataclass(frozen=True)
class _Downgrade:
    strategy: CreativeStrategy
    notices: tuple[str, ...]
    # KRI-594: something the creator asked for is not in the downgraded edit.
    loses_a_request: bool


def _placed_media_ids(manifest: ResolvedCreatorManifest, strategy: CreativeStrategy) -> set[str]:
    """What a native voiceover edit shows: its clips (up to the native cap) and every
    Visual a spoken word (`reaction_beats`) or the closing shot (`closing_media`) places."""

    placed = set(strategy.selected_media_ids[:MAX_MAIN_CREATOR_SELECTED_MEDIA])
    refs = [beat.visual_id for beat in strategy.reaction_beats or ()]
    if strategy.closing_media is not None:
        refs += [strategy.closing_media.visual_id, strategy.closing_media.badge_visual_id]
    for ref in refs:
        if not ref:
            continue
        media = resolve_creator_image_media_ref(
            manifest, ref
        ) or resolve_creator_visual_video_media_ref(manifest, ref)
        placed.add(media.media_id if media is not None else ref)
    return placed


def _downgrade_guided_voiceover(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    creator_text: str = "",
    requested_media: Mapping[str, str] | None = None,
) -> _Downgrade | RefusedStrategy:
    """The native voiceover edit over the project's own clips, and what it loses.

    KRI-594: the contract itself is our mechanism, not a creator request. The downgrade
    loses a request only when the edit no longer shows media the creator asked for, or drops
    exact photo/video timing or narration labels, which only the guided contract carries.
    Media the creator asked for:

    * every medium a live brief requirement targets (``requested_media``: asked in ANY
      turn, e.g. "show the bathtub photo when I say kırkayak"), when the guided plan would
      have shown it;
    * the plan's named ``selected_media_ids``;
    * for an "all" scope, every item -- or only the clips when the creator's own count names
      exactly the clips ("7 klibin hepsini kullan" with 7 clips and one Visuals photo). That
      reading never un-asks a medium the brief asks for.

    A Visual still shows when a reaction beat or the closing shot places it. A lost brief
    ask is named in the question; Visuals the guided plan would have shown without being
    asked for are left out with a notice, not a question.
    """

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
    intents = strategy.clip_intents or []
    kept_intents = [intent for intent in intents if intent.label_source != "transcript"]
    every_item = strategy.media_scope == "all"
    selected = [media_id for media_id in strategy.selected_media_ids if media_id in own_clips]
    downgraded = strategy.model_copy(
        update={
            "execution_contract": None,
            "mixed_media_timing": None,
            "media_scope": "selected",
            # An "all" scope keeps every clip; otherwise the clips the plan picked.
            "selected_media_ids": own_clips if every_item else (selected or own_clips),
            "clip_intents": kept_intents or None,
            "resolved_clip_intents": None,
            "render_program": "native",
        }
    )
    everything = [media.media_id for media in manifest.media]
    named = list(strategy.selected_media_ids)
    # The guided program shows its explicit selection, else every item.
    shown = named if strategy.media_scope == "selected" and named else everything
    brief_asked = {
        media_id: words for media_id, words in (requested_media or {}).items() if media_id in shown
    }
    if every_item:
        # The creator's latest scope words decide (PR 1550 review).
        clips_only = latest_scope_counts_own_clips(creator_text, len(own_clips))
        asked = set(own_clips if clips_only else everything)
    else:
        asked = set(named)
    asked |= set(brief_asked)
    placed = _placed_media_ids(manifest, downgraded)
    kinds = {media.media_id: media.kind for media in manifest.media}
    notices = [
        _asked_media_left_out_notice(kinds.get(media_id), words)
        for media_id, words in brief_asked.items()
        if media_id not in placed
    ]
    if any(
        media_id not in own_clips and media_id not in placed and media_id not in brief_asked
        for media_id in shown
    ):
        notices.append(_guided_voiceover_downgrade_notice(manifest))
    if len(downgraded.selected_media_ids) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        notices.append(say(en=VOICEOVER_CLIP_CAP_NOTICE, tr=_VOICEOVER_CLIP_CAP_NOTICE_TR))
    if strategy.mixed_media_timing is not None:
        notices.append(say(en=VOICEOVER_MEDIA_TIMING_NOTICE, tr=_VOICEOVER_MEDIA_TIMING_NOTICE_TR))
    if len(kept_intents) != len(intents):
        notices.append(transcript_labels_dropped_notice())
    loses_a_request = (
        any(media_id not in placed for media_id in asked)
        or strategy.mixed_media_timing is not None
        or len(kept_intents) != len(intents)
    )
    return _Downgrade(downgraded, tuple(notices), loses_a_request)


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
        if message.startswith("pinned_texts_too_many"):
            return RefusedStrategy(
                question=say(
                    en=(
                        f"I can keep up to {MAX_CREATOR_PINNED_TEXTS} lines of corner text. "
                        "Which ones should stay?"
                    ),
                    tr=(
                        f"Köşede en fazla {MAX_CREATOR_PINNED_TEXTS} satır yazı tutabilirim. "
                        "Hangileri kalsın?"
                    ),
                ),
                code="pinned_text_too_many",
            )
        if message.startswith("pinned_texts_too_long"):
            long_line = next(
                (pin.text for pin in strategy.pinned_texts or () if not pin.fits_a_corner), ""
            )
            shown = long_line[:40].rstrip() + "…"
            return RefusedStrategy(
                question=say(
                    en=(
                        f'"{shown}" is too long to fit on one line in a corner (about '
                        f"{PINNED_TEXT_MAX_CHARS} characters at most). Can you shorten it?"
                    ),
                    tr=(
                        f'"{shown}" köşede tek satıra sığmayacak kadar uzun (en fazla yaklaşık '
                        f"{PINNED_TEXT_MAX_CHARS} karakter). Kısaltır mısın?"
                    ),
                ),
                code="pinned_text_too_long",
            )
        if message.startswith("pinned_texts"):
            return RefusedStrategy(
                question=say(
                    en=(
                        "This kind of edit can't keep your text in a corner yet. "
                        "Should I make it without that text?"
                    ),
                    tr=(
                        "Bu tür bir düzenleme yazını henüz köşede tutamıyor. "
                        "O yazı olmadan yapayım mı?"
                    ),
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


def _dropped_fields(
    before: CreativeStrategy, after: CreativeStrategy, *, stated_settings: bool = False
) -> tuple[str, ...]:
    """The strategy fields a check lost (`selected_media_ids` = a selected clip left out)."""
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
    lost = [
        key
        for key in fields
        if prior.get(key) not in (None, [], "") and prior.get(key) != next_values.get(key)
    ]
    # PR 1556 review: a clip the strategy selected that the checked plan no longer has is
    # a lost ask (a reorder, or a clip added to the selection, is not).
    kept = set(next_values.get("selected_media_ids") or ())
    if any(media_id not in kept for media_id in prior.get("selected_media_ids") or ()):
        lost.append("selected_media_ids")
    if prior.get("caption_style") == "none" and next_values.get("caption_style") != "none":
        lost.append("caption_style")
    return tuple(lost)


def _drops_requested_action(
    before: CreativeStrategy, after: CreativeStrategy, *, stated_settings: bool = False
) -> bool:
    """Semantic losses need consent; styling normalization can still recover."""
    return bool(_dropped_fields(before, after, stated_settings=stated_settings))


# What a lost field is called in the creator's words. A noun phrase, so it reads in a list
# in either language without case endings.
_LOST_FIELD_WORDS: dict[str, tuple[str, str]] = {
    "clip_intents": ("your clip instructions", "klip talimatların"),
    "reaction_beats": ("the visuals timed to your words", "sözlerine göre zamanlanan görseller"),
    "closing_visual_id": ("the closing visual", "kapanış görseli"),
    "closing_badge_id": ("the closing badge", "kapanış rozeti"),
    "opening_title": ("your opening title", "açılış başlığın"),
    "shot_labels": ("your clip labels", "klip etiketlerin"),
    "closing_title": ("your closing title", "kapanış başlığın"),
    "pinned_texts": ("your on-screen texts", "ekran yazıların"),
    "execution_contract": ("the exact voiceover plan", "seslendirme planın"),
    "mixed_media_timing": ("the photo and video timing", "fotoğraf ve video zamanlaman"),
    "licensed_sfx": ("the sound effects", "ses efektleri"),
    "opening_title_duration_s": (
        "how long your title stays",
        "başlığının ekranda kalma süresi",
    ),
    "overlay_display": ("full-screen Visuals", "tam ekran Visuals"),
    "caption_style": ("your no-captions choice", "altyazısız isteğin"),
}


def _clip_numbers(manifest: ResolvedCreatorManifest, media_ids: Sequence[str]) -> list[int]:
    """The clips' places among the project's own clips, as the app numbers them (1-based)."""
    own = [media.media_id for media in manifest.media if not media.media_id.startswith("asset-")]
    return sorted(own.index(media_id) + 1 for media_id in media_ids if media_id in own)


def _join_numbers(numbers: Sequence[int]) -> str:
    words = [str(number) for number in numbers]
    if len(words) < 2:
        return "".join(words)
    return say(
        en=f"{', '.join(words[:-1])} and {words[-1]}",
        tr=f"{', '.join(words[:-1])} ve {words[-1]}",
    )


def _length_reason(
    manifest: ResolvedCreatorManifest, wanted: float, possible: float, played: Sequence[str]
) -> str:
    """Why a once-only montage can't be as long as asked: it can't outrun the clips it plays."""
    own_count = sum(1 for media in manifest.media if not media.media_id.startswith("asset-"))
    numbers = _clip_numbers(manifest, played)
    if numbers and len(numbers) < own_count:
        # Name the clips that are in, so a pick narrower than the creator meant shows.
        if len(numbers) > 6:
            return say(
                en=(
                    f"Only {len(numbers)} of your {own_count} clips are in this edit and "
                    f"together they run about {possible:g} seconds, so the video can't be "
                    f"{wanted:g} seconds."
                ),
                tr=(
                    f"Bu düzenlemede {own_count} klipten yalnızca {len(numbers)} tanesi var ve "
                    f"birlikte yaklaşık {possible:g} saniye sürüyor, bu yüzden video "
                    f"{wanted:g} saniye olamıyor."
                ),
            )
        several = len(numbers) > 1
        which = _join_numbers(numbers)
        return say(
            en=(
                f"Only {'clips' if several else 'clip'} {which} "
                f"{'are' if several else 'is'} in this edit and "
                f"{'together they run' if several else 'it runs'} about {possible:g} seconds, "
                f"so the video can't be {wanted:g} seconds."
            ),
            tr=(
                f"Bu düzenlemede yalnızca {which}. {'klipler' if several else 'klip'} var ve "
                f"{'birlikte ' if several else ''}yaklaşık {possible:g} saniye sürüyor, bu "
                f"yüzden video {wanted:g} saniye olamıyor."
            ),
        )
    return say(
        en=(
            f"Your clips add up to about {possible:g} seconds, so the video can't be "
            f"{wanted:g} seconds."
        ),
        tr=(
            f"Klipler toplam yaklaşık {possible:g} saniye, bu yüzden video {wanted:g} saniye "
            "olamıyor."
        ),
    )


def _lost_in_words(
    manifest: ResolvedCreatorManifest,
    before: CreativeStrategy,
    after: CreativeStrategy,
    fields: Sequence[str],
) -> tuple[str, str]:
    """(why the creator's request can't all be kept, the same as a done deal), or ("", "").

    Names only what the creator can act on: which clips are out, which length changed and
    why (a once-only montage can't run longer than the clips it plays), and the plain name
    of anything else. A field with no plain name here keeps the generic sentence.
    """
    reasons: list[str] = []
    agreed: list[str] = []
    named: list[tuple[str, str]] = []
    for field in fields:
        if field == "selected_media_ids":
            gone = [m for m in before.selected_media_ids if m not in after.selected_media_ids]
            numbers = _clip_numbers(manifest, gone)
            if not numbers or len(numbers) > 6:
                named.append(("the media you picked", "seçtiğin medya"))
                continue
            several = len(numbers) > 1
            which = _join_numbers(numbers)
            reasons.append(
                say(
                    en=f"{'Clips' if several else 'Clip'} {which} can't be in this edit.",
                    tr=f"{which}. {'klipler' if several else 'klip'} bu düzenlemeye giremiyor.",
                )
            )
            agreed.append(
                say(
                    en=f"I left out {'clips' if several else 'clip'} {which}.",
                    tr=f"{which}. {'klipleri' if several else 'klibi'} dışarıda bıraktım.",
                )
            )
        elif field == "target_duration_s":
            wanted, possible = before.target_duration_s, after.target_duration_s
            reasons.append(_length_reason(manifest, wanted, possible, after.selected_media_ids))
            agreed.append(
                say(
                    en=f"I made it {possible:g} seconds instead of {wanted:g}.",
                    tr=f"{wanted:g} saniye yerine {possible:g} saniyelik yaptım.",
                )
            )
        elif field in _LOST_FIELD_WORDS:
            named.append(_LOST_FIELD_WORDS[field])
        else:
            return "", ""
    if named:
        names = ", ".join(say(en=en, tr=tr) for en, tr in named)
        reasons.append(
            say(
                en=f"This edit can't keep: {names}.",
                tr=f"Bu düzenlemede korunamayanlar: {names}.",
            )
        )
        agreed.append(say(en=f"Left out: {names}.", tr=f"Çıkardıklarım: {names}."))
    return " ".join(reasons), " ".join(agreed)


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
    if (
        before.overlay_display in FULLSCREEN_OVERLAY_DISPLAYS
        and after.overlay_display not in FULLSCREEN_OVERLAY_DISPLAYS
    ):
        lines.append(
            say(
                en="Full-screen Visuals aren't available for this edit.",
                tr="Tam ekran Visuals bu düzenleme için yok.",
            )
        )
    return " ".join(lines)


def _talking_top_text_as_title(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, str | None]:
    """KRI-1009: 'Title "The Cabildo 🇦🇷" at the top' written as one top corner line.

    A Talking edit has no corner-text lane (KRI-523), so that line was left out, while the
    phone Talking title lane draws an opening title at the top, off the speaker's face
    (KRI-467). One whole-video line in a top corner, with no title beside it, becomes that
    title. Anything else (a bottom corner, several lines, a line scoped to seconds or a clip,
    a line longer than the title lane draws, no title lane) stays a corner text: it is left
    out and, when the creator quoted it, asked about.
    """

    pins = strategy.pinned_texts or []
    if (
        strategy.edit_format != "subtitled"
        or strategy.opening_title
        or len(pins) != 1
        or not pins[0].corner.startswith("top_")
        or not pins[0].is_whole_video
        or len(" ".join(pins[0].text.split())) > TITLE_MAX_CHARS
        or not phone_talking_title_lane_open(manifest)
    ):
        return strategy, None
    text = pins[0].text
    return (
        strategy.model_copy(update={"opening_title": text, "pinned_texts": None}),
        say(
            en=(
                f'"{text}" shows as the opening title for the first few seconds. '
                "Talking edits can't keep text on screen the whole time yet."
            ),
            tr=(
                f'"{text}" ilk birkaç saniye açılış başlığı olarak görünüyor. Konuşmalı '
                "düzenlemeler yazıyı henüz videonun tamamında ekranda tutamıyor."
            ),
        ),
    )


def _pin_range_refusal(strategy: CreativeStrategy) -> RefusedStrategy | None:
    """KRI-525: ask instead of guessing when a clip-scoped pin names a clip that isn't there.

    A montage cuts each selected clip once, in order, so the n-th clip exists only if at
    least n clips are selected. An empty selection means "all of them": nothing to check.
    """

    clips = len(strategy.selected_media_ids)
    if not clips:
        return None
    for pin in strategy.pinned_texts or ():
        if pin.clip is not None and pin.clip > clips:
            return RefusedStrategy(
                question=say(
                    en=(
                        f'You picked {clips} clip{"" if clips == 1 else "s"}, so "{pin.text}" '
                        f"can't go on clip {pin.clip}. Which clip should it be on?"
                    ),
                    tr=(
                        f'{clips} klip seçtin, o yüzden "{pin.text}" {pin.clip}. klibe '
                        "konamaz. Hangi klibe koyayım?"
                    ),
                ),
                code="pinned_text_clip_missing",
            )
    return None


# These decorate a playable edit. Media selection, source audio, timing conflicts
# and unknown validation errors are deliberately outside this recovery boundary.
_OPTIONAL_RENDER_FIELDS = frozenset(
    {"opening_title", "closing_title", "shot_labels", "pinned_texts", "licensed_sfx"}
)


def _unsupported_field(exc: ValueError) -> str | None:
    if isinstance(exc, CreatorSfxUnavailableError):
        return "licensed_sfx"
    if isinstance(exc, CreatorStrategyError) and exc.code == "unsupported_treatment":
        field = str(exc).split(" ", 1)[0]
        # Overlong/too-many pins need a different repair; don't erase valid pins.
        if field in _OPTIONAL_RENDER_FIELDS:
            return field
    return None


def _omission_notice(field: str, strategy: CreativeStrategy) -> str:
    name = say(en=_LOST_FIELD_WORDS[field][0], tr=_LOST_FIELD_WORDS[field][1])
    value = getattr(strategy, field)
    if field == "licensed_sfx":
        detail = value.effect_id
    elif field == "pinned_texts":
        detail = "; ".join(pin.text for pin in value)
    elif isinstance(value, list):
        detail = "; ".join(value)
    else:
        detail = str(value)
    return say(
        en=f'This edit cannot render {name}, so I left it out: "{detail}".',
        tr=f'Bu düzenlemede {name} gösterilemiyor, o yüzden çıkardım: "{detail}".',
    )


def check_strategy_for_runtime_v2(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    ask_before_simplifying: bool = False,
    ask_about_stated_settings: bool = False,
    simplify_unsupported: bool = False,
    creator_text: str = "",
    requested_media: Mapping[str, str] | None = None,
) -> CheckedStrategy | RefusedStrategy:
    """Run v1's plan compile over a v2 strategy; never raises a policy error.

    Both keywords serve a guided-voiceover downgrade (KRI-594): ``creator_text`` is the
    creator's own words (it tells an "all 7 clips" ask apart from "all my photos and
    videos"); ``requested_media`` maps each medium a live brief requirement asks for to that
    requirement's words (`app.kria.brief.requested_media`). ``requested_media`` also serves
    the native voiceover edit with no contract (KRI-598): a Visual it asks for that no
    reaction beat or closing shot places is named and asked about, never dropped silently.
    """

    notices: list[str] = []
    kept_intents = renderable_clip_intents(manifest, strategy.clip_intents or [])
    if kept_intents is not None:
        # KRI-691: a narration label the Main Creator wrote. Left out before the baseline
        # is taken: no answer to "a simpler version?" could make this chat draw it, so it
        # is a notice, never that question (the next turn would only ask it again).
        strategy = strategy.model_copy(update={"clip_intents": kept_intents or None})
        notices.append(transcript_labels_dropped_notice())
    # Before the baseline is taken: the title keeps the creator's words, so nothing is lost.
    strategy, title_notice = _talking_top_text_as_title(manifest, strategy)
    if title_notice:
        notices.append(title_notice)
    original = strategy
    # What a dropped request is judged against: the creator's strategy, with a guided
    # voiceover contract already swapped out when its downgrade keeps every request.
    baseline = original
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
    # KRI-598: a Visual the brief asks for that no spoken word places is lost on the native
    # voiceover edit: named in the creator's words, and asked about below.
    lost_asks = lost_visual_asks(manifest, strategy, requested_media)
    kinds = {media.media_id: media.kind for media in manifest.media}
    notices.extend(
        _asked_media_left_out_notice(kinds.get(media_id), words)
        for media_id, words in lost_asks.items()
    )
    # KRI-598: "use all my clips" on a native-only voiceover edit is every own clip. Own
    # clips beyond what one native edit takes were asked for by "all": a lost ask, like an
    # all-scope clip past the cap in the KRI-594 downgrade (PR 1556 review).
    strategy, scope_notices, clips_left_out, visuals_left_out = resolve_native_all_scope(
        manifest, strategy, asked=lost_asks, creator_text=creator_text
    )
    notices.extend(scope_notices)
    if clips_left_out:
        notices.append(say(en=VOICEOVER_CLIP_CAP_NOTICE, tr=_VOICEOVER_CLIP_CAP_NOTICE_TR))

    if strategy.execution_contract is not None:
        downgrade = _downgrade_guided_voiceover(
            manifest, strategy, creator_text=creator_text, requested_media=requested_media
        )
        if isinstance(downgrade, RefusedStrategy):
            return downgrade
        strategy = downgrade.strategy
        notices.extend(downgrade.notices)
        if not downgrade.loses_a_request:
            # KRI-594: runtime v2 never runs the guided contract, so swapping it for the
            # native voiceover edit is not a lost request when everything asked for stays.
            baseline = original.model_copy(
                update={field: getattr(strategy, field) for field in _DOWNGRADE_FIELDS}
            )
    pin_refusal = _pin_range_refusal(strategy)
    if pin_refusal is not None:
        return pin_refusal
    omitted_fields: set[str] = set()
    # Each failed compile must remove one known optional field. No model retry,
    # no catch-all fallback, and no loop once the edit reaches a fixed point.
    while True:
        try:
            edit_plan = compile_strategy_to_plan(manifest, strategy)
            break
        except ValueError as exc:
            field = _unsupported_field(exc) if simplify_unsupported else None
            if field is None or field in omitted_fields or not getattr(strategy, field):
                return _refusal_question(exc, strategy)
            notices.append(_omission_notice(field, strategy))
            omitted_fields.add(field)
            update = {field: None}
            if field == "opening_title":
                update["opening_title_duration_s"] = None
                omitted_fields.add("opening_title_duration_s")
            strategy = strategy.model_copy(update=update)
    checked = edit_plan.strategy
    if checked.render_program == "guided":
        # `compile_strategy_to_plan` widens a guided strategy's selection to the
        # whole manifest for the guided specialist. Runtime-v2 dispatches without
        # that specialist, so keep the selection the model boundary normalized.
        checked = checked.model_copy(update={"selected_media_ids": strategy.selected_media_ids})
    # Some compiler paths already repair an unavailable optional treatment. Keep
    # their notices and apply the same first-turn policy as a typed refusal.
    if simplify_unsupported:
        for field in sorted(_OPTIONAL_RENDER_FIELDS - omitted_fields):
            if getattr(baseline, field) and getattr(checked, field) is None:
                notices.append(_omission_notice(field, baseline))
                omitted_fields.add(field)
    all_notices = (*notices, *(localize_known_message(n) for n in edit_plan.notices))
    dropped = (
        _dropped_fields(baseline, checked, stated_settings=ask_about_stated_settings)
        if ask_before_simplifying
        else ()
    )
    dropped = tuple(field for field in dropped if field not in omitted_fields)
    left_out_media = bool(lost_asks or clips_left_out or visuals_left_out)
    if ask_before_simplifying and (left_out_media or dropped):
        # The creator-stated setting that was dropped comes first: a notice alone (e.g.
        # Visuals left out of a voiceover edit) may not be what made this a question.
        repair = _repair_detail(baseline, checked) if ask_about_stated_settings else ""
        stated = " ".join(part for part in (repair, *all_notices) if part)
        # KRI-759: a silent server rewrite has no notice. Say what it dropped instead of the
        # generic sentence, and keep the same words for the reply that follows an "OK".
        lost, agreed = ("", "") if stated else _lost_in_words(manifest, baseline, checked, dropped)
        details = (
            stated
            or lost
            or say(
                en="This edit cannot carry out that exact combination of requests.",
                tr="Bu düzenleme istediklerinin bu birleşimini aynen yapamıyor.",
            )
        )
        # KRI-519: a first request has no draft to be "unchanged".
        has_edit = manifest.current_edit is not None and manifest.current_edit.status != "none"
        tail = (
            say(
                en="Your current draft is unchanged. Should I make a simpler version?",
                tr="Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı?",
            )
            if has_edit
            else say(
                en="Should I make a simpler version?",
                tr="Daha sade bir sürüm yapayım mı?",
            )
        )
        return RefusedStrategy(
            question=f"{details} {tail}",
            code="simplification_requires_choice",
            fields=(*dropped, *(("left_out_media",) if left_out_media else ())),
            agreed=agreed,
        )
    return CheckedStrategy(
        strategy=checked, notices=all_notices, omitted_fields=tuple(sorted(omitted_fields))
    )


__all__ = [
    "GUIDED_VOICEOVER_DOWNGRADE_NOTICE",
    "TRANSCRIPT_LABELS_DROPPED_NOTICE",
    "VISUALS_NEED_A_WORD_NOTICE",
    "VOICEOVER_CLIP_CAP_NOTICE",
    "VOICEOVER_MEDIA_TIMING_NOTICE",
    "CheckedStrategy",
    "RefusedStrategy",
    "SONG_CAPTIONS_NEED_SONG_CODE",
    "check_strategy_for_runtime_v2",
    "renderable_clip_intents",
    "transcript_labels_dropped_notice",
    "song_captions_refusal",
    "localize_known_message",
]
