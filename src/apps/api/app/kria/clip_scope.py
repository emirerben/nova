"""The clips a creator ruled out by length never play (KRI-623).

Prod thread 35ccc8ef (2026-10-12, cabin kit M2): two long takes (70 s, 67 s) and two
short clips (5 s, 7 s). "Pick the best six moments from the long takes, each very short,
and skip the dark ones." The Main Creator took every clip (the day before, the same
prompt got only the two takes), nothing on the server read "from the long takes", and the
moment selector gives every selected source at least one cut, so the render opened on the
two short clips.

``apply_clip_scope`` re-checks the model's pick against the creator's own words on every
plan: a live Creative Brief ``select`` ask and the current message. Only asks about clip
LENGTH are read, because length is the one thing the server can measure (the clip
analysis has no lighting signal, so "skip the dark ones" stays the model's call). The
reader is deliberately narrow: a wrong drop removes footage the creator wanted, a missed
ask keeps today's behaviour. So a length word counts only when it names a subset of the
attached clips ("the long takes", "uzun çekimlerden") in a selecting phrase ("from",
"only", "skip", "except", "kullanma"); a description ("two long takes of our cabin") or a
cut-length style ("fast cuts, short clips", "each very short") never does.

Long and short come from the clips' own durations: one clear gap (the longer side at
least twice the shorter, and no second gap like it) splits them. Clips of similar length,
or a clip of unknown length, leave the pick alone. Photos and Visuals are never dropped:
they are not takes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import MAX_MAIN_CREATOR_SELECTED_MEDIA
from app.kria.brief_route import loose_text

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


def _group(adjective: str, *, superlative: bool = False) -> Group:
    long = adjective.startswith(("long", "uzun"))
    if superlative or adjective.endswith("est"):
        return "longest" if long else "shortest"
    return "long" if long else "short"


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
            effect = _english_effect(clause, match.start(), match.end())
            if effect is not None:
                asks.append(LengthAsk(_group(match.group("adj")), effect))
        for match in _TR_PHRASE.finditer(clause):
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
    dropped = ruled_out_clips(creator_texts, manifest.media)
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


__all__ = ["LengthAsk", "apply_clip_scope", "length_asks", "length_groups", "ruled_out_clips"]
