"""A refusal the Main Creator writes as an ``ask_user`` that the server's own gates contradict.

Prod thread 6fa36e51, turn 7aa16b1a (2026-11-02): with the Talking cutaway flags on, the Main
Creator answered ``AskUser(reason_code="unsupported_audio_intercut")`` ("I still can't intercut
those videos with their own audio ... silent full-screen videos, or switch to a Montage?") for
an ask the render worker builds. ``AskUser.reason_code`` is a free string the model invents, so
nothing checked the claim.

The reason code is the model's typed field, read by whole tokens, never the question prose or
the option labels (KRI-667, same as `app.kria.missing_media.asks_for_missing_media`). A code that
says "unsupported" / "cannot" / "unavailable" while the server's gates say the live brief's ask is
available is not trusted: the planner re-runs the Main Creator once with ``correction_note``. Any
other question (a clarification, a hook candidate, a clip picker) is untouched, so unknown codes
stay legitimate; only a claim the gates can disprove is checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents._schemas.creator_agent import AskUser
from app.kria.clip_sound_toggle_ask import SOUND_ON, brief_sound_toggle_asks
from app.kria.cutaway_show_ask import brief_full_screen_show_ask
from app.services.creator_capabilities import talking_footage_cutaways

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
    }
)
_NEGATION = frozenset({"not", "no", "isnt", "unable"})
_ABLE = frozenset({"supported", "possible", "available", "allowed", "able", "doable"})


def is_refusal_code(reason_code: str) -> bool:
    """True when ``reason_code`` says the ask cannot be done ("unsupported_audio_intercut",
    "not_supported", "cannot_intercut", "feature_unavailable")."""
    tokens = set(reason_code.casefold().replace("-", "_").split("_")) - {""}
    return bool(tokens & _REFUSAL) or bool(tokens & _NEGATION and tokens & _ABLE)


def stale_capability_refusal(
    action: object, manifest: ResolvedCreatorManifest, brief: CreativeBrief | None
) -> str | None:
    """The capability an ``ask_user`` refusal contradicts, else None.

    ``CAPABILITY_MENTION`` / ``CAPABILITY_SOUND`` when the action is a refusal-shaped
    ``AskUser``, this multi-clip Talking edit's gates are open for it
    (`talking_footage_cutaways`, the gates the worker reads), and the live brief carries the
    typed ask the lane builds (a full-screen display ask, or a clip-sound "on" ask).
    """
    if brief is None or not isinstance(action, AskUser) or not is_refusal_code(action.reason_code):
        return None
    found = talking_footage_cutaways(manifest)
    if found is None:
        return None
    if found.sound and any(ask.state == SOUND_ON for ask in brief_sound_toggle_asks(brief)):
        return CAPABILITY_SOUND
    if found.mention and brief_full_screen_show_ask(brief) is not None:
        return CAPABILITY_MENTION
    return None


def correction_note(capability: str) -> str:
    """The server-authored note for the one corrective re-run (model-facing, never shown)."""
    what = (
        "show the creator's other clips full screen over the talk and play a clip's own sound "
        "with the creator's voice muted under it"
        if capability == CAPABILITY_SOUND
        else "show the creator's other clips full screen over the talk where they are mentioned"
    )
    return (
        "Your last answer declined part of the request with `ask_user` as unsupported. The "
        f"server's own gates say this edit CAN {what} (the CLIPS SHOWN FULL SCREEN OVER THE "
        "TALK rules above). Do not decline that and do not offer a Montage or a silent "
        "version in its place: propose the strategy and say in `summary` that the creator's "
        "voice is muted under a clip that plays its own sound. If what you declined was "
        "something else, ask about exactly that part and give a reason_code that names it."
    )


__all__ = [
    "CAPABILITY_MENTION",
    "CAPABILITY_SOUND",
    "correction_note",
    "is_refusal_code",
    "stale_capability_refusal",
]
