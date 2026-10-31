"""Shared harness for the suggestion-chip replay eval (KRI-756).

Used by ``test_suggestion_chips_evals.py`` and by ``scripts/suggestion_chip_report.py`` so the
CI gate and the printed OLD-vs-NEW artifact are computed by the same code.

Offline, free, no network, no skia:

* the engine under test is ``app.services.suggestion_chips.build_suggestion_set`` (pure);
* a fixture is ``{name, description, context, expect}`` JSON in
  ``tests/fixtures/agent_evals/suggestion_chips/``;
* the planner dry-run witnesses are the REAL ones (``make_strategy_witness`` over a real
  ``ResolvedCreatorManifest`` and ``check_strategy_for_runtime_v2``; ``make_editor_witness``
  over ``parse_editor_operation`` + ``compile_editor_ops``), fed a synthetic variant that is
  rebuilt from the fixture's snapshot by ``runners/snapshot_variant.py``. Nothing is faked
  unless a fixture opts out with ``"witness": "none"``.

What this does NOT prove: that a live Main Creator / edit copilot, given a chip's message,
chooses the claimed op. Offline evals are compile-only (see tests/evals/README.md).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services import suggestion_chip_copy as copy_rules
from app.services import suggestion_chips as sc

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals" / "suggestion_chips"
CONTRACT_PREFIX = "contract_example_"
CASSETTE_FILE = FIXTURE_DIR / "executability_cassettes.json"
# Contract examples for the iOS lane (web ignores the feature): file -> the fixture it is
# generated from.
CONTRACT = {
    "contract_example_first_prompt.json": "fp_speech_and_broll",
    "contract_example_ready.json": "rd_plain_montage",
    "contract_example_pending.json": "fp_understanding_pending",
}
REPO_ROOT = Path(__file__).resolve().parents[5]

_HASH_A = "a" * 64
_HASH_B = "b" * 64

# ── Independent oracles (deliberately NOT imported from the engine) ───────────────────
#
# The engine owns CHIP_KIND_GATE. The eval re-states, from the planner's own sources, which
# gate each kind must pass so a mistake in the engine's table cannot also hide in the check.
# kind -> (first_prompt manifest capability, ready editor op family (any of)).
KIND_REQUIRES: dict[str, dict[str, tuple[str, ...]]] = {
    "default_edit": {"first_prompt": ("dispatch_render",)},
    "hook": {"first_prompt": ("dispatch_render",), "ready": ("title",)},
    # first_prompt captions are gated on the FOOTAGE (speech / to camera) and the edit format,
    # not on a manifest capability: `caption_style` mirrors an unrelated flag and enforces
    # nothing (docs/pipelines/suggestion-chips.md). Checked in check_capability_matrix.
    "captions": {"first_prompt": (), "ready": ("caption",)},
    "sfx": {"first_prompt": ("sound_effects",), "ready": ("sfx",)},
    "transition": {"first_prompt": ("transitions",), "ready": ("transition",)},
    "music": {"ready": ("music",)},
    # patch_text (the only op that sets an entrance animation) needs the text family.
    "title_animation": {"ready": ("text",)},
}

# Kinds that exist on the wire but must NEVER be emitted this pass (a proof is missing).
# opener_clip: CLIP_ORDINAL_PROVEN; pinned_text: PINNED_TEXT_PROVEN. Both need a cassette.
DARK_KINDS = ("opener_clip", "pinned_text")

# The closed copy deck. Every message the engine may send is one of these (G7). A copy change
# must be made here too, which is the point: it forces a conscious wording review.
MESSAGE_DECK: dict[str, dict[str, str]] = {
    "default_edit": {"en": "Suggest an edit.", "tr": "Suggest an edit."},
    "hook": {"en": "Add a hook that matches the video", "tr": "Videoya uyan bir hook ekle"},
    "captions": {"en": "Add captions", "tr": "Altyazı ekle"},
    "sfx": {"en": "Add sound effects", "tr": "Ses efektleri ekle"},
    "music": {"en": "Turn the music down", "tr": "Müziği kıs"},
    "transition": {
        "en": "Add transitions between the clips",
        "tr": "Klipler arasına geçişler ekle",
    },
    "title_animation": {"en": "Animate the title", "tr": "Başlığı canlandır"},
}

LABEL_DECK: dict[str, dict[str, str]] = {
    "default_edit": {"en": "Let Kria cut the first version", "tr": "İlk versiyonu Kria kessin"},
    "hook": {"en": "Add a hook to the opening", "tr": "Açılışa bir hook ekle"},
    "captions": {"en": "Add captions", "tr": "Altyazı ekle"},
    "sfx": {"en": "Add sound effects", "tr": "Ses efektleri ekle"},
    "music": {"en": "Turn the music down", "tr": "Müziği kıs"},
    "transition": {"en": "Add transitions", "tr": "Geçişler ekle"},
    "title_animation": {"en": "Animate the title", "tr": "Başlığı canlandır"},
}

# G6 stoplist: generic chips that would fit any video, plus register the empathy and cringe
# rubrics reject (tests/evals/rubrics/suggestion_chips.md). Case-insensitive substring match.
STOPLIST = (
    "try a stronger opening",
    "make it warmer",
    "make it pop",
    "make it better",
    "add effects",
    "viral",
    "banger",
    "elevate",
    "level up",
    "next level",
    "unlock",
    "supercharge",
    "stunning",
    "amazing",
    "perfect",
    "strongest",
    "best moment",
    "engagement",
    "audience",
    "algorithm",
    "brand",
    "tone of voice",
    "call to action",
)

# ── Snapshot presets (mirror build_editor_snapshot's shape) ───────────────────────────


def _slot(i: int, *, transition: str = "cut") -> dict[str, Any]:
    return {
        "slot_id": f"slot-{i}",
        "media_id": f"clip-{i + 1}",
        "output_start_s": i * 3.0,
        "output_end_s": (i + 1) * 3.0,
        "duration_s": 3.0,
        "transition_after": transition,
    }


def _base_snapshot() -> dict[str, Any]:
    return {
        "editor_ops_version": 2,
        "allowed_op_families": ["clip", "music", "sfx", "title", "transition"],
        "has_narrated_captions": False,
        "text_bars": [],
        "slots": [_slot(0), _slot(1), _slot(2)],
        "total_duration_s": 9.0,
        "music": {
            "swappable": False,
            "removable": True,
            "current_track_id": "track-1",
            "current_track_title": None,
            "candidates": [],
        },
        "mix": {"music_level": 0.6},
        "sfx": {
            "placements": [],
            "catalog": [
                {
                    "id": "whoosh_01",
                    "name": "Whoosh",
                    "label": "Whoosh",
                    "category": "transitions",
                    "duration_s": 0.8,
                }
            ],
        },
        "base_generation": "gen-1",
    }


SNAPSHOT_PRESETS: dict[str, dict[str, Any]] = {"plain_montage": _base_snapshot()}

_subtitled = _base_snapshot()
_subtitled["allowed_op_families"] = ["caption", "clip", "music", "sfx", "title", "transition"]
_subtitled["has_narrated_captions"] = True
_subtitled["captions"] = {
    "total_cues": 2,
    "truncated": False,
    "cues_editable": True,
    "cues": [
        {"index": 0, "id": "cue-1", "text": "first line", "start_s": 0.0, "end_s": 1.5},
        {"index": 1, "id": "cue-2", "text": "second line", "start_s": 1.5, "end_s": 3.0},
    ],
    "meta": {"enabled": True, "style": "sentence", "font": None, "y_frac": 0.8},
}
SNAPSHOT_PRESETS["subtitled"] = _subtitled

_guided = _base_snapshot()
_guided["allowed_op_families"] = ["clip", "music", "sfx", "text", "title", "transition"]
_guided["text_bars"] = [
    {
        "id": "guided-title",
        "role": "generative_intro",
        "text": "Sunday reset",
        "start_s": 0.0,
        "end_s": 2.5,
        "font_family": "Inter",
        "size_px": 64,
        "color": "#FFFFFF",
        "position": "middle",
        "alignment": "center",
        "effect": "static",
    }
]
_guided["text_appearance_version"] = 1
_guided["text_appearance"] = {
    "version": 1,
    "targets": [
        {
            "id": "guided-title",
            "kind": "text",
            "supported_fields": ["stroke_width", "shadow_enabled", "font_family"],
            "values": {"stroke_width": 0.0, "shadow_enabled": True, "font_family": "Inter"},
            "identity": "0123456789abcdef",
        }
    ],
}
SNAPSHOT_PRESETS["guided_story_titled"] = _guided


def deep_merge(base: Any, patch: Any) -> Any:
    """``patch`` over ``base``: dicts merge, anything else replaces, ``None`` deletes a key."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve_snapshot(context: dict[str, Any]) -> dict[str, Any] | None:
    preset = context.get("snapshot_preset")
    explicit = context.get("snapshot")
    if preset is None and explicit is None:
        return None
    base = copy.deepcopy(SNAPSHOT_PRESETS[preset]) if preset else {}
    return deep_merge(base, explicit or {}) if (explicit or preset) else base


