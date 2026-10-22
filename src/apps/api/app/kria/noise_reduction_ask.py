"""Explicit stationary engine-noise reduction; never a whole-track volume request.

The immutable brief owns consent. A request to replace the soundtrack, mute all
sound, or keep noise does not authorize filtering speech. Filters reduce noise;
they cannot promise complete separation of overlapping voices and engines.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.kria.brief import BriefRequirement
from app.kria.brief_route import loose_text


def wants_engine_noise_reduction(req: BriefRequirement) -> bool:
    if not req.live or req.kind != "audio":
        return False
    text = loose_text(req.description or "")
    if re.search(
        r"\b(?:not|never|don't|dont|without|keep|preserve|replace|music|mute|"
        r"koru\w*|kalsin|degistir\w*|muzik\w*|kis(?:ma|mayin)|azaltma\w*)\b",
        text,
    ):
        return False
    if req.facts.get("noise_reduction") == "engine":
        return True
    return bool(
        re.search(r"\b(?:engine|motor)\b", text)
        and re.search(r"\b(?:noise|sound|ses\w*|gurultu\w*)\b", text)
        and re.search(
            r"\b(?:reduce|lower|quieten|suppress|remove|kis\w*|azalt\w*|bastir\w*)\b", text
        )
    )


def engine_noise_requirement_ids(requirements: Iterable[BriefRequirement]) -> tuple[str, ...]:
    return tuple(req.id for req in requirements if wants_engine_noise_reduction(req))
