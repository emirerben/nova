"""Footage-aware suggestion chips for the Kria creation chat (KRI-756).

Two layers, one rule: **the generator decides which chips may exist, the agent only ranks them.**

1. A pure, free, LLM-free candidate generator (``build_candidates``). Every candidate it emits
   is *real* (the footage and edit it describes exist), *enabled* (the resolvers the planner
   enforces say the capability is available for THIS creator and client protocol), *executable*
   (a dry-run witness proved the claimed change is not a no-op) and *not already in the edit*.
2. The suggestion agent (``nova.chat.suggestion_agent``, ``app/agents/suggestion_agent.py``)
   that picks and orders kinds (and writes a ``why`` and, for a hook, the exact text), off the
   request path. It can only return kinds and target handles the server issued. The server
   builds every ``Action`` from the kind's registry row and proves it with the kind's witness
   (``services/suggestions/picks.py``); ``label``, ``message``, ``capability`` and ``anchor``
   are always the server's own, and a deterministic validator (``suggestion_chip_copy``) gates
   every ``why`` and ``copy`` it writes.

The GET route never calls a model: ``assemble_set`` joins the cached agent picks onto live
candidates and falls back to the deterministic order. A chip's ``label`` and ``message`` are
closed server templates with no Gemini-derived words, so the "Gemini metadata never becomes
overlay text" invariant holds by construction.

Layout:

* ``build_candidates(ctx)``          pure, never raises, no I/O -> ``CandidatePlan``.
* ``assemble_set(ctx, plan, ...)``   pure join of plan + agent picks -> ``SuggestionSetOut``.
* ``assemble_suggestions(...)``      the same join as the server-side ``SuggestionSet`` (actions).
* ``build_suggestion_set(ctx)``      ``assemble_set`` over ``build_candidates`` (no agent).
* ``build_agent_input(ctx, plan)``   the ``SuggestionAgentInput`` payload.
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
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, model_serializer

from app.services.suggestion_chip_copy import (
    MAX_LABEL_CHARS,
    footage_vocabulary,
    language_of,
    render_suggestion_copy,
    shares_run,
    validate_why,
)
from app.services.suggestions.context import (
    NO_TARGET,
    EditorWitness,
    StrategyWitness,
    SuggestionContext,
    Target,
    any_speech,
    any_to_camera,
    cap_ok,
    families,
    live_clips,
    music_level,
    slots,
    snapshot_digest,
    text_bars,
    title_bar,
    video_clips,
)
from app.services.suggestions.models import (
    MAX_SUGGESTIONS,
    ChipAnchorOut,
    ChipKind,
    ChipKindSpec,
    ChipMomentOut,
    SuggestionAgentOutput,
    SuggestionAnchorOut,
    SuggestionDigest,
    SuggestionOut,
    SuggestionSet,
)
from app.services.suggestions.picks import (
    COPY_KINDS,
    Issued,
    Pick,
    PoolRow,
    build_suggestions,
    deterministic_picks,
    issue_kinds,
)
from app.services.suggestions.registry import legacy_gate

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

# Bump together with prompts/suggestion_agent.txt and agents/skills/edit-improver (the agent's
# AgentSpec.prompt_version reads this; it also feeds every set digest, so a bump re-asks).
PROMPT_VERSION = "2026-11-04.1"

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
    # AI suggestion blocks (additive; old builds and web ignore them). Left out of the JSON while
    # empty, so a set without suggestions serializes exactly as it did before they existed.
    suggestions: list[SuggestionOut] = Field(default_factory=list)
    digest: SuggestionDigest | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_suggestions(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if not self.suggestions:
            data.pop("suggestions", None)
        if self.digest is None:
            data.pop("digest", None)
        return data


def unavailable_set() -> SuggestionSetOut:
    """Flag off, cohort miss, 404-equivalent or any failure: render nothing."""
    return SuggestionSetOut(status="unavailable", state="none", chips=[])


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


CHIP_KIND_GATE = legacy_gate()


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
    speech = any_speech(ctx)
    if kind is ChipKind.CAPTIONS and speech:
        score += 40  # speech + no captions outranks the hook (110 > 105)
    if kind is ChipKind.HOOK and (ctx.state == "first_prompt" or title_bar(ctx) is None):
        score += 25
    if kind is ChipKind.OPENER_CLIP and len(live_clips(ctx)) >= 2:
        score += 20
    if kind in (ChipKind.SFX, ChipKind.TRANSITION) and not speech:
        score += 15
    if kind is ChipKind.MUSIC and any_to_camera(ctx):
        score += 25
    return score


# ── Candidates ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One chip the engine would allow. The agent can only choose among these."""

    cid: str
    kind: ChipKind
    capability: str | None
    target: Target
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
    kind: ChipKind, language: str, target: Target
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


