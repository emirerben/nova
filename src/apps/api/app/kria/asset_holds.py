"""Creator authority for one photo's on-screen time (KRI-824 slice 1, KRI-825).

"Keep the beach photo on screen for 1 second" is one decision about one asset. The brief
extractor (a model) records it as a ``timing`` requirement scoped ``clip:<media_id>`` with
``facts.duration_s``. The model owns understanding (which photo, what the number is for;
the KRI-667 language policy), but its numbers are not creator intent: the server stamps
``BriefRequirement.authority = "explicit"`` only when the creator's own stored turn typed
those seconds as a length (``stamp_authority``: a number, digits or EN/TR words, followed
by a seconds unit, so English and Turkish read alike). A first ask with no stated length
("keep the beach photo short") stays unstamped and the photo keeps the AI pacing; a later
correction of a hold the creator already owns keeps it creator-owned. Everything
downstream reads
``asset_holds``, which returns only stamped holds on the item's own photos; an unstamped,
unresolvable or out-of-range hold is never applied (the receipt reports it unmet), and an
AI cadence (``MixedMediaTimingProfile``) never overrides a stamped one.

"Default" is not stored: no stamped requirement for a photo means the AI pacing decides.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

Authority = Literal["explicit", "accepted", "cleared"]

# Seconds a creator may give one photo. The AI cadence band (0.1-0.8 s,
# ``MixedMediaTimingProfile.image_hold_s``) is a taste default and does not bound this.
MIN_HOLD_S = 0.1
MAX_HOLD_S = 10.0
# Facts a model may file the seconds under; the first positive one is the hold.
HOLD_FACT_KEYS = ("hold_s", "duration_s", "clip_duration_s", "per_clip_duration_s")
# The renderer's frame clock: an applied hold is judged within one frame of the ask.
HOLD_FPS = 30
HOLD_TOLERANCE_S = 1.0 / HOLD_FPS + 1e-3


def stated_seconds(text: str) -> tuple[float, ...]:
    """Every length the creator wrote as seconds ("1 second", "1,5 sn", "iki saniye").

    The planner's own seconds reader (``_stated_holds``: a number, in digits or EN/TR
    words, followed by a seconds unit), so a count ("3 photos"), a caption number or "1000
    followers" is never a length. What the length is *for* stays the brief extractor's
    call (KRI-667), recorded in ``facts``; this only confirms the creator said it.
    """
    from app.kria.planner import _hold_key, _stated_holds  # noqa: PLC0415 - import cycle

    return tuple(dict.fromkeys(value for _, _, value in _stated_holds(_hold_key(text or ""))))


def _creator_typed(seconds: float, creator_text: str) -> bool:
    """The creator said these seconds as a length (a number and a seconds unit)."""
    return any(abs(number - seconds) <= 1e-6 for number in stated_seconds(creator_text))


def hold_seconds(req: Any) -> float | None:
    """The seconds a ``timing`` / ``clip:<id>`` requirement asks one asset to stay.

    Not a freeze or a speed change on that clip (their seconds are an effect's). Whether
    the asset is a photo is the reader's to resolve: only the item's own stills ever hold.
    """
    if getattr(req, "kind", None) != "timing" or not str(
        getattr(req, "scope", "") or ""
    ).startswith("clip:"):
        return None
    from app.kria.freeze_ask import asks_freeze_frame  # noqa: PLC0415 - import cycle
    from app.kria.speed_ask import asks_clip_speed  # noqa: PLC0415

    if asks_freeze_frame(req) or asks_clip_speed(req):
        return None
    facts = getattr(req, "facts", None) or {}
    for key in HOLD_FACT_KEYS:
        value = facts.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and value > 0:
            return value if MIN_HOLD_S - 1e-9 <= value <= MAX_HOLD_S + 1e-9 else None
    return None


def stamp_authority(
    req: Any, creator_text: str | None, prior: Any | None = None
) -> Authority | None:
    """The server's verdict on who chose ``req``'s hold. Never read from model output.

    - ``explicit`` when the creator's own turn said those seconds as a length (a number and
      a seconds unit; a bare number such as "3 photos" or "1000 followers" never does).
    - A change to a hold the creator already owns, on the same asset, keeps that ownership
      when it keeps the value, or when it comes from the creator's own turn ("make it two
      seconds", "a bit longer"): the creator re-decided their own decision and the
      extractor read the amount. It never falls back to the AI default.
    - Anything else is ``None``: a model's number on a decision nobody owns is a proposal.
    """
    value = hold_seconds(req)
    if value is None:
        return None
    if creator_text and _creator_typed(value, creator_text):
        return "explicit"
    if prior is None:
        return None
    prior_value = hold_seconds(prior)
    prior_authority = getattr(prior, "authority", None)
    if prior_authority not in {"explicit", "accepted"} or prior_value is None:
        return None
    if getattr(prior, "scope", None) != getattr(req, "scope", None):
        return None
    if abs(prior_value - value) <= 1e-6 or creator_text:
        return prior_authority
    return None


def resolve_clip_scope(token: str, media_ids: Iterable[str]) -> str | None:
    """The medium a ``clip:<token>`` scope names: the whole id, or a unique 6+ char tail.

    A tail two media share names neither: the scope is ambiguous and resolves to nothing.
    """
    ids = list(dict.fromkeys(str(media_id) for media_id in media_ids))
    if token in ids:
        return token
    if len(token) < 6:
        return None
    tails = [media_id for media_id in ids if media_id.endswith(token)]
    return tails[0] if len(tails) == 1 else None


@dataclass(frozen=True)
class AssetHold:
    """One photo's creator-owned on-screen time."""

    media_id: str
    hold_s: float
    requirement_id: str
    authority: Authority

    @property
    def frames(self) -> int:
        return max(1, int(round(self.hold_s * HOLD_FPS)))

    def record(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "hold_s": self.hold_s,
            "requirement_id": self.requirement_id,
            "authority": self.authority,
        }


