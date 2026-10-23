"""Explicit stationary engine-noise reduction; never a whole-track volume request.

The immutable brief owns consent. The model that reads the creator's message records an
engine/motor noise-reduction ask as ``facts.noise_reduction == "engine"`` on an `audio`
requirement (Main Creator v76 / Brief Extractor v21); this module only checks that field
(KRI-667), so replacing the soundtrack, muting all sound or keeping the noise never
authorizes filtering speech. Filters reduce noise; they cannot promise complete separation
of overlapping voices and engines.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.kria.brief import BriefRequirement


def wants_engine_noise_reduction(req: BriefRequirement) -> bool:
    return req.live and req.kind == "audio" and req.facts.get("noise_reduction") == "engine"


def engine_noise_requirement_ids(requirements: Iterable[BriefRequirement]) -> tuple[str, ...]:
    return tuple(req.id for req in requirements if wants_engine_noise_reduction(req))