def _dry_run(spec: ChipKindSpec, ctx: SuggestionContext, target: Target) -> frozenset[str] | None:
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
    speech = any_speech(ctx)
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
    refs = {id(c): f"c{c.get('ordinal')}" for c in live_clips(ctx)}
    speech_refs = [
        refs[id(c)] for c in live_clips(ctx) if c.get("has_speech") or c.get("to_camera")
    ]
    silent_refs = [
        refs[id(c)] for c in live_clips(ctx) if not (c.get("has_speech") or c.get("to_camera"))
    ]
    if kind is ChipKind.CAPTIONS:
        return {"speech_clips": speech_refs[:FOOTAGE_CLIPS_MAX]}
    if kind is ChipKind.SFX:
        return {"silent_clips": silent_refs[:FOOTAGE_CLIPS_MAX]}
    if kind is ChipKind.TRANSITION:
        return {
            "clip_count": len(live_clips(ctx)) if ctx.state == "first_prompt" else len(slots(ctx))
        }
    if kind is ChipKind.MUSIC:
        level = music_level(ctx)
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
    parts: dict[str, Any] = {
        "media": ctx.media_digest,
        "manifest": ctx.manifest_hash,
        "generation": ctx.render_generation_id,
        "variant": ctx.variant_id,
        "state": state,
        "language": language_of(ctx.language),
    }
    if ctx.signals_version:
        # Only present when KRIA_SUGGESTION_SIGNALS_ENABLED: off, the digest is unchanged.
        parts["signals"] = ctx.signals_version
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
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
        return "first_prompt" if video_clips(ctx) else "none"
    return ctx.state if ctx.state == "ready" else "none"


def _make_candidate(
    spec: ChipKindSpec,
    ctx: SuggestionContext,
    target: Target,
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
) -> list[tuple[int, ChipKindSpec, Target, frozenset[str]]]:
    found: list[tuple[int, ChipKindSpec, Target, frozenset[str]]] = []
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
            spec, scoped, NO_TARGET, base, touched=frozenset(), witness_ok=True, score=100
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
    return "done" if live_clips(ctx) or plan.state == "ready" else "empty"


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


_EVIDENCE_MAX = 48


def _evidence(cand: Candidate, ctx: SuggestionContext, state: str) -> str | None:
    """A one-line, deterministic reason this chip fits THIS footage (KRI-769).

    Built only from counts and edit state the server already holds (never Gemini prose, never
    ids or times), so it needs no model and cannot invent a moment. Every line states a fact the
    server measured, not a judgement about quality. Shown as the chip's subtitle whenever the
    wording agent supplied no grounded ``why`` of its own.
    """
    tr = language_of(ctx.language) == "tr"
    clips = live_clips(ctx)
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
        cuts = len(slots(ctx)) if state == "ready" else total
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


def _picks_of(output: SuggestionAgentOutput | None) -> list[Pick]:
    if output is None:
        return []
    return [
        Pick(kind=p.kind, target_ids=tuple(p.target_ids), why=p.why, copy=p.copy_text)
        for p in sorted(output.ranked, key=lambda p: p.rank)
    ]


def _pool_rows(plan: CandidatePlan) -> list[PoolRow]:
    return [
        PoolRow(str(c.kind), c.target, c.touched, c.score, c.gap_tags, c.facts) for c in plan.pool
    ]


def issued_for(ctx: SuggestionContext, plan: CandidatePlan) -> tuple[Issued, ...]:
    """The kinds the agent may pick for this plan (the engine pool plus registry kinds).

    Nothing before the clips are understood: a first-prompt card needs footage facts.
    """
    if plan.state not in ("first_prompt", "ready"):
        return ()
    if plan.state == "first_prompt" and ctx.understanding != "complete":
        return ()
    return issue_kinds(ctx, plan.state, _pool_rows(plan))


