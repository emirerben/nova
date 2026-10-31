"""Footage-aware suggestion chips for the Kria creation chat (KRI-756).

Two layers, one rule: **the generator decides which chips may exist, the agent only words them.**

1. A pure, free, LLM-free candidate generator (``build_candidates``). Every candidate it emits
   is *real* (the footage and edit it describes exist), *enabled* (the resolvers the planner
   enforces say the capability is available for THIS creator and client protocol), *executable*
   (a dry-run witness proved the claimed change is not a no-op) and *not already in the edit*.
2. A small text-only wording agent (``nova.chat.suggester``, ``app/agents/chip_suggester.py``)
   that ranks those candidates and rewrites their DISPLAY label and ``why`` subtitle, off the
   request path. It can only return candidate ids it was given. ``kind``, ``capability``,
   ``message``, ``target`` and ``anchor`` are always copied from the candidate, and a
   deterministic validator (``suggestion_chip_copy``) gates every label and ``why`` it writes.

The GET route never calls a model: ``assemble_set`` joins cached agent wording onto live
candidates and falls back, per chip, to the template label. A chip's ``message`` is a closed
server template with no Gemini-derived words, so the "Gemini metadata never becomes overlay
text" invariant holds by construction.

Layout:

* ``build_candidates(ctx)``          pure, never raises, no I/O -> ``CandidatePlan``.
* ``assemble_set(ctx, plan, ...)``   pure join of plan + wording -> ``SuggestionSetOut``.
* ``build_suggestion_set(ctx)``      ``assemble_set`` over ``build_candidates`` (template wording).
* ``resolve_wording(...)``           validates raw agent output against live candidates.
* ``CHIP_KIND_GATE``                 one spec row per ``ChipKind`` (guarded by a test).
* ``load_suggestion_context(...)``   the only read path (route + worker call it).
* ``persist_suggestion_set(...)``    ring write on ``thread.state`` (revision-neutral).
* ``match_offered_suggestion`` / ``suggestion_tap_payload``   tap detection for turns.

Contract + rollout: ``docs/pipelines/suggestion-chips.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from app.services.suggestion_chip_copy import (
    MAX_LABEL_CHARS,
    footage_vocabulary,
    language_of,
    validate_label,
    validate_why,
)

log = structlog.get_logger()

SCHEMA_VERSION = 1
RING_KEY = "suggestion_sets"
RING_SIZE = 5
MAX_CHIPS = 4
MAX_LEVERS = {"first_prompt": 3, "ready": 4}
MAX_POOL = 6  # candidates handed to the wording agent
MAX_MESSAGE_CHARS = 120
PENDING_RETRY_S = 3  # understanding still running
WORDING_RETRY_S = 2  # understanding done, agent result not cached yet
WORDING_WAIT_S = 6.0  # after this, serve template labels and freeze the set
FOOTAGE_CLIPS_MAX = 12
SUBJECT_WORDS = 6
SETTING_WORDS = 8
MOMENT_CHARS = 60
MOMENTS_PER_CLIP = 3

# Bump together with prompts/chip_suggester.txt (AgentSpec.prompt_version reads this).
PROMPT_VERSION = "2026-10-19.2"

# Must equal iOS ``ChatSubmission.mediaOnlyMessage`` (ChatTimeline.swift). A media-only send
# is an ordinary message: Main Creator has no special detection for it (gate (a)).
MEDIA_ONLY_MESSAGE = "Suggest an edit."

# Gate (b): the planner's "clip N" (main_creator.txt) is the order the clips PLAY, and the
# model resolves a creator's clip reference by filename/subject, never by upload ordinal.
# Nothing proves a filename-to-hero resolution, so `opener_clip` stays dark until a replay
# cassette proves it (see docs/pipelines/suggestion-chips.md).
CLIP_ORDINAL_PROVEN = False
# Pinned text needs the creator's own words, so the chip would be an ask the planner answers
# with a question. Not emitted in this pass; the kind exists so clients can render it later.
PINNED_TEXT_PROVEN = False

State = Literal["first_prompt", "ready", "none"]
DryRunMode = Literal["none", "strategy", "editor", "allowlist"]

# A witness returns the edit targets the change would touch (for dedupe) or None when the
# claimed change is refused / a no-op. Both are injected by the loader so the engine stays
# pure and the eval lane can inject fakes.
StrategyWitness = Callable[["ChipKind"], "frozenset[str] | None"]
EditorWitness = Callable[[dict[str, Any]], "frozenset[str] | None"]


class ChipKind(StrEnum):
    DEFAULT_EDIT = "default_edit"
    HOOK = "hook"
    CAPTIONS = "captions"
    MUSIC = "music"
    SFX = "sfx"
    TRANSITION = "transition"
    TITLE_ANIMATION = "title_animation"
    PINNED_TEXT = "pinned_text"
    OPENER_CLIP = "opener_clip"


# Kinds whose tap routes into the creative-copy flow: at most one per set, never any wording.
COPY_BEARING = frozenset({ChipKind.HOOK, ChipKind.PINNED_TEXT})

# ── Editor-op decisions (the denylist) ───────────────────────────────────────────────
# A chip may only CLAIM an op in CHIP_OP_ALLOW. Every editor op that carries creator-facing
# text, comes from a non-chat surface or is server-async is in CHIP_OP_DENY, so a model (or a
# careless edit) can never turn a chip into on-screen words. CHIP_OP_WITNESS_PROBES are ops a
# dry-run may use to prove a capability exists without the chip ever sending them. A guard
# test (tests/services/test_suggestion_chips_guards.py) fails when a new text-carrying op
# enters the editor op vocabulary without an explicit decision here.
CHIP_OP_ALLOW = frozenset(
    {"set_caption_meta", "add_sfx", "set_mix", "set_transition", "reorder_clip", "patch_text"}
)
CHIP_OP_WITNESS_PROBES = frozenset({"set_title"})
CHIP_OP_DENY = frozenset(
    {
        # text-carrying: wording is the creator's (or the creative-copy flow's), never a chip's
        "add_text",
        "edit_text",
        "rewrite_text",
        "set_title",
        "label_each_clip",
        "realign_labels",
        "edit_caption",
        "replace_caption_text",
        "replace_text_sequence",
        "set_post_caption",
        "set_edit_direction",
        "remove_text",
        "remove_texts",
        "set_text_timing",
        "set_texts_timing",
        "patch_text_style",
        "patch_text_appearance",
        # overlays / intro layout / speech cuts / music swap: not chat-executable from a chip
        "add_overlay",
        "patch_overlay",
        "remove_overlay",
        "accept_overlay_suggestion",
        "set_overlay_display",
        "set_intro_layout",
        "apply_speech_cut_candidate",
        "swap_music",
        "remove_music",
        # camera / motion / custom effects are device-only or need creator taste
        "add_camera_effect",
        "patch_camera_effect",
        "remove_camera_effect",
        "add_motion_block",
        "patch_motion_block",
        "remove_motion_block",
        "apply_custom_effect",
        "set_look_preset",
    }
)


# ── Wire models (clients ignore unknown keys; every field has a default) ──────────────


class SuggestionAnchorOut(BaseModel):
    media_digest: str = ""
    manifest_hash: str | None = None
    render_generation_id: str | None = None
    variant_id: str | None = None


class ChipMomentOut(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0


class ChipAnchorOut(BaseModel):
    media_id: str | None = None
    clip_ordinal: int | None = None
    moment: ChipMomentOut | None = None
    text_bar_index: int | None = None


class SuggestionChipOut(BaseModel):
    id: str = ""
    # default_edit | hook | captions | music | sfx | transition | title_animation |
    # pinned_text | opener_clip. A closed server enum; clients render an unknown kind by label.
    kind: str = ""
    # DISPLAY ONLY (<= 36 chars). The client sends ``message`` on tap, never the label.
    label: str = ""
    # The ONLY text a tap sends: a closed server template (<= 120 chars).
    message: str = ""
    rank: int = 1  # 1..4, unique, contiguous
    default_edit: bool = False  # true only for rank 1 at first_prompt
    # Manifest capability key or editor op family that gated the chip (audit only).
    capability: str | None = None
    anchor: ChipAnchorOut = Field(default_factory=ChipAnchorOut)
    # Display-only subtitle (<= 48 chars) with footage evidence; present only when it passed
    # the lexical grounding check.
    why: str | None = None
    label_source: str = "template"  # agent | template
    # The client drops the chip locally when this is set and above its own protocol.
    requires_protocol: int | None = None


class SuggestionSetOut(BaseModel):
    schema_version: int = SCHEMA_VERSION
    set_id: str = ""
    state: str = "none"  # first_prompt | ready | none
    status: str = "unavailable"  # ok | pending | unavailable
    understanding: str = "empty"  # pending | done | empty
    retry_after_s: int | None = None
    wording: str = "fallback"  # agent | fallback | pending
    anchor: SuggestionAnchorOut = Field(default_factory=SuggestionAnchorOut)
    chips: list[SuggestionChipOut] = Field(default_factory=list)


def unavailable_set() -> SuggestionSetOut:
    """Flag off, cohort miss, 404-equivalent or any failure: render nothing."""
    return SuggestionSetOut(status="unavailable", state="none", chips=[])


# ── Engine input ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SuggestionContext:
    """Everything the pure engine reads. Plain dicts/lists so fixtures stay easy.

    ``clips``: ``[{media_id, ordinal, kind, subject, setting, has_speech, to_camera,
    duration_s, filename, notable_moments: [{start_s, end_s, label}]}]`` (1-based ordinal).
    ``manifest``: ``{"capabilities": {name: {"available": bool, "reason_code": str|None}}}``.
    ``snapshot``: the editor snapshot (``build_editor_snapshot``) in ``ready``, else None.
    ``understanding``: ``complete | pending | empty | n/a`` (mapped to the wire's
    ``done | pending | empty`` by ``assemble_set``).
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


