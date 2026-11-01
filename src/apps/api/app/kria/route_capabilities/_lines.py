"""Creator sentences shared by the route tables, and the typed ask predicates they decline.

A builder answers ``(value, strategy) -> str | None``. The wording is route-neutral ("on this
video yet") unless a factory takes the route's own behaviour ("it is cut as a fast montage").
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.reply_language import say
from app.kria.route_capabilities._types import Builder


def clip(value: object, limit: int = 70) -> str:
    text = " ".join(str(value or "").split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def listed(values: Iterable[object]) -> str:
    return ", ".join(clip(v, 40) for v in values if str(v or "").strip())


def _words(value: object) -> str:
    return str(value).replace("_", " ")


# --- strategy fields, route-neutral wording ---------------------------------------------------


def hook(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=(
            f'I can\'t use the opening hook idea ("{clip(value)}") on this video yet, '
            "so it won't be shown as text"
        ),
        tr=(
            f'Açılış kanca fikrini ("{clip(value)}") bu videoda henüz kullanamıyorum, '
            "o yüzden yazı olarak gösterilmeyecek"
        ),
    )


def story(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't follow the planned story structure ({listed(items)}) on this video yet, "
            "so it won't change the order of the clips"
        ),
        tr=(
            f"Planlanan hikâye yapısını ({listed(items)}) bu videoda henüz uygulayamıyorum, "
            "o yüzden kliplerin sırasını değiştirmeyecek"
        ),
    )


def caption_style(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value or value in {"auto", "none"}:
        return None
    return say(
        en=f"I can't apply the {value} caption style on this video yet, so it won't be used",
        tr=f"{value} altyazı stilini bu videoda henüz uygulayamıyorum, kullanılmayacak",
    )


def song_caption_style(value: Any, strategy: Mapping[str, Any]) -> str | None:
    """On a creator-song montage the style reaches the lyric captions the planner pinned
    (``song_captions``, KRI-578); with no lyric captions it reaches nothing."""
    if strategy.get("song_captions"):
        return None
    return caption_style(value, strategy)


def captions_always_on(value: Any, _s: Mapping[str, Any]) -> str | None:
    """Talking and Narrated writers always caption: the item stores "none" as no style, which
    the worker reads as sentence captions, and no creation input turns them off."""
    if value != "none":
        return None
    return say(
        en="I can't turn captions off on this kind of edit yet, so the video will have captions",
        tr=(
            "Bu tür bir düzenlemede altyazıları henüz kapatamıyorum, o yüzden videoda altyazı "
            "olacak"
        ),
    )


def treatments(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't add the treatments ({listed(items)}) on this video yet, "
            "so they won't be applied"
        ),
        tr=f"Şu efektleri ({listed(items)}) bu videoda henüz ekleyemiyorum, uygulanmayacak",
    )


def archetype(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en=f"I can't use the {_words(value)} story archetype on this video yet",
        tr=f"{_words(value)} hikâye kalıbını bu videoda henüz kullanamıyorum",
    )


def hero(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't feature one hero clip on this video yet, so every clip is cut the same way",
        tr=(
            "Bu videoda tek bir ana klibi öne çıkaramıyorum, "
            "bu yüzden bütün klipler aynı şekilde kesiliyor"
        ),
    )


def overlay(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't use the planned overlay display on this video yet, so it won't be shown",
        tr="Planlanan katman gösterimini bu videoda henüz kullanamıyorum, gösterilmeyecek",
    )


def sfx(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't add sound effects on this video yet, so none will be played",
        tr="Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak",
    )


def title_animation(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en=f"I can't animate the title ({_words(value)}) on this video yet, so it shows without "
        "motion",
        tr=f"Başlığı ({_words(value)}) bu videoda henüz canlandıramıyorum, o yüzden hareketsiz "
        "görünecek",
    )


def label_position(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en=f"I can't place the clip labels at the {_words(value)} on this video yet",
        tr=f"Klip etiketlerini bu videoda henüz {_words(value)} konumuna yerleştiremiyorum",
    )


def opening_title(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=f'I can\'t add the opening title ("{clip(value)}") on this video yet',
        tr=f'Açılış başlığını ("{clip(value)}") bu videoya henüz ekleyemiyorum',
    )


def closing_title(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=f'I can\'t add the closing title ("{clip(value)}") on this video yet',
        tr=f'Kapanış başlığını ("{clip(value)}") bu videoya henüz ekleyemiyorum',
    )


def shot_labels(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not [v for v in (value or []) if str(v or "").strip()]:
        return None
    return say(
        en="I can't put the planned label on each shot on this video yet",
        tr="Planlanan etiketleri bu videoda henüz her çekime koyamıyorum",
    )


def text_look(value: Any, _s: Mapping[str, Any]) -> str | None:
    """A font or color for text the route does not draw from the plan."""
    if not str(value or "").strip():
        return None
    return say(
        en="I can't apply the planned text font and color on this video yet",
        tr="Planlanan yazı tipini ve rengini bu videoda henüz uygulayamıyorum",
    )


def stated_length(instead_en: str, instead_tr: str) -> Builder:
    """A length the creator stated that the route does not render to. Says no number: the
    card's other lengths are the plan's, and a second figure would contradict them."""

    def build(_value: Any, strategy: Mapping[str, Any]) -> str | None:
        stated = strategy.get("creator_duration_s") is not None or bool(
            strategy.get("target_duration_requested")
        )
        if not stated:
            return None
        return say(
            en=f"I can't make this video the length you asked for yet: {instead_en}",
            tr=f"Bu videoyu henüz istediğin uzunlukta yapamıyorum: {instead_tr}",
        )

    return build


def direction_line(own: str | None, instead_en: str, instead_tr: str) -> Builder:
    """``own`` is the direction the route always cuts (no line when the plan says the same)."""

    def build(value: Any, _s: Mapping[str, Any]) -> str | None:
        if not value or value == own:
            return None
        return say(
            en=f"The {_words(value)} direction isn't applied on this video yet: {instead_en}",
            tr=f"{_words(value)} yönü bu videoda henüz uygulanmıyor: {instead_tr}",
        )

    return build


def pacing_line(
    applied: Callable[[Any, Mapping[str, Any]], bool], instead_en: str, instead_tr: str
) -> Builder:
    """``applied(value, strategy)`` is True when the route follows this pace. The card always
    shows the plan's pace ("Balanced pacing"), so a default the route ignores is still named."""

    def build(value: Any, strategy: Mapping[str, Any]) -> str | None:
        if not value or applied(value, strategy):
            return None
        return say(
            en=f"The {_words(value)} pacing isn't applied on this video yet: {instead_en}",
            tr=f"{_words(value)} tempo bu videoda henüz uygulanmıyor: {instead_tr}",
        )

    return build


