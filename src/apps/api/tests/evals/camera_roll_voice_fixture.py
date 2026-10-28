"""Cunda-style camera-roll fixture for the idea-voice eval (plain data plus one builder).

Twelve no-GPS candidates (eight photos, four clips) written the way the analysis pipeline
words them in `media_moments.still_interval` / `_whole_source_context`. No candidate names
the place: with no GPS and no event description, nothing in the evidence can. Only the
"event" variant adds the creator's event title and notes (KRI-1003).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.agents.camera_roll_stories import CameraRollStoriesInput
from app.agents.media_moment_relevance import MomentCandidate

# Mirrors CameraRollSurface.defaultQuery in the iOS client; what an event with no text sends.
DEFAULT_QUERY = "Suggest different content ideas from this outing"
# The best a creator can do today: smuggle the context into the one-shot query.
WORKAROUND_QUERY = "Cunda Island: show why people should visit, stone architecture, nature, sea"
EVENT_TITLE = "Cunda Island"
EVENT_ABOUT = (
    "Island on Turkey's Aegean coast. Show why people should visit: stone architecture, nature, sea"
)
PLACE = EVENT_TITLE

_VISIBLE = {
    "source_visibility": "Clear daylight; subject well lit",
    "overlapping_unusable_spans": [],
    "verification": "cached_evidence_only",
}

# (media, seconds, analysis prose). Photos use Subject/Scene/Setting/Activity, clips the cached
# overall description, exactly the two shapes the collection pipeline emits.
_MOMENTS: list[tuple[str, float, str]] = [
    (
        "still",
        3,
        "Subject: A narrow cobbled street between old stone houses. "
        "Scene: Pale stone walls, wooden shutters and pink bougainvillea over a doorway. "
        "Setting: Quiet village street in daylight.",
    ),
    (
        "video",
        9,
        "Cached overall description: Handheld walk down a narrow stone alley between old "
        "houses, sunlight falling in patches on the cobbles, a few potted plants by the doors.",
    ),
    (
        "still",
        3,
        "Subject: A stone windmill on a low hill. "
        "Scene: Round weathered tower with a wooden cap, dry grass in front, clear blue sky. "
        "Setting: Open hilltop.",
    ),
    (
        "still",
        3,
        "Subject: A row of old stone houses with wooden balconies. "
        "Scene: Two-storey buildings with peeling shutters and ivy on one wall. "
        "Setting: Small square in the afternoon.",
    ),
    (
        "video",
        7,
        "Cached overall description: Waves rolling onto a rocky shore, the camera pans slowly "
        "across clear green water to small boats far out on the horizon.",
    ),
    (
        "still",
        3,
        "Subject: The sea at sunset. Scene: Orange light on flat water, a dark rock in the "
        "foreground and a thin line of cloud. Setting: Shoreline in the evening.",
    ),
    (
        "still",
        3,
        "Subject: An olive grove. Scene: Gnarled old olive trunks, silver-green leaves and dry "
        "grass underneath. Setting: Hillside in warm afternoon light.",
    ),
    (
        "video",
        6,
        "Cached overall description: Olive branches moving in a light breeze, sunlight "
        "flickering through the leaves; no people in frame.",
    ),
    (
        "still",
        3,
        "Subject: A small harbour with fishing boats. Scene: Wooden boats with faded blue and "
        "white hulls tied along a stone quay, calm water. Setting: Harbour in the morning.",
    ),
    (
        "video",
        8,
        "Cached overall description: Fishing boats bobbing at the quay while a man in the "
        "foreground coils a rope; gulls pass overhead and a few voices can be heard.",
    ),
    (
        "still",
        3,
        "Subject: An orange tabby cat. Scene: The cat sits on a worn stone step and looks "
        "straight at the camera. Setting: Doorstep of an old house.",
    ),
    (
        "still",
        3,
        "Subject: Grilled fish and small plates of meze on a table. Scene: A whole fish, olive "
        "oil dishes, bread and a glass of tea beside a window onto the water. "
        "Setting: Waterside restaurant table.",
    ),
]


def _uuid(kind: str, index: int) -> str:
    return f"{index:08d}-{kind}-4000-8000-000000000000"


def candidates() -> list[MomentCandidate]:
    result = []
    for index, (media, seconds, prose) in enumerate(_MOMENTS, start=1):
        still = media == "still"
        text = (
            f"Still photo shown for {seconds:g}s; no motion, sound or speech. {prose}"
            if still
            else f"Whole analysed clip; no localized highlight. {prose}"
        )
        quality = dict(_VISIBLE)
        if index == 2:
            quality["overlapping_unusable_spans"] = [
                {"start_s": 0.0, "end_s": 1.2, "reason": "camera shake at the start"}
            ]
        result.append(
            MomentCandidate(
                id=_uuid("0001", index),
                source=_uuid("0002", index),
                kind="visual",
                evidence_scope="timed_moment" if still else "whole_source_context",
                media=media if still else "video",
                text=text,
                quality_notes=json.dumps(quality),
                start_s=0.0,
                end_s=float(seconds),
            )
        )
    return result


def build_input(
    *,
    query: str = DEFAULT_QUERY,
    event_title: str = "",
    event_about: str = "",
) -> CameraRollStoriesInput:
    return CameraRollStoriesInput(
        query=query, event_title=event_title, event_about=event_about, candidates=candidates()
    )


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    query: str
    event: bool
    names_place: bool

    def build(self) -> CameraRollStoriesInput:
        if self.event:
            return build_input(query=self.query, event_title=EVENT_TITLE, event_about=EVENT_ABOUT)
        return build_input(query=self.query)

    @property
    def place(self) -> str | None:
        return PLACE if self.names_place else None


VARIANTS = (
    Variant(
        "bare",
        "No place anywhere: the default query only. What a fresh no-GPS event sends today.",
        DEFAULT_QUERY,
        event=False,
        names_place=False,
    ),
    Variant(
        "query",
        "Place and intent typed into the one-shot query: today's best workaround.",
        WORKAROUND_QUERY,
        event=False,
        names_place=True,
    ),
    Variant(
        "event",
        "event_title and event_about set, default query.",
        DEFAULT_QUERY,
        event=True,
        names_place=True,
    ),
)
