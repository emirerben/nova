"""The clips a creator ruled out by length never play (KRI-623).

Prod thread 35ccc8ef (2026-10-12, cabin kit M2): two long takes (70 s, 67 s) and two
short clips (5 s, 7 s). "Pick the best six moments from the long takes, each very short,
and skip the dark ones." The Main Creator took every clip (the day before, the same
prompt got only the two takes), nothing on the server read "from the long takes", and the
moment selector gives every selected source at least one cut, so the render opened on the
two short clips.

``apply_clip_scope`` re-checks the model's pick against the creator's own words on every
plan: a live Creative Brief ``select`` ask and the current message. Only asks about clip
LENGTH are read, because length is the one thing the server can measure from the words
alone ("skip the dark ones" is an ``exclude`` intent below; KRI-680 gives the resolver a
measured brightness line to match it against). The
reader is deliberately narrow: a wrong drop removes footage the creator wanted, a missed
ask keeps today's behaviour. So a length word counts only when it names a subset of the
attached clips ("the long takes", "uzun çekimlerden") in a selecting phrase ("from",
"only", "skip", "except", "kullanma"); a description ("two long takes of our cabin") or a
cut-length style ("fast cuts, short clips", "each very short") never does. Nor does ONE
clip told apart by what it shows ("Caro'nun eğilip tabağı masaya bıraktığı kısa klibi
kullanma", "skip the short clip where …", KRI-758): a singular phrase is the
``exclude`` intent's job below, not a length group (the single "longest" / "shortest"
clip is the exception).

Long and short come from the clips' own durations: one clear gap (the longer side at
least twice the shorter, and no second gap like it) splits them. Clips of similar length,
or a clip of unknown length, leave the pick alone. Photos and Visuals are never dropped:
they are not takes.

``apply_clip_exclusions`` (KRI-667) narrows the pick the same way for the clips the
creator named to leave out ("Skip the quad bike clip", "çamaşır klibini kullanma"): the
clip-intent planner states them as ``exclude`` intents and the resolver keeps only its
confident matches, so here nothing is read from words, only resolved intents applied.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import MAX_MAIN_CREATOR_SELECTED_MEDIA
from app.kria.brief_route import loose_text
from app.schemas.clip_intents import (
    CREATOR_EVIDENCE,
    MEMBERSHIP_MIN_CONFIDENCE,
    ResolvedClipIntent,
)

Group = Literal["long", "short", "longest", "shortest", "all"]
# "only" keeps just this group, "also" adds it back, "skip" leaves it out; "reset" (group
# "all": "use all the clips") forgets every earlier length ask.
Effect = Literal["only", "also", "skip", "reset"]


@dataclass(frozen=True)
class LengthAsk:
    group: Group
    effect: Effect


class _Media(Protocol):
    media_id: str
    kind: str
    duration_s: float | None


# A long/short split needs the longer side at least this many times the shorter, with
# no second gap that large; "the longest" needs it 1.5x the runner-up.
_SPLIT_RATIO = 2.0
_EXTREME_RATIO = 1.5

_QUOTED = re.compile(r"[\"“”«»„][^\"“”«»„]{0,300}[\"“”«»„]")
_CLAUSE_END = re.compile(r"[.!?;:,\n]+|\b(?:and|but|then|plus|while|ve|ama|sonra)\b")

# -- English ---------------------------------------------------------------------------
_EN_DET = r"(?:the|my|our|these|those|both|all\s+the|all|any\s+of\s+the|any)"
_EN_COUNT = r"(?:\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|couple\s+of|few)"
# A word between the length and the noun ("the long handheld takes"), never one that
# turns the phrase into moments ("short moments from the clips").
_EN_GAP = (
    r"(?!(?:moments?|cuts?|bits?|parts?|pieces?|sections?|segments?|highlights?|from|of|in"
    r"|on|at|the|a|an|and|or|with|each|every|to|for|into|out)\b)[a-z][a-z'-]*"
)
_EN_NOUN = r"(?:takes?|clips?|videos?|vids?|shots?|recordings?|footage|ones?|walkthroughs?)"
_EN_PHRASE = re.compile(
    rf"\b{_EN_DET}\s+(?:{_EN_COUNT}\s+)?(?:(?:very|really|super)\s+)?"
    rf"(?P<adj>long|longer|longest|short|shorter|shortest)\s+(?:{_EN_GAP}\s+){{0,2}}{_EN_NOUN}\b"
)
_EN_NEGATION = r"\b(?:don'?t|do\s+not|never|not|no|nothing|none)\b"
# "don't skip the short clips" keeps them: not an ask about the subset at all.
_EN_NEGATED_REMOVAL = re.compile(
    rf"{_EN_NEGATION}\s+(?:ever\s+)?(?:skip|exclude|drop|omit|ignore|remove|delete|cut|lose"
    r"|ditch|toss|hide|leave|take|throw|get\s+rid\s+of)\b"
)
_EN_EXCEPT = re.compile(r"\b(?:except(?:\s+for)?|apart\s+from|other\s+than|besides)\s+$")
_EN_SKIP = re.compile(
    r"\b(?:skip\w*|exclud\w*|omit\w*|ignor\w*|drop\w*|remov\w*|delet\w*|ditch\w*|toss\w*"
    r"|without|lose|not|never\s+use|(?:don'?t|do\s+not)\s+(?:use|include|show|put|add|want"
    r"|need|keep)|(?:leave|leaving|left|cut|get\s+rid)\s+(?:out|of)|leave\s+out)\b"
)
_EN_OUT_VERB = re.compile(r"\b(?:leave|leaving|keep|keeping|take|taking|throw|cut)\b")
_EN_OUT_TAIL = re.compile(r"^\s+(?:out|away)\b")
_EN_FROM = re.compile(r"\b(?:only\s+)?(?:from|out\s+of)\s+$")
_EN_ONLY = re.compile(
    r"\b(?:only|just|solely|exclusively)\s+(?:(?:use|using|from|of|keep|take|show|pick|choose)"
    r"\s+){0,2}$"
)
_EN_ONLY_TAIL = re.compile(r"^\s+only\b")
_EN_ALSO_VERB = re.compile(r"\b(?:use|include|add|keep|bring|put|want)\b")
_EN_ALSO = re.compile(r"\b(?:too|also|as\s+well|again|back)\b")
_EN_EVERYTHING = re.compile(
    r"\b(?:use|using|include|keep|add|want|put\s+in)\s+(?:all|every|each)"
    r"(?:\s+(?:of\s+)?(?:the|my|our|these|those))?(?:\s+\d{1,2})?"
    r"\s+(?:clips?|videos?|takes?|footage|shots?)\b"
)
# "the short clip where …" names ONE clip. "any short clip" and "all the short clip" read
# as the group, so only a definite determiner counts.
_EN_ONE_DET = ("the", "my", "our")
_EN_ONE_NOUN = frozenset(
    {"take", "clip", "video", "vid", "shot", "recording", "walkthrough", "one"}
)

# -- Turkish (folded: no diacritics, ı -> i) ---------------------------------------------
_TR_PHRASE = re.compile(
    r"\b(?:(?P<sup>en)\s+)?(?:daha\s+)?(?P<adj>uzun|kisa)\s+"
    r"(?:cekim|klip|klib|video|kayit|kayd|goruntu)(?P<suffix>[a-z]*)\b"
)
_TR_ONLY = re.compile(r"\b(?:sadece|yalnizca|yalniz)\s+$")
# "atlama" / "çıkarma" (don't skip / don't remove) are not removals: the exact forms only.
_TR_SKIP = re.compile(
    r"\b(?:atla(?:yin|yiniz|sin|yalim)?|kullanma(?:yin|yiniz|sin)?|cikar(?:in|iniz|sin|alim)?"
    r"|koyma(?:yin|yiniz|sin)?|gosterme(?:yin|yiniz|sin)?|ekleme(?:yin|yiniz|sin)?"
    r"|sil(?:in|iniz|sin|elim)?|kaldir(?:in|iniz|sin|alim)?|olmasin|istemiyorum|istemem"
    r"|haric\w*|disinda\w*)\b"
)
_TR_ALSO = re.compile(r"^\s+(?:de|da)\s+(?:kullan|ekle|koy|olsun|gelsin|girsin)")
_TR_EVERYTHING = re.compile(
    r"\b(?:tum|butun)\s+(?:klip|klib|video|cekim)\w*|\b(?:klip|klib|video|cekim)\w*\s+hepsi\w*"
)
# The relative-clause participle right before the length word, with its possessive ending
# ("-dığı", "-dığım", "-duğumuz"): "Caro'nun ... bıraktığı kısa klip" is one clip, told
# apart by what it shows. "-dık" forms need the plural possessor ("-dıkları") because
# a bare "-tik" ends plain words (plastik, praktik).
_TR_RELATIVE = (
    *(
        stem + ending
        for stem in ("dig", "tig", "dug", "tug")
        for ending in ("i", "u", "im", "um", "in", "un", "imiz", "umuz", "iniz", "unuz")
    ),
    *(stem + ending for stem in ("dik", "tik", "duk", "tuk") for ending in ("leri", "lari")),
)


def _group(adjective: str, *, superlative: bool = False) -> Group:
    long = adjective.startswith(("long", "uzun"))
    if superlative or adjective.endswith("est"):
        return "longest" if long else "shortest"
    return "long" if long else "short"


def _english_names_one_clip(match: re.Match[str]) -> bool:
    """One clip told apart by what it shows ("the short clip where …"), not the short ones.

    "the longest clip" is singular too, but it is the single longest clip of the set: a
    group of its own (KRI-758).
    """

    words = match.group().split()
    return (
        words[0] in _EN_ONE_DET
        and words[-1] in _EN_ONE_NOUN
        and not match.group("adj").endswith("est")
    )


def _turkish_names_one_clip(clause: str, match: re.Match[str]) -> bool:
    """One clip told apart by what it shows ("Caro'nun ... bıraktığı kısa klibi"), KRI-758.

    A definite singular noun ("klibi", "çekimi", "videoyu": the suffix ends in the vowel
    of the accusative or possessive) or a relative-clause participle right before the
    length word names a single clip. A plural suffix ("klipleri") never does, and "en
    kısa klibi" is the single shortest clip: a group of its own.
    """

    suffix = match.group("suffix")
    if match.group("sup") or "ler" in suffix or "lar" in suffix:
        return False
    words = clause[: match.start()].split()
    return suffix.endswith(("i", "u")) or bool(words and words[-1].endswith(_TR_RELATIVE))


def _english_effect(clause: str, start: int, end: int) -> Effect | None:
    before, after = clause[:start], clause[end:]
    except_word = _EN_EXCEPT.search(before)
    if except_word:
        # "everything except the short clips" skips them; "don't use anything except the
        # long takes" keeps only those.
        negated = re.search(_EN_NEGATION, before[: except_word.start()])
        return "only" if negated else "skip"
    if _EN_OUT_VERB.search(before) and _EN_OUT_TAIL.search(after):
        return "skip"
    if _EN_SKIP.search(before):
        return "skip"
    if _EN_FROM.search(before) or _EN_ONLY.search(before) or _EN_ONLY_TAIL.search(after):
        return "only"
    if _EN_ALSO_VERB.search(before) and _EN_ALSO.search(clause):
        return "also"
    return None


def _turkish_effect(clause: str, match: re.Match[str]) -> Effect | None:
    before, after = clause[: match.start()], clause[match.end() :]
    if _TR_SKIP.search(after):
        return "skip"
    if _TR_ALSO.search(after):
        return "also"
    if _TR_ONLY.search(before) or match.group("suffix").endswith(("den", "dan", "ten", "tan")):
        return "only"
    return None


def length_asks(text: str) -> list[LengthAsk]:
    """The length-scoped clip asks in one message or brief ask, in the order written."""

    asks: list[LengthAsk] = []
    plain = _QUOTED.sub(" ", text.replace("’", "'").replace("‘", "'"))
    for raw in _CLAUSE_END.split(loose_text(plain)):
        clause = f" {raw.strip()} "
        if _EN_NEGATED_REMOVAL.search(clause):
            continue
        if not (_EN_PHRASE.search(clause) or _TR_PHRASE.search(clause)):
            # "actually, use all the clips" / "tüm klipleri kullan": every clip again.
            if _EN_EVERYTHING.search(clause) or (
                _TR_EVERYTHING.search(clause) and not _TR_SKIP.search(clause)
            ):
                asks.append(LengthAsk("all", "reset"))
            continue
        for match in _EN_PHRASE.finditer(clause):
            if _english_names_one_clip(match):
                continue
            effect = _english_effect(clause, match.start(), match.end())
            if effect is not None:
                asks.append(LengthAsk(_group(match.group("adj")), effect))
        for match in _TR_PHRASE.finditer(clause):
            if _turkish_names_one_clip(clause, match):
                continue
            effect = _turkish_effect(clause, match)
            if effect is not None:
                asks.append(
                    LengthAsk(
                        _group(match.group("adj"), superlative=bool(match.group("sup"))), effect
                    )
                )
    return asks


def _own_videos(media: Iterable[_Media]) -> list[_Media]:
    return [m for m in media if m.kind == "video" and not m.media_id.startswith("asset-")]


def length_groups(media: Iterable[_Media]) -> dict[Group, set[str]] | None:
    """Long/short (and longest/shortest) clip ids, or None when no clear split exists."""

    videos = _own_videos(media)
    if len(videos) < 2 or any(not m.duration_s or m.duration_s <= 0 for m in videos):
        return None
    ordered = sorted(videos, key=lambda m: float(m.duration_s or 0))
    seconds = [float(m.duration_s or 0) for m in ordered]
    ratios = [later / earlier for earlier, later in zip(seconds, seconds[1:], strict=False)]
    best = max(range(len(ratios)), key=ratios.__getitem__)
    if ratios[best] < _SPLIT_RATIO or any(
        ratio >= _SPLIT_RATIO for index, ratio in enumerate(ratios) if index != best
    ):
        return None
    groups: dict[Group, set[str]] = {
        "short": {m.media_id for m in ordered[: best + 1]},
        "long": {m.media_id for m in ordered[best + 1 :]},
        "shortest": set(),
        "longest": set(),
    }
    if seconds[1] >= seconds[0] * _EXTREME_RATIO:
        groups["shortest"] = {ordered[0].media_id}
    if seconds[-1] >= seconds[-2] * _EXTREME_RATIO:
        groups["longest"] = {ordered[-1].media_id}
    return groups


def ruled_out_clips(texts: Iterable[str | None], media: Sequence[_Media]) -> set[str]:
    """Own video ids the creator's length asks leave out, oldest ask first.

    "only"/"from" keeps a group (several keep their union), "skip" drops one, a later
    "too"/"back" adds a group back and "use all the clips" starts over. Nothing is dropped
    when the words name no length subset, the clips have no clear split, or the asks
    would leave no clip at all.
    """

    asks = [ask for text in texts if isinstance(text, str) for ask in length_asks(text)]
    if not asks:
        return set()
    groups = length_groups(media)
    if groups is None:
        return set()
    own = {m.media_id for m in _own_videos(media)}
    kept: set[str] | None = None
    skipped: set[str] = set()
    for ask in asks:
        if ask.effect == "reset":
            kept, skipped = None, set()
            continue
        ids = groups[ask.group]
        if not ids:
            return set()  # "the longest" with no clear longest: leave the pick alone
        if ask.effect == "only":
            kept = (kept or set()) | ids
            skipped -= ids
        elif ask.effect == "also":
            kept = None if kept is None else kept | ids
            skipped -= ids
        else:
            skipped |= ids
    allowed = (own if kept is None else kept) - skipped
    if not allowed:
        return set()
    return own - allowed


def apply_clip_scope(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    creator_texts: Iterable[str | None],
) -> CreativeStrategy:
    """``strategy`` without the clips the creator ruled out by length, or unchanged.

    An "all" scope or an empty pick (the guided program's "take everything") becomes an
    explicit ``selected`` list of the manifest media minus the ruled-out clips, so the
    dispatch, the length plan and the phone montage all see the narrowed set. A pick that
    kept none of the allowed clips gets the allowed clips (attachment order). A guided
    voiceover contract (``execution_contract``) keeps its scope. A native edit
    takes at most ``MAX_MAIN_CREATOR_SELECTED_MEDIA`` clips; a longer list is left to the
    model rather than cut to an arbitrary dozen.
    """

    if strategy.execution_contract is not None:
        # The guided voiceover contract needs its all-media scope; its specialist picks.
        return strategy
    # A clip the strategy itself plays or places stays (as in ``excluded_clips``): a length
    # word never outranks the montage's own audio source or a reaction beat.
    dropped = ruled_out_clips(creator_texts, manifest.media) - placed_media(strategy)
    if not dropped:
        return strategy
    takes_everything = strategy.media_scope == "all" or not strategy.selected_media_ids
    base = (
        [media.media_id for media in manifest.media]
        if takes_everything
        else list(dict.fromkeys(strategy.selected_media_ids))
    )
    kept = [media_id for media_id in base if media_id not in dropped]
    if kept == base and strategy.media_scope == "selected":
        return strategy
    own = {media.media_id for media in _own_videos(manifest.media)}
    if not own.intersection(kept):
        kept = [*kept, *(m.media_id for m in manifest.media if m.media_id in own - dropped)]
    if strategy.render_program == "native" and len(kept) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        return strategy
    update: dict[str, object] = {"media_scope": "selected", "selected_media_ids": kept}
    if strategy.hero_media_id in dropped:
        update["hero_media_id"] = None
    return strategy.model_copy(update=update)


def placed_media(strategy: CreativeStrategy) -> set[str]:
    """Media the strategy itself places or plays: never removed by an exclusion."""

    placed = {beat.visual_id for beat in strategy.reaction_beats or [] if beat.visual_id}
    placed.update(
        beat.following_clip_id for beat in strategy.reaction_beats or [] if beat.following_clip_id
    )
    if strategy.closing_media is not None:
        placed.add(strategy.closing_media.visual_id)
        if strategy.closing_media.badge_visual_id:
            placed.add(strategy.closing_media.badge_visual_id)
    if strategy.ending_clip is not None:
        placed.add(strategy.ending_clip.media_id)  # KRI-841: the clip the edit ends on
    if strategy.background_media is not None:
        placed.add(strategy.background_media.media_id)  # KRI-1117: the backdrop behind the talk
    if strategy.montage_audio is not None:
        placed.update(strategy.montage_audio.source_media_ids)
    if strategy.montage_cadence is not None:
        placed.update(strategy.montage_cadence.source_media_ids)
    return placed


def excluded_clips(
    resolved_intents: Iterable[ResolvedClipIntent] | None, strategy: CreativeStrategy
) -> set[str]:
    """Media ids resolved ``exclude`` intents leave out (KRI-667), minus every protected one.

    A clip another resolved intent names (an include, an order, a group, a caption or a
    label) stays: two asks about one clip contradict, and keeping footage is the safe
    side. So does media the strategy itself places (a reaction beat, the closing media,
    a montage audio or cadence source).

    KRI-1026: two kinds of "naming" are not a contradiction. A whole-set order ("in the
    order we filmed it") assigns EVERY clip and says nothing about any one of them, so it
    protects none. And a clip the creator tapped out (or named by file) stays out unless
    the creator also settled another ask onto it: a resolver match for a label or an
    include is a guess, the tap is their word.

    Leverkusen M1 run 2 #5: a first/last ask that matched several clips ("starting with
    the train ride": the train video and two photos) gives up the ones a skip names ("the
    photo of the empty train seat") while one of its clips is left to open the edit.
    """

    intents = [i for i in resolved_intents or [] if i.status == "resolved"]
    excluded = {a.media_id for i in intents if i.op == "exclude" for a in i.assignments}
    if not excluded:
        return set()
    tapped = {
        a.media_id
        for i in intents
        if i.op == "exclude"
        for a in i.assignments
        if a.evidence in CREATOR_EVIDENCE
    }
    # KRI-993: a subject scope ("only about the steak") never protects a clip the creator
    # excluded: a skip beats a scope match.
    named = {
        a.media_id
        for i in intents
        if i.op != "exclude"
        and not i.exclusive
        and not _orders_every_clip(i)
        and not (places_a_group(i) and any(m.media_id not in excluded for m in i.assignments))
        for a in i.assignments
        if a.media_id not in tapped or a.evidence in CREATOR_EVIDENCE
    }
    return excluded - named - placed_media(strategy)


def _orders_every_clip(intent: ResolvedClipIntent) -> bool:
    """A filming-time or route order: it sorts the whole set, whatever the set is."""
    return intent.op == "order" and intent.order_by is not None


def places_a_group(intent: ResolvedClipIntent) -> bool:
    """A first/last ask the resolver matched to several clips ("starting with the train ride").

    It seats the group and says nothing about any one clip in it, so a skip naming one of
    them is the narrower ask. A first/last ask matched to ONE clip ("open on the tray
    clip", KRI-761) is the creator pinning that clip.
    """
    return (
        intent.op == "order"
        and intent.position is not None
        and len({a.media_id for a in intent.assignments}) > 1
    )


def apply_clip_exclusions(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    resolved_intents: Iterable[ResolvedClipIntent] | None,
) -> CreativeStrategy:
    """``strategy`` without the clips the creator asked to leave out (KRI-667), or unchanged.

    The same narrowing as ``apply_clip_scope``: an "all" scope or an empty pick becomes
    an explicit ``selected`` list of the manifest media minus the excluded clips, and an
    excluded hero is cleared. Unchanged when a guided voiceover contract needs its
    all-media scope, when no excluded clip is in the pick, when the exclusion would
    leave no own clip (or nothing at all), or when a native edit's explicit list would
    pass ``MAX_MAIN_CREATOR_SELECTED_MEDIA``: a missed exclusion keeps today's behaviour,
    a wrong one loses footage the creator wanted.
    """

    if strategy.execution_contract is not None:
        return strategy
    dropped = excluded_clips(resolved_intents, strategy)
    if not dropped:
        return strategy
    takes_everything = strategy.media_scope == "all" or not strategy.selected_media_ids
    base = (
        [media.media_id for media in manifest.media]
        if takes_everything
        else list(dict.fromkeys(strategy.selected_media_ids))
    )
    kept = [media_id for media_id in base if media_id not in dropped]
    if len(kept) == len(base) or not kept:
        return strategy
    own = {media.media_id for media in _own_videos(manifest.media)}
    if own.intersection(base) and not own.intersection(kept):
        return strategy
    if strategy.render_program == "native" and len(kept) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        return strategy
    update: dict[str, object] = {"media_scope": "selected", "selected_media_ids": kept}
    if strategy.hero_media_id in dropped:
        update["hero_media_id"] = None
    return strategy.model_copy(update=update)


@dataclass(frozen=True)
class SubjectScope:
    """What a subject scope did to the pick (KRI-993), for the log line and the tests."""

    strategy: CreativeStrategy
    kept: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    added: tuple[str, ...] = ()


def scoped_clips(resolved_intents: Iterable[ResolvedClipIntent] | None) -> set[str]:
    """Media ids the resolved ``exclusive`` includes confidently match (KRI-993).

    Only a RESOLVED intent counts (an unmatched or asked one scopes nothing: the creator is
    asked which clips show it), and only an assignment at the resolver's membership bar, so
    a below-bar guess never keeps or adds a clip.
    """

    return {
        a.media_id
        for i in resolved_intents or []
        if i.status == "resolved" and i.op == "include" and i.exclusive
        for a in i.assignments
        if a.confidence >= MEMBERSHIP_MIN_CONFIDENCE
    }


def apply_subject_scope(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    resolved_intents: Iterable[ResolvedClipIntent] | None,
) -> SubjectScope:
    """The pick limited to the clips "only about X" matched (KRI-993), or unchanged.

    Prod thread 4C536AFE: "10-second montage, only about the steak ..." hooked on the
    provoleta boards because an `include` only guarantees its clips are in the edit. Now an
    `exclusive` include limits the VIDEO pick to the clips it matched, plus:

    - kept: any clip another resolved ask names (an order pin, a group, a label ...) or the
      strategy itself places (``placed_media``), and every non-video item (photos are not
      takes of the subject; whether they stay is the creator's own ask);
    - added: a matched video clip the model left out, unless the creator excluded it;
    - never: a clip the creator excluded (a skip beats a scope match).

    Unchanged when nothing matched confidently (the creator is asked, nothing is dropped),
    when a guided voiceover contract needs its all-media scope, when the result would hold
    no own video, when nothing would change, or when a native edit's explicit list would
    pass ``MAX_MAIN_CREATOR_SELECTED_MEDIA``.
    """

    unchanged = SubjectScope(strategy)
    if strategy.execution_contract is not None:
        return unchanged
    intents = [i for i in resolved_intents or [] if i.status == "resolved"]
    own = [m.media_id for m in _own_videos(manifest.media)]
    matched = scoped_clips(intents) & set(own)
    if not matched:
        return unchanged
    skipped = excluded_clips(intents, strategy)
    # KRI-1026: a filming-time order names every clip, so it keeps none outside the scope.
    named = {
        a.media_id
        for i in intents
        if i.op != "exclude" and not i.exclusive and not _orders_every_clip(i)
        for a in i.assignments
    }
    protected = named | placed_media(strategy)
    takes_everything = strategy.media_scope == "all" or not strategy.selected_media_ids
    base = (
        [media.media_id for media in manifest.media]
        if takes_everything
        else list(dict.fromkeys(strategy.selected_media_ids))
    )
    own_set = set(own)
    kept = [
        media_id
        for media_id in base
        if media_id not in skipped
        and (media_id not in own_set or media_id in matched or media_id in protected)
    ]
    added = [m for m in own if m in matched and m not in base and m not in skipped]
    result = [*kept, *added]
    if not own_set.intersection(result):
        return unchanged
    if not takes_everything and result == base:
        return unchanged
    if takes_everything and set(result) == set(base):
        return unchanged
    if strategy.render_program == "native" and len(result) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        return unchanged
    update: dict[str, object] = {"media_scope": "selected", "selected_media_ids": result}
    if strategy.hero_media_id and strategy.hero_media_id not in result:
        update["hero_media_id"] = None
    return SubjectScope(
        strategy.model_copy(update=update),
        kept=tuple(kept),
        dropped=tuple(m for m in base if m not in result),
        added=tuple(added),
    )


__all__ = [
    "SubjectScope",
    "apply_subject_scope",
    "scoped_clips",
    "LengthAsk",
    "apply_clip_exclusions",
    "apply_clip_scope",
    "excluded_clips",
    "length_asks",
    "length_groups",
    "placed_media",
    "places_a_group",
    "ruled_out_clips",
]
