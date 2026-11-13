"""Author one request-following thread per `agent_ops` group of the editor parity table.

    cd src/apps/api && python -m tests.evals.request_following.author_action_fixtures [--probe]

``app/kria/editor_capability_parity.py`` says which editor actions the agent has an op for. It
checks names, never that the op works from an iPhone chat. Each thread here is a plain creator
request for one group on a phone snapshot (``render_destination: "device"``), built by the real
``build_editor_snapshot`` with the capability map the phone advertises when the lane's gate is
open. It expects the op to be applied.

Three facts are frozen per thread, so replay needs no builder and no network:

* the snapshot the model saw (``copilot.agent_input``),
* what the real parser and compiler do with a hand-written reference op for the request on the
  real variant (``reference_probe``), and
* the requirement ``op_applied``, whose ``reference_compiles`` param is that probe's verdict.

The harness replay rebuilds a variant from the snapshot alone and cannot hold split sections,
backgrounds or cards, so the model's op is judged by the real parser and the reference op by the
real compiler, never by the harness compile.

``--probe`` prints the verdict per thread without writing.
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from .models import PlanText
from .phone_snapshot import (
    SOURCE_PREFIX,
    clip_context_for,
    job_for,
    seed_plan,
    variant_for,
)
from .runner import THREAD_DIR, load_footage

FOOTAGE_ID = "harbor_run"

PHONE_CAPS: dict[str, Any] = {
    "text_elements": True,
    "intro_controls": True,
    "timeline": True,
    "clips": {
        "transitions": {"editable": True},
        "tilt": {"editable": True},
        "audio": True,
        "source_crop": True,
        "playback_rate": True,
    },
    "mix": True,
    "music_operations": {"remove": True},
    "original_audio": {"editable": True},
    "sfx": True,
    "overlays": True,
    "caption_animation": True,
}

TITLE = PlanText(id="title", role="title", text="Harbour run", start_s=0.0, end_s=2.5)


@dataclass
class Scenario:
    group: str
    say: str
    expect: list[dict[str, Any]]
    reference: list[dict[str, Any]]
    note: str = ""
    suffix: str = ""
    plan_texts: tuple[PlanText, ...] = ()
    variant: Callable[[dict[str, Any]], None] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, Any] = field(default_factory=dict)
    needs_split: bool = False
    # Editor predicates to pin while the snapshot is built (gate state that depends on the app
    # build and on rollout settings the authoring run cannot read).
    patches: dict[str, Callable[..., Any]] = field(default_factory=dict)
    # Accepted generated images the compiler resolves by pool id (the planner reads them).
    pool_assets: dict[str, str] = field(default_factory=dict)

    @property
    def fixture_id(self) -> str:
        return f"action_{self.group}{('__' + self.suffix) if self.suffix else ''}"


def _cues(variant: dict[str, Any]) -> None:
    variant["resolved_archetype"] = "subtitled"
    variant["caption_cues"] = [
        {"id": "cue-1", "text": "so we started at the point", "start_s": 0.0, "end_s": 2.0},
        {"id": "cue-2", "text": "then ran along the harbour", "start_s": 2.0, "end_s": 4.5},
        {"id": "cue-3", "text": "and finished at the pier", "start_s": 4.5, "end_s": 7.0},
    ]


def _split_room_to_loop(variant: dict[str, Any]) -> None:
    _split(variant)
    variant["user_timeline"]["split_screen_sections"][0]["duration_s"] = 6.0


def _split_two_in_panel(variant: dict[str, Any]) -> None:
    _split(variant)
    timeline = variant["user_timeline"]
    timeline["slots"][2].update(split_section_id="duo", split_panel_index=0, split_offset_s=3.0)
    timeline["split_screen_sections"][0]["duration_s"] = 7.0


def _split(variant: dict[str, Any]) -> None:
    timeline = variant["user_timeline"]
    slots = timeline["slots"]
    for index, panel in ((0, 0), (1, 1)):
        slots[index].update(split_section_id="duo", split_panel_index=panel, split_offset_s=0.0)
    timeline["split_screen_sections"] = [
        {
            "id": "duo",
            "duration_s": 3.0,
            "panel_count": 2,
            "panel_weights": [1, 1],
            "gutter": 0,
            "background_color": "#000000",
        }
    ]


def _music(variant: dict[str, Any]) -> None:
    variant["music_track_id"] = "00000000-0000-0000-0000-000000000001"
    variant["mix"] = 0.6
    variant["original_audio_level"] = 1.0


def _sfx(variant: dict[str, Any]) -> None:
    variant["sound_effects"] = []


def _talking(variant: dict[str, Any]) -> None:
    variant["resolved_archetype"] = "subtitled"
    variant["speaker_background"] = {"media_id": "bg-2", "kind": "video", "source_start_s": 0.0}
    variant["speaker_frame"] = {"x": 0.55, "y": 0.5, "width": 0.4, "height": 0.45}


BACKGROUND_OPEN = {
    "background_state": lambda *_: (True, None),
    "background_focus_open": lambda *_: True,
    "background_windows_open": lambda *_: True,
    "background_color_reason": lambda *_: None,
}
BACKGROUND_POOL = [
    {"id": "bg-1", "kind": "image", "label": "beach.jpg"},
    {"id": "bg-2", "kind": "video", "label": "waves.mp4", "duration_s": 8.0},
]


def _accepted_image(variant: dict[str, Any]) -> None:
    variant["resolved_archetype"] = "subtitled"
    variant["media_overlays"] = []


SFX_CATALOG = [{"id": "whoosh_01", "label": "Whoosh", "duration_s": 0.8, "tags": ["transition"]}]


def _cards(variant: dict[str, Any]) -> None:
    variant["resolved_archetype"] = "subtitled"
    variant["media_overlays"] = [
        {
            "id": "card-1",
            "kind": "image",
            "start_s": 1.0,
            "end_s": 4.0,
            "x_frac": 0.74,
            "y_frac": 0.22,
            "scale": 0.36,
            "display_mode": "pip",
        }
    ]


SCENARIOS: list[Scenario] = [
    Scenario(
        "text_authoring",
        "Change the title to 'Sunday harbour run'.",
        [{"one_of": ["set_title", "edit_text", "rewrite_text"]}],
        [{"op": "set_title", "title": "Sunday harbour run"}],
        plan_texts=(TITLE,),
    ),
    Scenario(
        "text_look",
        "Make the title bigger and yellow.",
        [{"one_of": ["patch_text_style", "patch_text", "patch_text_appearance"]}],
        [{"op": "patch_text_style", "bar_index": 0, "patch": {"size_px": 96, "color": "#FFD60A"}}],
        plan_texts=(TITLE,),
    ),
    Scenario(
        "text_timing",
        "Keep the title on screen for the first four seconds.",
        [{"one_of": ["set_text_timing", "set_texts_timing"]}],
        [{"op": "set_text_timing", "bar_index": 0, "start_s": 0.0, "end_s": 4.0}],
        plan_texts=(TITLE,),
    ),
    Scenario(
        "captions",
        "Change the second caption to say 'then we ran the whole harbour'.",
        [{"one_of": ["edit_caption", "replace_caption_text"]}],
        [{"op": "edit_caption", "cue_index": 1, "text": "then we ran the whole harbour"}],
        variant=_cues,
    ),
    Scenario(
        "caption_animation",
        "Make the captions pop in.",
        [{"op": "set_caption_meta", "field": "animation_phases"}],
        [{"op": "set_caption_meta", "patch": {"animation_phases": {"entrance": "pop"}}}],
        variant=_cues,
    ),
    Scenario(
        "clip_trim_and_move",
        "Cut the first clip down to 2 seconds.",
        [{"one_of": ["set_clip_duration", "patch_slots", "trim_output_start"]}],
        [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 2.0}],
    ),
    Scenario(
        "clip_add_remove",
        "Remove the third clip.",
        [{"one_of": ["remove_clip", "patch_slots"]}],
        [{"op": "remove_clip", "slot_index": 2}],
    ),
    Scenario(
        "clip_look_and_transition",
        "Add a crossfade between the first and second clip.",
        [{"one_of": ["set_transition", "patch_slots"]}],
        [{"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}],
    ),
    Scenario(
        "clip_look_and_transition",
        "Give the whole video a golden hour look.",
        [{"one_of": ["set_look_preset", "patch_slots"]}],
        [{"op": "patch_slots", "selector": {"all": True}, "patch": {"look_preset": "golden_hour"}}],
        suffix="look_preset",
        note="Look presets are not offered on the phone today; expected to fail.",
    ),
    Scenario(
        "clip_look_and_transition",
        "Add a glitch effect to the second clip.",
        [{"op": "apply_custom_effect"}],
        [{"op": "apply_custom_effect", "effect": {"name": "glitch"}}],
        suffix="custom_effect",
        note="Custom effects have no phone compile handler today; expected to fail.",
    ),
    Scenario(
        "clip_speed",
        "Play the third clip at half speed.",
        [{"op": "patch_slots", "field": "playback_rate"}],
        [{"op": "patch_slots", "selector": {"slot_indexes": [2]}, "patch": {"playback_rate": 0.5}}],
    ),
    Scenario(
        "clip_crop",
        "Crop the second clip tighter on the left side.",
        [{"op": "patch_slots", "field": "source_crop"}],
        [
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [1]},
                "patch": {"source_crop": {"x": 0.0, "y": 0.0, "width": 0.6, "height": 1.0}},
            }
        ],
    ),
    Scenario(
        "clip_duplicate",
        "Duplicate the first clip.",
        [{"op": "duplicate_clip"}],
        [{"op": "duplicate_clip", "slot_index": 0}],
    ),
    Scenario(
        "clip_source_replacement",
        "Replace the second clip with the last one I uploaded.",
        [{"op": "replace_clip_source"}],
        [{"op": "replace_clip_source", "slot_index": 1, "clip_index": 7}],
    ),
    Scenario(
        "clip_tilt",
        "Tilt the second clip ten degrees to the right.",
        [{"op": "patch_slots", "field": "tilt"}],
        [
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [1]},
                "patch": {"tilt": {"rotation_deg": 10}},
            }
        ],
    ),
    Scenario(
        "clip_audio",
        "Mute the original sound.",
        [{"op": "set_mix"}],
        [{"op": "set_mix", "original_level": 0}],
        variant=_music,
    ),
    Scenario(
        "clip_source_gain",
        "Make the sound of the third clip louder.",
        [{"op": "patch_slots", "field": "source_audio_volume"}],
        [
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [2]},
                "patch": {"source_audio_volume": 1.5},
            }
        ],
    ),
    Scenario(
        "split_screen_create",
        "Put the first two clips side by side on one screen.",
        [{"op": "create_split_screen"}],
        [{"op": "create_split_screen", "slot_indexes": [0, 1], "panel_count": 2, "duration_s": 4}],
        needs_split=True,
    ),
    Scenario(
        "split_screen_layout",
        "Make the split screen three panels with a thin gap.",
        [{"op": "update_split_screen"}],
        [{"op": "update_split_screen", "section_id": "duo", "gutter": 0.02}],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_panel_order",
        "Swap the top and bottom panels of the split screen.",
        [{"op": "swap_split_panels"}],
        [{"op": "swap_split_panels", "section_id": "duo", "first_panel": 0, "second_panel": 1}],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_clip_order",
        "In the split screen, play the second clip of the top panel first.",
        [{"op": "move_split_clip"}],
        [{"op": "move_split_clip", "slot_index": 2, "direction": "earlier"}],
        variant=_split_two_in_panel,
        needs_split=True,
    ),
    Scenario(
        "split_screen_section_order",
        "Move the split screen to the end of the video.",
        [{"op": "move_split_screen"}],
        [{"op": "move_split_screen", "section_id": "duo", "direction": "later"}],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_clip_offset",
        "Start the bottom clip of the split screen one second later.",
        [{"op": "assign_split_panel", "field": "offset_s"}],
        [
            {
                "op": "assign_split_panel",
                "slot_index": 1,
                "section_id": "duo",
                "panel_index": 1,
                "offset_s": 1.0,
            }
        ],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_remove",
        "Remove the split screen.",
        [{"op": "remove_split_screen"}],
        [{"op": "remove_split_screen", "section_id": "duo"}],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_whole_video",
        "Make the split screen last the whole video.",
        [{"one_of": ["update_split_screen", "assign_split_panel"]}],
        [{"op": "update_split_screen", "section_id": "duo", "duration_s": 23}],
        variant=_split,
        needs_split=True,
    ),
    Scenario(
        "split_screen_loop_fill",
        "Loop the top clip of the split screen so it fills its panel.",
        [{"op": "assign_split_panel", "field": "duplicate"}],
        [
            {
                "op": "assign_split_panel",
                "slot_index": 0,
                "section_id": "duo",
                "panel_index": 0,
                "offset_s": 3.0,
                "duplicate": True,
            }
        ],
        variant=_split_room_to_loop,
        needs_split=True,
    ),
    Scenario(
        "music",
        "Lower the music volume.",
        [{"op": "set_mix"}],
        [{"op": "set_mix", "music_level": 0.3}],
        variant=_music,
    ),
    Scenario(
        "music",
        "Swap the music for something calmer.",
        [{"op": "swap_music"}],
        [{"op": "swap_music", "track_id": "00000000-0000-0000-0000-000000000002"}],
        suffix="swap",
        variant=_music,
        note="The snapshot never lists swap candidates; expected to fail.",
    ),
    Scenario(
        "sound_effects",
        "Add a whoosh at 2 seconds.",
        [{"op": "add_sfx"}],
        [{"op": "add_sfx", "effect_id": "whoosh_01", "at_s": 2.0}],
        variant=_sfx,
        context={"sfx_catalog": SFX_CATALOG},
    ),
    Scenario(
        "media_overlays",
        "Make the photo on top bigger.",
        [{"op": "patch_overlay"}],
        [{"op": "patch_overlay", "overlay_index": 0, "patch": {"scale": 0.5}}],
        variant=_cards,
    ),
    Scenario(
        "visual_blocks_and_motion",
        "Add a motion title card at the start.",
        [{"op": "add_motion_block"}],
        [
            {
                "op": "add_motion_block",
                "preset_id": "title_card",
                "start_s": 0.0,
                "end_s": 2.0,
                "params": {},
            }
        ],
        note="Motion blocks are not in the phone's op families; expected to fail.",
    ),
    Scenario(
        "camera_effects",
        "Add a camera pulse at 3 seconds.",
        [{"op": "add_camera_effect"}],
        [{"op": "add_camera_effect", "start_s": 3.0, "end_s": 3.6}],
        note="The phone has no camera-effect lane; expected to fail.",
    ),
    Scenario(
        "carousel",
        "Add a photo carousel in the middle of the video.",
        [{"op": "set_carousel_moment"}],
        [{"op": "set_carousel_moment", "config": {"position": "middle"}}],
        note="Carousel moments are not in the phone's op families; expected to fail.",
    ),
    Scenario(
        "speaker_background",
        "Put the beach photo behind me.",
        [{"op": "set_background"}],
        [{"op": "set_background", "asset_id": "bg-1"}],
        variant=_talking,
        context={"background_pool": BACKGROUND_POOL},
        patches=BACKGROUND_OPEN,
    ),
    Scenario(
        "background_focus",
        "Center the recording behind me and zoom in a little.",
        [{"op": "set_background_focus"}],
        [{"op": "set_background_focus", "position": "center", "zoom_delta": 0.25}],
        variant=_talking,
        context={"background_pool": BACKGROUND_POOL},
        patches=BACKGROUND_OPEN,
    ),
    Scenario(
        "speaker_frame_by_hand",
        "Move me to the small bottom left corner.",
        [{"op": "set_speaker_frame"}],
        [{"op": "set_speaker_frame", "preset": "small_bottom_left"}],
        variant=_talking,
        context={"background_pool": BACKGROUND_POOL},
        patches=BACKGROUND_OPEN,
    ),
    Scenario(
        "generated_overlay_approval",
        "Put the picture you made on top from 2 to 4 seconds.",
        [{"op": "add_overlay"}],
        [{"op": "add_overlay", "asset_id": "gen-1", "start_s": 2.0, "end_s": 4.0}],
        variant=_accepted_image,
        context={
            "generated_overlay_pool": [
                {"id": "gen-1", "kind": "image", "subject": "harbour sketch"}
            ]
        },
        pool_assets={"gen-1": f"{SOURCE_PREFIX}assets/gen-1.png"},
    ),
    Scenario(
        "history",
        "Undo that.",
        [{"op": "undo_last_edit"}],
        [{"op": "undo_last_edit"}],
        note="Undo has no phone compile handler; expected to fail.",
    ),
]


def _group_names() -> list[str]:
    from app.kria.editor_capability_parity import PARITY_GROUPS

    return [name for name, group in PARITY_GROUPS.items() if group.kind == "agent_ops"]


def build(scenario: Scenario) -> dict[str, Any]:
    """Snapshot, parse verdict and compile verdict for one scenario."""
    from app.agents._runtime import ModelClient
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.services import kria_editor_ops as editor

    footage = load_footage(FOOTAGE_ID)
    plan = seed_plan(footage).model_copy(update={"texts": list(scenario.plan_texts)})
    variant = variant_for(footage, plan)
    variant["editor_timeline_mode"] = "authored"
    variant["user_timeline"] = variant.pop("ai_timeline")
    if scenario.variant:
        scenario.variant(variant)
    job = job_for(footage, variant)
    caps = {**PHONE_CAPS, **scenario.caps}
    context = {**clip_context_for(footage), **scenario.context}
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(editor, "_editor_capabilities", lambda *_args: copy.deepcopy(caps))
        )
        for name, replacement in scenario.patches.items():
            stack.enter_context(mock.patch.object(editor, name, replacement))
        if scenario.needs_split:
            stack.enter_context(
                mock.patch.object(editor, "split_screen_available", lambda *_: True)
            )
        snapshot = editor.build_editor_snapshot(job, variant, clip_context=context)
        ids = [c.clip_id for c in footage.clips]
        for key in ("source_pool", "replacement_source_pool"):
            for row in snapshot.get(key) or []:
                row["media_id"] = ids[row["clip_index"]]
        raw = json.dumps(
            {
                "intent": "edit",
                "ops": scenario.reference,
                "confidence": 0.95,
                "reply": "Done.",
                "claimed_ops": [op["op"] for op in scenario.reference],
            }
        )
        parsed = EditCopilotAgent(ModelClient()).parse(
            raw, EditCopilotInput(utterance=scenario.say, variant_snapshot=snapshot)
        )
        probe: dict[str, Any] = {
            "families": snapshot["allowed_op_families"],
            "parsed_ops": [op.get("op") for op in parsed.ops],
            "parse_outcome": parsed.outcome,
            "parse_reasons": [dict(r) for r in parsed.rejection_reasons],
        }
        if parsed.ops:
            try:
                editor.compile_editor_ops(
                    job, variant, parsed.ops, pool_assets=scenario.pool_assets or None
                )
                probe["compiles"] = True
            except Exception as exc:  # noqa: BLE001 - the verdict is the message
                probe["compiles"] = False
                probe["compile_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return {"snapshot": snapshot, "probe": probe}


def fixture_for(scenario: Scenario, built: dict[str, Any]) -> dict[str, Any]:
    probe = built["probe"]
    reaches = bool(probe["parsed_ops"]) and probe.get("compiles") is True
    blocked_reason = ""
    if not reaches:
        reasons = probe["parse_reasons"]
        blocked_reason = (
            probe["compile_error"]
            if probe.get("compile_error")
            else "; ".join(f"{r['op']}: {r['detail']}" for r in reasons)
        )
    footage = load_footage(FOOTAGE_ID)
    plan = seed_plan(footage)
    return {
        "fixture_id": scenario.fixture_id,
        "provenance": "authored",
        "footage": FOOTAGE_ID,
        "description": (
            f"Parity group {scenario.group}: a plain request on a phone snapshot, expecting the op "
            f"applied. {scenario.note}".strip()
        ),
        "turns": [
            {
                "turn_id": "t0",
                "user_message": scenario.say,
                "engine": "v1_copilot",
                "copilot": {
                    "agent_input": {
                        "utterance": scenario.say,
                        "prior_turns": [],
                        "variant_snapshot": built["snapshot"],
                    }
                },
            }
        ],
        "requirements": [
            {
                "id": f"{scenario.group}-op-applied",
                "kind": "style",
                "scope": "global",
                "request_type": "action_parity",
                "checker": "op_applied",
                "params": {
                    "ops": scenario.expect,
                    "stage": "parsed",
                    "reference_compiles": reaches,
                    **({} if reaches else {"blocked_reason": blocked_reason}),
                },
                "source": scenario.say,
                "introduced_in_turn": 0,
                "claim_terms": [],
            }
        ],
        "initial_plan": plan.model_dump(mode="json", exclude_none=True),
        "reference": {
            "plan_after": plan.model_dump(mode="json", exclude_none=True),
            "reply": "Done.",
            "applied_ops": scenario.reference,
        },
        "reference_probe": probe,
    }


def _keep_recording(path: Any, fresh: dict[str, Any]) -> dict[str, Any]:
    """Re-authoring must not drop a paid recording whose input is unchanged."""
    if not path.exists():
        return fresh
    old = json.loads(path.read_text(encoding="utf-8"))
    recorded = old["turns"][0].get("copilot") or {}
    if (
        "raw_text" in recorded
        and recorded.get("agent_input") == fresh["turns"][0]["copilot"]["agent_input"]
    ):
        fresh["turns"][0]["copilot"] = recorded
        if "baseline" in old:
            fresh["baseline"] = old["baseline"]
    return fresh


def main(argv: list[str]) -> None:
    import os

    for key, value in {
        "STORAGE_BUCKET": "nova-test",
        "STORAGE_PROVIDER": "gcs",
        "REDIS_URL": "redis://localhost:6379",
        "OPENAI_API_KEY": "sk-test",
    }.items():
        os.environ.setdefault(key, value)
    probe_only = "--probe" in argv
    covered = {s.group for s in SCENARIOS}
    missing = [g for g in _group_names() if g not in covered]
    if missing:
        print("groups without a thread:", missing)
    for scenario in SCENARIOS:
        built = build(scenario)
        probe = built["probe"]
        print(
            f"{scenario.fixture_id}: families={probe['families']} parsed={probe['parsed_ops']} "
            f"compiles={probe.get('compiles')} {probe.get('compile_error', '')} "
            f"{probe['parse_outcome']} {probe['parse_reasons']}"
        )
        if not probe_only:
            path = THREAD_DIR / f"{scenario.fixture_id}.json"
            fixture = _keep_recording(path, fixture_for(scenario, built))
            path.write_text(
                json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )


if __name__ == "__main__":
    main(sys.argv[1:])