# ── Copy: ONE table, no free text ────────────────────────────────────────────────────

_COPY: dict[ChipKind, dict[str, tuple[str, str]]] = {
    # (label, message) per language. Messages never carry quotes or Gemini words.
    ChipKind.DEFAULT_EDIT: {
        "en": ("Let Kria cut the first version", MEDIA_ONLY_MESSAGE),
        "tr": ("İlk versiyonu Kria kessin", MEDIA_ONLY_MESSAGE),
    },
    ChipKind.HOOK: {
        "en": ("Add a hook to the opening", "Add a hook that matches the video"),
        "tr": ("Açılışa bir hook ekle", "Videoya uyan bir hook ekle"),
    },
    ChipKind.CAPTIONS: {
        "en": ("Add captions", "Add captions"),
        "tr": ("Altyazı ekle", "Altyazı ekle"),
    },
    ChipKind.SFX: {
        "en": ("Add sound effects", "Add sound effects"),
        "tr": ("Ses efektleri ekle", "Ses efektleri ekle"),
    },
    ChipKind.MUSIC: {
        "en": ("Turn the music down", "Turn the music down"),
        "tr": ("Müziği kıs", "Müziği kıs"),
    },
    ChipKind.TRANSITION: {
        "en": ("Add transitions", "Add transitions between the clips"),
        "tr": ("Geçişler ekle", "Klipler arasına geçişler ekle"),
    },
    ChipKind.TITLE_ANIMATION: {
        "en": ("Animate the title", "Animate the title"),
        "tr": ("Başlığı canlandır", "Başlığı canlandır"),
    },
    # opener_clip (dark): "{filename}" is a creator-owned upload name, filled identically in
    # the label and the message by ``_render_copy``.
    ChipKind.OPENER_CLIP: {
        "en": ("Open on {filename}", "Start with {filename}"),
        "tr": ("{filename} ile aç", "{filename} ile başla"),
    },
}
_QUOTES = set("\"'`“”‘’«»")


def _copy(kind: ChipKind, language: str) -> tuple[str, str]:
    table = _COPY[kind]
    return table.get(language_of(language)) or table["en"]


# ── Targets + helpers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Target:
    key: str = "-"
    media_id: str | None = None
    clip_ordinal: int | None = None
    moment: tuple[float, float] | None = None
    text_bar_index: int | None = None
    filename: str = ""  # opener_clip only; creator-owned upload name


_NO_TARGET = _Target()


def _cap_ok(ctx: SuggestionContext, name: str) -> bool:
    caps = (ctx.manifest or {}).get("capabilities") or {}
    row = caps.get(name)
    if isinstance(row, Mapping):
        return row.get("available") is True
    return bool(getattr(row, "available", False))


def _families(ctx: SuggestionContext) -> frozenset[str]:
    snap = ctx.snapshot or {}
    return frozenset(str(f) for f in snap.get("allowed_op_families") or [])


