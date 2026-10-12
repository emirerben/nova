"""A Voiceover edit keeps every attached clip unless the creator limited them (KRI-593).

On a Voiceover edit, a cue ("when I say 'Deniz', show Deniz at the laptop") says WHERE
a clip goes in the narration, not WHICH clips exist. The Main Creator read such cues as
a selection: prod thread f77a5c16 (2026-10-11) got ``media_scope: "selected"`` with the
four clips the cues named out of seven, the phone rendered those four, and the reply
never said three were left out. The native voiceover edit plays exactly the selected
clips (``content_plan_build._creator_selected_clip_paths``), so the model's pick was
final.

``keep_every_voiceover_clip`` makes "every attached video" the server's default for that
edit, whatever selection the model wrote, unless the creator limited the footage: in
their own words (any message in the thread) or in a Creative Brief ``select``
requirement. The reader of their words is deliberately broad. Reading a limit that is
not there keeps today's behaviour (the model's pick); missing a real one ("skip the
bathroom") would put back a clip the creator took out. So any narrowing word counts,
unless everything it governs is text, audio or length ("Başlık dışında başka yazı
olmasın", "no other text except the title", "without music").

Visuals (``asset-…``) and the creator's own photos are never added: the native
voiceover edit takes the project's own videos as clips, and Visuals reach it only as
reaction beats.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import MAX_MAIN_CREATOR_SELECTED_MEDIA
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief_route import loose_text

# Quoted words are usually a cue's trigger or a title, so their own words ("Only Us",
# "mutfak") are never read as an ask. They stand in as one unknown word, though: in
# 'skip "the bathroom"' the quote is what the ask is about.
_QUOTED = re.compile(r"[\"“”«»„][^\"“”«»„]{0,300}[\"“”«»„]|(?<!\w)['‘’][^'‘’\n]{1,300}['‘’](?!\w)")
_QUOTE_STAND_IN = " quotedwords "
_CLAUSE_END = re.compile(r"[.!?;\n]+")
_WORD = re.compile(r"[a-z0-9']+")

_EN_NUMBERS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
# "bir" (one) is left out: it is also the indefinite article ("bir klip daha").
_TR_NUMBERS = {
    "iki": 2,
    "uc": 3,
    "dort": 4,
    "bes": 5,
    "alti": 6,
    "yedi": 7,
    "sekiz": 8,
    "dokuz": 9,
}
_EN_NUMBER = "|".join(_EN_NUMBERS)
_TR_NUMBER = "|".join(_TR_NUMBERS)
_TIME_UNIT = r"(?:s|sec|secs|second|seconds|min|mins|minute|minutes|sn|saniye\w*|dakika\w*)"

# Asks for LESS than the whole footage set, folded by ``loose_text`` (lower case, no
# diacritics). Turkish forms are Turkish-only spellings, so they never fire on English.
_EN_NARROWING = re.compile(
    r"\b(?:only|skip\w*|exclud\w*|except\w*|omit\w*|without|ignor\w*|drop\w*|remov\w*"
    r"|delet\w*|cut\s+out|get\s+rid|lose|ditch\w*|toss\w*|hid(?:e|es|ing|den)"
    r"|leav(?:e|ing)\b(?:\s+\S+){0,4}?\s+out|left\b(?:\s+\S+){0,4}?\s+out"
    r"|(?:take|taking|took|taken|keep|kept|throw\w*)\b(?:\s+\S+){0,4}?\s+(?:out|away)"
    r"|(?:don'?t|do\s+not|never|shouldn'?t|should\s+not|mustn'?t|must\s+not|won'?t)\s+"
    r"(?:use|include|show|put|add|need|want|keep|be|appear)"
    r"|not\s+(?:all|every|the|these|those|that|this|any|my)"
    # A bare "No," answering a question is not a limit; "no bathroom" is.
    r"|no(?!\s*(?:[,:]|$))|nothing|none"
    r"|fewer|less|some\s+of|a\s+few|subset|pick\w*|choos\w*|select\w*|best|strongest"
    rf"|favou?rites?|too\s+many|just\s+(?:the|these|those|my|\d+|{_EN_NUMBER})"
    r"|instead\s+of|rather\s+than|other\s+than|apart\s+from|besides|but\s+not)\b"
)
_TR_NARROWING = re.compile(
    r"\b(?:sadece|yalnizca|yalniz|haric\w*|disinda\w*|olmadan|bazi\w*|hepsi(?:ni)?\s+degil"
    r"|en\s+(?:iyi|guzel|guclu)\w*|daha\s+az|az\s+sayida|cok\s+fazla"
    r"|secili\w*|sectigim\w*|secilen\w*|sec(?:in|iniz|elim)|yerine"
    r"|kullanma(?:yin|yiniz|sin|yalim|dan)?|gosterme(?:yin|yiniz|sin)?|koyma(?:yin|yiniz|sin)?"
    r"|ekleme(?:yin|yiniz|sin)?|olmasin|cikmasin|gorunmesin|gozukmesin|yer\s+almasin"
    r"|atla(?:yin|yiniz|sin|yalim)?|gec(?:in|iniz|elim)?|cikar(?:in|iniz|t|tin|sin|alim)?"
    r"|sil(?:in|iniz|sin|elim)?|kaldir(?:in|iniz|sin|alim)?|istemiyorum|istemem"
    r"|dahil\s+etme(?:yin|yiniz|sin|yelim)?|gizle(?:yin|yiniz|sin|yelim)?"
    r"|disari(?:da)?\s+birak\w*)\b"
)
# The words of those asks, one by one: what remains of a clause once they (and glue
# words) are gone is what the ask is about. Dropping words, not whole matches, keeps
# "leave the bathroom out" about the bathroom.
_NARROWING_WORD = re.compile(
    r"only|skip\w*|exclud\w*|except\w*|omit\w*|without|ignor\w*|drop\w*|remov\w*|delet\w*"
    r"|cut|out|get|rid|lose|leave|leaving|left|don't|dont|not|never|shouldn't|should"
    r"|ditch\w*|toss\w*|hide|hides|hiding|hidden|take|taking|took|taken|kept|throw\w*|away"
    r"|dahil|etme\w*|gizle\w*|disari\w*|birak\w*"
    r"|mustn't|won't|no|nothing|none|fewer|less|some|few|subset|pick\w*|choos\w*|select\w*"
    r"|best|strongest|favou?rites?|many|just|instead|rather|apart|besides"
    r"|sadece|yalnizca|yalniz|haric\w*|disinda\w*|olmadan|bazi\w*|hepsi|hepsini|degil|en"
    r"|iyi\w*|guzel\w*|guclu\w*|daha|az|sayida|fazla|secili\w*|sectigim\w*|secilen\w*"
    r"|secin|seciniz|secelim|yerine|kullanma\w*|gosterme\w*|koyma\w*|ekleme\w*|olmasin"
    r"|cikmasin|gorunmesin|gozukmesin|yer|almasin|atla\w*|gec|gecin|geciniz|gecelim"
    r"|cikar\w*|sil|silin|siliniz|silsin|silelim|kaldir\w*|istemiyorum|istemem"
)
# "on the video" / "ekranda" name the finished edit, not one of its clips.
_WHOLE_EDIT = re.compile(
    r"\b(?:on|in|to|throughout|across|over|of|from)\s+(?:the|this|my|our|your)\s+"
    r"(?:whole\s+|entire\s+|final\s+)?(?:video|edit|reel|tiktok)\b"
)
_FOOTAGE_WORD = re.compile(
    r"clips?|videos?|shots?|footage|scenes?|takes|photos?|pictures?|pics?|images?|stills?"
    r"|media|files?|b-?roll|moments?|parts?|bits?"
    r"|kli[pb]\w*|video\w+|cekim\w*|goruntu\w*|sahne\w*|fotograf\w*|foto\w*|resim\w*|resm\w*"
    r"|medya\w*|dosya\w*"
)
# What a narrowing word may govern without touching the footage.
_NOT_FOOTAGE_WORD = re.compile(
    r"texts?|titles?|captions?|subtitles?|labels?|words?|fonts?|colou?rs?|music|songs?"
    r"|sounds?|audio|voice\w*|narration|effects?|sfx|transitions?|filters?|stickers?|emojis?"
    r"|logos?|watermarks?|overlays?|seconds?|secs?|minutes?|mins?|length|duration|long"
    r"|intro|outro|hooks?|hashtags?|screen|onscreen|on-screen"
    r"|yazi\w*|baslik\w*|altyazi\w*|etiket\w*|metin\w*|font\w*|renk\w*|muzi[gk]\w*|sarki\w*"
    r"|ses\w*|efekt\w*|gecis\w*|filtre\w*|cikartma\w*|emoji\w*|logo\w*|saniye\w*|sn"
    r"|dakika\w*|sure\w*|uzunlu\w*|ekran\w*"
)
_STOP_WORDS = frozenset(
    (
        # English glue, pronouns and the generic verbs of an edit request.
        "a an the any other others else more most all at on in into onto to of for from with "
        "by about it its it's this that these those there here me my mine i i'm you your we "
        "our us they them their is are be been being was were am do does did should would "
        "will can could must may might please also too very really so then than as and or "
        "but yet make made put add show shown have has had use used using want need keep let "
        "lets let's see appear visible over during throughout whole entire final edit reel "
        "yes ok okay sure thanks thank anything something way "
        # Turkish glue, the same verbs, and "in/on the video / on screen".
        "baska baskasi hic hicbir bir de da ve ile ama fakat ancak lutfen olsun olacak olmali "
        "olmayacak yok bu su o ben sen biz siz onlar icin gibi kadar cok her herhangi sey "
        "seyler evet tamam kullan kullanilsin koy ekle goster yap gorunsun istiyorum uzerinde "
        "ustunde ustune icinde olarak ki mi mu videoda videonun videonda videoya"
    ).split()
)


def _clauses(text: str) -> list[str]:
    plain = _QUOTED.sub(_QUOTE_STAND_IN, text.replace("’", "'").replace("‘", "'"))
    return [loose_text(part) for part in _CLAUSE_END.split(plain) if part.strip()]


def _stated_counts(clause: str) -> list[int]:
    english = re.finditer(
        rf"\b(\d{{1,3}}|{_EN_NUMBER})\s+(?:of\s+(?:the|my|your|these|those)\s+)?"
        rf"(?:(?!{_TIME_UNIT}\b)[a-z']+\s+)?"
        r"(?:clips?|videos?|shots?|photos?|pictures?|images?|scenes?|takes?)\b",
        clause,
    )
    turkish = re.finditer(
        rf"\b(\d{{1,3}}|{_TR_NUMBER})\s+(?:tane\s+)?(?:(?!{_TIME_UNIT}\b)[a-z]+\s+)?"
        r"(?:kli[pb]|video|cekim|fotograf|foto|sahne|goruntu)\w*",
        clause,
    )
    counts = []
    for match in (*english, *turkish):
        raw = match.group(1)
        counts.append(int(raw) if raw.isdigit() else _EN_NUMBERS.get(raw, _TR_NUMBERS.get(raw, 0)))
    return counts


# A removal the creator negates keeps the clip ("don't take out the loft clip", "never
# remove it"): it is cut from the clause before the narrowing check, with its "… out" tail.
_NEGATED_REMOVAL = re.compile(
    r"\b(?:don'?t|do\s+not|doesn'?t|does\s+not|never|shouldn'?t|should\s+not|mustn'?t"
    r"|must\s+not|won'?t|will\s+not|not)(?:\s+(?:ever|even|please))?\s+"
    r"(?:skip\w*|exclud\w*|omit\w*|ignor\w*|drop\w*|remov\w*|delet\w*|cut|lose|ditch\w*"
    r"|toss\w*|hid(?:e|ing)|leav(?:e|ing)|take|throw\w*|get\s+rid\s+of)\b"
    r"(?:(?:\s+\S+){0,4}?\s+(?:out|away)\b)?"
)


def _narrows(clause: str) -> bool:
    """A narrowing word whose clause names footage, or anything that isn't text/audio."""

    clause = _NEGATED_REMOVAL.sub(" ", clause)
    if not (_EN_NARROWING.search(clause) or _TR_NARROWING.search(clause)):
        return False
    for raw in _WORD.findall(_WHOLE_EDIT.sub(" ", clause)):
        word = raw.strip("'")
        # A number stays: "pick the best 4" is a limit with no noun at all.
        if not word or word in _STOP_WORDS:
            continue
        if _FOOTAGE_WORD.fullmatch(word):
            return True
        if _NARROWING_WORD.fullmatch(word) or _NOT_FOOTAGE_WORD.fullmatch(word):
            continue
        return True
    return False