def _card_copy(
    ctx: SuggestionContext, plan: CandidatePlan
) -> Callable[[str, Sequence[Target], str | None], tuple[str, str, list[str]] | None]:
    """Title, chat prompt and evidence of a card: closed server text, never agent words.

    A KRI-756 kind keeps its chip label and message verbatim (the tap sends the same words the
    legacy chip sent). Every other kind renders from ``SUGGESTION_COPY``.
    """
    by_kind = {str(c.kind): c for c in plan.pool}

    def render(
        kind: str, targets: Sequence[Target], _copy: str | None
    ) -> tuple[str, str, list[str]] | None:
        tr = language_of(ctx.language) == "tr"
        ordinals = [t.clip_ordinal for t in targets if t.clip_ordinal is not None]
        cand = by_kind.get(kind)
        if cand is not None:
            title, prompt = cand.fallback_label, cand.message
            line = _evidence(cand, ctx, plan.state)
            evidence = [line] if line else []
            if kind == "hook":
                # The card shows the agent's text; its tap sentence must not carry or quote it.
                closed = render_suggestion_copy(kind, ctx.language, ordinals)
                if closed is None:
                    return None
                title, prompt = closed
        else:
            rendered = render_suggestion_copy(kind, ctx.language, ordinals)
            if rendered is None:
                return None
            title, prompt = rendered
            evidence = _measured_evidence(ctx, kind, targets)
            if evidence:
                return title, prompt, evidence[:3]
        for n in ordinals[:2]:
            evidence.append(f"Klip {n}" if tr else f"Clip {n}")
        return title, prompt, evidence[:3]

    return render


def _measured_evidence(ctx: SuggestionContext, kind: str, targets: Sequence[Target]) -> list[str]:
    """Closed-form lines a kind builds from its own measurement (never a transcript word)."""
    try:
        if kind == "filler_words":
            from app.services.suggestions.kinds import filler_words  # noqa: PLC0415

            found = ctx.kind_inputs.get(filler_words.INPUTS_KEY)
            if isinstance(found, filler_words.FillerEvidence):
                return filler_words.evidence_lines(found, ctx.language)
        elif kind == "duplicates":
            from app.services.suggestions.kinds import duplicates  # noqa: PLC0415

            return duplicates.evidence_lines(ctx, targets)
    except Exception:  # noqa: BLE001 - a missing evidence line falls back to the clip names
        log.warning("suggestion_chips.error", stage="measured_evidence", kind=kind, exc_info=True)
    return []


def _agent_texts(ctx: SuggestionContext) -> tuple[list[str], list[str]]:
    """``(transcripts, footage_texts)``: what an agent's ``why`` or ``copy`` must not echo."""
    transcripts = [
        str(t) for t in (ctx.kind_inputs.get("transcripts") or {}).values() if isinstance(t, str)
    ]
    footage: list[str] = []
    for clip in live_clips(ctx):
        footage += [str(clip.get("subject") or ""), str(clip.get("setting") or "")]
        for moment in clip.get("notable_moments") or []:
            if isinstance(moment, Mapping):
                footage.append(str(moment.get("label") or moment.get("description") or ""))
        on_screen = clip.get("on_screen_text")
        if isinstance(on_screen, str):
            footage.append(on_screen)
    return transcripts, [t for t in footage if t]


def _build_kwargs(ctx: SuggestionContext, plan: CandidatePlan) -> dict[str, Any]:
    transcripts, footage = _agent_texts(ctx)
    return {
        "base_digest": plan.base_digest,
        "copy_for": _card_copy(ctx, plan),
        "transcripts": transcripts,
        "footage_texts": footage,
    }


@dataclass(frozen=True)
class PickStats:
    """What came of an agent answer: picks in, picks that serve something, drops by reason."""

    n_in: int
    n_out: int
    drops: dict[str, int]


def pick_stats(
    ctx: SuggestionContext, plan: CandidatePlan, output: SuggestionAgentOutput
) -> PickStats:
    """Score an answer against the LIVE plan. Pure; never raises.

    A pick counts when its kind is an engine candidate (the legacy chip serves it) or the
    registry built and proved its card. Everything else is a drop, by reason.
    """
    picks = _picks_of(output)
    try:
        cards, drops = build_suggestions(
            ctx, issued_for(ctx, plan), picks, **_build_kwargs(ctx, plan)
        )
    except Exception:  # noqa: BLE001 - telemetry must not fail a run
        log.warning("suggestion_chips.error", stage="pick_stats", exc_info=True)
        cards, drops = [], {"stats_error": 1}
    served = {str(c.kind) for c in plan.pool} | {c.kind for c in cards}
    kept = {p.kind for p in picks} & served
    return PickStats(n_in=len(picks), n_out=len(kept), drops=drops)


