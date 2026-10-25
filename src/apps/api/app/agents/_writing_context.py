"""Delivery-only projections for public copy; account topics never become voice."""

import re

# Emit canonical qualities only, never the caller's surrounding free text.
_VOICE_QUALITIES = (
    (r"warm|sıcak|samimi|içten", "warm"),
    (r"calm|sakin", "calm"),
    (r"direct|sharp|net|doğrudan", "direct"),
    (r"conversational|casual|sohbet", "conversational"),
    (r"playful|mizahlı|eğlenceli", "playful"),
    (r"dry humou?r", "dry humour"),
    (r"energetic|high[- ]energy|enerjik", "energetic"),
    (r"intimate", "intimate"),
    (r"practical", "practical"),
    (r"confident", "confident"),
    (r"clear|sade", "clear"),
    (r"measured", "measured"),
    (r"understated|quiet", "understated"),
    (r"punchy", "punchy"),
)


def writing_voice(tone: str) -> str:
    """Keep recognizable delivery qualities; discard niches, slogans and commands."""
    normalized = tone.casefold().replace("̇", "")
    qualities = [
        quality
        for pattern, quality in _VOICE_QUALITIES
        if re.search(rf"(?<!\w)(?:{pattern})(?!\w)", normalized)
    ]
    return ", ".join(qualities[:5]) or "natural, conversational"


def turkish_audience_instruction(language: str) -> str:
    """Public audience copy only; never changes chat address or quoted source words."""
    if language != "tr":
        return ""
    return (
        "When addressing viewers in Turkish, use plural 'siz'/'size' and plural verb forms. "
        "Keep the voice warm and everyday, without stiff formal wording. Do not force "
        "second-person address into a natural first-person statement or short phrase. "
        "Preserve verbatim quoted speech and the creator's first-person wording."
    )