# ── Manifest + witnesses ──────────────────────────────────────────────────────────────

_DEFAULT_FIRST_PROMPT_CAPS = {
    "dispatch_render": True,
    "main_creator_agent": True,
    "caption_style": True,
    "sound_effects": True,
    "transitions": True,
}


def capabilities_from_fixture(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Shorthand ``{name: bool}`` (or full ``{available, reason_code}``) -> manifest rows."""
    raw = context.get("capabilities")
    if raw is None:
        raw = dict(_DEFAULT_FIRST_PROMPT_CAPS) if context.get("state") == "first_prompt" else {}
    out: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if isinstance(value, bool):
            out[name] = (
                {"available": True}
                if value
                else {
                    "available": False,
                    "reason_code": "flag_off",
                }
            )
        else:
            out[name] = dict(value)
    return out


def build_manifest(context: dict[str, Any]) -> Any | None:
    """A real ``ResolvedCreatorManifest`` for the strategy witness (None without media)."""
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest  # noqa: PLC0415

    clips = context.get("clips") or []
    if not clips:
        return None
    edit_format = context.get("edit_format") or "montage"
    caps = capabilities_from_fixture(context)
    # A real manifest always advertises its format and the guided proposal; a fixture can
    # switch either off explicitly to exercise the baseline-refused path.
    # creator_capabilities lists EVERY edit format; montage is always available.
    caps.setdefault("edit_format:montage", {"available": True})
    caps.setdefault(f"edit_format:{edit_format}", {"available": True})
    caps.setdefault("draft_guided_proposal", {"available": True})
    return ResolvedCreatorManifest(
        item_id=f"item-{context.get('thread_id') or 'fixture'}",
        edit_format=edit_format,
        render_program=context.get("render_program") or "guided",
        media=[
            {
                "media_id": c["media_id"],
                "kind": c.get("kind", "video"),
                **({"duration_s": c["duration_s"]} if c.get("duration_s") else {}),
            }
            for c in clips
        ],
        capabilities=caps,
        context_hash=_HASH_A,
        manifest_hash=_HASH_B,
    )


def synthetic_job_and_variant(
    snapshot: dict[str, Any], *, archetype: str | None
) -> tuple[Any, dict[str, Any]]:
    from tests.evals.runners.snapshot_variant import (  # noqa: PLC0415
        build_synthetic_job,
        build_synthetic_variant,
    )

    variant = build_synthetic_variant(snapshot, [])
    variant["resolved_archetype"] = archetype
    return build_synthetic_job(variant), variant


# ── Cases ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Case:
    name: str
    path: Path
    description: str
    raw: dict[str, Any]
    expect: dict[str, Any]
    tags: list[str] = field(default_factory=list)

    @property
    def context_dict(self) -> dict[str, Any]:
        return self.raw["context"]

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return resolve_snapshot(self.context_dict)

    @property
    def language(self) -> str:
        return self.context_dict.get("language", "en")

    @property
    def state(self) -> str:
        return self.context_dict.get("state", "none")


def fixture_paths() -> list[Path]:
    return sorted(
        p
        for p in FIXTURE_DIR.glob("*.json")
        if not p.name.startswith(CONTRACT_PREFIX) and p.name != CASSETTE_FILE.name
    )


def load_case(path: Path) -> Case:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Case(
        name=raw["name"],
        path=path,
        description=raw.get("description", ""),
        raw=raw,
        expect=raw.get("expect", {}),
        tags=list(raw.get("tags", [])),
    )


def load_cases() -> list[Case]:
    return [load_case(p) for p in fixture_paths()]


def case_by_name(name: str) -> Case:
    for case in all_cases():
        if case.name == name:
            return case
    raise KeyError(name)


_CONTEXT_FIELDS = {
    "thread_id",
    "state",
    "language",
    "client_protocol",
    "media_digest",
    "manifest_hash",
    "render_generation_id",
    "variant_id",
    "resolved_archetype",
    "runtime_version",
    "understanding",
    "clips",
    "edit_format",
    "brief_headline",
}


def build_context(case: Case, *, witness: str | None = None) -> sc.SuggestionContext:
    """The fixture's ``SuggestionContext`` with real planner witnesses attached.

    ``witness``: ``"real"`` (default) | ``"none"`` (engine gets no witness, so any kind that
    needs a dry run is dropped, which is how it fails closed in production).
    """
    ctx = case.context_dict
    mode = witness or ctx.get("witness") or "real"
    kwargs = {k: copy.deepcopy(v) for k, v in ctx.items() if k in _CONTEXT_FIELDS}
    kwargs.setdefault("thread_id", f"thread-{case.name}")
    kwargs.setdefault("media_digest", f"digest-{case.name}")
    kwargs.setdefault("manifest_hash", _HASH_B)
    snapshot = case.snapshot
    caps = capabilities_from_fixture(ctx)
    kwargs["manifest"] = {
        "capabilities": {
            name: {"available": row.get("available") is True, "reason_code": row.get("reason_code")}
            for name, row in caps.items()
        },
        "edit_format": ctx.get("edit_format") or "montage",
    }
    kwargs["snapshot"] = snapshot
    strategy_witness = None
    editor_witness = None
    if mode == "real":
        if kwargs.get("state") == "first_prompt" and kwargs.get("understanding") == "complete":
            manifest = build_manifest(ctx)
            if manifest is not None:
                strategy_witness = sc.make_strategy_witness(manifest)
        if kwargs.get("state") == "ready" and snapshot is not None:
            job, variant = synthetic_job_and_variant(
                snapshot, archetype=ctx.get("resolved_archetype")
            )
            editor_witness = sc.make_editor_witness(job, variant, snapshot)
    return sc.SuggestionContext(
        strategy_witness=strategy_witness, editor_witness=editor_witness, **kwargs
    )


def run_case(
    case: Case, *, witness: str | None = None, wording: sc.Wording | None = None
) -> sc.SuggestionSetOut:
    return sc.build_suggestion_set(build_context(case, witness=witness), wording)


def plan_for(case: Case, *, witness: str | None = None) -> sc.CandidatePlan:
    """The deterministic candidate plan (what the wording agent is allowed to choose from)."""
    return sc.build_candidates(build_context(case, witness=witness))


def witness_tiers(case: Case) -> dict[str, str]:
    """kind -> witness tier (strategy | editor | policy_only) for the chips this case offers."""
    plan = plan_for(case)
    cands = ([plan.default] if plan.default else []) + list(plan.levers)
    return {str(c.kind): c.witness_tier for c in cands}


# ── nermin-* replay fixtures (tests/fixtures/kria_turns) as chip cases ───────────────

KRIA_TURNS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "kria_turns"


def nermin_cases() -> list[Case]:
    """Project each ``nermin-*`` replay fixture into chip cases.

    The fixture's media labels become the clips (no speech is known, so none is claimed);
    ``plan.turn_value == "question"`` or an empty media list is the open-question / no-media
    state ``none``. Each fixture with media yields a ``first_prompt`` case (what the creator
    sees before sending anything) and a ``ready`` case (the montage that fixture's plan would
    render: one slot per label, no title, a track, the plain-montage lane set).
    """
    cases: list[Case] = []
    for path in sorted(KRIA_TURNS_DIR.glob("nermin-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        snap = data.get("snapshot") or {}
        labels = list(snap.get("media_labels") or [])
        fid = data["fixture_id"]
        fmt = snap.get("edit_format") or "montage"
        clips = [
            {
                "media_id": f"clip-{i + 1}",
                "ordinal": i + 1,
                "kind": "video",
                "subject": label,
                "setting": "",
                "has_speech": False,
                "to_camera": False,
                "duration_s": 4.0,
                "notable_moments": [],
            }
            for i, label in enumerate(labels)
        ]
        question = (data.get("plan") or {}).get("turn_value") == "question"
        base = {
            "thread_id": f"thread-{fid}",
            "language": "en",
            "client_protocol": 2,
            "media_digest": f"digest-{fid}",
            "runtime_version": 2,
            "edit_format": fmt,
            "clips": clips,
        }
        stages: list[tuple[str, dict[str, Any], dict[str, Any]]]
        if question or not clips:
            stages = [
                (
                    "none",
                    {**base, "state": "none", "understanding": "n/a"},
                    {"state": "none", "kinds": []},
                )
            ]
        else:
            slots = [
                {**_slot(i), "media_id": c["media_id"], "duration_s": 3.0}
                for i, c in enumerate(clips)
            ]
            stages = [
                (
                    "first_prompt",
                    {**base, "state": "first_prompt", "understanding": "complete"},
                    {"state": "first_prompt", "kinds_include": ["default_edit"]},
                ),
                (
                    "ready",
                    {
                        **base,
                        "state": "ready",
                        "understanding": "n/a",
                        "render_generation_id": "gen-1",
                        "variant_id": "v1",
                        "snapshot_preset": "plain_montage",
                        "snapshot": {
                            "slots": slots,
                            "total_duration_s": 3.0 * len(slots),
                        },
                    },
                    {"state": "ready", "kinds_exclude": ["captions", "title_animation"]},
                ),
            ]
        for stage, context, expect in stages:
            raw = {
                "name": f"{fid}::{stage}",
                "description": f"{data.get('user_message', '')}",
                "context": context,
                "expect": expect,
                "tags": ["nermin", stage],
            }
            cases.append(
                Case(
                    name=raw["name"],
                    path=path,
                    description=raw["description"],
                    raw=raw,
                    expect=expect,
                    tags=raw["tags"],
                )
            )
    return cases


def east_run_cases() -> list[Case]:
    """``east-run-brief`` (the KRI-185 East Run thread, Turkish) projected into chip cases.

    The fixture only names five clips (``c1``..``c5``); their analysis is HAND-AUTHORED here
    (a Turkish run: bridge, coast road, ferry, finish line, a talking-to-camera runner) because
    the replay fixture carries none. It is the Turkish, five-clip, one-speaker cell.
    """
    data = json.loads((KRIA_TURNS_DIR / "east-run-brief.json").read_text(encoding="utf-8"))
    subjects = [
        ("köprüde koşan koşucu", False),
        ("sahil yolunda gün doğumu", False),
        ("boğazda vapur", False),
        ("bitiş çizgisinde kalabalık", False),
        ("kameraya konuşan koşucu", True),
    ]
    clips = [
        {
            "media_id": cid,
            "ordinal": i + 1,
            "kind": "video",
            "subject": subject,
            "setting": "",
            "has_speech": speaks,
            "to_camera": speaks,
            "duration_s": 5.0,
            "notable_moments": [{"start_s": 0.5, "end_s": 2.0, "label": subject}],
        }
        for i, (cid, (subject, speaks)) in enumerate(zip(data["clip_ids"], subjects, strict=True))
    ]
    fid = data["fixture_id"]
    base = {
        "thread_id": f"thread-{fid}",
        "language": "tr",
        "client_protocol": 13,
        "media_digest": f"digest-{fid}",
        "runtime_version": 2,
        "edit_format": "montage",
        "clips": clips,
    }
    slots = [
        {**_slot(i), "media_id": c["media_id"], "duration_s": 3.0} for i, c in enumerate(clips)
    ]
    stages = [
        (
            "first_prompt",
            {**base, "state": "first_prompt", "understanding": "complete"},
            {
                "state": "first_prompt",
                "status": "ok",
                "understanding": "done",
                "kinds_include": ["default_edit", "captions"],
            },
        ),
        (
            "ready",
            {
                **base,
                "state": "ready",
                "understanding": "n/a",
                "render_generation_id": "gen-1",
                "variant_id": "v1",
                "snapshot_preset": "plain_montage",
                "snapshot": {"slots": slots, "total_duration_s": 3.0 * len(slots)},
            },
            {"state": "ready", "kinds_include": ["hook"], "kinds_exclude": ["title_animation"]},
        ),
    ]
    cases = []
    for stage, context, expect in stages:
        name = f"{fid}::{stage}"
        cases.append(
            Case(
                name=name,
                path=KRIA_TURNS_DIR / "east-run-brief.json",
                description=(
                    "East Run (Turkish, 5 clips, one talking to camera), hand-authored analysis"
                ),
                raw={"name": name, "context": context, "expect": expect},
                expect=expect,
                tags=["east_run", "tr", stage],
            )
        )
    return cases


def _intro_bar(bar_id: str, role: str | None, start_s: float | None, **extra: Any) -> dict:
    bar: dict[str, Any] = {
        "id": bar_id,
        "role": role,
        "text": "Assos: Settled since the Bronze Age",
        "end_s": 4.0,
        "font_family": "Inter",
        "size_px": 64,
        "effect": "static",
        **extra,
    }
    if start_s is not None:
        bar["start_s"] = start_s
    return bar


# Failure modes of the "Animate the title" chip (errors unit). The chip runs a
# ``group: "title"`` op, so it may be offered ONLY for a bar `kria_editor_ops_text.classify`
# calls the title. Each row is one way the suggester's idea of a title and the editor's can
# disagree; ``offered`` is whether the chip may appear.
TITLE_RESOLUTION_ROWS: list[tuple[str, str, list[dict[str, Any]], bool]] = [
    (
        "caption_only_first_bar_at_2s",
        "Assos: only researched caption bars, first at 2.27 s. The editor names no title.",
        [_intro_bar("authored-text-0", "generative_intro", 2.27)],
        False,
    ),
    (
        "intro_bar_inside_half_second",
        "An opening intro bar at 0.4 s is the title for both sides.",
        [_intro_bar("authored-text-0", "generative_intro", 0.4)],
        True,
    ),
    (
        "intro_bar_just_after_half_second",
        "0.51 s is past the editor's opening window: not a title.",
        [_intro_bar("authored-text-0", "generative_intro", 0.51)],
        False,
    ),
    (
        "intro_bar_without_a_start_time",
        "An older snapshot row with no start_s cannot be proven to open the video.",
        [_intro_bar("authored-text-0", "generative_intro", None)],
        False,
    ),
    (
        "explicit_title_role_late",
        "role=title is a title whenever it starts.",
        [_intro_bar("title-1", "title", 3.0)],
        True,
    ),
    (
        "late_intro_then_real_title",
        "A body caption first, an explicit title second: the chip points at the title.",
        [
            _intro_bar("authored-text-0", "generative_intro", 2.27),
            _intro_bar("title-1", "title", 6.0),
        ],
        True,
    ),
    (
        "chat_added_text_at_zero",
        "A chat-added kria- text at 0 s is free text, never the title.",
        [_intro_bar("kria-text-1", "generative_intro", 0.0)],
        False,
    ),
    (
        "removed_title",
        "A removed title bar is not offered.",
        [_intro_bar("guided-title", "generative_intro", 0.0, removed=True)],
        False,
    ),
]


def title_resolution_cases() -> list[Case]:
    cases = []
    for key, description, bars, offered in TITLE_RESOLUTION_ROWS:
        name = f"title_resolution::{key}"
        expect = {
            "state": "ready",
            **({"kinds_include": ["title_animation"]} if offered else {}),
            **({} if offered else {"kinds_exclude": ["title_animation"]}),
        }
        context = {
            "thread_id": f"thread-{key}",
            "state": "ready",
            "language": "en",
            "client_protocol": 13,
            "media_digest": f"digest-{key}",
            "understanding": "n/a",
            "runtime_version": 2,
            "render_generation_id": "gen-1",
            "variant_id": "v1",
            "resolved_archetype": "unified_montage",
            "edit_format": "montage",
            "clips": [
                {
                    "media_id": f"clip-{i + 1}",
                    "ordinal": i + 1,
                    "kind": "video",
                    "subject": subject,
                    "setting": "",
                    "has_speech": False,
                    "to_camera": False,
                    "duration_s": 4.0,
                    "notable_moments": [{"start_s": 0.5, "end_s": 2.0, "label": subject}],
                }
                for i, subject in enumerate(("old harbour", "temple columns", "hillside view"))
            ],
            "snapshot_preset": "guided_story_titled",
            "snapshot": {"text_bars": bars},
        }
        cases.append(
            Case(
                name=name,
                path=FIXTURE_DIR / f"{name}.json",
                description=description,
                raw={"name": name, "context": context, "expect": expect},
                expect=expect,
                tags=["title_resolution", "offered" if offered else "hidden"],
            )
        )
    return cases


def all_cases() -> list[Case]:
    return [*load_cases(), *nermin_cases(), *east_run_cases(), *title_resolution_cases()]


# ── G9 executability: recorded planner response -> real adapter + compiler ───────────


@dataclass
class Replay:
    engine: str  # "editor" | "strategy"
    notes: list[str]
    reply: str
    execution_path: str
    model_call_observed: bool
    # editor
    ops: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    # strategy
    action_kind: str | None = None
    reason_code: str | None = None
    decision: dict[str, Any] | None = None
    strategy_before: Any = None
    checked: Any = None  # CheckedStrategy | RefusedStrategy | None
    commands: list[str] = field(default_factory=list)


def _cassette_output(model_output: Any) -> Any:
    if isinstance(model_output, dict):
        return json.dumps(model_output, ensure_ascii=False)
    return model_output


def replay_editor(case: Case, message: str, model_output: dict[str, Any]) -> Replay:
    """``replay_v2_editor``: the recorded copilot answer through the REAL copilot parser,
    the v2 adapter and ``compile_editor_ops`` (compile-only; no stage/save/render)."""
    from tests.evals.request_following.models import FinalPlan, Turn  # noqa: PLC0415
    from tests.evals.request_following.runner import replay_v2_editor  # noqa: PLC0415

    turn = Turn(
        turn_id=f"chip-{case.name}",
        user_message=message,
        engine="v2_kria",
        kria={
            "cassette": {
                "agent_input": {
                    "utterance": message,
                    "prior_turns": [],
                    "variant_snapshot": case.snapshot,
                },
                "model_output": model_output,
            }
        },
    )
    _plan, reply, notes, proof = replay_v2_editor(turn, FinalPlan())
    ops: list[str] = []
    changes: list[str] = []
    for note in notes:
        if note.startswith("ops="):
            ops = list(json.loads(note[4:].replace("'", '"')))
        if note.startswith("changes="):
            changes = list(json.loads(note[8:].replace("'", '"')))
    return Replay(
        engine="editor",
        notes=notes,
        reply=reply,
        execution_path=proof.execution_path,
        model_call_observed=proof.model_call_observed,
        ops=ops,
        changes=changes,
    )


def main_creator_payload(case: Case, message: str) -> dict[str, Any]:
    manifest = build_manifest(case.context_dict)
    assert manifest is not None, f"{case.name}: no media, no manifest"
    clips = case.context_dict.get("clips") or []
    return {
        "user_message": message,
        "creator_request": message,
        "item_context": f"{len(clips)} clips attached; no recorded voiceover.",
        "media_context": [
            {
                "media_id": c["media_id"],
                "kind": c.get("kind", "video"),
                **({"duration_s": c["duration_s"]} if c.get("duration_s") else {}),
            }
            for c in clips
        ],
        "conversation": [],
        "creative_copy_state": [],
        "capability_manifest": manifest.model_dump(mode="json"),
    }


def replay_strategy(case: Case, message: str, model_output: dict[str, Any]) -> Replay:
    """``replay_v2_strategy`` for the compile proof, then the same recorded output once more
    through ``check_strategy_for_runtime_v2`` so ``optional_treatments`` can be diffed
    before/after (the policy drops disabled treatments silently)."""
    from app.agents._schemas.creator_agent import ResolvedCreatorManifest  # noqa: PLC0415
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput  # noqa: PLC0415
    from app.kria.planner import adapt_creator_action  # noqa: PLC0415
    from app.kria.registry import KRIA_TOOLS  # noqa: PLC0415
    from app.kria.strategy_policy import check_strategy_for_runtime_v2  # noqa: PLC0415
    from tests.evals.request_following.models import FinalPlan, Turn  # noqa: PLC0415
    from tests.evals.request_following.runner import replay_v2_strategy  # noqa: PLC0415
    from tests.evals.runners.eval_runner import (  # noqa: PLC0415
        CassetteModelClient,
        build_eval_run_context,
    )

    payload = main_creator_payload(case, message)
    turn = Turn(
        turn_id=f"chip-{case.name}",
        user_message=message,
        engine="v2_kria",
        kria={"strategy_cassette": {"agent_input": payload, "model_output": model_output}},
    )
    _plan, reply, notes, proof = replay_v2_strategy(turn, FinalPlan())
    output = MainCreatorAgent(CassetteModelClient(str(_cassette_output(model_output)))).run(
        MainCreatorInput.model_validate(payload),
        ctx=build_eval_run_context(f"chip-eval:{case.name}", is_live=False),
    )
    result = Replay(
        engine="strategy",
        notes=notes,
        reply=reply,
        execution_path=proof.execution_path,
        model_call_observed=proof.model_call_observed,
        action_kind=getattr(output.action, "kind", None),
        reason_code=getattr(output.action, "reason_code", None),
        decision=(
            output.creative_decision.model_dump(mode="json") if output.creative_decision else None
        ),
        commands=[
            cmd
            for n in notes
            if n.startswith("commands=")
            for cmd in re.findall(r"'([A-Za-z_]+)'", n)
        ],
    )
    typed = adapt_creator_action(output.action)
    if typed.intents:
        intent = typed.intents[0]
        strategy = (
            KRIA_TOOLS.get(intent.tool_name, intent.tool_version)
            .arguments_model.model_validate(intent.arguments)
            .strategy
        )
        result.strategy_before = strategy
        result.checked = check_strategy_for_runtime_v2(
            ResolvedCreatorManifest.model_validate(payload["capability_manifest"]), strategy
        )
    return result


# ── Independent checkers (return a list of human-readable failures; [] = pass) ────────


def _clips_by_media(case: Case) -> dict[str, dict[str, Any]]:
    return {c["media_id"]: c for c in case.context_dict.get("clips") or []}


def _non_caption_bars(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        b
        for b in (snapshot or {}).get("text_bars") or []
        if isinstance(b, dict) and not b.get("removed") and not b.get("caption_cue")
    ]


def check_grounding(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G1: every id, ordinal, moment and bar index in a chip exists in the fixture input."""
    problems: list[str] = []
    clips = _clips_by_media(case)
    snapshot = case.snapshot
    slot_media = {s.get("media_id") for s in (snapshot or {}).get("slots") or []}
    bars = _non_caption_bars(snapshot)
    for chip in out.chips:
        a = chip.anchor
        if a.media_id is not None and a.media_id not in clips and a.media_id not in slot_media:
            problems.append(f"{chip.id}: media_id {a.media_id!r} not in input")
        if a.clip_ordinal is not None:
            clip = clips.get(a.media_id or "")
            if clip is None or clip.get("ordinal") != a.clip_ordinal:
                problems.append(f"{chip.id}: clip_ordinal {a.clip_ordinal} != input ordinal")
        if a.moment is not None:
            clip = clips.get(a.media_id or "")
            moments = [
                (m["start_s"], m.get("end_s") or 0.0)
                for m in (clip or {}).get("notable_moments") or []
            ]
            if (a.moment.start_s, a.moment.end_s) not in moments:
                problems.append(f"{chip.id}: moment {a.moment} not a notable moment of the clip")
            duration = (clip or {}).get("duration_s")
            if duration and a.moment.start_s > duration:
                problems.append(f"{chip.id}: moment starts after the clip ends")
        if a.text_bar_index is not None and not (0 <= a.text_bar_index < len(bars)):
            problems.append(f"{chip.id}: text_bar_index {a.text_bar_index} out of range")
        if chip.kind == "title_animation" and a.text_bar_index is not None:
            # Dry run: the editor must resolve the very bar the chip points at as "the title".
            from app.services.kria_editor_ops_text import title_bar_ids

            if a.text_bar_index < len(bars) and bars[a.text_bar_index].get(
                "id"
            ) not in title_bar_ids(snapshot or {}):
                problems.append(f"{chip.id}: the editor cannot resolve this bar as the title")
        # A whole-video chip still needs real footage or a real render behind it.
        if not clips and snapshot is None:
            problems.append(f"{chip.id}: no footage and no render to ground on")
    return problems


def specificity_stats(outs: list[sc.SuggestionSetOut]) -> dict[str, int]:
    """How much of what G1 "passed" was actually checked, and how footage-specific the
    deterministic chips are. G1 only validates an anchor a chip carries and the template
    engine emits a closed deck of generic labels, so a 100% G1 does NOT show the chips fit THIS
    footage; that is the wording agent's job and needs the live replay to measure."""
    chips = [c for out in outs for c in out.chips]
    return {
        "chips": len(chips),
        "distinct_labels": len({c.label for c in chips}),
        "anchored": sum(
            1
            for c in chips
            if c.anchor.media_id is not None
            or c.anchor.clip_ordinal is not None
            or c.anchor.moment is not None
            or c.anchor.text_bar_index is not None
        ),
        "moment_anchored": sum(1 for c in chips if c.anchor.moment is not None),
        "with_why": sum(1 for c in chips if c.why),
    }


def check_capability_matrix(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G2: no chip whose manifest capability / editor op family is unavailable.

    Uses ``KIND_REQUIRES`` (independent of the engine's table) and the fixture's own
    manifest / snapshot, plus kind-specific data gates (catalog, cues, track, 2+ clips,
    speech). Dark kinds (``DARK_KINDS``) are never allowed.
    """
    problems: list[str] = []
    caps = capabilities_from_fixture(case.context_dict)
    snapshot = case.snapshot or {}
    families = set(snapshot.get("allowed_op_families") or [])
    clips = case.context_dict.get("clips") or []
    speech = any(c.get("has_speech") or c.get("to_camera") for c in clips)
    for chip in out.chips:
        if chip.kind in DARK_KINDS:
            problems.append(f"{chip.id}: dark kind {chip.kind!r} emitted")
            continue
        required = KIND_REQUIRES.get(chip.kind)
        if required is None:
            problems.append(f"{chip.id}: unknown kind {chip.kind!r}")
            continue
        wanted = required.get(out.state)
        if wanted is None:
            problems.append(f"{chip.id}: kind {chip.kind} has no gate for state {out.state}")
            continue
        if out.state == "first_prompt":
            for name in wanted:
                if caps.get(name, {}).get("available") is not True:
                    problems.append(f"{chip.id}: manifest capability {name!r} is not available")
            if chip.kind == "transition" and len(clips) < 2:
                problems.append(f"{chip.id}: transitions need 2+ clips")
            if chip.kind == "captions" and not speech:
                problems.append(f"{chip.id}: captions without any speech")
        else:
            if not families.intersection(wanted):
                problems.append(f"{chip.id}: no editor op family in {wanted} allowed")
            if chip.kind == "sfx" and not (snapshot.get("sfx") or {}).get("catalog"):
                problems.append(f"{chip.id}: sfx with an empty catalog (add_sfx fails closed)")
            if chip.kind == "music":
                if (snapshot.get("music") or {}).get("current_track_id") is None:
                    problems.append(f"{chip.id}: music without a track")
                if not speech:
                    problems.append(f"{chip.id}: turn the music down with no voice to hear")
            if chip.kind == "transition" and len(snapshot.get("slots") or []) < 2:
                problems.append(f"{chip.id}: transition with fewer than 2 slots")
    return problems


def check_not_already_in_edit(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G3: zero chips for a title / captions / music / slot-0 clip already in the edit."""
    problems: list[str] = []
    snapshot = case.snapshot or {}
    fmt = str(case.context_dict.get("edit_format") or "")
    for chip in out.chips:
        if out.state == "ready":
            if chip.kind == "hook" and any(_is_title_like(b) for b in _non_caption_bars(snapshot)):
                problems.append(f"{chip.id}: hook offered but a title/intro bar exists")
            if chip.kind == "captions":
                captions = snapshot.get("captions") or {}
                meta = captions.get("meta") or {}
                if meta.get("enabled") is not False or not captions.get("cues"):
                    problems.append(f"{chip.id}: captions already on (or nothing to show)")
            if chip.kind == "music":
                level = (snapshot.get("mix") or {}).get("music_level")
                if isinstance(level, (int, float)) and level <= 0.3:
                    problems.append(f"{chip.id}: music already quiet ({level})")
            if chip.kind == "sfx" and (snapshot.get("sfx") or {}).get("placements"):
                problems.append(f"{chip.id}: sound effects already placed")
            if chip.kind == "transition":
                slots = snapshot.get("slots") or []
                if any(str(s.get("transition_after") or "cut") != "cut" for s in slots[:-1]):
                    problems.append(f"{chip.id}: a boundary already has a transition")
            if chip.kind == "title_animation":
                bar = next((b for b in _non_caption_bars(snapshot) if _is_title_like(b)), None)
                phases = (bar or {}).get("animation_phases") or {}
                if bar is None or phases.get("entrance") not in (None, "", "none"):
                    problems.append(f"{chip.id}: title animation absent target or already set")
        else:
            if chip.kind == "captions" and (fmt == "subtitled" or fmt.startswith("narrated")):
                problems.append(f"{chip.id}: this format always captions")
    return problems


def _is_title_like(bar: dict[str, Any]) -> bool:
    """The codebase's own notion of a title bar (kria/plan_review.section_of_text_bar)."""
    bar_id = str(bar.get("id") or "")
    role = str(bar.get("role") or "")
    if bar_id.startswith(("clip-label-", "montage-text-", "group-title-")):
        return False
    return bar_id.startswith("guided-title") or role in {
        "title",
        "intro",
        "hook",
        "generative_intro",
    }


def check_diversity(out: sc.SuggestionSetOut) -> list[str]:
    """G4: at most 4 chips; 4 chips => 3+ kinds; no duplicate (kind, target); 1-based ranks."""
    problems: list[str] = []
    if len(out.chips) > 4:
        problems.append(f"{len(out.chips)} chips (max 4)")
    kinds = [c.kind for c in out.chips]
    if len(out.chips) == 4 and len(set(kinds)) < 3:
        problems.append(f"4 chips but only {len(set(kinds))} distinct kinds")
    seen: set[tuple[str, Any]] = set()
    for chip in out.chips:
        key = (
            chip.kind,
            chip.anchor.media_id,
            chip.anchor.text_bar_index,
            (chip.anchor.moment.start_s if chip.anchor.moment else None),
        )
        if key in seen:
            problems.append(f"duplicate (kind, target): {key}")
        seen.add(key)
    if len(set(kinds)) != len(kinds):
        problems.append(f"a kind appears twice: {kinds}")
    messages = [c.message for c in out.chips]
    if len(set(messages)) != len(messages):
        problems.append(f"duplicate message: {messages}")
    if sum(1 for c in out.chips if c.kind in ("hook", "pinned_text")) > 1:
        problems.append("more than one copy-bearing chip (hook / pinned_text)")
    ranks = [c.rank for c in out.chips]
    if ranks != list(range(1, len(out.chips) + 1)):
        problems.append(f"ranks are not 1-based, dense and ordered: {ranks}")
    return problems


def check_first_chip(out: sc.SuggestionSetOut) -> list[str]:
    """G5: chip #1 on first_prompt is default_edit (rank 1) with the exact media-only message."""
    if out.state != "first_prompt":
        return (
            ["default_edit outside first_prompt"]
            if any(c.default_edit or c.kind == "default_edit" for c in out.chips)
            else []
        )
    if not out.chips:
        return []  # first prompt with the default edit gated off: render nothing
    problems = []
    first = out.chips[0]
    if first.kind != "default_edit" or not first.default_edit:
        problems.append(f"chips[0] is {first.kind}, not default_edit")
    if first.rank != 1:
        problems.append(f"default_edit rank {first.rank} != 1")
    if first.message != "Suggest an edit.":
        problems.append(f"chips[0].message {first.message!r} != 'Suggest an edit.'")
    if any(c.default_edit for c in out.chips[1:]) or any(
        c.kind == "default_edit" for c in out.chips[1:]
    ):
        problems.append("default_edit appears after chips[0]")
    if len(out.chips) > 4:
        problems.append("first_prompt carries more than 4 chips")
    return problems


def _footage_words(case: Case) -> set[str]:
    """Folded 4+ letter words of the footage analysis (an INDEPENDENT restatement of what a
    ``why`` may be grounded in: subject, setting, notable-moment labels)."""
    words: set[str] = set()
    for clip in case.context_dict.get("clips") or []:
        texts = [clip.get("subject"), clip.get("setting")]
        texts += [m.get("label") for m in clip.get("notable_moments") or []]
        for text in texts:
            if isinstance(text, str):
                words.update(w for w in re.findall(r"[^\W\d_]+", text.casefold()) if len(w) >= 4)
    return words


def why_is_grounded(case: Case, why: str) -> bool:
    """Independent of ``validate_why``: some 4+ letter word of ``why`` shares its first four
    letters with a footage word (loose on purpose; the engine's check is stricter)."""
    footage = {w[:4] for w in _footage_words(case)}
    return any(w[:4] in footage for w in re.findall(r"[^\W\d_]+", why.casefold()) if len(w) >= 4)


def label_problems(label: str, kind: str, lang: str) -> list[str]:
    """Independent label rules (G6) shared by the gate and the G10 fuzz oracle."""
    problems = []
    if len(label) > 36:
        problems.append(f"{len(label)} chars (max 36)")
    if any(ch.isdigit() for ch in label):
        problems.append("digit in label")
    if any(ch in "\"'`“”‘’«»{}" for ch in label):
        problems.append("quote or placeholder brace in label")
    low = label.casefold()
    for bad in STOPLIST:
        if bad in low:
            problems.append(f"stoplist phrase {bad!r}")
    rendered, reason = copy_rules.validate_label(label, kind=kind, language=lang, slots={})
    if rendered is None:
        problems.append(f"label fails the validator ({reason})")
    return problems


def expected_evidence(case: Case, chip: sc.SuggestionChipOut, state: str) -> str | None:
    """The deterministic subtitle (KRI-769) a chip must carry when no agent ``why`` exists.

    Written independently of the engine: counts come from the fixture clips and slots.
    (Original docstring follows.)

    Built only from counts and edit state the server already holds (never Gemini prose, never
    ids or times), so it needs no model and cannot invent a moment. Every line states a fact the
    server measured, not a judgement about quality. Shown as the chip's subtitle whenever the
    wording agent supplied no grounded ``why`` of its own.
    """
    tr = (case.language if case.language in ("en", "tr") else "en") == "tr"
    clips = [c for c in case.context_dict.get("clips") or [] if isinstance(c, dict)]
    total = len(clips)
    speech = sum(1 for c in clips if c.get("has_speech") or c.get("to_camera"))
    quiet = total - speech
    kind = chip.kind
    line: str | None = None
    if kind == "default_edit":
        line = "Kesimi ve temposunu Kria seçer" if tr else "Kria picks the cuts and pace"
    elif kind == "captions" and speech:
        if speech == total:
            line = "Her klipte konuşma var" if tr else "Speech in every clip"
        else:
            line = (
                f"{speech}/{total} klipte konuşma var"
                if tr
                else f"Speech in {speech} of {total} clips"
            )
    elif kind == "sfx":
        if quiet > 0:
            line = (
                f"{quiet} klipte konuşma yok"
                if tr
                else ("1 clip has no speech" if quiet == 1 else f"{quiet} clips have no speech")
            )
        else:
            line = "Henüz ses efekti yok" if tr else "No sound effects yet"
    elif kind == "transition":
        slots = [
            x
            for x in (case.snapshot or {}).get("slots") or []
            if isinstance(x, dict) and not x.get("removed")
        ]
        cuts = len(slots) if state == "ready" else total
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
    elif kind == "hook":
        if state == "first_prompt":
            line = "Henüz bir açılış cümlesi yok" if tr else "No opening line yet"
        else:
            line = "Henüz başlık yok" if tr else "There is no title yet"
    elif kind == "music":
        line = "Konuşmalı klipler ve müzik var" if tr else "Speech clips and a music track"
    elif kind == "title_animation":
        line = "Başlık sabit duruyor" if tr else "The title just sits there"
    return line if line and len(line) <= 48 else None


def check_hygiene(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G6: length, language, quotes, stoplist; label parity; ``why`` grounded and short."""
    problems: list[str] = []
    lang = case.language if case.language in ("en", "tr") else "en"
    for chip in out.chips:
        if len(chip.label) > 36:
            problems.append(f"{chip.id}: label {len(chip.label)} chars (max 36)")
        if len(chip.message) > 120:
            problems.append(f"{chip.id}: message {len(chip.message)} chars (max 120)")
        if any(ch in chip.message for ch in "\"'`“”‘’«»"):
            problems.append(f"{chip.id}: quote character in message")
        if "{" in chip.label or "{" in chip.message:
            problems.append(f"{chip.id}: unrendered placeholder")
        low = f"{chip.label} {chip.message} {chip.why or ''}".casefold()
        for bad in STOPLIST:
            if bad in low:
                problems.append(f"{chip.id}: stoplist phrase {bad!r}")
        if chip.label_source not in ("agent", "template"):
            problems.append(f"{chip.id}: label_source {chip.label_source!r}")
        elif chip.label_source == "template":
            if chip.label != LABEL_DECK.get(chip.kind, {}).get(lang):
                problems.append(f"{chip.id}: template label {chip.label!r} is not the {lang} label")
        else:
            problems += [
                f"{chip.id}: agent {p}" for p in label_problems(chip.label, chip.kind, lang)
            ]
        if chip.why is not None and chip.why == expected_evidence(case, chip, out.state):
            continue  # the deterministic evidence line: counted from the footage, no model
        if chip.why is not None:
            if len(chip.why) > 48:
                problems.append(f"{chip.id}: why {len(chip.why)} chars (max 48)")
            if not why_is_grounded(case, chip.why):
                problems.append(f"{chip.id}: why {chip.why!r} shares no word with the footage")
            if chip.label_source == "template" and out.wording == "fallback":
                problems.append(f"{chip.id}: a why on a fallback set (no agent ran)")
    return problems


def _leak_phrases(case: Case) -> set[str]:
    leaks: set[str] = set()
    for clip in case.context_dict.get("clips") or []:
        leaks.update(
            s.casefold()
            for s in (clip.get("subject"), clip.get("setting"))
            if isinstance(s, str) and len(s) >= 4
        )
        leaks.update(
            m["label"].casefold()
            for m in clip.get("notable_moments") or []
            if isinstance(m.get("label"), str) and len(m["label"]) >= 4
        )
    headline = case.context_dict.get("brief_headline")
    if isinstance(headline, str) and len(headline) >= 4:
        leaks.add(headline.casefold())
    return leaks


def check_no_gemini_text(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G7: every message is a member of the closed template set; no clip subject / moment label
    / setting / brief headline from the input appears in a ``message`` OR a ``label`` (Gemini
    text may live only in ``why``)."""
    problems: list[str] = []
    lang = case.language if case.language in ("en", "tr") else "en"
    leaks = _leak_phrases(case)
    for chip in out.chips:
        if chip.message != MESSAGE_DECK.get(chip.kind, {}).get(lang):
            problems.append(f"{chip.id}: message {chip.message!r} is not in the closed deck")
        for leak in leaks:
            if leak in chip.message.casefold():
                problems.append(f"{chip.id}: input text {leak!r} leaked into the message")
            if leak in chip.label.casefold():
                problems.append(f"{chip.id}: input text {leak!r} leaked into the label")
    return problems


def check_determinism(case: Case) -> list[str]:
    """G8: the same context in, byte-identical JSON out (twice, and via a rebuilt context)."""
    first = run_case(case).model_dump_json()
    second = run_case(case).model_dump_json()
    return [] if first == second else ["two runs of the same fixture differ"]


KNOWN_KINDS = {
    "default_edit",
    "hook",
    "captions",
    "music",
    "sfx",
    "transition",
    "title_animation",
    "pinned_text",
    "opener_clip",
}


def check_wire(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """G11: the response obeys the fixed wire contract (shape and state semantics)."""
    problems: list[str] = []
    if out.schema_version != 1:
        problems.append(f"schema_version {out.schema_version}")
    if out.state not in ("first_prompt", "ready", "none"):
        problems.append(f"state {out.state!r}")
    if out.status not in ("ok", "pending", "unavailable"):
        problems.append(f"status {out.status!r}")
    if out.understanding not in ("pending", "done", "empty"):
        problems.append(f"understanding {out.understanding!r}")
    if out.wording not in ("agent", "fallback", "pending"):
        problems.append(f"wording {out.wording!r}")
    if out.status == "pending":
        if not out.retry_after_s or out.retry_after_s <= 0:
            problems.append("pending without retry_after_s")
        if out.wording != "pending":
            problems.append(f"pending set reports wording {out.wording!r}")
        if [c.kind for c in out.chips] not in (["default_edit"], []):
            problems.append(f"pending set carries {[c.kind for c in out.chips]}")
    elif out.retry_after_s is not None:
        problems.append(f"retry_after_s {out.retry_after_s} on a {out.status} set")
    if out.state == "none" and out.chips:
        problems.append("state none carries chips")
    if out.status == "unavailable" and out.chips:
        problems.append("unavailable set carries chips")
    if out.status == "ok" and out.chips and out.wording == "pending":
        problems.append("final set still says wording pending")
    if out.chips and not out.set_id.startswith("sg_"):
        problems.append(f"set_id {out.set_id!r}")
    ids = [c.id for c in out.chips]
    if len(set(ids)) != len(ids):
        problems.append("duplicate chip id")
    for chip in out.chips:
        if not chip.id.startswith("sc_"):
            problems.append(f"{chip.id}: chip id shape")
        if chip.kind not in KNOWN_KINDS:
            problems.append(f"{chip.id}: kind {chip.kind!r} outside the closed enum")
        if chip.default_edit != (chip.kind == "default_edit" and out.state == "first_prompt"):
            problems.append(f"{chip.id}: default_edit flag {chip.default_edit}")
        if chip.requires_protocol is not None and (
            not isinstance(chip.requires_protocol, int) or chip.requires_protocol < 1
        ):
            problems.append(f"{chip.id}: requires_protocol {chip.requires_protocol!r}")
        if not chip.label.strip() or not chip.message.strip():
            problems.append(f"{chip.id}: empty label or message")
    anchor = out.anchor
    if out.chips and not anchor.media_digest:
        problems.append("anchor.media_digest is empty")
    if out.state == "ready" and out.chips and not anchor.render_generation_id:
        problems.append("ready set without anchor.render_generation_id")
    return problems


GATES = {
    "G1 grounding": check_grounding,
    "G2 capability matrix": check_capability_matrix,
    "G3 not already in edit": check_not_already_in_edit,
    "G4 diversity": lambda case, out: check_diversity(out),
    "G5 chip #1 is default_edit": lambda case, out: check_first_chip(out),
    "G6 hygiene": check_hygiene,
    "G7 closed template set": check_no_gemini_text,
    "G8 determinism": lambda case, out: check_determinism(case),
    "G11 wire contract": check_wire,
}


def evaluate_case(case: Case) -> dict[str, Any]:
    out = run_case(case)
    results = {name: fn(case, out) for name, fn in GATES.items()}
    return {"case": case, "out": out, "failures": results}


def expectation_failures(case: Case, out: sc.SuggestionSetOut) -> list[str]:
    """The fixture's own ``expect`` block (a regression pin, reviewed by a human)."""
    expect = case.expect
    problems: list[str] = []
    kinds = [c.kind for c in out.chips]
    if "state" in expect and out.state != expect["state"]:
        problems.append(f"state {out.state!r} != {expect['state']!r}")
    if "status" in expect and out.status != expect["status"]:
        problems.append(f"status {out.status!r} != {expect['status']!r}")
    if "understanding" in expect and out.understanding != expect["understanding"]:
        problems.append(f"understanding {out.understanding!r} != {expect['understanding']!r}")
    if "retry_after_s" in expect and out.retry_after_s != expect["retry_after_s"]:
        problems.append(f"retry_after_s {out.retry_after_s!r} != {expect['retry_after_s']!r}")
    if "wording" in expect and out.wording != expect["wording"]:
        problems.append(f"wording {out.wording!r} != {expect['wording']!r}")
    if "kinds" in expect and kinds != expect["kinds"]:
        problems.append(f"kinds {kinds} != {expect['kinds']}")
    for kind in expect.get("kinds_include", []):
        if kind not in kinds:
            problems.append(f"missing kind {kind}: {kinds}")
    for kind in expect.get("kinds_exclude", []):
        if kind in kinds:
            problems.append(f"forbidden kind {kind} present: {kinds}")
    return problems


# ── Known gaps (strict xfails in the test; listed in the report) ─────────────────────

# Empty on purpose: the two engine bugs that lived here (generative_intro bars invisible to
# `_title_bar`; title_animation offered without the text family) are fixed. Add a cell only
# with a reason; the test treats each as a strict xfail and the report prints the count.
KNOWN_GAPS: dict[tuple[str, str], str] = {}


# ── G9 driver shared by the test and the report ──────────────────────────────────────


def load_cassettes() -> list[dict[str, Any]]:
    return json.loads(CASSETTE_FILE.read_text(encoding="utf-8"))["cassettes"]


def cassette_failures(cassette: dict[str, Any], cases: dict[str, Case]) -> list[str]:
    """Replay one recorded answer through the real adapter + compiler. [] = executable."""
    case = cases[cassette["fixture"]]
    expect = cassette["expect"]
    problems: list[str] = []
    if cassette["engine"] == "editor":
        replay = replay_editor(case, cassette["message"], cassette["model_output"])
        if replay.execution_path != "compile_only":
            problems.append(f"execution_path {replay.execution_path}")
        if replay.ops != expect["ops"]:
            problems.append(f"ops {replay.ops} != {expect['ops']} ({replay.notes})")
        if expect.get("changes") is not None and replay.changes != expect["changes"]:
            problems.append(f"changes {replay.changes} != {expect['changes']}")
        if not replay.changes:
            problems.append(f"compile produced no change: {replay.notes}")
        if any("rejected" in n or "no executable" in n for n in replay.notes):
            problems.append(f"compile refused: {replay.notes}")
        return problems
    from app.kria.strategy_policy import CheckedStrategy  # noqa: PLC0415

    replay = replay_strategy(case, cassette["message"], cassette["model_output"])
    if replay.execution_path != "compile_only":
        problems.append(f"execution_path {replay.execution_path}")
    if replay.action_kind != expect["action_kind"]:
        problems.append(f"action {replay.action_kind} != {expect['action_kind']}")
    if cassette["kind"] == "hook":
        # "reaches wording approval with a candidate": weaker than executing an op.
        if replay.reason_code != expect["reason_code"]:
            problems.append(f"reason_code {replay.reason_code}")
        decision = replay.decision or {}
        if decision.get("status") != expect["decision_status"]:
            problems.append(f"decision status {decision.get('status')}")
        if decision.get("target") != expect["decision_target"]:
            problems.append(f"decision target {decision.get('target')}")
        if not decision.get("proposed_text"):
            problems.append("a candidate needs a proposed wording")
        return problems
    if not isinstance(replay.checked, CheckedStrategy):
        return [*problems, f"strategy refused: {replay.checked!r}"]
    if not replay.commands:
        problems.append("the strategy compiled to no commands")
    strategy = replay.checked.strategy
    if "caption_style" in expect and strategy.caption_style != expect["caption_style"]:
        problems.append(f"policy coerced caption_style to {strategy.caption_style}")
    if "treatment" in expect:
        if expect["treatment"] not in replay.strategy_before.optional_treatments:
            problems.append("recorded answer lacks the treatment")
        elif expect["treatment"] not in strategy.optional_treatments:
            problems.append(f"treatment {expect['treatment']!r} silently dropped by the policy")
    return problems