FRESHNESS_KEY = "freshness"


def _fallback_picks(ctx: SuggestionContext, issued: Sequence[Issued]) -> list[Pick]:
    """The deterministic order, with the clips a clip-scoped kind needs named.

    A mute card is about one clip and a duplicates card about its pair; with no agent to name
    them a whole-video pick builds nothing, so these two take the clips their own measurement
    found (the same ones the kind would accept from an agent). Every other kind is unchanged.
    """
    picks: list[Pick] = []
    for pick in deterministic_picks(issued):
        try:
            if pick.kind == "mute_clip":
                from app.services.suggestions.kinds import mute_clip  # noqa: PLC0415

                named = mute_clip.candidate_targets(ctx)[:1]
                pick = Pick(kind=pick.kind, target_ids=tuple(f"c{t.clip_ordinal}" for t in named))
            elif pick.kind == "duplicates":
                from app.services.suggestions.kinds import duplicates  # noqa: PLC0415

                plans = duplicates._plans(ctx)
                ordinal = duplicates._ordinals(ctx)
                if plans:
                    members = (*plans[0].drops, plans[0].keep)  # a dropped clip is the anchor
                    pick = Pick(kind=pick.kind, target_ids=tuple(f"c{ordinal[m]}" for m in members))
        except Exception:  # noqa: BLE001 - that kind then builds nothing; the others still do
            log.warning("suggestion_chips.error", stage="fallback_targets", exc_info=True)
        picks.append(pick)
    return picks


def suggestion_digest(ctx: SuggestionContext, state: str) -> SuggestionDigest:
    """What a set was computed from. The loader's ``kind_inputs["freshness"]`` (the head draft's
    ``snapshot_hash`` and the brief version, read together with the footage) joins the digest only
    when it was loaded; without it the digest is what it always was."""
    fresh = ctx.kind_inputs.get(FRESHNESS_KEY) if isinstance(ctx.kind_inputs, Mapping) else None
    fresh = fresh if isinstance(fresh, Mapping) else {}
    return SuggestionDigest(
        media_digest=ctx.media_digest,
        snapshot_hash=fresh.get("snapshot_hash") or snapshot_digest(ctx.snapshot),
        brief_version=fresh.get("brief_version"),
        signals_version=ctx.signals_version,
        state=state,
        language=language_of(ctx.language),
        prompt_version=PROMPT_VERSION,
    )


def assemble_suggestions(
    ctx: SuggestionContext,
    plan: CandidatePlan,
    picks: SuggestionAgentOutput | None = None,
) -> tuple[SuggestionSet | None, dict[str, int]]:
    """The server-side set of v2 cards (actions included) and what was dropped, by reason.

    The agent's picks are built in its rank order; when none survives (an empty answer, or
    every pick failed its witness) the deterministic order is built instead, so the creator
    never sees fewer cards because the model was unhelpful. None when nothing can be offered.
    """
    issued = issued_for(ctx, plan)
    if not issued:
        return None, {}
    common = _build_kwargs(ctx, plan)
    cards, drops = build_suggestions(ctx, issued, _picks_of(picks), **common)
    if not cards:
        cards, more = build_suggestions(ctx, issued, _fallback_picks(ctx, issued), **common)
        for reason, count in more.items():
            drops[reason] = drops.get(reason, 0) + count
    if not cards:
        return None, drops
    return (
        SuggestionSet(
            set_id=plan.set_id,
            digest=suggestion_digest(ctx, plan.state),
            state=plan.state,
            suggestions=cards,
        ),
        drops,
    )