def creator_limits_clips(texts: Iterable[str | None], *, own_clip_count: int) -> bool:
    """True when the creator's words ask for fewer than all ``own_clip_count`` clips."""

    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for clause in _clauses(text):
            if any(0 < count < own_clip_count for count in _stated_counts(clause)):
                return True
            if _narrows(clause):
                return True
    return False


def keep_every_voiceover_clip(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    creator_texts: Iterable[str | None],
    brief_selects_clips: bool = False,
) -> CreativeStrategy:
    """The voiceover strategy with every own video selected, or ``strategy`` unchanged.

    Unchanged unless: a narrated (Voiceover) edit with a recorded voiceover, rendered
    natively, that leaves out some of the project's own videos, all of which fit one
    native edit, with no ``select`` requirement in the live brief and no limit in the
    creator's words. The model's order is kept; the missing videos follow in the order
    they were attached (the resolved ``order`` intents still place clips at dispatch).
    """

    if (
        strategy.edit_format not in NARRATED_EDIT_FORMATS
        or not manifest.has_voiceover
        or strategy.render_program != "native"
        or brief_selects_clips
    ):
        return strategy
    own_videos = [
        media.media_id
        for media in manifest.media
        if media.kind == "video" and not media.media_id.startswith("asset-")
    ]
    selected = list(dict.fromkeys(strategy.selected_media_ids))
    missing = [media_id for media_id in own_videos if media_id not in selected]
    if not missing or len(selected) + len(missing) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        return strategy
    if creator_limits_clips(creator_texts, own_clip_count=len(own_videos)):
        return strategy
    return strategy.model_copy(update={"selected_media_ids": [*selected, *missing]})


__all__ = ["creator_limits_clips", "keep_every_voiceover_clip"]
