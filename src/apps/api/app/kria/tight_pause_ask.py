"""Qendresa 13: does the creator ask for every pause to go ("cut out all my pauses so it's tight")?

Speech cleanup cuts only long pauses. A tight ask is a cleanup ask whose words (or a recorded
fact) say every / all pauses, or a tight pace. Callers check ``TIGHT_PAUSE_CUT_ENABLED``; nothing
here reads the flag.

A recorded ``facts["pauses"] == "tight"`` decides (KRI-667); the words are the fallback.
"""

from __future__ import annotations

import re

from app.kria.brief import BriefRequirement, CreativeBrief

# Text arrives case/accent-folded by brief_checks._fold (Turkish dotless i -> "i"; ç, ş, ü stay).
# Turkish pause nouns only: "her" is also an English pronoun ("cut out her long pauses").
_TR_PAUSE_WORD = r"(duraklama\w*|duraksama\w*|bo[sş]lu\w*|sessizlik\w*|es(ler\w*|i|leri)?)"
_TIGHT_RE = re.compile(
    r"\b(tight|tighter|tightly|snappy|snappier|fast[- ]?paced?|quick[- ]?paced?)\b"
    r"|\b(no|zero|without any|without|not a single) (more )?(pauses?|gaps?|silences?|dead air)\b"
    r"|\bdead air\b"
    r"|\b(all|every|each)( of)?( (my|the|these|those|your))? (little |tiny |small |short |single )?"
    r"(pauses?|gaps?|silences?)\b"
    r"|\bevery single (pause|gap|silence)\b"
    r"|\bbetween every (sentence|word|line|phrase)\b"
    r"|\b(t[uü]m|b[uü]t[uü]n|her)\b( \w+)? " + _TR_PAUSE_WORD + r"\b"
    r"|\bhi[cç] bo[sş]luk|\bbo[sş]luksuz\b|\bs[iı]k[iı]\b|\bs[iı]k[iı] olsun"
    r"|\bseri olsun\b|\bh[iı]zl[iı] tempo\b"
)
# "tight" that is about framing or a shot, not the pace of the speech.
_TIGHT_FRAMING_RE = re.compile(
    r"\b(tight|tighter|tightly)"
    r"( (shot|shots|crop|framing|frame|close ?up|angle|on (my|the|his|her)\b))"
    r"|\b(keep|stay|zoom)\b.{0,20}\btight on\b"
)


def wants_speech_cleanup(req: BriefRequirement) -> bool:
    """A live requirement that asks speech cleanup (the one ``_check_speech_cleanup`` judges)."""
    from app.kria import brief_checks  # noqa: PLC0415 (brief_checks imports this package)

    return bool(req.live and brief_checks._wants_cleanup(req))


def wants_tight_pauses(req: BriefRequirement) -> bool:
    """A cleanup ask that wants every pause gone / a tight pace, not just the long ones."""
    from app.kria import brief_checks  # noqa: PLC0415

    if not wants_speech_cleanup(req):
        return False
    recorded = req.facts.get("pauses")
    if isinstance(recorded, str):
        folded = recorded.strip().casefold()
        if folded == "tight":
            return True
        if folded in {"long", "natural", "keep"}:
            return False
    text = brief_checks._req_text(req)
    if _TIGHT_FRAMING_RE.search(text):
        text = _TIGHT_FRAMING_RE.sub(" ", text)
    return bool(_TIGHT_RE.search(text))


def brief_wants_speech_cleanup(brief: CreativeBrief | None) -> bool:
    return bool(brief) and any(wants_speech_cleanup(req) for req in brief.requirements)


def brief_tight_pauses(brief: CreativeBrief | None) -> bool:
    return bool(brief) and any(wants_tight_pauses(req) for req in brief.requirements)
