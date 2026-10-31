"""Leverkusen M2: Kria looks inside the clips before it calls a shot the creator named missing.

Leverkusen M2 run 1 (thread d048973b, 2026-10-30): the creator pinned "the "TOR" on the big
screen" as the third beat. It is 1.5 s inside an 18 s goal clip, and the clip records the Main
Creator reads (two moments per clip) never mention it. On the same input the Main Creator once
proposed a strategy (the clip-intent planner, the resolver and vision then looked for the shot)
and once answered ``AskUser(reason_code="missing_media")``: "I couldn't find the shot ... Build
without it / I'll upload it", for a file the creator had already given. Nothing had looked
inside a clip.

So the planner never shows such an ask on an item with clips before the server has looked
(``planner._look_inside_clips_first``, the same clip-intent planner, resolver and vision checks
a strategy turn runs), and this module decides what to say from what the looking found:

* ``asks_for_missing_media``: the Main Creator's ask says footage is missing, read from its
  machine ``reason_code`` (``missing_media``), never from its prose or options (KRI-667). An
  ask about a missing voiceover or song is not one: no clip can hold it.
* ``after_looking``: every shot the creator named was found (``FOUND``); the creator already
  said one is in none of their clips (``CHECKED``: every clip was checked, so the ask stands,
  "upload it" included); or the looking placed no shot (``UNPLACED``).
* ``found_question`` / ``unplaced_reply``: the server's own words for ``FOUND`` and
  ``UNPLACED``. Neither offers an upload.
* ``answers_found_card``: the creator is answering the ``FOUND`` card, so it is never asked
  twice in a row.

Nothing here reads the creator's words or the model's prose: the ask is the reason code's
tokens, the shots the clip-intent planner's, the matches the resolver's and vision's, the
"none" the creator's own tap.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.kria.reply_language import say
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.choice_questions import EVENT_TYPE_KEY
from app.services.clip_selection import category_label

# The ``AskUser`` reason code of the server's own "found every shot" card; its choice question
# carries ``ask_user:<this>`` like every other ask card.
FOUND_REASON = "clips_checked_found"
FOUND_CONFLICT = f"ask_user:{FOUND_REASON}"

FOUND: Literal["found"] = "found"
CHECKED: Literal["checked"] = "checked"
UNPLACED: Literal["unplaced"] = "unplaced"

# Tokens of a machine reason code ("missing_media", "shot_not_found", "no_matching_clip"). The
# code is the AskUser's typed field (snake_case, English whatever the chat's language); its
# tokens are matched whole, never searched for inside prose.
_FOOTAGE = frozenset(
    {
        "asset",
        "assets",
        "broll",
        "clip",
        "clips",
        "file",
        "files",
        "footage",
        "image",
        "images",
        "media",
        "moment",
        "moments",
        "photo",
        "photos",
        "picture",
        "pictures",
        "scene",
        "scenes",
        "shot",
        "shots",
        "take",
        "takes",
        "video",
        "videos",
        "visual",
        "visuals",
    }
)
_MISSING = frozenset(
    {"absent", "lacking", "missing", "nonexistent", "unavailable", "unfound", "unmatched"}
)
_UPLOAD = frozenset({"reupload", "upload", "uploads"})
_NEGATION = frozenset({"cannot", "cant", "no", "none", "not"})
_FOUND_WORDS = frozenset(
    {"attached", "available", "found", "located", "match", "matched", "matching", "present"}
)
# Sound the creator records or picks, which no clip can hold.
_SOUND = frozenset(
    {
        "audio",
        "lyrics",
        "music",
        "narration",
        "recording",
        "song",
        "songs",
        "sound",
        "speech",
        "track",
        "voice",
        "voiceover",
        "vo",
    }
)


class _Clip(Protocol):
    media_id: str
    kind: str
    file_name: str | None


def asks_for_missing_media(reason_code: str) -> bool:
    """True when the Main Creator's ``AskUser`` reason code says named footage is missing.

    "missing_media", "missing_shot", "shot_not_found", "no_matching_clip", "upload_needed".
    A code about something else that is missing ("missing_duration", "missing_title") is not
    one, and neither is one about sound ("missing_voiceover", "song_upload"): no clip holds
    those.
    """
    tokens = set(reason_code.casefold().replace("-", "_").split("_"))
    if tokens & _SOUND:
        return False
    missing = bool(tokens & _MISSING) or bool(tokens & _NEGATION and tokens & _FOUND_WORDS)
    return bool((missing and tokens & _FOOTAGE) or tokens & _UPLOAD)


@dataclass(frozen=True)
class Looked:
    outcome: Literal["found", "checked", "unplaced"]
    # FOUND: the shots the creator named, each with the clips it was found in.
    shots: tuple[ResolvedClipIntent, ...] = ()


# The asks that name a shot to find ("open on our selfie", "show the TOR", "the beach clips").
_SHOT_OPS = frozenset({"order", "include", "group"})


def after_looking(intents: Iterable[ResolvedClipIntent], rejected: Sequence[Any]) -> Looked:
    """What the clip checks of a resolved turn say about a "missing" ask.

    ``rejected`` are the asks the creator answered "None" in the clip picker: they looked at
    every clip for that shot, so an ask to upload it is honest. Otherwise every shot named
    (a described pin, include or group; a fact order like "in filming order" names none) found
    in a clip is ``FOUND``; anything else (no shot read from the words, or one placed nowhere)
    is ``UNPLACED``.
    """
    if rejected:
        return Looked(CHECKED)
    shots = [i for i in intents if i.op in _SHOT_OPS and i.order_by is None]
    if shots and all(i.status == "resolved" and i.assignments for i in shots):
        return Looked(FOUND, tuple(shots))
    return Looked(UNPLACED)


def clip_names(clips: Sequence[_Clip]) -> dict[str, str]:
    """How the reply names each clip: the file name the app shows, else "clip N" / "photo N"."""
    names: dict[str, str] = {}
    videos = photos = 0
    for clip in clips:
        if clip.kind == "image":
            photos += 1
            fallback = say(en=f"photo {photos}", tr=f"fotoğraf {photos}")
        else:
            videos += 1
            fallback = say(en=f"clip {videos}", tr=f"klip {videos}")
        names[clip.media_id] = clip.file_name or fallback
    return names


def _listed(items: Sequence[str], *, limit: int, sep: str = ", ") -> str:
    shown = sep.join(items[:limit])
    if len(items) > limit:
        more = len(items) - limit
        shown += say(en=f" and {more} more", tr=f" ve {more} tane daha")
    return shown


def found_question(
    shots: Sequence[ResolvedClipIntent], clips: Sequence[_Clip]
) -> tuple[str, tuple[str, str]]:
    """The ``FOUND`` card: where each named shot was found, and whether to build with them."""
    names = clip_names(clips)
    seen: set[str] = set()
    parts: list[str] = []
    for shot in shots:
        label = category_label(shot)
        if label.casefold() in seen:
            continue
        seen.add(label.casefold())
        where = ", ".join(names[m] for m in dict.fromkeys(shot.media_ids()) if m in names)
        parts.append(f"“{label}” ({where})")
    found = _listed(parts, limit=4, sep="; ")
    question = say(
        en=(
            f"I looked inside your clips and found every shot you named: {found}. "
            "Should I build the edit with them?"
        ),
        tr=(
            f"Kliplerinin içine baktım ve istediğin her çekimi buldum: {found}. "
            "Videoyu bunlarla oluşturayım mı?"
        ),
    )
    options = (
        say(en="Yes, build it", tr="Evet, oluştur"),
        say(en="No, something's wrong", tr="Hayır, bir şey yanlış"),
    )
    return question, options


def unplaced_reply(clips: Sequence[_Clip]) -> str:
    """The ``UNPLACED`` reply: no "upload it", the clips named, the creator asked to point."""
    names = _listed(list(clip_names(clips).values()), limit=6)
    return say(
        en=(
            "I couldn't place that shot from my notes on your clips, so I haven't ruled them "
            f"out. If it's in one of them ({names}), tell me which one and about when it "
            "happens. Or tell me to build the edit without it."
        ),
        tr=(
            "O çekimi kliplerin hakkındaki notlarımdan yerleştiremedim, bu yüzden kliplerini "
            f"elemedim. Birindeyse ({names}) hangisi olduğunu ve yaklaşık ne zaman geçtiğini "
            "söyle. Ya da videoyu onsuz oluşturmamı söyle."
        ),
    )


def answers_found_card(events: Sequence[tuple[str, Any]]) -> bool:
    """True when the reply just before the creator's latest event was the ``FOUND`` card.

    The card asks once; a creator who answered it and still gets a "missing" ask has had every
    clip checked, so that ask stands (no "found it" card on a loop).
    """
    latest = next((i for i in range(len(events) - 1, -1, -1) if events[i][0] == "user"), None)
    if latest is None:
        return False
    for role, payload in reversed(events[:latest]):
        if (
            role == "assistant"
            and isinstance(payload, dict)
            and ("turn_value" in payload or payload.get(EVENT_TYPE_KEY) == "assistant_response")
        ):
            card = payload.get("choice_question")
            return isinstance(card, dict) and card.get("conflict") == FOUND_CONFLICT
    return False


__all__ = [
    "CHECKED",
    "FOUND",
    "FOUND_CONFLICT",
    "FOUND_REASON",
    "UNPLACED",
    "Looked",
    "after_looking",
    "answers_found_card",
    "asks_for_missing_media",
    "clip_names",
    "found_question",
    "unplaced_reply",
]
