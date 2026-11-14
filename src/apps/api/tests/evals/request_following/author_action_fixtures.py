"""Author one request-following thread per `agent_ops` group of the editor parity table.

    cd src/apps/api && python -m tests.evals.request_following.author_action_fixtures [--probe]

``app/kria/editor_capability_parity.py`` says which editor actions the agent has an op for. Each
thread here is a plain creator request for one group on a phone snapshot (``render_destination:
"device"``), built by the real ``build_editor_snapshot`` with the capability map the phone
advertises when the lane's gate is open. It expects the op to be applied. A group the phone
cannot run (``agent_unsupported`` since KRI-1388) keeps its thread as the recorded refusal.
``build`` is also the phone probe of ``tests/kria/test_editor_capability_parity.py``, which runs
``SCENARIOS`` and ``PHONE_OP_PROBES`` (every other op the table claims).

Three facts are frozen per thread, so replay needs no builder and no network:

* the snapshot the model saw (``copilot.agent_input``),
* what the real parser and compiler do with a hand-written reference op for the request on the
  real variant (``reference_probe``), and
* the requirement ``op_applied``, whose ``reference_compiles`` param is that probe's verdict.

The harness replay rebuilds a variant from the snapshot alone and cannot hold split sections,
backgrounds or cards, so the model's op is judged by the real parser and the reference op by the
real compiler, never by the harness compile.

``--probe`` prints the verdict per thread without writing. ``--only=<fixture_id>`` (repeatable)
authors just those threads.
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
FULL_OPENING_OPEN = {**BACKGROUND_OPEN, "background_full_opening_open": lambda *_: True}
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


def _stacked_cards(variant: dict[str, Any]) -> None:
    _cards(variant)
    variant["media_overlays"][0]["z"] = 1
    variant["media_overlays"].append(
        {
            "id": "card-2",
            "kind": "image",
            "label": "beach.jpg",
            "start_s": 2.0,
            "end_s": 5.0,
            "x_frac": 0.7,
            "y_frac": 0.3,
            "scale": 0.4,
            "display_mode": "pip",
            "z": 0,
        }
    )


def _stacked_pictures(variant: dict[str, Any]) -> None:
    variant["visual_blocks"] = [
        {
            "id": f"photo-{index}",
            "kind": "media",
            "origin": "user",
            "media_kind": "image",
            "asset_id": f"photo-{index}",
            "src_gcs_path": f"{SOURCE_PREFIX}photo-{index}.jpg",
            "display_mode": "overlay",
            "start_s": 1.0,
            "end_s": 4.0,
            "z": index,
        }
        for index in (1, 2)
    ]


def _placed_sfx(variant: dict[str, Any]) -> None:
    variant["sound_effects"] = [{"id": "s1", "effect_id": "whoosh_01", "at_s": 1.0, "gain": 1.0}]


def _cornered(variant: dict[str, Any]) -> None:
    _talking(variant)
    variant["speaker_background"]["speaker_frame"] = {
        "width_fraction": 0.4,
        "x_fraction": 0.75,
        "y_fraction": 0.8,
    }


def _full_opening(variant: dict[str, Any]) -> None:
    _talking(variant)
    variant["speaker_background"]["full_screen_windows"] = [
        {"start_s": 0.0, "end_s": 2.5},
        {"start_s": 20.0, "end_s": 23.0},
    ]


def _speaker_take(variant: dict[str, Any]) -> None:
    _talking(variant)
    variant["user_timeline"]["slots"][0]["speaker_subject"] = True


def _after_chat_recolor(variant: dict[str, Any]) -> None:
    """The editor right after a chat turn turned the title red, as the planner offers undo for it
    (KRI-1384): the turn's restore record, and the staged state still holding what it wrote."""
    from app.services import kria_editor_undo

    before = copy.deepcopy(variant["text_elements"])
    after = [{**row, "color": "#FF0000"} if row.get("id") == "title" else row for row in before]
    variant["text_elements"] = after
    record = kria_editor_undo.chat_record(
        {"text_elements": after}, {"text_elements": before}, turn_id="chat-turn", base_job_id="job"
    )
    changes = ["Patch text style"]
    record[kria_editor_undo.MARK] = {
        "v": 1,
        "changes": changes,
        "order": {"text_elements": [row["id"] for row in before]},
    }
    kria_editor_undo.attach(
        variant,
        {
            "turn_id": "chat-turn",
            "record": record,
            "current": {"text_elements": after},
            "changes": changes,
        },
    )


