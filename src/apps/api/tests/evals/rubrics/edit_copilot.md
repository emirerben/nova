# Edit Copilot Rubric

Score each fixture 1-5:

- The output reaching this judge has already passed the current runtime schema
  and deterministic structural validator. Do not call a parsed operation or
  value "undocumented" merely because it was introduced after v1. Judge its
  semantics against CURRENT DRAFT and `allowed_op_families`.
- Use 4 as the neutral passing score when a dimension is genuinely not
  applicable and the output does not violate it. Use 3 only for relevant but
  merely adequate behavior. Unrelated dimensions must not drag an otherwise
  correct single-family edit below the threshold.
- Current story-native timeline operations include `trim_clip_start` (an
  explicit segment target) and `trim_output_start` (the assembled output). If
  the requested state already matches CURRENT DRAFT, the correct result is no
  op plus truthful unchanged-draft copy.
- Current caption operations intentionally address listed cues by `cue_index`,
  not cue ID. Caption language changes/re-transcription are permanent redirects
  to the video-page caption controls, even when the caption family is present.
- `set_intro_layout` is the current render-family operation for Classic/Editorial
  switching. It is correct only when the render family is allowed, the word gate
  passes, and no blocking reason is present. `ink-reveal` is a supported text
  effect when it appears in the supplied effect catalog/output schema.
- The runtime's complete current operation vocabulary is authoritative:
  text/style = `edit_text`, `set_text_timing`, `add_text`, `remove_text`,
  `patch_text_style`; clips = `set_clip_duration`, `set_clip_in`,
  `trim_clip_start`, `trim_output_start`, `reorder_clip`, `remove_clip`,
  `split_clip`, `set_look_preset`; SFX = `add_sfx`, `patch_sfx`, `remove_sfx`;
  overlays = `add_overlay`, `patch_overlay`, `remove_overlay`,
  `accept_overlay_suggestion`; captions = `edit_caption`,
  `replace_caption_text`, `set_caption_timing`, `set_caption_meta`,
  `set_caption_emphasis`; music/mix = `swap_music`, `set_mix`, `remove_music`;
  render = `set_intro_layout`, `apply_custom_effect`; carousel =
  `set_carousel_moment`; title/tool = `set_title`, `open_tool`; effects and
  timed blocks = `add_camera_effect`, `patch_camera_effect`,
  `remove_camera_effect`, `set_transition`, `set_visual_fade`,
  `apply_speech_cut_candidate`, `add_motion_block`, `patch_motion_block`,
  `remove_motion_block`; history = `undo_last_edit`, `repeat_last_edit`.
  Do not invent alternate operation names or reject any listed operation when
  its corresponding family is allowed. `replace_caption_text` is deliberately the one atomic
  full-draft replacement operation for an "every/all" request, including when
  the visible cue list is truncated. `set_clip_duration` shortens a named clip;
  the new trim operations do not replace it.
- `patch_text_style` is valid under either the `text` or `style` family.
  `handwriting`, `staggered-slice`, `slide-up`, `pop-in`, and `ink-reveal` are
  established supported text effects. Do not reject them merely because an
  individual fixture omits the global effect catalog.
- `set_caption_meta.patch` may atomically carry `size_px`, `color`,
  `highlight_color`, `stroke_width`, `shadow_enabled`, `enabled`, `style`,
  `font`, and `y_frac`. A compound appearance request must encode every
  requested field and may not claim an omitted field changed.
- `set_caption_emphasis` is the cue-indexed boolean emphasis operation; never
  reinterpret it as text markup or caption meta. `set_carousel_moment.config`
  is a partial patch and may contain only the requested subset of `position`,
  `mode`, `effect`, `focus_clip_index`, `duration_s`, and `transition`.
- `allowed_op_families` is authoritative. When captions exist but `caption` is
  absent, the exact redirect "Select a caption cue in the editor timeline to
  edit it." is correct. Do not substitute the separate video-page redirect,
  which is reserved for language change, re-transcription, Apply, or reburn.
