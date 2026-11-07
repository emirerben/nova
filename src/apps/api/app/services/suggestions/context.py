"""Engine input and the read-only readers every suggestion kind shares.

Moved out of ``services/suggestion_chips.py`` so a kind module (``suggestions/kinds/*.py``) can
read the footage and the editor snapshot without importing the engine that imports the registry.
Everything here is pure and I/O free. Nothing in this module may import ``suggestion_chips``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.kria.spoken_text_ask import is_spoken_text_bar

if TYPE_CHECKING:
    from app.services.suggestions.models import ChipKind

# A witness returns the edit targets the change would touch (for dedupe) or None when the
# claimed change is refused / a no-op. Both are injected by the loader so the engine stays
# pure and the eval lane can inject fakes.
StrategyWitness = Callable[["ChipKind"], "frozenset[str] | None"]
EditorWitness = Callable[[dict[str, Any]], "frozenset[str] | None"]


@dataclass(frozen=True)
class SuggestionContext:
    """Everything the pure engine reads. Plain dicts/lists so fixtures stay easy.

    ``clips``: ``[{media_id, ordinal, kind, subject, setting, has_speech, to_camera,
    duration_s, filename, notable_moments: [{start_s, end_s, label}]}]`` (1-based ordinal).
    ``manifest``: ``{"capabilities": {name: {"available": bool, "reason_code": str|None}}}``.
    ``snapshot``: the editor snapshot (``build_editor_snapshot``) in ``ready``, else None.
    ``understanding``: ``complete | pending | empty | n/a`` (mapped to the wire's
    ``done | pending | empty`` by ``assemble_set``).
    ``signals``: ``{media_id: {signal_name: payload}}`` from ``services/clip_signals``; empty
    unless ``KRIA_SUGGESTION_SIGNALS_ENABLED``. ``kind_inputs``: loader reads that only one kind
    needs, keyed by kind name, so adding a kind never adds a field here.
    """

    thread_id: str = ""
    state: str = "none"
    language: str = "en"
    client_protocol: int | None = None
    media_digest: str = ""
    manifest_hash: str | None = None
    render_generation_id: str | None = None
    variant_id: str | None = None
    resolved_archetype: str | None = None
    runtime_version: int = 2
    understanding: str = "n/a"
    clips: Sequence[Mapping[str, Any]] = ()
    manifest: Mapping[str, Any] = field(default_factory=dict)
    snapshot: Mapping[str, Any] | None = None
    edit_format: str | None = None
    brief_headline: str | None = None
    # ready only: the creator's own latest request (<= 200 chars), a hint for the wording agent.
    typed_request: str | None = None
    job_id: str | None = None
    creator_id: str | None = None
    strategy_witness: StrategyWitness | None = field(default=None, compare=False, repr=False)
    editor_witness: EditorWitness | None = field(default=None, compare=False, repr=False)
    signals: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    signals_version: str = ""
    kind_inputs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    key: str = "-"
    media_id: str | None = None
    clip_ordinal: int | None = None
    moment: tuple[float, float] | None = None
    text_bar_index: int | None = None
    filename: str = ""  # opener_clip only; creator-owned upload name


NO_TARGET = Target()


def snapshot_digest(snapshot: Mapping[str, Any] | None) -> str | None:
    """Fingerprint of the editor snapshot an editor-ops witness ran against.

    A pinned tap recomputes it from the live editor target and refuses to apply when it moved.
    """
    if not isinstance(snapshot, Mapping):
        return None
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:32]


def cap_ok(ctx: SuggestionContext, name: str) -> bool:
    caps = (ctx.manifest or {}).get("capabilities") or {}
    row = caps.get(name)
    if isinstance(row, Mapping):
        return row.get("available") is True
    return bool(getattr(row, "available", False))


def families(ctx: SuggestionContext) -> frozenset[str]:
    snap = ctx.snapshot or {}
    return frozenset(str(f) for f in snap.get("allowed_op_families") or [])


def live_clips(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [c for c in ctx.clips if isinstance(c, Mapping)]


def video_clips(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [c for c in live_clips(ctx) if (c.get("kind") or "video") == "video"]


def any_speech(ctx: SuggestionContext) -> bool:
    return any(bool(c.get("has_speech")) or bool(c.get("to_camera")) for c in live_clips(ctx))


def any_to_camera(ctx: SuggestionContext) -> bool:
    return any(bool(c.get("to_camera")) for c in live_clips(ctx))


def text_bars(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [
        b
        for b in (ctx.snapshot or {}).get("text_bars") or []
        if isinstance(b, Mapping) and not b.get("removed") and not b.get("caption_cue")
    ]


# Mirrors kria/plan_review.section_of_text_bar: generative, narrated and phone-spoken edits
# carry their opening text as role=generative_intro (the intro text is not in the snapshot).
TITLE_ROLES = frozenset({"title", "intro", "hook", "generative_intro"})


def title_bar(
    ctx: SuggestionContext, *, editor_ids: frozenset[str] | None = None
) -> tuple[int, Mapping[str, Any]] | None:
    for index, bar in enumerate(text_bars(ctx)):
        bar_id = str(bar.get("id") or "")
        if bar_id.startswith(("clip-label-", "montage-text-", "group-title-")):
            continue
        if editor_ids is not None and bar_id not in editor_ids:
            continue
        if is_spoken_text_bar(bar):
            continue  # KRI-747: the text on a spoken word wears the title's role, it is not it
        if bar.get("role") in TITLE_ROLES or bar_id.startswith("guided-title"):
            return index, bar
    return None


def editable_title_bar(ctx: SuggestionContext) -> tuple[int, Mapping[str, Any]] | None:
    """``title_bar`` narrowed to a title the editor can actually resolve.

    Title-targeted chips ("Animate the title") run a ``group: "title"`` op, and the editor
    only calls a bar the title under its own rules (``kria_editor_ops_text.classify``: an
    intro bar must start within the first 0.5 s). Offering the chip for any other opening
    text sends a tap the editor answers with "no title" -- so a chip is offered only when
    BOTH agree. Kill switch ``KRIA_ERROR_HONESTY_ENABLED`` restores the looser ``title_bar``.
    """
    from app.config import settings  # noqa: PLC0415

    if not settings.kria_error_honesty_enabled:
        return title_bar(ctx)
    from app.services.kria_editor_ops_text import title_bar_ids  # noqa: PLC0415 - import cycle

    return title_bar(ctx, editor_ids=title_bar_ids(ctx.snapshot or {}))


def slots(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [
        s
        for s in (ctx.snapshot or {}).get("slots") or []
        if isinstance(s, Mapping) and not s.get("removed")
    ]


def output_boundary_time(ctx: SuggestionContext, boundary_index: int) -> str | None:
    """``m:ss`` of the END of output slot ``boundary_index`` on the OUTPUT timeline.

    The only time a ``{time}`` placeholder may carry: a slot boundary read from the editor
    snapshot, never a source-clip time and never a Gemini moment.
    """
    rows = slots(ctx)
    if not 0 <= boundary_index < len(rows):
        return None
    end = rows[boundary_index].get("output_end_s")
    if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
        return None
    whole = int(end)
    return f"{whole // 60}:{whole % 60:02d}"


def music_level(ctx: SuggestionContext) -> float | None:
    mix = (ctx.snapshot or {}).get("mix")
    level = mix.get("music_level") if isinstance(mix, Mapping) else None
    return float(level) if isinstance(level, (int, float)) and not isinstance(level, bool) else None


def can_draft(ctx: SuggestionContext) -> bool:
    """Can a first message produce a draft? The planner's own gate: v2 needs a renderable
    manifest (`dispatch_render`); the v1 Creator route also needs the agent switched on."""
    if not cap_ok(ctx, "dispatch_render"):
        return False
    return ctx.runtime_version == 2 or cap_ok(ctx, "main_creator_agent")


def ground_none(ctx: SuggestionContext) -> list[Target]:
    """A whole-video chip: grounded when there is real footage (clips) or a real render."""
    return [NO_TARGET] if ctx.state == "ready" or live_clips(ctx) else []
