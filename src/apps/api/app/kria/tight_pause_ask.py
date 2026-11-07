"""Qendresa 13: does the creator ask for every pause to go ("cut out all my pauses so it's tight")?

Speech cleanup cuts only long pauses. A tight ask is a cleanup ask whose recorded fact says
``facts["pauses"] == "tight"``. The model that reads the message records it (KRI-667: no word
recognisers here, in any language). Callers check ``TIGHT_PAUSE_CUT_ENABLED``; nothing here
reads the flag.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief


def wants_speech_cleanup(req: BriefRequirement) -> bool:
    """A live requirement the model recorded as ``speech_cleanup: true`` and that
    ``_check_speech_cleanup`` judges. Words alone never decide (KRI-667): "no need to cut the
    pauses" is no cleanup ask."""
    from app.kria import brief_checks  # noqa: PLC0415 (brief_checks imports this package)

    return bool(
        req.live
        and brief_checks._fact_flag(req, "speech_cleanup") is True
        and brief_checks._wants_cleanup(req)
    )


def wants_tight_pauses(req: BriefRequirement) -> bool:
    """A cleanup ask recorded as ``pauses: "tight"`` (every pause, not just the long ones)."""
    recorded = req.facts.get("pauses")
    return (
        wants_speech_cleanup(req)
        and isinstance(recorded, str)
        and recorded.strip().casefold() == "tight"
    )


def brief_wants_speech_cleanup(brief: CreativeBrief | None) -> bool:
    return bool(brief) and any(wants_speech_cleanup(req) for req in brief.requirements)


def brief_tight_pauses(brief: CreativeBrief | None) -> bool:
    return bool(brief) and any(wants_tight_pauses(req) for req in brief.requirements)


def wants_original_speech(req: BriefRequirement) -> bool:
    """A live whole-video requirement the model recorded as ``keep_whole_take`` (any kind, no
    length of its own) or ``speech_cleanup: false``: the creator wants the speech left as filmed
    (KRI-971). A clip-scoped row says nothing about the other clips the one card covers."""
    from app.kria import brief_checks  # noqa: PLC0415 (brief_checks imports this package)

    return bool(
        req.live
        and req.scope == "global"
        and (
            brief_checks._wants_whole_take(req)
            or brief_checks._fact_flag(req, "speech_cleanup") is False
        )
    )


def brief_wants_original_speech(brief: CreativeBrief | None) -> bool:
    """Whether the approval card should lead with "Keep original speech". A cleanup ask in the
    same brief wins: the creator contradicting themselves is theirs to settle on the card."""
    return (
        bool(brief)
        and any(wants_original_speech(req) for req in brief.requirements)
        and not brief_wants_speech_cleanup(brief)
    )