- Vague cleanup language that implies deleting one of several records must
  clarify rather than guess, even when the populated draft gives the model a
  plausible candidate to delete. This destructive-scope rule overrides the
  general instruction to act on non-destructive aesthetic requests. A Creator
  Block request using "this image" must
  clarify when multiple images are eligible and none is selected. A requested
  block whose active-time union would exceed 8 seconds must fail closed with no
  op and truthful budget copy. Prior numbered references resolve against the
  latest assistant list before draft indices and must use supplied speech marks.
  When an overlay should end "at the next speech pause," use the pause start
  (6.7 in the numbered-reference fixture), not the pause end.

- Op correctness: chooses the exact current operation(s), correct indices, and no out-of-scope operations. Same-value requests must produce no op.
- Magnitude quality: relative edits become sensible concrete values from the snapshot.
- Clarification discipline: only genuinely ambiguous requests (no usable draft context, ambiguous destructive scope, contradictions) clarify. Non-destructive aesthetic asks like "make it pop" with draft context must ACT with a coherent bundle; vague cleanup that could delete any of several records must clarify as specified above.
- Creative direction: aesthetic bundles are coherent (each op supports one stated intent), reference slot moments where relevant, and the reply states the creative intent in one sentence.
- Beat fidelity: when the snapshot lists MUSIC BEAT MARKS, beat-sync timings are copied exactly from the list (never invented or rounded); with no marks present, the reply says beat-sync is unavailable instead of fabricating times.
- Bundle separation: clip-timeline mutations (`set_clip_duration`, `set_clip_in`, trims, reorder, remove, split) and beat/snapped or speech-synced text/SFX/overlay times never appear in the same reply. Text timing, text styling, Looks, and SFX may form one coherent creative bundle when no clip-timeline mutation is present.
- Reject/redirect quality: excluded capabilities are rejected politely with the correct redirect.
- Coverage discipline: sound effects, overlays, captions, music, mix, title, and tool-opening requests use only the documented ops when the family is available in the snapshot.
- ID discipline: effect_id, asset_id, suggestion_id, and track_id are copied exactly from the snapshot; missing or invented ids must produce no surviving op. Index-addressed operations such as caption edits correctly use the listed zero-based index.
- Music-swap warning: swap_music replies must warn that saving a song swap can reset custom cuts to the new beat grid.
- Hook voice: rewrites are short, specific, creator-like, and avoid generic clickbait.

Bulk-media regression rubric:

- `add_unused_sources`, `set_media_duration`, and `stack_images` are typed,
  atomic operations. An `all`/`every` request uses exactly one selector with
  `scope`, `media_kind`, and `quantifier: "all"`; it must not be expanded into
  one operation per item.
- `set_media_duration` resolved from an image clarification uses
  `{"scope":"timeline","media_kind":"image","quantifier":"all"}` and
  cannot target videos. `stack_images` uses the same selector and leaves
  grouping and canonical `asset_ids` to the editor compiler; model output must
  never use runtime `assets` or author a partial group list.
- Clarifications persist `clarification_context.selector` and
  `pending_actions`. After "Which images...", "all of them" and "make them"
  resolve to that image selector. Preserve pending `add_unused_sources` when
  later turns clarify only the image target.
- `all` is an integrity contract. If capacity, readiness, duplicate, timing,
  or block limits prevent complete coverage, the honest result is a precise
  clarification with zero operations—not a subset and never `applied`.
- Guided Save has a hard 50-active-slot limit. For a snapshot with 17 active
  slots and 104 ready unused sources, adding all is impossible: only 33 more
  slots fit. Treat that exact capacity clarification as required behavior, not
  an invented constraint. Also expect the reply to disclose any independently
  conflicting Card Stack/Film Strip block or active-union limit represented by
  the snapshot.

Passing threshold: average >= 3.5 with no structural failures.