def _live_clips(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [c for c in ctx.clips if isinstance(c, Mapping)]


def _video_clips(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [c for c in _live_clips(ctx) if (c.get("kind") or "video") == "video"]


def _any_speech(ctx: SuggestionContext) -> bool:
    return any(bool(c.get("has_speech")) or bool(c.get("to_camera")) for c in _live_clips(ctx))


def _any_to_camera(ctx: SuggestionContext) -> bool:
    return any(bool(c.get("to_camera")) for c in _live_clips(ctx))


def _text_bars(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
    return [
        b
        for b in (ctx.snapshot or {}).get("text_bars") or []
        if isinstance(b, Mapping) and not b.get("removed") and not b.get("caption_cue")
    ]


# Mirrors kria/plan_review.section_of_text_bar: generative, narrated and phone-spoken edits
# carry their opening text as role=generative_intro (the intro text is not in the snapshot).
_TITLE_ROLES = frozenset({"title", "intro", "hook", "generative_intro"})


def _title_bar(
    ctx: SuggestionContext, *, editor_ids: frozenset[str] | None = None
) -> tuple[int, Mapping[str, Any]] | None:
    for index, bar in enumerate(_text_bars(ctx)):
        bar_id = str(bar.get("id") or "")
        if bar_id.startswith(("clip-label-", "montage-text-", "group-title-")):
            continue
        if editor_ids is not None and bar_id not in editor_ids:
            continue
        if bar.get("role") in _TITLE_ROLES or bar_id.startswith("guided-title"):
            return index, bar
    return None


def _editable_title_bar(ctx: SuggestionContext) -> tuple[int, Mapping[str, Any]] | None:
    """``_title_bar`` narrowed to a title the editor can actually resolve.

    Title-targeted chips ("Animate the title") run a ``group: "title"`` op, and the editor
    only calls a bar the title under its own rules (``kria_editor_ops_text.classify``: an
    intro bar must start within the first 0.5 s). Offering the chip for any other opening
    text sends a tap the editor answers with "no title" -- so a chip is offered only when
    BOTH agree. Kill switch ``KRIA_ERROR_HONESTY_ENABLED`` restores the looser ``_title_bar``.
    """
    from app.config import settings  # noqa: PLC0415

    if not settings.kria_error_honesty_enabled:
        return _title_bar(ctx)
    from app.services.kria_editor_ops_text import title_bar_ids  # noqa: PLC0415 - import cycle

    return _title_bar(ctx, editor_ids=title_bar_ids(ctx.snapshot or {}))


def _slots(ctx: SuggestionContext) -> list[Mapping[str, Any]]:
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
    slots = _slots(ctx)
    if not 0 <= boundary_index < len(slots):
        return None
    end = slots[boundary_index].get("output_end_s")
    if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
        return None
    whole = int(end)
    return f"{whole // 60}:{whole % 60:02d}"


# ── Per-kind predicates ──────────────────────────────────────────────────────────────


def _can_draft(ctx: SuggestionContext) -> bool:
    """Can a first message produce a draft? The planner's own gate: v2 needs a renderable
    manifest (`dispatch_render`); the v1 Creator route also needs the agent switched on."""
    if not _cap_ok(ctx, "dispatch_render"):
        return False
    return ctx.runtime_version == 2 or _cap_ok(ctx, "main_creator_agent")


def _hook_available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return _can_draft(ctx)
    return "title" in _families(ctx)


def _hook_present(ctx: SuggestionContext) -> bool:
    # first_prompt: nothing is in the edit yet. ready: any title bar already says something.
    return ctx.state == "ready" and _title_bar(ctx) is not None


# Formats that always caption what is said (strategy_policy): a captions chip would be a no-op.
def _always_captioned(ctx: SuggestionContext) -> bool:
    fmt = str(ctx.edit_format or "")
    return fmt == "subtitled" or fmt.startswith("narrated")


def _captions_available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        # Gated on the footage (speech / talking to camera) and the edit format. The manifest
        # `caption_style` capability only mirrors main_creator_agent_execution_enabled and
        # nothing enforces it, so it is deliberately NOT consulted here.
        return _any_speech(ctx)
    snap = ctx.snapshot or {}
    captions = snap.get("captions")
    return "caption" in _families(ctx) and isinstance(captions, Mapping)


def _captions_present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return _always_captioned(ctx)
    captions = (ctx.snapshot or {}).get("captions")
    if not isinstance(captions, Mapping):
        return True
    meta = captions.get("meta") if isinstance(captions.get("meta"), Mapping) else {}
    # Only a switched-off caption lane with cues to show is something Kria can turn on.
    return meta.get("enabled") is not False or not captions.get("cues")


def _sfx_available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return _cap_ok(ctx, "sound_effects")
    sfx = (ctx.snapshot or {}).get("sfx")
    return (
        "sfx" in _families(ctx)
        and isinstance(sfx, Mapping)
        and bool(sfx.get("catalog"))  # empty catalog: add_sfx always fails closed
    )


def _sfx_present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return False
    sfx = (ctx.snapshot or {}).get("sfx")
    return bool(isinstance(sfx, Mapping) and sfx.get("placements"))


def _transition_available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return _cap_ok(ctx, "transitions") and len(_live_clips(ctx)) >= 2
    return "transition" in _families(ctx) and len(_slots(ctx)) >= 2


def _transition_present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return False
    return any(str(s.get("transition_after") or "cut") != "cut" for s in _slots(ctx)[:-1])


def _music_present(ctx: SuggestionContext) -> bool:
    music = (ctx.snapshot or {}).get("music")
    return isinstance(music, Mapping) and music.get("current_track_id") is not None


def _music_available(ctx: SuggestionContext) -> bool:
    # Only worth offering when a voice competes with the song; a music-led, speech-free edit
    # is the song's showcase and "turn it down" would hurt it.
    return (
        ctx.state == "ready"
        and "music" in _families(ctx)
        and _music_present(ctx)
        and _any_speech(ctx)
    )


_MUSIC_QUIET_AT = 0.3


def _music_level(ctx: SuggestionContext) -> float | None:
    mix = (ctx.snapshot or {}).get("mix")
    level = mix.get("music_level") if isinstance(mix, Mapping) else None
    return float(level) if isinstance(level, (int, float)) and not isinstance(level, bool) else None


def _music_already_quiet(ctx: SuggestionContext) -> bool:
    level = _music_level(ctx)
    return level is not None and level <= _MUSIC_QUIET_AT


_TITLE_ANIMATION_ARCHETYPES = frozenset({"guided_story", "unified_montage"})


def _title_anim_available(ctx: SuggestionContext) -> bool:
    return (
        ctx.state == "ready"
        and ctx.resolved_archetype in _TITLE_ANIMATION_ARCHETYPES
        and "text" in _families(ctx)  # the op is patch_text; the parser refuses it without it
        and _editable_title_bar(ctx) is not None
    )


def _title_anim_present(ctx: SuggestionContext) -> bool:
    found = _editable_title_bar(ctx)
    if found is None:
        return True
    phases = found[1].get("animation_phases")
    entrance = phases.get("entrance") if isinstance(phases, Mapping) else None
    return entrance not in (None, "", "none")


def _opener_available(ctx: SuggestionContext) -> bool:
    return CLIP_ORDINAL_PROVEN and len(_live_clips(ctx)) >= 2


def _opener_first_slot_media(ctx: SuggestionContext) -> str | None:
    slots = _slots(ctx)
    media = slots[0].get("media_id") if slots else None
    return media if isinstance(media, str) else None


def _opener_present(ctx: SuggestionContext) -> bool:
    return False  # per-target: a clip already at slot 0 is skipped in ground()


def _pinned_available(ctx: SuggestionContext) -> bool:
    return False  # PINNED_TEXT_PROVEN: dark this pass


# ── Grounding: which real thing does the chip point at? ──────────────────────────────


def _ground_none(ctx: SuggestionContext) -> list[_Target]:
    """A whole-video chip: grounded when there is real footage (clips) or a real render."""
    return [_NO_TARGET] if ctx.state == "ready" or _live_clips(ctx) else []


def _ground_captions(ctx: SuggestionContext) -> list[_Target]:
    if ctx.state == "first_prompt":
        return [_NO_TARGET] if _any_speech(ctx) else []
    return [_NO_TARGET]


def _ground_music(ctx: SuggestionContext) -> list[_Target]:
    return [_NO_TARGET]


def _ground_title_anim(ctx: SuggestionContext) -> list[_Target]:
    found = _editable_title_bar(ctx)
    if found is None:
        return []
    index, bar = found
    return [_Target(key=f"bar:{bar.get('id') or index}", text_bar_index=index)]


def _clip_to_camera(ctx: SuggestionContext, media_id: str | None) -> bool:
    return any(c.get("media_id") == media_id and c.get("to_camera") for c in _live_clips(ctx))


def _ground_opener(ctx: SuggestionContext) -> list[_Target]:
    first = _opener_first_slot_media(ctx) if ctx.state == "ready" else None
    out: list[_Target] = []
    for clip in _live_clips(ctx):
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
            _Target(
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


# ── Editor-path witness ops (never sent to a client) ─────────────────────────────────


def _op_hook(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    # A PROBE: proves a title CAN be set on this edit. The chip itself routes into the
    # creative-copy flow and carries no wording; set_title is in CHIP_OP_WITNESS_PROBES.
    return {"op": "set_title", "title": "Hook"}


def _op_captions(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    return {"op": "set_caption_meta", "patch": {"enabled": True}}


def _op_sfx(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    catalog = ((ctx.snapshot or {}).get("sfx") or {}).get("catalog") or []
    first = next((r for r in catalog if isinstance(r, Mapping) and r.get("id")), None)
    if first is None:
        return None
    total = (ctx.snapshot or {}).get("total_duration_s")
    at_s = 0.5 if not isinstance(total, (int, float)) or total <= 1.0 else min(0.5, total / 2)
    return {"op": "add_sfx", "effect_id": first["id"], "at_s": round(float(at_s), 2)}


def _op_music(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    level = _music_level(ctx)
    quieter = 0.2 if level is None else max(0.05, round(level * 0.6, 2))
    return {"op": "set_mix", "music_level": quieter}


def _op_transition(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    return {"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}


def _op_opener(ctx: SuggestionContext, target: _Target) -> dict[str, Any] | None:
    slots = _slots(ctx)
    for index, slot in enumerate(slots):
        if slot.get("media_id") == target.media_id and index > 0:
            return {"op": "reorder_clip", "from_index": index, "to_index": 0}
    return None


# ── The guard table ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChipKindSpec:
    """One row per kind. A test fails if any ``ChipKind`` lacks a row."""

    kind: ChipKind
    states: frozenset[str]
    capability: Mapping[str, str]  # state -> manifest capability key / editor op family
    available: Callable[[SuggestionContext], bool]
    already_present: Callable[[SuggestionContext], bool]
    ground: Callable[[SuggestionContext], list[_Target]]
    dry_run: Mapping[str, DryRunMode]  # state -> how executability is proven
    expects: str  # what a successful tap must change (the eval lane asserts this)
    op: Callable[[SuggestionContext, _Target], dict[str, Any] | None] | None = None
    # The editor op a tap is expected to cause (None: routes into a flow, not one op) and the
    # op the dry-run probes with. Guarded against CHIP_OP_ALLOW / CHIP_OP_WITNESS_PROBES.
    claimed_op: str | None = None
    witness_op: str | None = None
    # Lowest iOS protocol that can execute the chip; None = every client. Text bend (9) and
    # freehand drawing (6/7) have no chat op, so no kind carries those numbers.
    min_protocol: int | None = None
    # Tag the wording agent sees in ``already_in_edit`` when this kind is already satisfied.
    edit_tag: str = ""


_BOTH = frozenset({"first_prompt", "ready"})

CHIP_KIND_GATE: dict[ChipKind, ChipKindSpec] = {
    ChipKind.DEFAULT_EDIT: ChipKindSpec(
        kind=ChipKind.DEFAULT_EDIT,
        states=frozenset({"first_prompt"}),
        capability={"first_prompt": "dispatch_render"},
        available=_can_draft,
        already_present=lambda ctx: False,
        ground=_ground_none,
        dry_run={"first_prompt": "none"},
        expects="a first draft built by the no-prompt Main Creator path",
    ),
    ChipKind.HOOK: ChipKindSpec(
        kind=ChipKind.HOOK,
        states=_BOTH,
        capability={"first_prompt": "dispatch_render", "ready": "family:title"},
        available=_hook_available,
        already_present=_hook_present,
        ground=_ground_none,
        dry_run={"first_prompt": "strategy", "ready": "editor"},
        expects="an opening hook, through the creative-copy approval flow",
        op=_op_hook,
        witness_op="set_title",
        edit_tag="title",
    ),
    ChipKind.OPENER_CLIP: ChipKindSpec(
        kind=ChipKind.OPENER_CLIP,
        states=_BOTH,
        capability={"first_prompt": "dispatch_render", "ready": "family:clip"},
        available=_opener_available,
        already_present=_opener_present,
        ground=_ground_opener,
        dry_run={"first_prompt": "strategy", "ready": "editor"},
        expects="the named clip plays first",
        op=_op_opener,
        claimed_op="reorder_clip",
        witness_op="reorder_clip",
        edit_tag="opener_set",
    ),
    ChipKind.CAPTIONS: ChipKindSpec(
        kind=ChipKind.CAPTIONS,
        states=_BOTH,
        capability={"first_prompt": "footage:speech", "ready": "family:caption"},
        available=_captions_available,
        already_present=_captions_present,
        ground=_ground_captions,
        dry_run={"first_prompt": "strategy", "ready": "editor"},
        expects="captions burned on the speech",
        op=_op_captions,
        claimed_op="set_caption_meta",
        witness_op="set_caption_meta",
        edit_tag="captions",
    ),
    ChipKind.SFX: ChipKindSpec(
        kind=ChipKind.SFX,
        states=_BOTH,
        capability={"first_prompt": "sound_effects", "ready": "family:sfx"},
        available=_sfx_available,
        already_present=_sfx_present,
        ground=_ground_none,
        dry_run={"first_prompt": "strategy", "ready": "editor"},
        expects="sound-effect placements on the timeline",
        op=_op_sfx,
        claimed_op="add_sfx",
        witness_op="add_sfx",
        edit_tag="sfx",
    ),
    ChipKind.MUSIC: ChipKindSpec(
        kind=ChipKind.MUSIC,
        states=frozenset({"ready"}),
        capability={"ready": "family:music"},
        available=_music_available,
        already_present=_music_already_quiet,
        ground=_ground_music,
        dry_run={"ready": "editor"},
        expects="a lower music level (mix.music_level)",
        op=_op_music,
        claimed_op="set_mix",
        witness_op="set_mix",
        edit_tag="quiet_music",
    ),
    ChipKind.TRANSITION: ChipKindSpec(
        kind=ChipKind.TRANSITION,
        states=_BOTH,
        capability={"first_prompt": "transitions", "ready": "family:transition"},
        available=_transition_available,
        already_present=_transition_present,
        ground=_ground_none,
        dry_run={"first_prompt": "strategy", "ready": "editor"},
        expects="transitions on the clip boundaries",
        op=_op_transition,
        claimed_op="set_transition",
        witness_op="set_transition",
        edit_tag="transitions",
    ),
    ChipKind.TITLE_ANIMATION: ChipKindSpec(
        kind=ChipKind.TITLE_ANIMATION,
        states=frozenset({"ready"}),
        capability={"ready": "family:text"},
        available=_title_anim_available,
        already_present=_title_anim_present,
        ground=_ground_title_anim,
        # The compiler cannot prove an entrance animation, so this kind is allow-listed:
        # guided_story / unified_montage only, a title bar present, text family allowed.
        dry_run={"ready": "allowlist"},
        expects="an entrance animation on the existing title",
        claimed_op="patch_text",
        edit_tag="title_animation",
    ),
    ChipKind.PINNED_TEXT: ChipKindSpec(
        kind=ChipKind.PINNED_TEXT,
        states=frozenset(),  # dark: PINNED_TEXT_PROVEN is False
        capability={},
        available=_pinned_available,
        already_present=lambda ctx: False,
        ground=lambda ctx: [],
        dry_run={},
        expects="a corner line, through the creative-copy approval flow",
        edit_tag="pinned_text",
    ),
}


_WITNESS_TIER: dict[str, str] = {
    "none": "policy_only",
    "allowlist": "policy_only",
    "strategy": "strategy",
    "editor": "editor",
}


# ── Ranking ──────────────────────────────────────────────────────────────────────────

_BASE_SCORE = {
    ChipKind.HOOK: 80,
    ChipKind.CAPTIONS: 70,
    ChipKind.OPENER_CLIP: 60,
    ChipKind.SFX: 50,
    ChipKind.TRANSITION: 40,
    ChipKind.MUSIC: 35,
    ChipKind.TITLE_ANIMATION: 30,
}


def _score(ctx: SuggestionContext, kind: ChipKind) -> int:
    score = _BASE_SCORE.get(kind, 0)
    speech = _any_speech(ctx)
    if kind is ChipKind.CAPTIONS and speech:
        score += 40  # speech + no captions outranks the hook (110 > 105)
    if kind is ChipKind.HOOK and (ctx.state == "first_prompt" or _title_bar(ctx) is None):
        score += 25
    if kind is ChipKind.OPENER_CLIP and len(_live_clips(ctx)) >= 2:
        score += 20
    if kind in (ChipKind.SFX, ChipKind.TRANSITION) and not speech:
        score += 15
    if kind is ChipKind.MUSIC and _any_to_camera(ctx):
        score += 25
    return score


# ── Candidates ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One chip the engine would allow. The agent can only choose among these."""

    cid: str
    kind: ChipKind
    capability: str | None
    target: _Target
    message: str
    fallback_label: str
    facts: Mapping[str, Any]
    label_slots: Mapping[str, str]
    min_protocol: int | None
    witness_ok: bool
    witness_tier: str
    gap_tags: tuple[str, ...]
    score: int = 0
    touched: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CandidatePlan:
    state: str
    understanding: str
    base_digest: str
    input_digest: str
    set_id: str
    default: Candidate | None
    levers: tuple[Candidate, ...]
    already_in_edit: tuple[str, ...]

    @property
    def pool(self) -> tuple[Candidate, ...]:
        return self.levers[:MAX_POOL]


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _sha(*parts: object, size: int) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:size]


def _protocol_ok(ctx: SuggestionContext, spec: ChipKindSpec) -> bool:
    if spec.min_protocol is None or ctx.client_protocol is None:
        return True  # unknown client: emit with requires_protocol, the app drops it locally
    return ctx.client_protocol >= spec.min_protocol


def _render_copy(
    kind: ChipKind, language: str, target: _Target
) -> tuple[str, str, dict[str, str]] | None:
    """``(label, message, slots)`` for a candidate, or None when it cannot be worded safely."""
    label, message = _copy(kind, language)
    slots: dict[str, str] = {}
    if kind is ChipKind.OPENER_CLIP:
        name = " ".join(target.filename.split())[:20].strip()
        if not name:
            return None
        slots["filename"] = name
    for key, value in slots.items():
        label = label.replace("{" + key + "}", value)
        message = message.replace("{" + key + "}", value)
    if (
        not label
        or len(label) > MAX_LABEL_CHARS
        or not message
        or len(message) > MAX_MESSAGE_CHARS
        or (kind is not ChipKind.OPENER_CLIP and any(ch in _QUOTES for ch in message))
    ):
        return None
    return label, message, slots


def _dry_run(spec: ChipKindSpec, ctx: SuggestionContext, target: _Target) -> frozenset[str] | None:
    mode = spec.dry_run.get(ctx.state, "none")
    if mode == "none":
        return frozenset()
    if mode == "allowlist":
        return frozenset({f"allow:{spec.kind}"}) if spec.available(ctx) else None
    if mode == "strategy":
        witness = ctx.strategy_witness
        return witness(spec.kind) if witness is not None else None
    witness = ctx.editor_witness
    op = spec.op(ctx, target) if spec.op is not None else None
    if witness is None or op is None:
        return None
    return witness(op)


def _gap_tags(ctx: SuggestionContext, kind: ChipKind) -> tuple[str, ...]:
    speech = _any_speech(ctx)
    if kind is ChipKind.HOOK:
        return ("opening_unwritten",) if ctx.state == "first_prompt" else ("no_title",)
    if kind is ChipKind.CAPTIONS:
        return ("speech_uncaptioned",)
    if kind is ChipKind.SFX:
        return ("no_sfx", "silent_footage") if not speech else ("no_sfx",)
    if kind is ChipKind.TRANSITION:
        return ("hard_cuts",)
    if kind is ChipKind.MUSIC:
        return ("music_competes_with_voice",)
    if kind is ChipKind.TITLE_ANIMATION:
        return ("static_title",)
    if kind is ChipKind.OPENER_CLIP:
        return ("strong_clip_late",)
    return ()


def _facts(ctx: SuggestionContext, kind: ChipKind) -> dict[str, Any]:
    """Structured facts for the agent. Handles only (``c1``): never ids, times or words."""
    refs = {id(c): f"c{c.get('ordinal')}" for c in _live_clips(ctx)}
    speech_refs = [
        refs[id(c)] for c in _live_clips(ctx) if c.get("has_speech") or c.get("to_camera")
    ]
    silent_refs = [
        refs[id(c)] for c in _live_clips(ctx) if not (c.get("has_speech") or c.get("to_camera"))
    ]
    if kind is ChipKind.CAPTIONS:
        return {"speech_clips": speech_refs[:FOOTAGE_CLIPS_MAX]}
    if kind is ChipKind.SFX:
        return {"silent_clips": silent_refs[:FOOTAGE_CLIPS_MAX]}
    if kind is ChipKind.TRANSITION:
        return {
            "clip_count": len(_live_clips(ctx)) if ctx.state == "first_prompt" else len(_slots(ctx))
        }
    if kind is ChipKind.MUSIC:
        level = _music_level(ctx)
        return {"music_level": level} if level is not None else {}
    return {}


def _already_in_edit(ctx: SuggestionContext) -> tuple[str, ...]:
    tags: list[str] = []
    for kind, spec in CHIP_KIND_GATE.items():
        if not spec.edit_tag or ctx.state not in spec.states:
            continue
        try:
            if spec.already_present(ctx):
                tags.append(spec.edit_tag)
        except Exception:  # noqa: BLE001 - a tag is context for the agent, never a gate
            continue
    return tuple(sorted(set(tags)))


def _base_digest(ctx: SuggestionContext, state: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "media": ctx.media_digest,
                "manifest": ctx.manifest_hash,
                "generation": ctx.render_generation_id,
                "variant": ctx.variant_id,
                "state": state,
                "language": language_of(ctx.language),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]


def _empty_plan(ctx: SuggestionContext, state: str) -> CandidatePlan:
    base = _base_digest(ctx, state)
    digest = _input_digest(base, [])
    return CandidatePlan(
        state=state,
        understanding=ctx.understanding,
        base_digest=base,
        input_digest=digest,
        set_id=_set_id(ctx.thread_id, digest),
        default=None,
        levers=(),
        already_in_edit=(),
    )


def _input_digest(base: str, cids: Sequence[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"base": base, "cids": list(cids), "prompt_version": PROMPT_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]


def _set_id(thread_id: str, input_digest: str) -> str:
    return f"sg_{_sha(thread_id, input_digest, size=16)}"


def _effective_state(ctx: SuggestionContext) -> str:
    if ctx.state == "first_prompt":
        # Footage-only first prompt: needs at least one video. Photos or slides => no chips.
        return "first_prompt" if _video_clips(ctx) else "none"
    return ctx.state if ctx.state == "ready" else "none"


def _make_candidate(
    spec: ChipKindSpec,
    ctx: SuggestionContext,
    target: _Target,
    base: str,
    *,
    touched: frozenset[str],
    witness_ok: bool,
    score: int,
) -> Candidate | None:
    copy = _render_copy(spec.kind, ctx.language, target)
    if copy is None:
        return None
    label, message, slots = copy
    return Candidate(
        cid=f"sc_{_sha(spec.kind, target.key, base, size=12)}",
        kind=spec.kind,
        capability=spec.capability.get(ctx.state) or None,
        target=target,
        message=message,
        fallback_label=label,
        facts=_facts(ctx, spec.kind),
        label_slots=slots,
        min_protocol=spec.min_protocol,
        witness_ok=witness_ok,
        witness_tier=_WITNESS_TIER.get(spec.dry_run.get(ctx.state, "none"), "policy_only"),
        gap_tags=_gap_tags(ctx, spec.kind),
        score=score,
        touched=touched,
    )


def _lever_rows(
    ctx: SuggestionContext, base: str
) -> list[tuple[int, ChipKindSpec, _Target, frozenset[str]]]:
    found: list[tuple[int, ChipKindSpec, _Target, frozenset[str]]] = []
    for kind, spec in CHIP_KIND_GATE.items():
        if kind is ChipKind.DEFAULT_EDIT or ctx.state not in spec.states:
            continue
        try:
            if not _protocol_ok(ctx, spec) or not spec.available(ctx) or spec.already_present(ctx):
                continue
            targets = spec.ground(ctx)
        except Exception:  # noqa: BLE001 - one kind failing never takes the set down
            log.warning("suggestion_chips.kind_error", kind=str(kind), exc_info=True)
            continue
        for target in targets:
            try:
                touched = _dry_run(spec, ctx, target)
            except Exception:  # noqa: BLE001
                log.warning("suggestion_chips.witness_error", kind=str(kind), exc_info=True)
                touched = None
            if touched is None:
                continue
            found.append((_score(ctx, kind), spec, target, touched))
            break  # one chip per kind: the first grounded + proven target
    found.sort(key=lambda row: -row[0])
    return found


def build_candidates(ctx: SuggestionContext) -> CandidatePlan:
    """Pure, I/O-free, LLM-free, never raises. The set of chips that MAY exist for ``ctx``."""
    try:
        return _build_candidates(ctx)
    except Exception:  # noqa: BLE001 - fail open to "render nothing"
        log.warning("suggestion_chips.error", stage="candidates", exc_info=True)
        return _empty_plan(ctx, "none")


def _build_candidates(ctx: SuggestionContext) -> CandidatePlan:
    state = _effective_state(ctx)
    if state == "none":
        return _empty_plan(ctx, "none")
    base = _base_digest(ctx, state)
    scoped = ctx if ctx.state == state else SuggestionContext(**{**ctx.__dict__, "state": state})
    default: Candidate | None = None
    levers: list[Candidate] = []
    if state == "first_prompt":
        spec = CHIP_KIND_GATE[ChipKind.DEFAULT_EDIT]
        if not spec.available(scoped) or not spec.ground(scoped):
            return _empty_plan(ctx, "first_prompt")
        default = _make_candidate(
            spec, scoped, _NO_TARGET, base, touched=frozenset(), witness_ok=True, score=100
        )
        if default is None:
            return _empty_plan(ctx, "first_prompt")
    if state == "ready" or ctx.understanding == "complete":
        seen_targets: set[str] = set()
        copy_bearing = 0
        for score, spec, target, touched in _lever_rows(scoped, base):
            # Two chips must not touch the same edit target (director-style dedupe).
            if touched & seen_targets:
                continue
            if spec.kind in COPY_BEARING:
                if copy_bearing:
                    continue
                copy_bearing += 1
            cand = _make_candidate(
                spec, scoped, target, base, touched=touched, witness_ok=True, score=score
            )
            if cand is None:
                continue
            seen_targets |= touched
            levers.append(cand)
    cids = ([default.cid] if default else []) + [c.cid for c in levers]
    digest = _input_digest(base, cids)
    return CandidatePlan(
        state=state,
        understanding=ctx.understanding,
        base_digest=base,
        input_digest=digest,
        set_id=_set_id(ctx.thread_id, digest),
        default=default,
        levers=tuple(levers),
        already_in_edit=_already_in_edit(scoped),
    )


# ── Wording: validated agent output joined onto live candidates ──────────────────────


@dataclass(frozen=True)
class RankedWording:
    cid: str
    label_template: str
    why: str | None = None


@dataclass(frozen=True)
class Wording:
    """Agent wording that already passed the structural checks (cids are a subset of the pool)."""

    status: Literal["agent", "fallback"] = "fallback"
    default_label: str | None = None
    ranked: tuple[RankedWording, ...] = ()
    prompt_version: str = PROMPT_VERSION
    fallback_reason: str | None = None


@dataclass
class WordingStats:
    n_in: int = 0
    n_out: int = 0
    n_dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.n_dropped += 1
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1


def wording_to_dict(wording: Wording) -> dict[str, Any]:
    return {
        "status": wording.status,
        "default_label": wording.default_label,
        "ranked": [
            {"cid": r.cid, "label_template": r.label_template, "why": r.why} for r in wording.ranked
        ],
        "prompt_version": wording.prompt_version,
        "fallback_reason": wording.fallback_reason,
    }


def wording_from_dict(raw: object) -> Wording | None:
    """Parse a stored wording; anything malformed is None (the caller falls back)."""
    if not isinstance(raw, Mapping):
        return None
    try:
        status = "agent" if raw.get("status") == "agent" else "fallback"
        ranked = tuple(
            RankedWording(
                cid=str(row["cid"]),
                label_template=str(row.get("label_template") or ""),
                why=str(row["why"]) if row.get("why") else None,
            )
            for row in raw.get("ranked") or []
            if isinstance(row, Mapping) and row.get("cid")
        )
        default = raw.get("default_label")
        return Wording(
            status=status,
            default_label=str(default) if isinstance(default, str) and default else None,
            ranked=ranked,
            prompt_version=str(raw.get("prompt_version") or PROMPT_VERSION),
            fallback_reason=(str(raw["fallback_reason"]) if raw.get("fallback_reason") else None),
        )
    except (KeyError, TypeError, ValueError):
        return None


def resolve_wording(
    ctx: SuggestionContext,
    plan: CandidatePlan,
    *,
    default_label: str | None,
    ranked: Sequence[tuple[str, str, str | None]],
) -> tuple[Wording, WordingStats]:
    """Validate raw agent output against LIVE candidates. Pure; never raises.

    ``ranked`` is ``[(cid, label_template, why)]`` in the agent's order. A cid not in the pool,
    a duplicate kind or an over-limit item is dropped; a label that fails the deterministic
    validator keeps the chip with its template label; a ``why`` that is not grounded in the
    footage is cleared. The model can therefore never add, rename or re-target a chip.
    """
    stats = WordingStats(n_in=len(ranked) + (1 if default_label else 0))
    pool = {c.cid: c for c in plan.pool}
    vocab = footage_vocabulary(_live_clips(ctx))
    cap = MAX_LEVERS.get(plan.state, MAX_CHIPS)
    kept: list[RankedWording] = []
    seen_kinds: set[ChipKind] = set()
    copy_bearing = 0
    for cid, template, why in ranked:
        cand = pool.get(cid)
        if cand is None:
            stats.drop("unknown_cid")
            continue
        if cand.kind in seen_kinds:
            stats.drop("duplicate_kind")
            continue
        if cand.kind in COPY_BEARING:
            if copy_bearing:
                stats.drop("copy_bearing")
                continue
            copy_bearing += 1
        if len(kept) >= cap:
            stats.drop("over_limit")
            continue
        label_ok, label_reason = validate_label(
            template, kind=str(cand.kind), language=ctx.language, slots=cand.label_slots
        )
        if label_ok is None:
            stats.drop(f"label_{label_reason}")
            template = ""
        why_ok: str | None = None
        if why:
            why_ok, why_reason = validate_why(
                why, language=ctx.language, vocabulary=vocab, kind=str(cand.kind)
            )
            if why_ok is None:
                stats.drop(f"why_{why_reason}")
        seen_kinds.add(cand.kind)
        kept.append(RankedWording(cid=cid, label_template=template, why=why_ok))
    resolved_default: str | None = None
    if default_label:
        ok, reason = validate_label(
            default_label, kind=str(ChipKind.DEFAULT_EDIT), language=ctx.language, slots={}
        )
        if ok is None:
            stats.drop(f"default_{reason}")
        else:
            resolved_default = default_label
    stats.n_out = len(kept) + (1 if resolved_default else 0)
    usable = any(r.label_template or r.why for r in kept) or resolved_default
    wording = Wording(
        status="agent" if kept or resolved_default else "fallback",
        default_label=resolved_default,
        ranked=tuple(kept),
        fallback_reason=None if usable else "label_rejected",
    )
    return wording, stats


# ── Assembly ─────────────────────────────────────────────────────────────────────────


def _anchor(ctx: SuggestionContext) -> SuggestionAnchorOut:
    return SuggestionAnchorOut(
        media_digest=ctx.media_digest,
        manifest_hash=ctx.manifest_hash,
        render_generation_id=ctx.render_generation_id,
        variant_id=ctx.variant_id,
    )


def _understanding_out(ctx: SuggestionContext, plan: CandidatePlan) -> str:
    if plan.state == "first_prompt":
        return {"complete": "done", "pending": "pending"}.get(ctx.understanding, "empty")
    return "done" if _live_clips(ctx) or plan.state == "ready" else "empty"


def _chip(
    cand: Candidate,
    *,
    rank: int,
    label: str,
    source: str,
    why: str | None,
    default_edit: bool,
) -> SuggestionChipOut:
    moment = (
        ChipMomentOut(start_s=cand.target.moment[0], end_s=cand.target.moment[1])
        if cand.target.moment
        else None
    )
    return SuggestionChipOut(
        id=cand.cid,
        kind=str(cand.kind),
        label=label,
        message=cand.message,
        rank=rank,
        default_edit=default_edit,
        capability=cand.capability,
        anchor=ChipAnchorOut(
            media_id=cand.target.media_id,
            clip_ordinal=cand.target.clip_ordinal,
            moment=moment,
            text_bar_index=cand.target.text_bar_index,
        ),
        why=why,
        label_source=source,
        requires_protocol=cand.min_protocol,
    )


def _worded(
    cand: Candidate,
    ctx: SuggestionContext,
    template: str | None,
    why: str | None,
    vocab: frozenset[str],
) -> tuple[str, str, str | None]:
    """``(label, label_source, why)`` re-validated at assembly time (never trust the store)."""
    label, source = cand.fallback_label, "template"
    if template:
        rendered, _reason = validate_label(
            template, kind=str(cand.kind), language=ctx.language, slots=cand.label_slots
        )
        if rendered is not None:
            label, source = rendered, "agent"
    checked_why: str | None = None
    if why:
        checked_why, _why_reason = validate_why(
            why, language=ctx.language, vocabulary=vocab, kind=str(cand.kind)
        )
    return label, source, checked_why


_EVIDENCE_MAX = 48


def _evidence(cand: Candidate, ctx: SuggestionContext, state: str) -> str | None:
    """A one-line, deterministic reason this chip fits THIS footage (KRI-769).

    Built only from counts and edit state the server already holds (never Gemini prose, never
    ids or times), so it needs no model and cannot invent a moment. Every line states a fact the
    server measured, not a judgement about quality. Shown as the chip's subtitle whenever the
    wording agent supplied no grounded ``why`` of its own.
    """
    tr = language_of(ctx.language) == "tr"
    clips = _live_clips(ctx)
    total = len(clips)
    speech = sum(1 for c in clips if c.get("has_speech") or c.get("to_camera"))
    quiet = total - speech
    kind = cand.kind
    line: str | None = None
    if kind is ChipKind.DEFAULT_EDIT:
        line = "Kesimi ve temposunu Kria seçer" if tr else "Kria picks the cuts and pace"
    elif kind is ChipKind.CAPTIONS and speech:
        if speech == total:
            line = "Her klipte konuşma var" if tr else "Speech in every clip"
        else:
            line = (
                f"{speech}/{total} klipte konuşma var"
                if tr
                else f"Speech in {speech} of {total} clips"
            )
    elif kind is ChipKind.SFX:
        if quiet > 0:
            line = (
                f"{quiet} klipte konuşma yok"
                if tr
                else ("1 clip has no speech" if quiet == 1 else f"{quiet} clips have no speech")
            )
        else:
            line = "Henüz ses efekti yok" if tr else "No sound effects yet"
    elif kind is ChipKind.TRANSITION:
        cuts = len(_slots(ctx)) if state == "ready" else total
        if cuts > 1:
            if state == "ready":
                line = (
                    f"{cuts} klip sert kesmelerle bağlı"
                    if tr
                    else f"{cuts} clips joined by hard cuts"
                )
            else:
                line = (
                    f"Birleştirilecek {cuts} klip var" if tr else f"{cuts} clips ready to combine"
                )
    elif kind is ChipKind.HOOK:
        if state == "first_prompt":
            line = "Henüz bir açılış cümlesi yok" if tr else "No opening line yet"
        else:
            line = "Henüz başlık yok" if tr else "There is no title yet"
    elif kind is ChipKind.MUSIC:
        line = "Konuşmalı klipler ve müzik var" if tr else "Speech clips and a music track"
    elif kind is ChipKind.TITLE_ANIMATION:
        line = "Başlık sabit duruyor" if tr else "The title just sits there"
    return line if line and len(line) <= _EVIDENCE_MAX else None


def assemble_set(
    ctx: SuggestionContext,
    plan: CandidatePlan,
    wording: Wording | None = None,
    *,
    pending: bool = False,
    retry_after_s: int | None = None,
) -> SuggestionSetOut:
    """Join a plan with (optional) validated wording. Pure; labels are re-validated here."""
    understanding = _understanding_out(ctx, plan)
    if plan.state == "none":
        return SuggestionSetOut(
            set_id=plan.set_id,
            state="none",
            status="ok",
            understanding=understanding,
            wording="fallback",
            anchor=_anchor(ctx),
            chips=[],
        )
    vocab = footage_vocabulary(_live_clips(ctx))
    cap = MAX_LEVERS[plan.state]
    chosen: list[tuple[Candidate, str, str, str | None]] = []
    used_agent = False

    if plan.default is not None:
        template = wording.default_label if wording and not pending else None
        label, source, _ = _worded(plan.default, ctx, template, None, vocab)
        used_agent = used_agent or source == "agent"
        chosen.append((plan.default, label, source, None))

    if not pending:
        by_cid = {c.cid: c for c in plan.pool}
        if wording is not None and wording.status == "agent":
            seen: set[ChipKind] = set()
            for row in wording.ranked:
                cand = by_cid.get(row.cid)
                if (
                    cand is None
                    or cand.kind in seen
                    or len(chosen) - (1 if plan.default else 0) >= cap
                ):
                    continue
                label, source, why = _worded(cand, ctx, row.label_template, row.why, vocab)
                seen.add(cand.kind)
                used_agent = used_agent or source == "agent" or why is not None
                chosen.append((cand, label, source, why))
        if len(chosen) == (1 if plan.default else 0):
            # No usable agent ranking: the deterministic top of the pool, template labels.
            for cand in plan.levers[:cap]:
                chosen.append((cand, cand.fallback_label, "template", None))

    chips = [
        _chip(
            cand,
            rank=index + 1,
            label=label,
            source=source,
            why=why or _evidence(cand, ctx, plan.state),
            default_edit=cand.kind is ChipKind.DEFAULT_EDIT and plan.state == "first_prompt",
        )
        for index, (cand, label, source, why) in enumerate(chosen)
    ]
    if pending:
        wording_state = "pending"
    else:
        wording_state = "agent" if used_agent else "fallback"
    return SuggestionSetOut(
        set_id=plan.set_id,
        state=plan.state,
        status="pending" if pending else "ok",
        understanding=understanding,
        retry_after_s=retry_after_s if pending else None,
        wording=wording_state,
        anchor=_anchor(ctx),
        chips=chips,
    )


def build_suggestion_set(
    ctx: SuggestionContext, wording: Wording | None = None
) -> SuggestionSetOut:
    """Pure, I/O-free, LLM-free, never raises: candidates + (optional) wording, one call.

    With no wording this is the template-label set. Understanding still running yields the
    ``pending`` set (``default_edit`` only, ``retry_after_s``).
    """
    try:
        plan = _build_candidates(ctx)
        if plan.state == "first_prompt" and ctx.understanding == "pending":
            return assemble_set(ctx, plan, None, pending=True, retry_after_s=PENDING_RETRY_S)
        return assemble_set(ctx, plan, wording)
    except Exception:  # noqa: BLE001 - fail open to "render nothing"
        log.warning("suggestion_chips.error", stage="build", exc_info=True)
        return unavailable_set()


# ── Agent input ──────────────────────────────────────────────────────────────────────


def _clean_phrase(value: object, *, words: int, chars: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isprintable() and ch not in "{}$`\\")
    return " ".join(text.split()[:words])[:chars].strip()


def build_agent_input(ctx: SuggestionContext, plan: CandidatePlan) -> dict[str, Any]:
    """The ``ChipSuggesterInput`` payload: opaque handles, truncated Gemini text, no ids.

    ``c1..cN`` name clips and ``c2.m1`` names a moment; the server maps them back, so the
    model never types a media id or a timestamp. Transcripts and creator-typed text are
    never sent at first_prompt; at ready only the creator's own latest request is.
    """
    footage: list[dict[str, Any]] = []
    for clip in _live_clips(ctx)[:FOOTAGE_CLIPS_MAX]:
        ordinal = clip.get("ordinal")
        if not isinstance(ordinal, int):
            continue
        ref = f"c{ordinal}"
        speech = (
            "to_camera"
            if clip.get("to_camera")
            else "voiceover"
            if clip.get("has_speech")
            else "none"
        )
        moments = []
        for index, moment in enumerate((clip.get("notable_moments") or [])[:MOMENTS_PER_CLIP], 1):
            if not isinstance(moment, Mapping):
                continue
            description = _clean_phrase(
                moment.get("label") or moment.get("description"), words=12, chars=MOMENT_CHARS
            )
            if description:
                moments.append({"ref": f"{ref}.m{index}", "description": description})
        footage.append(
            {
                "ref": ref,
                "kind": "image" if clip.get("kind") == "image" else "video",
                "subject": _clean_phrase(clip.get("subject"), words=SUBJECT_WORDS, chars=48),
                "setting": _clean_phrase(clip.get("setting"), words=SETTING_WORDS, chars=60),
                "speech": speech,
                "moments": moments,
            }
        )
    payload: dict[str, Any] = {
        "state": plan.state,
        "language": language_of(ctx.language),
        "max_chips": MAX_LEVERS.get(plan.state, MAX_CHIPS),
        "footage": footage,
        "already_in_edit": list(plan.already_in_edit),
        "candidates": [
            {
                "cid": c.cid,
                "kind": str(c.kind),
                "gap_tags": list(c.gap_tags),
                "facts": dict(c.facts),
                "label_slots": sorted(c.label_slots),
                "fallback_label": c.fallback_label,
            }
            for c in plan.pool
        ],
    }
    if plan.state == "ready" and ctx.typed_request:
        payload["typed_request"] = " ".join(ctx.typed_request.split())[:200]
    return payload


# ── Tap detection ────────────────────────────────────────────────────────────────────


def _ring(thread: Any) -> list[Mapping[str, Any]]:
    state = getattr(thread, "state", None)
    ring = state.get(RING_KEY) if isinstance(state, Mapping) else None
    return [e for e in ring if isinstance(e, Mapping)] if isinstance(ring, list) else []


def _stamp(entry: Mapping[str, Any], chip: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "set_id": str(entry.get("set_id") or ""),
        "chip_id": str(chip.get("id") or ""),
        "kind": str(chip.get("kind") or ""),
        "rank": int(chip.get("rank") or 0),
        "state": str(entry.get("state") or ""),
        "label_source": str(chip.get("label_source") or "template"),
        "prompt_version": entry.get("prompt_version"),
        "default_edit": bool(chip.get("default_edit")),
    }


def match_offered_suggestion(
    thread: Any,
    message: str,
    *,
    media_digest: str | None = None,
    suggestion: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """The chip a message equals, from the sets offered on this thread, or None.

    An explicit ``suggestion={"set_id","chip_id"}`` is validated against the offered-set ring
    first (and the message must still equal that chip's message, so a stamp can never claim a
    chip the creator did not send). Absent or invalid, it falls back to the text match:
    casefold + whitespace-collapse, newest set first. When ``media_digest`` is given, only
    sets offered for the current media qualify.
    """
    wanted = _norm(message)
    ring = _ring(thread)
    if not wanted or not ring:
        return None

    def eligible(entry: Mapping[str, Any]) -> bool:
        return media_digest is None or entry.get("media_digest") == media_digest

    if suggestion:
        set_id, chip_id = str(suggestion.get("set_id") or ""), str(suggestion.get("chip_id") or "")
        for entry in reversed(ring):
            if entry.get("set_id") != set_id or not eligible(entry):
                continue
            for chip in entry.get("chips") or []:
                if (
                    isinstance(chip, Mapping)
                    and chip.get("id") == chip_id
                    and _norm(str(chip.get("message") or "")) == wanted
                ):
                    return _stamp(entry, chip)
    for entry in reversed(ring):
        if not eligible(entry):
            continue
        for chip in entry.get("chips") or []:
            if isinstance(chip, Mapping) and _norm(str(chip.get("message") or "")) == wanted:
                return _stamp(entry, chip)
    return None


async def suggestion_tap_payload(
    db: Any, thread: Any, message: str, *, suggestion: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """``{}`` or ``{"suggestion": {...}}`` to splice into a user_message payload.

    Flag off (or no offered sets) returns ``{}`` before touching the database, so every
    existing payload and ``request_digest`` stays byte-identical. Never raises.
    """
    from app.config import settings  # noqa: PLC0415

    try:
        if not settings.suggestion_chips_for(getattr(thread, "creator_id", None)):
            return {}
        if not _ring(thread):
            return {}
        if match_offered_suggestion(thread, message, suggestion=suggestion) is None:
            return {}
        from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
        from app.models import PlanItem  # noqa: PLC0415
        from app.services.creative_copy_decisions import media_digest as digest_of  # noqa: PLC0415

        item_id = getattr(thread, "active_plan_item_id", None)
        item = await db.get(PlanItem, item_id) if item_id else None
        current = digest_of(snapshot_media(item)) if item is not None else None
        matched = match_offered_suggestion(
            thread, message, media_digest=current, suggestion=suggestion
        )
        if matched is None:
            return {}
        log.info(
            "suggestion_chips.tap",
            thread_id=str(getattr(thread, "id", "")),
            **matched,
        )
        return {"suggestion": matched}
    except Exception:  # noqa: BLE001 - tap stamping must never fail a message
        log.warning("suggestion_chips.error", stage="tap", exc_info=True)
        return {}


# ── Persistence: the offered-set ring on thread.state (revision-neutral) ─────────────


async def persist_suggestion_set(
    db: Any,
    thread_id: Any,
    out: SuggestionSetOut,
    *,
    media_digest: str,
    protocol: int | None,
    input_digest: str = "",
    witness_tiers: Mapping[str, str] | None = None,
) -> bool:
    """Record ``out`` in the ring. Returns True when the ring changed.

    A set_id is written once as ``final: false`` while its labels are pending and replaced
    in place by the final set (same set_id, same chip ids), so a tap on the pending
    ``default_edit`` chip is attributed too. A final entry is never rewritten.

    No event, no revision bump, no migration. The row is taken ``FOR UPDATE SKIP LOCKED``
    so the GET never waits behind a turn that holds the thread. A locked row is retried
    twice (0.1s, 0.25s, so at most ~0.35s of added latency); if it is still locked the write
    is dropped and logged as ``persist_skipped``: that tap is simply not attributed
    (telemetry loss only; the chip still works). ``updated_at`` is pinned to its own value so
    a read does not reorder the creator's project list. Never use ``record_pipeline_event``
    here (the FOR UPDATE self-deadlock trap): structlog only.
    """
    from sqlalchemy import select, update  # noqa: PLC0415

    from app.models import CreationThread  # noqa: PLC0415

    if out.status not in ("ok", "pending") or not out.chips or not out.set_id:
        return False
    final = out.status == "ok"
    row = None
    for delay in (0.1, 0.25, None):
        row = (
            await db.execute(
                select(CreationThread.state)
                .where(CreationThread.id == thread_id)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if row is not None:
            break
        await db.rollback()
        if delay is not None:
            await asyncio.sleep(delay)
    if row is None:
        log.info("suggestion_chips.persist_skipped", thread_id=str(thread_id), set_id=out.set_id)
        return False
    state = dict(row[0] or {})
    ring = [e for e in state.get(RING_KEY) or [] if isinstance(e, Mapping)]
    existing = next((e for e in ring if e.get("set_id") == out.set_id), None)
    if existing is not None and (existing.get("final") or not final):
        await db.rollback()
        return False
    from datetime import UTC, datetime  # noqa: PLC0415

    entry = {
        "set_id": out.set_id,
        "state": out.state,
        "digest": input_digest,
        "media_digest": media_digest,
        "created_at": datetime.now(UTC).isoformat(),
        "final": final,
        "wording": out.wording,
        "prompt_version": PROMPT_VERSION,
        "chips": [
            {
                "id": c.id,
                "kind": c.kind,
                "rank": c.rank,
                "message": c.message,
                "label_source": c.label_source,
                "default_edit": c.default_edit,
            }
            for c in out.chips
        ],
    }
    ring = [e for e in ring if e.get("set_id") != out.set_id] + [entry]
    state[RING_KEY] = ring[-RING_SIZE:]
    await db.execute(
        update(CreationThread)
        .where(CreationThread.id == thread_id)
        .values(state=state, updated_at=CreationThread.updated_at)
    )
    await db.commit()
    if final:
        log.info(
            "suggestion_chips.shown",
            thread_id=str(thread_id),
            set_id=out.set_id,
            state=out.state,
            wording=out.wording,
            kinds=[c.kind for c in out.chips],
            n=len(out.chips),
            protocol=protocol,
            witness_tiers=dict(witness_tiers or {}),
        )
    return True


# ── Loader: the only read path ───────────────────────────────────────────────────────

# Newest-first scan: events that say nothing about whether a question is open.
_BOOKKEEPING_EVENTS = frozenset(
    {
        "plan_block",
        "status_update",
        "thread_renamed",
        "editor_variant_selected",
        "editor_thread_linked",
        "media_added",
        "song_range_set",
        "speaker_background_set",
    }
)
_QUESTION_EVENTS = frozenset(
    {"assistant_question", "format_prompt", "approval_requested", "assistant_strategy"}
)
_OPEN_COPY_STATES = frozenset({"authorship", "write_my_own", "generate", "revise"})
_ACTIVE_TURN_STATUSES = (
    "pending",
    "queued",
    "planning",
    "executing",
    "awaiting_approval",
    "observing",
)


def _open_question(events: Sequence[tuple[str, Any]]) -> bool:
    """``events`` newest first: (event_type, payload)."""
    for event_type, payload in events:
        if event_type in _BOOKKEEPING_EVENTS:
            continue
        if event_type in _QUESTION_EVENTS:
            return True
        if isinstance(payload, Mapping):
            if payload.get("turn_value") == "question":
                return True
            if any("question" in str(key) and payload.get(key) for key in payload):
                return True
        return False
    return False


def _clip_rows(item: Any) -> list[dict[str, Any]]:
    from app.services.clip_file_names import clip_display_name  # noqa: PLC0415
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    for index, assignment in enumerate(getattr(item, "clip_assignments", None) or []):
        if not isinstance(assignment, dict):
            continue
        media_id = assignment.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            continue
        kind = assignment.get("kind") if assignment.get("kind") in {"video", "image"} else "video"
        record = clip_record(assignment.get("analysis"), kind=kind)
        duration = assignment.get("duration_s")
        rows.append(
            {
                "media_id": media_id,
                "ordinal": index + 1,
                "kind": kind,
                "subject": record.subject,
                "setting": record.setting,
                "has_speech": bool(record.speech.has_speech or record.speech.transcript),
                "to_camera": bool(record.speech.to_camera or record.people.speaks_to_camera),
                "duration_s": duration if isinstance(duration, (int, float)) else None,
                "filename": clip_display_name(assignment.get("source_filename")) or "",
                "notable_moments": [
                    {"start_s": m.start_s, "end_s": m.end_s, "label": m.description}
                    for m in record.notable_moments[:8]
                ],
                "_empty": record.is_empty(),
                "_incomplete": False,
                "_analysis": assignment.get("analysis"),
            }
        )
    return rows


def _understanding_state(rows: list[dict[str, Any]]) -> str:
    from app.config import settings  # noqa: PLC0415
    from app.services.clip_understanding import understanding_incomplete  # noqa: PLC0415

    if not rows:
        return "n/a"
    if all(r["_empty"] for r in rows) and not settings.kria_clip_understanding_enabled:
        return "empty"
    # Only videos are analysed in the background (attach enqueues video clips only), so an
    # unanalysed photo is never "still being looked at".
    if any(
        understanding_incomplete(r["_analysis"], kind=r["kind"])
        for r in rows
        if r["_empty"] and r["kind"] == "video"
    ):
        return "pending"
    return "empty" if all(r["_empty"] for r in rows) else "complete"


def _manifest_view(manifest: Any) -> dict[str, Any]:
    return {
        "capabilities": {
            name: {"available": bool(cap.available), "reason_code": cap.reason_code}
            for name, cap in (manifest.capabilities or {}).items()
        },
        "edit_format": manifest.edit_format,
        "render_program": manifest.render_program,
    }


def make_strategy_witness(manifest: Any) -> StrategyWitness:
    """Dry-run a synthetic strategy through the planner's own v2 strategy check.

    The baseline (no chip treatment) must pass first, else nothing is proven. A kind then
    passes only if the check does not refuse AND its treatment survives (v2 drops disabled
    optional treatments silently, so the before/after diff is the real test).
    """
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
    from app.kria.strategy_policy import (  # noqa: PLC0415
        CheckedStrategy,
        check_strategy_for_runtime_v2,
    )

    media_ids = [m.media_id for m in manifest.media]

    def build(**extra: Any) -> CreativeStrategy:
        return CreativeStrategy(
            edit_format=manifest.edit_format,
            render_program=manifest.render_program,
            selected_media_ids=media_ids,
            **extra,
        )

    def check(strategy: CreativeStrategy) -> Any:
        return check_strategy_for_runtime_v2(manifest, strategy)

    baseline = check(build())
    if not isinstance(baseline, CheckedStrategy):
        return lambda kind: None

    def witness(kind: ChipKind) -> frozenset[str] | None:
        try:
            if kind is ChipKind.HOOK:
                checked = check(build(opening_title="Preview"))
                ok = isinstance(checked, CheckedStrategy) and bool(checked.strategy.opening_title)
            elif kind is ChipKind.CAPTIONS:
                checked = check(build(caption_style="clean"))
                ok = (
                    isinstance(checked, CheckedStrategy)
                    and checked.strategy.caption_style == "clean"
                )
            elif kind is ChipKind.SFX:
                checked = check(build(optional_treatments=["sfx"]))
                ok = isinstance(checked, CheckedStrategy) and "sfx" in (
                    checked.strategy.optional_treatments
                )
            elif kind is ChipKind.TRANSITION:
                checked = check(build(optional_treatments=["transitions"]))
                ok = isinstance(checked, CheckedStrategy) and "transitions" in (
                    checked.strategy.optional_treatments
                )
            else:
                return None
        except Exception:  # noqa: BLE001 - a witness that cannot run proves nothing
            return None
        return frozenset({f"strategy:{kind}"}) if ok else None

    return witness


def make_editor_witness(
    job: Any, variant: dict[str, Any], snapshot: dict[str, Any]
) -> EditorWitness:
    """Dry-run one editor op through the copilot parser and the draft compiler.

    Runs on the REAL editor snapshot of the completed render. Passes only when the parser
    accepts the op against this snapshot AND the compiled bundle changes something real
    (``EditorDiff.real()`` non-empty).
    """
    from app.agents.edit_copilot import (  # noqa: PLC0415
        EditorOperationParseState,
        parse_editor_operation,
    )
    from app.agents.edit_director import _operation_targets  # noqa: PLC0415
    from app.services.kria_editor_ops import compile_editor_ops  # noqa: PLC0415

    def witness(op: dict[str, Any]) -> frozenset[str] | None:
        try:
            parsed = parse_editor_operation(dict(op), snapshot, EditorOperationParseState(1.0))
            if not parsed:
                return None
            compiled = compile_editor_ops(job, variant, [parsed])
            if compiled.diff.unavailable or not compiled.diff.real():
                return None
            return frozenset(_operation_targets(parsed)) or frozenset({f"op:{parsed.get('op')}"})
        except Exception:  # noqa: BLE001 - refused or crashed => not proven
            return None

    return witness


async def load_suggestion_context(
    db: Any,
    thread: Any,
    user: Any,
    client_protocol: int | None,
    *,
    locale: str | None = None,
    language_hint: str | None = None,
) -> SuggestionContext:
    """Read the thread, footage and edit into a ``SuggestionContext``. Wait-free.

    ``state`` is server-derived (see docs/pipelines/suggestion-chips.md):

    * ``first_prompt``  media attached, no user_message, no job, no open question.
    * ``ready``         a completed render with an editor target and nothing pending.
    * ``none``          anything else (open question, render in flight/failed, no target).

    ``language_hint`` is the language the GET last saw (the worker has no request headers).
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.kria.brief_binding import snapshot_media  # noqa: PLC0415
    from app.kria.reply_language import locale_language, thread_reply_language  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        ContentPlan,
        CreationThreadEvent,
        CreatorAgentTurn,
        Persona,
        PlanItem,
    )
    from app.services.creative_copy_decisions import media_digest  # noqa: PLC0415

    thread_id = str(thread.id)
    language = language_of(
        thread_reply_language(thread) or locale_language(locale) or language_hint or "en"
    )
    creator_id = str(getattr(thread, "creator_id", "") or "") or None
    base = SuggestionContext(
        thread_id=thread_id,
        state="none",
        language=language,
        client_protocol=client_protocol,
        creator_id=creator_id,
    )
    if thread.status != "active" or not thread.active_plan_item_id:
        return base
    item = await db.get(PlanItem, thread.active_plan_item_id)
    plan = await db.get(ContentPlan, item.content_plan_id) if item is not None else None
    persona = await db.get(Persona, plan.persona_id) if plan is not None else None
    if item is None or plan is None or persona is None or plan.user_id != thread.creator_id:
        return base
    digest = media_digest(snapshot_media(item))
    rows = _clip_rows(item)
    if not rows:
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})

    user_messages = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread.id,
                    CreationThreadEvent.event_type == "user_message",
                )
            )
        ).scalar_one()
    )
    recent = (
        await db.execute(
            select(CreationThreadEvent.event_type, CreationThreadEvent.payload)
            .where(CreationThreadEvent.thread_id == thread.id)
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(12)
        )
    ).all()
    open_turn = (
        await db.execute(
            select(CreatorAgentTurn.id)
            .where(
                CreatorAgentTurn.thread_id == thread.id,
                CreatorAgentTurn.status.in_(_ACTIVE_TURN_STATUSES),
            )
            .limit(1)
        )
    ).first()
    has_job = bool(item.current_job_id or thread.active_job_id)
    question_open = _open_question([(r[0], r[1]) for r in recent])
    if open_turn is not None or question_open:
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})

    from app.kria import planner  # noqa: PLC0415
    from app.services.creator_sessions import resolve_item_creator_context  # noqa: PLC0415

    manifest, _media = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
    clips = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    common = {
        "thread_id": thread_id,
        "language": language,
        "client_protocol": client_protocol,
        "runtime_version": int(getattr(thread, "runtime_version", 1) or 1),
        "media_digest": digest,
        "manifest_hash": manifest.manifest_hash,
        "clips": clips,
        "manifest": _manifest_view(manifest),
        "edit_format": manifest.edit_format,
        "creator_id": creator_id,
    }
    if user_messages == 0 and not has_job:
        understanding = _understanding_state(rows)
        witness = make_strategy_witness(manifest) if understanding == "complete" else None
        return SuggestionContext(
            state="first_prompt",
            understanding=understanding,
            strategy_witness=witness,
            **common,
        )
    if not has_job or user_messages == 0:
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})

    # ready: a completed render with an editor target, no copy discussion open.
    from app.models import Job  # noqa: PLC0415
    from app.services.creative_copy_decisions import fold_creative_copy  # noqa: PLC0415

    events = await planner._load_thread_events(db, thread.id, release=False)
    states = fold_creative_copy(events, dependency_digest=digest)
    if any(s.status in _OPEN_COPY_STATES for s in states.values()):
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})
    target = await planner._load_editor_target(db, thread_id=thread.id, item=item)
    if target is None or not target.variant:
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})
    job = await db.get(Job, target.job_id)
    if job is None:
        return SuggestionContext(**{**base.__dict__, "media_digest": digest})
    variant = target.variant
    snapshot = target.snapshot
    typed = (
        await db.execute(
            select(CreationThreadEvent.content)
            .where(
                CreationThreadEvent.thread_id == thread.id,
                CreationThreadEvent.event_type == "user_message",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        )
    ).first()
    typed_request = " ".join(str(typed[0]).split())[:200] if typed and typed[0] else None
    return SuggestionContext(
        state="ready",
        understanding="n/a",
        render_generation_id=str(snapshot.get("base_generation") or "") or None,
        variant_id=str(variant.get("variant_id") or "") or None,
        resolved_archetype=str(variant.get("resolved_archetype") or "") or None,
        snapshot=snapshot,
        typed_request=typed_request,
        job_id=str(job.id),
        editor_witness=make_editor_witness(job, variant, snapshot),
        **common,
    )