def assemble_set(
    ctx: SuggestionContext,
    plan: CandidatePlan,
    picks: SuggestionAgentOutput | None = None,
    *,
    pending: bool = False,
    retry_after_s: int | None = None,
) -> SuggestionSetOut:
    """Join a plan with (optional) agent picks. Pure; every ``why`` is re-validated here.

    The legacy ``chips`` are the engine's proven levers in the agent's order (template labels,
    a validated ``why`` or the deterministic evidence line); without usable picks, the
    deterministic top of the pool. The v2 ``suggestions`` are added only behind
    ``KRIA_SUGGESTION_PINNED_TAPS_ENABLED``: a card is only safe to tap when the tap is pinned.
    """
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
    vocab = footage_vocabulary(live_clips(ctx))
    transcripts, _footage = _agent_texts(ctx)
    cap = MAX_LEVERS[plan.state]
    chosen: list[tuple[Candidate, str | None]] = []
    used_agent = False

    if not pending:
        by_kind = {str(c.kind): c for c in plan.pool}
        seen: set[str] = set()
        copy_bearing = 0
        for pick in _picks_of(picks):
            cand = by_kind.get(pick.kind)
            if cand is None or pick.kind in seen or len(chosen) >= cap:
                continue
            if cand.kind in COPY_BEARING:
                if copy_bearing:
                    continue
                copy_bearing += 1
            why: str | None = None
            if pick.why:
                why, _reason = validate_why(
                    pick.why, language=ctx.language, vocabulary=vocab, kind=pick.kind
                )
                if why is not None and shares_run(why, transcripts):
                    why = None
            seen.add(pick.kind)
            used_agent = True
            chosen.append((cand, why))
        if not chosen:
            # No usable agent ranking: the deterministic top of the pool, template wording.
            chosen = [(cand, None) for cand in plan.levers[:cap]]

    rows: list[tuple[Candidate, str | None]] = (
        [(plan.default, None)] if plan.default is not None else []
    ) + chosen
    chips = [
        _chip(
            cand,
            rank=index + 1,
            label=cand.fallback_label,
            source="template",
            why=why or _evidence(cand, ctx, plan.state),
            default_edit=cand.kind is ChipKind.DEFAULT_EDIT and plan.state == "first_prompt",
        )
        for index, (cand, why) in enumerate(rows)
    ]
    suggestions: list[SuggestionOut] = []
    digest: SuggestionDigest | None = None
    if not pending:
        from app.config import settings  # noqa: PLC0415

        if settings.kria_suggestion_pinned_taps_enabled:
            built, _drops = assemble_suggestions(ctx, plan, picks)
            if built is not None:
                suggestions, digest = built.wire(), built.digest
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
        suggestions=suggestions,
        digest=digest,
    )


def build_suggestion_set(
    ctx: SuggestionContext, picks: SuggestionAgentOutput | None = None
) -> SuggestionSetOut:
    """Pure, I/O-free, LLM-free, never raises: candidates + (optional) agent picks, one call.

    With no picks this is the deterministic set. Understanding still running yields the
    ``pending`` set (``default_edit`` only, ``retry_after_s``).
    """
    try:
        plan = _build_candidates(ctx)
        if plan.state == "first_prompt" and ctx.understanding == "pending":
            return assemble_set(ctx, plan, None, pending=True, retry_after_s=PENDING_RETRY_S)
        return assemble_set(ctx, plan, picks)
    except Exception:  # noqa: BLE001 - fail open to "render nothing"
        log.warning("suggestion_chips.error", stage="build", exc_info=True)
        return unavailable_set()


# ── Agent input ──────────────────────────────────────────────────────────────────────


def _clean_phrase(value: object, *, words: int, chars: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isprintable() and ch not in "{}$`\\")
    return " ".join(text.split()[:words])[:chars].strip()


_TRANSCRIPT_WORDS = 70
_TRANSCRIPT_CHARS = 420
_SIGNAL_CHARS = 600


