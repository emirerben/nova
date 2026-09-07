# Kria operation portability inventory

Recorded 2026-09-07 against the checked `CopilotOp` union and the runtime-v2
server compiler. This inventory distinguishes the existing browser catalog from
the smaller launch-critical request wedge. It does not treat a browser reducer
as server authority.

## Result

- Existing browser catalog: 49 operation names.
- Portable from an authoritative snapshot: 26/49 (53.1%).
- Adapter required: 19/49 (38.8%).
- Unsupported/navigation/history-only: 4/49 (8.2%).
- Additional server-owned reviewed action: `apply_speech_cut_candidate`.
- Plan-defined launch-critical intent families: 15/15 have a server-authoritative
  path (100%). The 80% code-feasibility gate therefore passes for the declared
  launch wedge. Validation against an independently observed creator corpus is
  still a consented cohort gate and is not claimed here.

## Portable

These compile into a complete `EditorCommitRequest` section, or into the
revision-fenced reviewed speech-cut request, and are revalidated by the existing
editor commit service before an approved render is staged.

| Family | Operations |
|---|---|
| Text | `edit_text`, `patch_text_style`, `set_text_timing`, `add_text`, `remove_text` |
| Timeline/media | `add_unused_sources`, `set_media_duration`, `stack_images`, `set_clip_duration`, `set_clip_in`, `trim_clip_start`, `trim_output_start`, `reorder_clip`, `remove_clip`, `split_clip`, `set_look_preset` |
| Captions | `edit_caption`, `replace_caption_text`, `set_caption_timing`, `set_caption_meta`, `set_caption_emphasis` |
| Audio | `swap_music`, `remove_music`, `set_mix` |
| Structure | `set_title`, `set_transition` |
| Server-only reviewed action | `apply_speech_cut_candidate` |

The launch-critical families are: reorder, remove, clip trim, output trim,
split, duration, text content/style/timing, title, caption content/timing/style,
music/mix, transition, look, mixed-media insertion/stacking, and reviewed speech
cuts. Voiceover readiness and format selection are session/strategy operations,
not editor reducers.

## Adapter required

These currently depend on client-side proposal state, feature-flagged lanes, or
specialized render payload construction. They stay out of the runtime-v2
manifest until the server can reconstruct and validate the exact post-state.

| Family | Operations |
|---|---|
| SFX | `add_sfx`, `patch_sfx`, `remove_sfx` |
| Overlays | `add_overlay`, `patch_overlay`, `remove_overlay`, `accept_overlay_suggestion` |
| Render/layout | `set_intro_layout`, `set_carousel_moment`, `set_edit_direction`, `set_visual_fade` |
| Camera/motion | `add_camera_effect`, `patch_camera_effect`, `remove_camera_effect`, `add_motion_block`, `patch_motion_block`, `remove_motion_block` |
| Generated media | `insert_generated_asset`, `replace_generated_segment` |

## Unsupported or non-tool operations

| Operations | Reason |
|---|---|
| `apply_custom_effect` | An open-ended effect dictionary is intentionally not exposed to the model-owned tool manifest. |
| `open_tool` | Navigation is a client action, not a completed edit. |
| `undo_last_edit`, `repeat_last_edit` | Browser-history commands are not authoritative. Runtime v2 has a separate revision-fenced, head-only server Undo contract. |

## Authority and guards

- Snapshot authority: `app.services.kria_editor_ops.build_editor_snapshot`.
- Compiler: `app.services.kria_editor_ops.compile_editor_ops`.
- Final validator and render staging: the existing editor-commit service.
- Atomicity: at most eight editor operations form one reversible draft bundle;
  one invalid operation rejects the whole bundle.
- Consequence boundary: `render.request` must depend on that exact bundle and
  creates approval only. Runtime policy rejects every other tool graph.
- Coverage: `tests/services/test_kria_editor_ops.py`,
  `tests/kria/test_runtime_tasks.py`, and the 240-case format matrix.