def _one_unused(variant: dict[str, Any]) -> None:
    variant["user_timeline"]["slots"].pop()


def _picture_block(variant: dict[str, Any]) -> None:
    variant["visual_blocks"] = [
        {
            "id": "photo-1",
            "kind": "media",
            "origin": "user",
            "media_kind": "image",
            "start_s": 1.0,
            "end_s": 3.0,
        }
    ]


def _song(mode: str) -> Callable[[dict[str, Any]], None]:
    """The creator's own 60 s song under the edit (KRI-428), as the phone plan pins it."""

    def apply(variant: dict[str, Any]) -> None:
        variant["_phone_editor_plan_v1"] = {
            "user_song": {
                "mode": mode,
                "plan_item_id": "item-1",
                "generation": 1,
                "duration_s": 60.0,
                "window_start_s": 0.0,
                "window_end_s": 23.0,
            }
        }

    return apply


SHAPE_CAPS = {
    "orientation": {"editable": True, "reason": None},
    "landscape_fit": {"editable": True, "value": "fill", "reason": None},
}
CAPTION_SCOPE_CAPS = {"caption_editor_style": True, "caption_style_scope": True}
SONG_CAPS = {
    "user_song": {
        "volume": {"editable": True, "reason": None},
        "window": {"editable": True, "reason": None},
        "remove": {"editable": True, "reason": None},
    }
}
LIPSYNC_CAPS = {
    "user_song": {
        **SONG_CAPS["user_song"],
        "window": {"editable": False, "reason": "user_song_lipsync_locked"},
    }
}


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
        "caption_style_scope",
        "Make the second caption bigger.",
        [{"op": "patch_caption_line"}],
        [{"op": "patch_caption_line", "cue_id": "cue-2", "size_px": 96}],
        variant=_cues,
        caps=CAPTION_SCOPE_CAPS,
        note="Phone edits gained this op in KRI-1394.",
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
        "clip_add_remove",
        "Remove the photo I added.",
        [{"op": "remove_visual_media"}],
        [{"op": "remove_visual_media", "target_ids": ["photo-1"]}],
        suffix="visual_media",
        variant=_picture_block,
        caps={"visual_blocks": True},
        note="Phone edits gained this op in KRI-1388.",
    ),
    Scenario(
        "clip_transition",
        "Add a crossfade between the first and second clip.",
        [{"one_of": ["set_transition", "patch_slots"]}],
        [{"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}],
    ),
    Scenario(
        "clip_look",
        "Give the whole video a golden hour look.",
        [{"one_of": ["set_look_preset", "patch_slots"]}],
        [{"op": "patch_slots", "selector": {"all": True}, "patch": {"look_preset": "golden_hour"}}],
        note="Look presets are not offered on the phone today; expected to fail.",
    ),
    Scenario(
        "clip_look",
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
        "split_screen_panel_add",
        "In the split screen, play the finish arch clip after the Halden Hill clip on the top "
        "panel and keep the Old Mill clip on the bottom.",
        [{"op": "add_split_panel_clip", "field": "clip_index"}],
        [{"op": "add_split_panel_clip", "section_id": "duo", "panel_index": 0, "clip_index": 2}],
        variant=_split,
        needs_split=True,
        note="KRI-1420: A then B on the top panel, C on the bottom, on an existing split.",
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
        "split_screen_full_screen",
        "After two seconds, let the bottom video take over the whole screen.",
        [{"op": "make_split_full_screen", "field": "panel"}],
        [
            {
                "op": "make_split_full_screen",
                "section_id": "duo",
                "at_s": 2.0,
                "side": "from",
                "panel": 1,
            }
        ],
        variant=_split,
        needs_split=True,
        note="Phone edits gained this op in KRI-1394.",
    ),
    Scenario(
        "music",
        "Lower the music volume.",
        [{"op": "set_mix"}],
        [{"op": "set_mix", "music_level": 0.3}],
        variant=_music,
    ),
    Scenario(
        "music_swap",
        "Swap the music for something calmer.",
        [{"op": "swap_music"}],
        [{"op": "swap_music", "track_id": "00000000-0000-0000-0000-000000000002"}],
        variant=_music,
        note="The snapshot never lists swap candidates; expected to fail.",
    ),
    Scenario(
        "creator_song",
        "Make my song quieter.",
        [{"op": "set_user_song", "field": "volume"}],
        [{"op": "set_user_song", "volume": 0.6}],
        variant=_song("background"),
        caps=SONG_CAPS,
        note="Phone edits gained this op in KRI-1394.",
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
        "layer_order",
        "Put the second photo in front of the first one.",
        [{"op": "set_layer_order"}],
        [{"op": "set_layer_order", "layer": "card-2", "direction": "front"}],
        variant=_stacked_cards,
        note="Phone edits gained this op in KRI-1394.",
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
        "speaker_full_opening",
        "Start with the background behind me, no full-screen intro.",
        [{"op": "remove_full_screen_opening"}],
        [{"op": "remove_full_screen_opening"}],
        variant=_full_opening,
        context={"background_pool": BACKGROUND_POOL},
        patches=FULL_OPENING_OPEN,
        note="Phone edits gained this op in KRI-1394.",
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
        "video_shape",
        "Don't crop my landscape clips, I want to see the whole frame.",
        [{"op": "set_video_shape", "field": "landscape_fit"}],
        [{"op": "set_video_shape", "landscape_fit": "fit"}],
        variant=_talking,
        caps=SHAPE_CAPS,
        note="Phone edits gained this op in KRI-1394.",
    ),
    Scenario(
        "history",
        "Undo that.",
        [{"op": "undo_last_edit"}],
        [{"op": "undo_last_edit"}],
        note="Right after a chat turn turned the title red; the editor still holds it.",
        plan_texts=(TITLE,),
        variant=_after_chat_recolor,
    ),
    Scenario(
        "history",
        "Revert that, I liked it before.",
        [{"op": "undo_last_edit"}],
        [{"op": "undo_last_edit"}],
        note="Revert phrasing of the same undo (KRI-1392 serves it as an in-place edit).",
        suffix="revert",
        plan_texts=(TITLE,),
        variant=_after_chat_recolor,
    ),
]


_GEN_POOL = {"generated_overlay_pool": [{"id": "gen-1", "kind": "image", "subject": "harbour"}]}
_GEN_ASSET = {"gen-1": f"{SOURCE_PREFIX}assets/gen-1.png"}
_BACKGROUND = {"context": {"background_pool": BACKGROUND_POOL}, "patches": BACKGROUND_OPEN}
_APPEARANCE = {"scope": "editable_text", "quantifier": "all", "category": "text"}


def _probe(group: str, op: dict[str, Any], **kwargs: Any) -> Scenario:
    return Scenario(group, "", [], [op], **kwargs)


# The ops of an `agent_ops` group that SCENARIOS (one plain request per group) does not exercise,
# each on the phone snapshot it needs. test_editor_capability_parity runs SCENARIOS and these: every
# op and named field the parity table claims must survive the real parser and compiler here.
PHONE_OP_PROBES: list[Scenario] = [
    _probe("text_authoring", {"op": "add_text", "text": "Pier", "start_s": 3.0, "end_s": 5.0}),
    _probe(
        "text_authoring",
        {"op": "rewrite_text", "selector": {"ids": ["title"]}, "text": "Sunday run"},
        plan_texts=(TITLE,),
    ),
    _probe(
        "text_authoring",
        {"op": "edit_text", "bar_index": 0, "text": "Sunday run"},
        plan_texts=(TITLE,),
    ),
    _probe("text_authoring", {"op": "remove_text", "bar_index": 0}, plan_texts=(TITLE,)),
    _probe(
        "text_authoring",
        {"op": "remove_texts", "selector": {"ids": ["title"]}},
        plan_texts=(TITLE,),
    ),
    _probe(
        "text_look",
        {"op": "patch_text", "selector": {"ids": ["title"]}, "patch": {"color": "#FFD60A"}},
        plan_texts=(TITLE,),
    ),
    _probe(
        "text_look",
        {
            "op": "patch_text_appearance",
            "selector": _APPEARANCE,
            "patch": {"font_family": "Montserrat"},
            "text_appearance_version": 1,
        },
        plan_texts=(TITLE,),
    ),
    _probe(
        "text_timing",
        {"op": "set_texts_timing", "selector": {"group": "all"}, "start_s": 0.0, "end_s": 3.0},
        plan_texts=(TITLE,),
    ),
    _probe(
        "captions",
        {"op": "replace_caption_text", "find": "pier", "replace": "jetty"},
        variant=_cues,
    ),
    _probe("captions", {"op": "set_caption_meta", "patch": {"enabled": False}}, variant=_cues),
    _probe(
        "captions",
        {"op": "set_caption_timing", "cue_index": 0, "start_s": 0.0, "end_s": 1.8},
        variant=_cues,
    ),
    _probe(
        "captions",
        {"op": "set_caption_emphasis", "cue_index": 0, "emphasis": True},
        variant=_cues,
    ),
    _probe(
        "caption_style_scope",
        {
            "op": "patch_caption_line",
            "cue_id": "cue-3",
            "position_x_frac": 0.3,
            "position_y_frac": 0.2,
        },
        variant=_cues,
        caps=CAPTION_SCOPE_CAPS,
    ),
    _probe(
        "caption_style_scope",
        {"op": "patch_caption_line", "all": True, "size_px": 64, "position_y_frac": 0.8},
        variant=_cues,
        caps=CAPTION_SCOPE_CAPS,
    ),
    _probe("clip_trim_and_move", {"op": "set_clip_in", "slot_index": 1, "in_s": 0.5}),
    _probe("clip_trim_and_move", {"op": "trim_clip_start", "slot_index": 1, "start_s": 0.5}),
    _probe("clip_trim_and_move", {"op": "trim_output_start", "start_s": 0.5}),
    _probe("clip_trim_and_move", {"op": "reorder_clip", "from_index": 2, "to_index": 0}),
    _probe("clip_trim_and_move", {"op": "reorder_clips_by", "criterion": "capture_time"}),
    _probe("clip_trim_and_move", {"op": "set_total_duration", "target_s": 12}),
    _probe(
        "clip_trim_and_move",
        {"op": "patch_slots", "selector": {"slot_indexes": [0]}, "patch": {"duration_s": 2.0}},
    ),
    _probe(
        "clip_add_remove",
        {
            "op": "add_unused_sources",
            "selector": {"scope": "unused_sources", "media_kind": "all", "quantifier": "all"},
        },
        variant=_one_unused,
    ),
    _probe("clip_add_remove", {"op": "split_clip", "slot_index": 0, "at_s": 1.0}),
    _probe(
        "split_screen_layout",
        {
            "op": "update_split_screen",
            "section_id": "duo",
            "duration_s": 4.0,
            "panel_count": 3,
            "panel_weights": [2, 1, 1],
            "background_color": "#111111",
        },
        variant=_split,
        needs_split=True,
    ),
    _probe(
        "split_screen_layout",
        {"op": "update_split_screen", "section_id": "duo", "collapse_empty_panels": True},
        variant=_split,
        needs_split=True,
        patches={"split_collapse_available": lambda *_: True},
    ),
    _probe(
        "split_screen_panel_add",
        {
            "op": "add_split_panel_clip",
            "section_id": "duo",
            "panel_index": 1,
            "clip_index": 2,
            "offset_s": 3.0,
            "duration_s": 2.0,
        },
        variant=_split,
        needs_split=True,
    ),
    _probe(
        "split_screen_panel_add",
        {
            "op": "add_split_panel_clip",
            "section_id": "duo",
            "panel_index": 0,
            "clip_index": 2,
            "grow_section": False,
        },
        variant=_split_room_to_loop,
        needs_split=True,
    ),
    _probe(
        "split_screen_remove",
        {"op": "remove_split_screen", "section_id": "duo", "keep_panel": 0},
        variant=_split,
        needs_split=True,
    ),
    _probe(
        "split_screen_full_screen",
        {"op": "make_split_full_screen", "section_id": "duo", "at_s": 1.0, "side": "until"},
        variant=_split,
        needs_split=True,
    ),
    _probe(
        "split_screen_whole_video",
        {
            "op": "assign_split_panel",
            "slot_index": 2,
            "section_id": "duo",
            "panel_index": 0,
            "offset_s": 3.0,
        },
        variant=_split_room_to_loop,
        needs_split=True,
    ),
    _probe("music", {"op": "remove_music"}, variant=_music),
    _probe(
        "creator_song",
        {"op": "set_user_song", "window_start_s": 12.0, "window_end_s": 40.0},
        variant=_song("background"),
        caps=SONG_CAPS,
    ),
    _probe(
        "creator_song",
        {"op": "set_user_song", "remove": True},
        variant=_song("lipsync"),
        caps=LIPSYNC_CAPS,
    ),
    _probe(
        "creator_song",
        {"op": "set_user_song", "volume": 0.4},
        variant=_song("lipsync"),
        caps=LIPSYNC_CAPS,
    ),
    _probe(
        "creator_song_lipsync_trim",
        {"op": "set_user_song", "window_start_s": 10.0, "window_end_s": 20.0},
        variant=_song("lipsync"),
        caps=LIPSYNC_CAPS,
    ),
    _probe(
        "sound_effects",
        {"op": "patch_sfx", "sfx_index": 0, "gain": 0.5},
        variant=_placed_sfx,
        context={"sfx_catalog": SFX_CATALOG},
    ),
    _probe(
        "sound_effects",
        {"op": "remove_sfx", "sfx_index": 0},
        variant=_placed_sfx,
        context={"sfx_catalog": SFX_CATALOG},
    ),
    _probe(
        "media_overlays",
        {"op": "add_overlay", "asset_id": "gen-1", "start_s": 2.0, "end_s": 4.0},
        variant=_cards,
        context=_GEN_POOL,
        pool_assets=_GEN_ASSET,
    ),
    _probe("media_overlays", {"op": "remove_overlay", "overlay_index": 0}, variant=_cards),
    _probe(
        "layer_order",
        {"op": "set_layer_order", "layer": "photo-2", "z": 0},
        variant=_stacked_pictures,
        caps={"visual_blocks": True},
    ),
    _probe(
        "media_overlays",
        {"op": "set_overlay_display", "overlay_index": 0, "display_mode": "fullscreen"},
        variant=_cards,
    ),
    _probe(
        "speaker_background",
        {"op": "set_background_color", "color": "#FFFFFF"},
        variant=_talking,
        **_BACKGROUND,
    ),
    _probe("speaker_background", {"op": "remove_background"}, variant=_talking, **_BACKGROUND),
    _probe(
        "video_shape",
        {"op": "set_video_shape", "orientation": "landscape"},
        variant=_talking,
        caps=SHAPE_CAPS,
    ),
    _probe(
        "speaker_background",
        {"op": "set_background_sequence", "asset_ids": ["bg-1", "bg-2"]},
        variant=_talking,
        **_BACKGROUND,
    ),
    _probe(
        "speaker_background",
        {"op": "set_speaker_frame", "preset": "small_bottom_left"},
        variant=_talking,
        **_BACKGROUND,
    ),
    _probe("speaker_background", {"op": "clear_speaker_frame"}, variant=_cornered, **_BACKGROUND),
    _probe(
        "speaker_frame_by_hand",
        {
            "op": "set_speaker_frame",
            "selector": {"slot_indexes": [0]},
            "frame": {"width_fraction": 0.4, "x_fraction": 0.2, "y_fraction": 0.8},
        },
        variant=_speaker_take,
        needs_split=True,
        **_BACKGROUND,
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
        compiled = None
        if parsed.ops:
            try:
                compiled = editor.compile_editor_ops(
                    job, variant, parsed.ops, pool_assets=scenario.pool_assets or None
                )
                probe["compiles"] = True
            except Exception as exc:  # noqa: BLE001 - the verdict is the message
                probe["compiles"] = False
                probe["compile_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return {
        "snapshot": snapshot,
        "probe": probe,
        "ops": parsed.ops,
        "compiled": compiled,
        "job": job,
        "variant": variant,
    }


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
    only = {a.removeprefix("--only=") for a in argv if a.startswith("--only=")}
    covered = {s.group for s in SCENARIOS}
    missing = [g for g in _group_names() if g not in covered]
    if missing:
        print("groups without a thread:", missing)
    for scenario in SCENARIOS:
        if only and scenario.fixture_id not in only:
            continue
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