def _compact_signal(value: Any, depth: int = 0) -> Any:
    """A measured signal payload reduced to short scalars, so one clip cannot flood the prompt."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 3) if isinstance(value, float) else value
    if isinstance(value, str):
        return value[:48]
    if isinstance(value, Mapping) and depth < 2:
        return {str(k)[:32]: _compact_signal(v, depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)) and depth < 2:
        return [_compact_signal(v, depth + 1) for v in list(value)[:6]]
    return None


def _clip_signals(ctx: SuggestionContext, media_id: object) -> dict[str, Any]:
    raw = ctx.signals.get(str(media_id)) if media_id is not None else None
    if not isinstance(raw, Mapping):
        return {}
    compact = {str(k)[:32]: _compact_signal(v) for k, v in raw.items()}
    return compact if len(json.dumps(compact, default=str)) <= _SIGNAL_CHARS else {}


def _editor_summary(ctx: SuggestionContext) -> dict[str, Any] | None:
    """What the finished edit contains now: counts and states, never any on-screen words."""
    if ctx.snapshot is None:
        return None
    captions = (ctx.snapshot or {}).get("captions")
    meta = captions.get("meta") if isinstance(captions, Mapping) else None
    summary: dict[str, Any] = {
        "op_families": sorted(families(ctx)),
        "slots": len(slots(ctx)),
        "text_bars": len(text_bars(ctx)),
        "has_title": title_bar(ctx) is not None,
    }
    if isinstance(meta, Mapping):
        summary["captions_on"] = meta.get("enabled") is not False
    level = music_level(ctx)
    if level is not None:
        summary["music_level"] = level
    return summary


def build_agent_input(ctx: SuggestionContext, plan: CandidatePlan) -> dict[str, Any]:
    """The ``SuggestionAgentInput`` payload: the agent's full read access, ids kept server side.

    ``c1..cN`` name clips, so the model never types a media id or a timestamp. Measured facts
    (``speech``, ``duration_s``, ``signals``, ``editor``) are server-trusted; every footage
    sentence (Gemini subject / setting / moments, the transcript) sits under the clip's
    ``untrusted`` key and the prompt says to treat it as data. The kinds are the issued ones:
    the schema the model answers in is closed over them.
    """
    transcripts = ctx.kind_inputs.get("transcripts") or {}
    clips: list[dict[str, Any]] = []
    for clip in live_clips(ctx)[:FOOTAGE_CLIPS_MAX]:
        ordinal = clip.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            continue
        speech = (
            "to_camera"
            if clip.get("to_camera")
            else "voiceover"
            if clip.get("has_speech")
            else "none"
        )
        moments = [
            text
            for moment in (clip.get("notable_moments") or [])[:MOMENTS_PER_CLIP]
            if isinstance(moment, Mapping)
            and (
                text := _clean_phrase(
                    moment.get("label") or moment.get("description"), words=12, chars=MOMENT_CHARS
                )
            )
        ]
        duration = clip.get("duration_s")
        clips.append(
            {
                "ref": f"c{ordinal}",
                "kind": "image" if clip.get("kind") == "image" else "video",
                "speech": speech,
                "duration_s": round(float(duration), 1)
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else None,
                "signals": _clip_signals(ctx, clip.get("media_id")),
                "untrusted": {
                    "subject": _clean_phrase(clip.get("subject"), words=SUBJECT_WORDS, chars=48),
                    "setting": _clean_phrase(clip.get("setting"), words=SETTING_WORDS, chars=60),
                    "moments": moments,
                    "transcript": _clean_phrase(
                        transcripts.get(clip.get("media_id")),
                        words=_TRANSCRIPT_WORDS,
                        chars=_TRANSCRIPT_CHARS,
                    ),
                },
            }
        )
    payload: dict[str, Any] = {
        "state": plan.state,
        "language": language_of(ctx.language),
        "max_suggestions": MAX_SUGGESTIONS,
        "kinds": [
            {
                "kind": row.kind.kind,
                "gap_tags": list(row.gap_tags),
                "facts": dict(row.facts),
                "carries_copy": row.kind.kind in COPY_KINDS,
            }
            for row in issued_for(ctx, plan)[:16]
        ],
        "clips": clips,
        "already_in_edit": list(plan.already_in_edit),
        "capabilities": sorted(
            name
            for name, row in ((ctx.manifest or {}).get("capabilities") or {}).items()
            if cap_ok(ctx, str(name))
        )[:24],
    }
    editor = _editor_summary(ctx) if plan.state == "ready" else None
    if editor is not None:
        payload["editor"] = editor
    if ctx.brief_headline:
        payload["brief"] = _clean_phrase(ctx.brief_headline, words=40, chars=240)
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

    A set needs an entry when it offers anything: legacy chips OR v2 cards. A set whose only
    cards are v2-only kinds (a polished edit, or one whose four legacy chips are handled) has no
    chips, and without an entry the cards have nothing to be attached to. Its ``chips`` are ``[]``.

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

    if out.status not in ("ok", "pending") or not (out.chips or out.suggestions) or not out.set_id:
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

    from app.config import settings  # noqa: PLC0415
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
    if settings.kria_suggestion_signals_enabled:
        from app.services import clip_signals  # noqa: PLC0415

        common["signals"] = {r["media_id"]: clip_signals.read_signals(r["_analysis"]) for r in rows}
        common["signals_version"] = clip_signals.signals_version()
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
