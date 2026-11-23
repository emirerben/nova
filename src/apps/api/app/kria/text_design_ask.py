"""KRI-1582: the text design picker's previews, drawn on the creator's iPhone.

The design question (`choice_questions._text_design`) is asked before research writes any
fact, so a preview shows sample copy in the design, over a frame of her own first clip. The
server lays the sample rows out exactly as the render would (`resolve_phone_text_style` under
the design stamp, then the phone compile), and the app draws those layers over the frame
with its own engine. Only a build at ``TEXT_DESIGN_PICKER_PROTOCOL`` gets them, as
``choice_question`` version 2; an older build reads version 1, the same text-only cards.

Preview shape, per option::

    {"media_id": str, "media_kind": "video" | "image", "at_s": float,
     "time_s": float, "text_layers": [PortableTextLayer], "fonts": {asset_id: file}}

``at_s`` is the second of her clip under the text, ``time_s`` the second of the layers'
clock the still samples, ``fonts`` each layer font's bundled file (the app ships the same
font files as the server).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import structlog

log = structlog.get_logger()

PREVIEW_VERSION = 2
# Every sample row holds the whole window; the still is taken in the middle of it.
_PREVIEW_WINDOW_S = (0.0, 4.0)
_PREVIEW_TIME_S = 2.0
# How far into her clip the frame is taken.
_FRAME_SHARE = 0.4
# Past this her topic reads as a sentence, not a title, so the sample title stands in.
_HOOK_MAX_CHARS = 48
# The four previews ride one chat event; past this the question goes out as text-only cards.
MAX_PREVIEWS_BYTES = 96 * 1024


def _sample_rows(design_id: str, hook: str, language: str | None) -> list[dict[str, Any]]:
    """A hook and one fact in ``design_id``'s shape, tagged the way the research writer tags
    its rows (`research.design`), so the resolver draws them through the same path."""
    from app.kria.reply_language import say  # noqa: PLC0415
    from app.pipeline.text_designs import TEXT_DESIGNS  # noqa: PLC0415

    design = TEXT_DESIGNS[design_id]
    start, end = _PREVIEW_WINDOW_S
    base = {
        "start_s": start,
        "end_s": end,
        "role": "generative_intro",
        "effect": "static",
        "position": "middle",
        **({"text_language": language} if language else {}),
    }
    rows: list[dict[str, Any]] = [
        {
            **base,
            "id": "preview-hook",
            "text": hook,
            "source_params": {
                "phone_text_role": "hook",
                "text_design": design_id,
                "research": {"design": {"id": design_id, "level": "hook"}},
            },
        }
    ]
    if design.mode == "running_guide":
        rows.append(
            {
                **base,
                "id": "preview-line",
                "text": say(
                    en="One line on every clip, as if you were telling it",
                    tr="Her klipte sanki sen anlatıyormuşsun gibi bir satır",
                ),
                "source_params": {
                    "text_design": design_id,
                    "research": {"design": {"id": design_id, "level": "line"}},
                },
            }
        )
        return rows
    for level, text in (
        ("heading", say(en="A fact's heading", tr="Bilginin başlığı")),
        (
            "detail",
            say(
                en="Its detail sits right under it, on one short line",
                tr="Ayrıntısı hemen altında, kısa bir satırda durur",
            ),
        ),
    ):
        rows.append(
            {
                **base,
                "id": f"preview-{level}",
                "text": text,
                "source_params": {
                    "text_design": design_id,
                    "research": {"design": {"id": design_id, "level": level, "pair": 0}},
                },
            }
        )
    return rows


def _compiled_layers(rows: list[dict[str, Any]]) -> tuple[list[dict], dict[str, str]]:
    """The rows resolved under the design stamp and compiled for the phone, as the render
    would: the layers (wire JSON) and each font asset's bundled file."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.canvas import PORTRAIT  # noqa: PLC0415
    from app.pipeline.generative_overlays import (  # noqa: PLC0415
        build_overlays_from_text_elements,
    )
    from app.pipeline.phone_text_style import resolve_phone_text_style  # noqa: PLC0415
    from app.pipeline.portable_text_layout import compile_text_overlay  # noqa: PLC0415

    rows = [TextElement.model_validate(row).model_dump(mode="json") for row in rows]
    rows = resolve_phone_text_style(rows, informative=True, shapes=True)
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in rows],
        video_duration_s=_PREVIEW_WINDOW_S[1],
        independent_box_alignment=True,
    )
    layers: list[dict] = []
    fonts: dict[str, str] = {}
    for index, overlay in enumerate(overlays):
        layer, font = compile_text_overlay(overlay, layer_id=f"preview-{index}", canvas=PORTRAIT)
        layers.append(layer.model_dump(mode="json", exclude_none=True))
        if font is not None:
            fonts[font.id] = font.catalog_id
    return layers, fonts


def _frame(media_snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Her first clip (a video if she gave one, else a photo) and the second shown under the
    text. The opening clip is where the hook sits."""
    rows = [
        row
        for row in (media_snapshot or {}).get("clip_assignments") or []
        if isinstance(row, Mapping) and row.get("media_id")
    ]
    videos = [row for row in rows if str(row.get("kind") or "video") != "image"]
    row = (videos or rows or [None])[0]
    if row is None:
        return None
    from app.services.choice_questions import _row_duration_s  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "video") == "image" else "video"
    duration = _row_duration_s(row) if kind == "video" else None
    return {
        "media_id": str(row["media_id"]),
        "media_kind": kind,
        "at_s": round(duration * _FRAME_SHARE, 3) if duration else 0.0,
    }


def _hook_text(brief: Any) -> str:
    """Her research topic in her own words, else a sample title."""
    from app.kria.reply_language import say  # noqa: PLC0415
    from app.kria.research_preview import research_requirement  # noqa: PLC0415

    found = research_requirement(brief)
    topic = " ".join(str(found[1].topic).split()) if found is not None else ""
    if topic and len(topic) <= _HOOK_MAX_CHARS:
        return topic
    return say(en="Your opening title", tr="Açılış başlığın")


def with_design_previews(
    question: Mapping[str, Any],
    *,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    client_protocol: int | None,
) -> dict[str, Any]:
    """``question`` as version 2 with a preview on every design option, for a build that
    draws them. Anything that cannot be previewed (no clip, a layout or size failure) leaves
    the text-only question, which every build answers the same way."""
    from app.kria.reply_language import current_reply_language  # noqa: PLC0415
    from app.services.client_protocol import TEXT_DESIGN_PICKER_PROTOCOL  # noqa: PLC0415

    plain = dict(question)
    if client_protocol is None or client_protocol < TEXT_DESIGN_PICKER_PROTOCOL:
        return plain
    frame = _frame(media_snapshot)
    if frame is None:
        return plain
    hook = _hook_text(brief)
    language = current_reply_language()
    upgraded = copy.deepcopy(plain)
    try:
        for option in upgraded.get("options") or []:
            layers, fonts = _compiled_layers(_sample_rows(str(option["key"]), hook, language))
            option["preview"] = {
                **frame,
                "time_s": _PREVIEW_TIME_S,
                "text_layers": layers,
                "fonts": fonts,
            }
    except Exception:  # noqa: BLE001
        log.exception("kria_text_design_preview_failed")
        return plain
    size = len(json.dumps(upgraded["options"], separators=(",", ":")).encode("utf-8"))
    if size > MAX_PREVIEWS_BYTES:
        log.warning("kria_text_design_preview_too_large", bytes=size)
        return plain
    upgraded["version"] = PREVIEW_VERSION
    return upgraded
