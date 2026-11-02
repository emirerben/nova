"""A refusal the Main Creator writes that the server's own gates contradict.

Prod thread 6fa36e51, turn 7aa16b1a (2026-11-02): with the Talking cutaway flags on, the Main
Creator answered ``AskUser(reason_code="unsupported_audio_intercut")`` ("I still can't intercut
those videos with their own audio ... silent full-screen videos, or switch to a Montage?") for
an ask the render worker builds. ``AskUser.reason_code`` is a free string the model invents, so
nothing checked the claim.

Everything is read as whole tokens, never with a pattern and never as sentences (KRI-667, same as
`app.kria.missing_media.asks_for_missing_media`): the model's own reason code, its question and
its option labels are split into lowercase words, and sets of words are compared. A claim is only
checked when ALL hold:

- it is a refusal: a code that says "unsupported" / "cannot" / "not available" / "requires
  montage", or a proposal that moves the Talking edit to a Montage;
- it is about what the gates can disprove: the code, question or options are about sound, audio,
  intercutting, cutaways or a Montage;
- it is not a refusal OF an insert or a pause (the talk stopping while a clip plays). That is
  the other lane (`insert_clip_candidates`, its own gate): a reason code that names it is never
  contradicted, open or closed. A question that merely names a pause is the model's honest "no
  pause" answer while that lane is closed; while it is open, a refusal whose own code names the
  audio / cutaway ask is stale even when its question offers a pause;
- this multi-clip Talking edit's gates are open for it (`talking_footage_cutaways`) and the live
  brief carries the typed ask the lane builds (a full-screen display ask, or a clip-sound "on" ask).

Then the planner re-runs the Main Creator once with ``correction_note``. Any other question (a
clarification, a hook candidate, a clip picker, footage that is too short) is untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents._schemas.creator_agent import AskUser, ProposeStrategy
from app.kria.clip_sound_toggle_ask import SOUND_ON, brief_sound_toggle_asks
from app.kria.cutaway_show_ask import brief_full_screen_show_ask
from app.kria.reply_language import say
from app.services.creator_capabilities import insert_clip_candidates, talking_footage_cutaways

if TYPE_CHECKING:
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest
    from app.kria.brief import CreativeBrief

CAPABILITY_MENTION = "mention"
CAPABILITY_SOUND = "sound"

_REFUSAL = frozenset(
    {
        "unsupported",
        "unavailable",
        "impossible",
        "cannot",
        "cant",
        "unable",
        "limitation",
        "limitations",
        "limited",
        "refused",
        "declined",
        "blocked",
        "gap",
    }
)
_NEGATION = frozenset({"not", "no", "isnt", "unable"})
_ABLE = frozenset({"supported", "possible", "available", "allowed", "able", "doable"})
# "requires_montage", "montage_only", "switch_to_montage": the claim is that another format is
# the only way.
_NEEDS_MONTAGE = frozenset({"requires", "required", "needs", "only", "switch"})
# What the gates can disprove: the audio / intercut / cutaway / Montage-switch asks.
_TOPIC = frozenset(
    {
        "audio",
        "sound",
        "sounds",
        "intercut",
        "intercuts",
        "intercutting",
        "cutaway",
        "cutaways",
        "montage",
        "silent",
        "ses",
        "sesi",
        "sesiyle",
        "sessiz",
        "montaj",
    }
)
# An insert or a pause (the talk stops, the clip plays, the talk continues) does not exist.
_PAUSE_OR_INSERT = frozenset(
    {"insert", "inserts", "pause", "pauses", "paused", "pausing", "resume", "resumes", "freeze"}
)
_APOSTROPHES = frozenset("'’`")


def tokens(text: str) -> frozenset[str]:
    """The lowercase words of ``text``. Splits on anything that is not a letter or digit and at
    a lower-to-upper seam (``unsupportedAudioIntercut``); apostrophes are dropped (``can't`` =>
    ``cant``)."""
    found: set[str] = set()
    current: list[str] = []
    previous = ""
    for char in text:
        if char in _APOSTROPHES:
            continue
        if not char.isalnum():
            if current:
                found.add("".join(current))
                current = []
            previous = ""
            continue
        if char.isupper() and previous.islower() and current:
            found.add("".join(current))
            current = []
        current.append(char.casefold())
        previous = char
    if current:
        found.add("".join(current))
    return frozenset(found)


def is_refusal_code(reason_code: str) -> bool:
    """True when ``reason_code`` says the ask cannot be done ("unsupported_audio_intercut",
    "not supported", "cannot_intercut", "requires_montage", "capability_gap")."""
    words = tokens(reason_code)
    return (
        bool(words & _REFUSAL)
        or bool(words & _NEGATION and words & _ABLE)
        or bool("montage" in words and words & _NEEDS_MONTAGE)
    )


def _ask_words(action: AskUser) -> frozenset[str]:
    return tokens(" ".join([action.reason_code, action.question, *action.options]))


def _refuses_an_insert(action: AskUser, *, inserts_open: bool) -> bool:
    """True when the refusal is about the insert / pause lane, not the audio / cutaway ask.

    The reason code is the model's own label: when it names an insert or a pause the refusal is
    that lane's, whatever else the question says. Otherwise a question or option that names a
    pause is read as a (correct) refusal of a pause while the insert lane is closed. While it is
    open, a pause is something this edit does, so a code that names the audio / cutaway ask
    keeps its refusal stale and the one corrective re-run teaches the difference.
    """
    code = tokens(action.reason_code)
    if code & _PAUSE_OR_INSERT:
        return True
    rest = tokens(" ".join([action.question, *action.options]))
    if not rest & _PAUSE_OR_INSERT:
        return False
    return not (inserts_open and code & _TOPIC)


def stale_capability_refusal(
    action: object, manifest: ResolvedCreatorManifest, brief: CreativeBrief | None
) -> str | None:
    """The capability a refusal-shaped answer contradicts, else None.

    ``CAPABILITY_MENTION`` / ``CAPABILITY_SOUND`` when the answer declines the audio / intercut /
    cutaway ask (an ``AskUser``) or moves the edit to a Montage (a ``ProposeStrategy``), this
    multi-clip Talking edit's gates are open for it, and the live brief carries the typed ask the
    lane builds. A refusal of an insert or a pause is never contradicted (see
    `_refuses_an_insert`).
    """
    if brief is None:
        return None
    if isinstance(action, AskUser):
        words = _ask_words(action)
        if not is_refusal_code(action.reason_code) or not words & _TOPIC:
            return None
        if _refuses_an_insert(action, inserts_open=bool(insert_clip_candidates(manifest))):
            return None
    elif isinstance(action, ProposeStrategy):
        if action.strategy.edit_format != "montage":
            return None
    else:
        return None
    found = talking_footage_cutaways(manifest)
    if found is None:
        return None
    if found.sound and any(ask.state == SOUND_ON for ask in brief_sound_toggle_asks(brief)):
        return CAPABILITY_SOUND
    if found.mention and brief_full_screen_show_ask(brief) is not None:
        return CAPABILITY_MENTION
    return None


def correction_note(capability: str, *, inserts_open: bool = False) -> str:
    """The server-authored note for the one corrective re-run (model-facing, never shown).
    `inserts_open` is the insert lane's own gate: it decides whether a pause ask is pointed at
    `insert_clips` or answered plainly as unavailable."""
    what = (
        "show the creator's other clips full screen over the talk and play a clip's own sound "
        "with the creator's voice muted under it"
        if capability == CAPABILITY_SOUND
        else "show the creator's other clips full screen over the talk where they are mentioned"
    )
    muted = (
        " and say in `summary` that the creator's voice is muted under a clip that plays its "
        "own sound"
        if capability == CAPABILITY_SOUND
        else ""
    )
    pause = (
        "If the creator explicitly asked for the talk to pause or stop while a clip plays and "
        "then continue, that is a different thing: use `insert_clips` for it as described "
        "above, and do not use it for a clip that is simply shown while the talk keeps running."
        if inserts_open
        else "If the creator really asked for the talk to pause while a clip plays and then "
        "continue, that is a different thing and it is not available: say so plainly and "
        "offer the full-screen overlay (and a video at the very end when that is offered "
        "above)."
    )
    return (
        "Your last answer said part of the request cannot be done, or moved the video to a "
        f"Montage to get it. The server checked and this edit CAN {what} (see the rules for "
        "clips shown full screen over the talk above). Earlier replies in this chat that said "
        "this does not work in this edit are out of date. Do not decline it and do not move it "
        f"to a Montage: propose the strategy for this edit{muted}. {pause} If what you declined "
        "was something else, ask about exactly that part and describe it in plain words."
    )


def unmet_reply() -> str:
    """What the creator reads when the corrective re-run still refuses the same ask: the plain
    truth, with no invented reason and no question the server knows is wrong."""
    return say(
        en=(
            "I couldn't set up showing your other clips full screen over your talk this time. "
            "Send the request again, or tell me which clip should cover which part, and I'll "
            "try once more."
        ),
        tr=(
            "Diğer kliplerini konuşmanın üzerinde tam ekran göstermeyi bu sefer hazırlayamadım. "
            "İsteği tekrar gönder ya da hangi klibin hangi kısmı kaplayacağını söyle, bir kez "
            "daha deneyeyim."
        ),
    )


__all__ = [
    "CAPABILITY_MENTION",
    "CAPABILITY_SOUND",
    "correction_note",
    "is_refusal_code",
    "stale_capability_refusal",
    "tokens",
    "unmet_reply",
]
