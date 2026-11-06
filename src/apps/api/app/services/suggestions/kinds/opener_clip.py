"""``opener_clip``: "Start with {filename}". Dark until ``CLIP_ORDINAL_PROVEN``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind
from app.services.suggestions.context import SuggestionContext, Target, live_clips, slots
from app.services.suggestions.models import ChipKind, ChipKindSpec


def _available(ctx: SuggestionContext) -> bool:
    # The proof flag lives in suggestion_chips (docs/pipelines/suggestion-chips.md, gate (b)) and
    # the eval flips it there, so it is read at call time.
    from app.services import suggestion_chips  # noqa: PLC0415

    return suggestion_chips.CLIP_ORDINAL_PROVEN and len(live_clips(ctx)) >= 2


def first_slot_media(ctx: SuggestionContext) -> str | None:
    rows = slots(ctx)
    media = rows[0].get("media_id") if rows else None
    return media if isinstance(media, str) else None


def _present(ctx: SuggestionContext) -> bool:
    return False  # per-target: a clip already at slot 0 is skipped in ground()


def _clip_to_camera(ctx: SuggestionContext, media_id: str | None) -> bool:
    return any(c.get("media_id") == media_id and c.get("to_camera") for c in live_clips(ctx))


def _ground(ctx: SuggestionContext) -> list[Target]:
    first = first_slot_media(ctx) if ctx.state == "ready" else None
    out: list[Target] = []
    for clip in live_clips(ctx):
        media_id = clip.get("media_id")
        ordinal = clip.get("ordinal")
        filename = str(clip.get("filename") or "").strip()
        if not isinstance(media_id, str) or not isinstance(ordinal, int) or ordinal < 1:
            continue
        if ordinal == 1 or media_id == first or not filename:
            continue  # already the opener, or no creator-owned name to refer to it by
        moments = [
            m
            for m in clip.get("notable_moments") or []
            if isinstance(m, Mapping) and isinstance(m.get("start_s"), (int, float))
        ]
        if not (clip.get("to_camera") or moments):
            continue  # nothing real to open on
        moment = (
            (float(moments[0]["start_s"]), float(moments[0].get("end_s") or 0.0))
            if moments
            else None
        )
        out.append(
            Target(
                key=f"clip:{media_id}",
                media_id=media_id,
                clip_ordinal=ordinal,
                moment=moment,
                filename=filename,
            )
        )
    # One opener chip: the clip that talks to camera, else the first with a real moment.
    out.sort(key=lambda t: (not _clip_to_camera(ctx, t.media_id), t.clip_ordinal or 0))
    return out[:1]


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    for index, slot in enumerate(slots(ctx)):
        if slot.get("media_id") == target.media_id and index > 0:
            return {"op": "reorder_clip", "from_index": index, "to_index": 0}
    return None


SPEC = ChipKindSpec(
    kind=ChipKind.OPENER_CLIP,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "dispatch_render", "ready": "family:clip"},
    available=_available,
    already_present=_present,
    ground=_ground,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="the named clip plays first",
    op=_op,
    claimed_op="reorder_clip",
    witness_op="reorder_clip",
    edit_tag="opener_set",
)

KIND = legacy_kind(SPEC, surfaces=PHONE_AND_CLOUD)