def no_pace_chosen(_value: Any, strategy: Mapping[str, Any]) -> bool:
    """For a writer that follows a voice, not a pace: only a pace someone chose (the creator,
    or the planner picking relaxed or fast) is worth a line, never the silent default."""
    from app.services.length_policy import effective_pace  # noqa: PLC0415

    return effective_pace(strategy) is None


def slide_audio(value: Any, _s: Mapping[str, Any]) -> str | None:
    """A slide post plays each video slide's own sound and nothing added."""
    if value not in {"licensed_music", "user_song", "voiceover"}:
        return None
    return say(
        en="A slide post plays no added music or voiceover yet, so the planned audio won't be used",
        tr="Slayt gönderisi henüz eklenmiş müzik ya da seslendirme çalmıyor, planlanan ses "
        "kullanılmayacak",
    )


# --- typed brief asks (facts only, KRI-667) ---------------------------------------------------


def asks_text_animation(req: BriefRequirement) -> bool:
    """A typed ``animation`` on a text or style ask (an edge id, or "any")."""
    if req.kind not in {"text", "style"}:
        return False
    return bool(req.facts.get("animation"))


def asks_captions_on(req: BriefRequirement) -> bool:
    """A typed ``captions`` fact that turns speech captions on ("on" or "word_by_word")."""
    from app.kria.brief_checks import _caption_fact  # noqa: PLC0415

    return _caption_fact(req) in {"on", "word_by_word"}


# Positions the creation text pass can give a title from the creator's own words
# (`schemas.phone_text_style.explicit_text_rule` reads corner phrases only).
_CORNER_POSITIONS = frozenset({"top_left", "top_right", "bottom_left", "bottom_right"})


def asks_title_position(req: BriefRequirement) -> bool:
    """A title-scope text ask with a typed ``position`` no title placement reads.

    `unified_montage.brief_view` reads ``facts.position`` on per-clip and global text only; a
    corner may still reach the title through the phone text style pass, so only an edge or the
    middle is declined."""
    from app.schemas.text_style_intent import normalize_label_position  # noqa: PLC0415

    if req.kind != "text" or req.scope != "title":
        return False
    spot = normalize_label_position(req.facts.get("position"))
    return spot is not None and spot not in _CORNER_POSITIONS


ANIMATION_DECLINE_EN = (
    "I can't animate on-screen text on this video yet, so the text shows without motion"
)
ANIMATION_DECLINE_TR = (
    "Bu videoda ekrandaki yazıyı henüz canlandıramıyorum, o yüzden yazı hareketsiz görünecek"
)
CAPTIONS_DECLINE_EN = "I can't add subtitles of the speech to this video yet, so it has none"
CAPTIONS_DECLINE_TR = "Bu videoya henüz konuşmanın altyazısını ekleyemiyorum, o yüzden altyazı yok"
TITLE_POSITION_DECLINE_EN = (
    "I can't move the title to the spot you asked for on this video yet, so it stays where the "
    "layout puts it"
)
TITLE_POSITION_DECLINE_TR = (
    "Bu videoda başlığı henüz istediğin yere taşıyamıyorum, o yüzden düzenin koyduğu yerde kalıyor"
)


def animation_decline() -> str:
    return say(en=ANIMATION_DECLINE_EN, tr=ANIMATION_DECLINE_TR)


def captions_decline() -> str:
    return say(en=CAPTIONS_DECLINE_EN, tr=CAPTIONS_DECLINE_TR)


def title_position_decline() -> str:
    return say(en=TITLE_POSITION_DECLINE_EN, tr=TITLE_POSITION_DECLINE_TR)


# Every route-decline reason, both languages: `route_disclosure.is_route_decline_reason`.
DECLINE_PREFIXES: tuple[str, ...] = (
    ANIMATION_DECLINE_EN,
    ANIMATION_DECLINE_TR,
    CAPTIONS_DECLINE_EN,
    CAPTIONS_DECLINE_TR,
    TITLE_POSITION_DECLINE_EN,
    TITLE_POSITION_DECLINE_TR,
)