def asset_holds(brief: Any, photo_ids: Iterable[str]) -> dict[str, AssetHold]:
    """Each of ``photo_ids`` (the item's own stills) a live, creator-owned hold names.

    Read from the live brief only, so a superseded or removed hold never comes back from an
    older turn. The latest live requirement for a photo wins; a ``cleared`` one leaves the
    photo to the AI default.
    """
    if brief is None:
        return {}
    return holds_from_requirements(brief.live(), photo_ids)


def hold_requirements(brief: Any) -> tuple[Any, ...]:
    """The live requirements that name one asset's seconds, in brief order."""
    if brief is None:
        return ()
    return tuple(req for req in brief.live() if hold_seconds(req) is not None)


def holds_from_requirements(
    requirements: Iterable[Any], photo_ids: Iterable[str]
) -> dict[str, AssetHold]:
    """``asset_holds`` over already-live requirements (see there)."""
    ids = list(dict.fromkeys(str(media_id) for media_id in photo_ids))
    holds: dict[str, AssetHold] = {}
    for req in requirements:
        value = hold_seconds(req)
        if value is None:
            continue
        media_id = resolve_clip_scope(req.scope.split(":", 1)[1], ids)
        if media_id is None:
            continue
        authority = getattr(req, "authority", None)
        if authority == "cleared":
            holds.pop(media_id, None)
        elif authority in {"explicit", "accepted"}:
            holds[media_id] = AssetHold(media_id, round(value, 3), req.id, authority)
    return holds


def held_visual_seconds(
    brief: Any, clip_ids: Iterable[str], visual_photo_ids: Iterable[str] | None
) -> float:
    """Seconds the creator-held Visual photos (pool stills outside ``clip_ids``) will take.

    The approved length plan sizes only the clip record's footage; a held Visual plays its
    own length beside it, so a creator total must leave that much room. Only a hold that
    ``holds_from_requirements`` would apply counts: resolved (never an ambiguous tail)
    against every medium of the item, to a still ``visual_photo_ids`` lists (the Visuals
    this format places), once per photo. ``None`` (the format places no Visuals, or the
    pool is unknown) subtracts nothing.
    """
    if brief is None or not visual_photo_ids:
        return 0.0
    clips = list(dict.fromkeys(str(media_id) for media_id in clip_ids))
    visuals = [m for m in dict.fromkeys(str(v) for v in visual_photo_ids) if m not in clips]
    holds = holds_from_requirements(hold_requirements(brief), [*clips, *visuals])
    total = sum(hold.frames for media_id, hold in holds.items() if media_id in visuals)
    return round(total / HOLD_FPS, 3)


def hold_outcomes(
    holds: Mapping[str, AssetHold], rendered_s: Mapping[str, float]
) -> list[dict[str, Any]]:
    """One receipt row per applied hold: what was asked and what the plan rendered."""
    rows = []
    for media_id, hold in holds.items():
        rendered = rendered_s.get(media_id)
        rows.append(
            {
                **hold.record(),
                "rendered_s": None if rendered is None else round(float(rendered), 4),
            }
        )
    return rows


__all__ = [
    "AssetHold",
    "Authority",
    "HOLD_TOLERANCE_S",
    "MAX_HOLD_S",
    "MIN_HOLD_S",
    "asset_holds",
    "hold_outcomes",
    "held_visual_seconds",
    "hold_requirements",
    "hold_seconds",
    "holds_from_requirements",
    "resolve_clip_scope",
    "stamp_authority",
    "stated_seconds",
]
