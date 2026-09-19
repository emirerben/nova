# Edit-copilot snapshot parity: web drawer vs. server chat path (2026-09-19)

Context: PR #1100 fixed one instance of a systemic gap — the server-built
snapshot (`build_editor_snapshot` in `src/apps/api/app/services/kria_editor_ops.py`,
used by the phone/iOS chat path via `execute_copilot_edit` in
`src/apps/api/app/services/creation_editor_actions.py`) never carried several
sections the web drawer (`src/apps/web/src/lib/edit-copilot/snapshot.ts`,
`buildCopilotSnapshot`) negotiates client-side. This audit enumerates every
section the web snapshot can emit, what the shared parser
(`src/apps/api/app/agents/edit_copilot.py`) does with it, whether it applies
to the server/phone chat path at all, and what this PR does about it.

The parser (`_parse_op`/`_coerce_payload`/`_family_allowed`/`_indices_valid`)
and the operation catalog are **shared** between both paths — only the
snapshot *builder* and the *compiler* (`compile_editor_ops`, deliberately a
reduced "portable" subset per its own module docstring) differ. A family can
only be advertised safely if `compile_editor_ops` can turn every op in that
family into a section of `EditorCommitRequest`; that rule drove every
"not implemented" decision below.

| Section / key | Present on server before this PR | What the parser/prompt does with it | Applies to phone/server chat path? | Action taken |
|---|---|---|---|---|
| `text_appearance_version` / `text_appearance` | Yes (PR #1100) | Gates `patch_text_appearance` | Yes | No change (already fixed) |
| `text_bars`, `slots`, `has_narrated_captions`, `total_duration_s`/`max_duration_s`/`remaining_duration_s` | Yes | Index bounds, timing clamps | Yes | No change |
| `editor_limits.max_timeline_slots` | No | Not consulted by the parser (informational only — the real ceiling is `_GUIDED_TIMELINE_MAX_SLOTS`) | Yes | **Added** — cheap, matches web, helps the model avoid slot-limit dead ends |
| `source_pool` | No | `_bulk_source_catalog_present`/`_bulk_target_rows`/`_snapshot_bulk_integrity` require this (or `sources`/`clips`) to resolve real target counts + a selection digest for `add_unused_sources`/`set_media_duration`/`stack_images` | Yes | **Added** (`_source_pool_rows`, path-free, derived from `job.all_candidates.clip_paths`). Without it, every bulk selector on this path failed closed with a "stale_target"/clarification response — the exact class of bug this PR is about. Per-row `generation` is a sha256 prefix of the (never-exposed) clip path, since `add_unused_sources` requires a truthy generation on every target as a staleness guard |
| `source_pool_summary` | No | Fallback used only when the full catalog was wire-compacted away (browser byte-budget mechanic) | No (server snapshots are small; full `source_pool` always present) | Not added — `source_pool` alone satisfies `_bulk_source_catalog_present` |
| `sfx.placements` | No | `_indices_valid`'s `sfx_index` bound check; `patch_sfx`/`remove_sfx` operate on these | Yes | **Added**, gated on `caps.sfx is True` (new `"sfx"` family) |
| `sfx.catalog` | No | `add_sfx`'s `effect_id` must resolve via `_id_in_section(..., "sfx", "catalog", "id")` | Needs a DB-backed `SoundEffect` lookup `build_editor_snapshot(job, variant)` cannot make (no `db` session in this pure function's signature) | **Left empty, deliberately.** Verified this fails *safely*: `add_sfx` is rejected at parse time (never reaches `compile_editor_ops`), so advertising `"sfx"` cannot silently drop a sibling op. `patch_sfx`/`remove_sfx` on existing placements work fully. Compile support added for both. Real "add a new effect" support is a follow-up needing either an async `build_editor_snapshot`/`db` thread-through, or reusing `_resolve_sound_effect_placements` (app/routes/plan_items.py) post-compile the way visual-media asset resolution already works |
| `overlays.cards` / `.asset_pool` / `.pending_suggestions` | No | Gates `add_overlay`/`patch_overlay`/`remove_overlay`/`accept_overlay_suggestion`; `add_overlay`'s `asset_id` must resolve to a real, path-bearing asset at compile time | Needs the same `PlanItemAsset` resolution `execute_copilot_edit` only does for `visual_media` today, plumbed into `compile_editor_ops` | **Not implemented.** Unlike sfx, there is no cheap "leave the catalog empty and it fails safe" option, because `remove_overlay`/`patch_overlay` on *existing* cards still need `variant.get("media_overlays")` wired the same way sfx/camera_effects now are — that part is a reasonable, small follow-up PR; `add_overlay`'s asset resolution is the harder remainder. Not advertised, so not reachable |
| `captions.*` | Yes (cues, meta, cues_editable, total_cues) | Gates caption ops | Yes | No change — already reasonably complete, and `meta.appearance` is server-only extra richness the web doesn't emit |
| `music.swappable` | Yes (hardcoded `False`) | `swap_music` requires `music.swappable is True` **and** `track_id` in `music.candidates` | Real candidates need a DB-backed music-library query this pure function cannot make | Left `False`, with an explicit code comment explaining why (would otherwise advertise a family the parser can only ever reject for that op) |
| `music.removable` | Yes (`bool(current_track_id)` — ignored capability) | `remove_music` requires `music.removable is not False` | Yes | **Fixed** — now also checks `music_operations.remove.editable` when that map exists (guided_story-v2's `reference_only` case), preserving the prior behavior when it doesn't (legacy/montage) |
| `music.candidates` | Yes (always `[]`) | `swap_music` id lookup | Same DB dependency as swappable | Documented follow-up (same fix as swappable) |
| `title` family gating | Yes, but tied to `text_elements` | `set_title` compiles to `EditorCommitRequest.title`, which the atomic Save route **rejects outright** for guided_story-v2 variants (`guided_story_editor_v2_section_unsupported`) — that archetype's title is an ordinary `"guided-title"` text bar instead | Yes | **Fixed** — gated on `caps.intro_controls is not False` instead, mirroring the web drawer's `canEditIntroControls`. This was a real, reachable instance of the exact "advertised a family the server can't commit" bug class (it failed safely — a rejection reply, not a crash — but was dishonest capability advertising) |
| `title` (current on-screen title text) | No | Purely informational | The web's `title` is ephemeral client-drafted state (`useState`, seeded from a local `doc.title`), not a persisted `variant`/`Job` field reachable from `(job, variant)` | Not added — no server source of truth exists to echo |
| `camera_effects` | No | Gates `add_camera_effect`/`patch_camera_effect`/`remove_camera_effect` | Yes (`caps.camera_effects` is a real, reachable bool — subtitled variants with a clean base video) | **Added**, new `"effect"` family, full add/patch/remove compile support (self-contained: no DB, no asset resolution) |
| `visual_blocks` (`"visual"` family) | No (only the separate `visual_media` remove-only lane exists) | Gates `set_visual_fade` and (via web only) full block authoring | `visual_blocks`/`visual_editor_style` is a real capability, but full editing needs the richer `VisualBlock` schema + asset-pool plumbing | Not implemented — follow-up |
| `motion.*` | No | Gates `add_motion_block`/`patch_motion_block`/`remove_motion_block` | Needs the Creator Block catalog + an image asset pool + preset-specific param validation | Not implemented — largest remaining lift, follow-up |
| `intro` (render/intro-layout) | No | Gates `set_intro_layout` (`"render"` family, single-op-only, starts a re-render) | `"render"` isn't in `_PORTABLE_FAMILIES`; `compile_editor_ops` has no case for it | Not implemented — follow-up |
| `carousel` | No | Gates `set_carousel_moment` | Needs `CarouselMomentEditRequest` merge semantics (`_merge_carousel_moment_override`) | Not implemented — follow-up |
| `open_tools` (`"tool"` family) | No | Gates `open_tool` (browser drawer-tab navigation) | Web-only by design (no drawer to open server-side) | Skipped |
| `render_step_summary` / `recent_edit_history` / `history_state` | No | Orientation context only; `undo_last_edit`/`repeat_last_edit` never mutate server draft state (their own doc comment: "there is nothing to enforce here since there is no server draft state") and aren't in `_PORTABLE_FAMILIES` | Web-only (client-owned render-status feed + local undo stack) | Skipped by design |
| `editor_focus` / `asset_context_status` / `component_context_version` / `source_assets` | No | Grounding context for a live browser selection/cursor; not required by any parser validation branch | Web-only (no live cursor/selection concept on a stateless chat turn) | Skipped, per task guidance |
| per-row `mutation_fingerprint` | No | N/A — defined as a non-enumerable JS property, so it is **never serialized** even from the browser | N/A everywhere | No action needed |
| `beat_marks` / `speech` | No | Clamp `at_s` placements against real beat/word timing for sfx/text | Plausible future win but not required for this pass's op families | Not implemented — follow-up |
| `wire_compact` | No | Browser-only byte-budget wire-compaction protocol | Server snapshots for a single portable variant stay well under budget | Not applicable |

## What changed in this PR

`src/apps/api/app/services/kria_editor_ops.py`:
- `_PORTABLE_FAMILIES` gains `"sfx"` and `"effect"`.
- `_allowed_families` now gates `"title"` on `caps.intro_controls` (not
  `text_elements`), and adds `"sfx"`/`"effect"` on their own capability
  flags.
- `build_editor_snapshot` adds `editor_limits`, `source_pool`, `sfx`
  (placements + empty catalog), and `camera_effects`; fixes `music.removable`
  to respect `music_operations.remove.editable` when present.
- `compile_editor_ops` adds `add_camera_effect`/`patch_camera_effect`/
  `remove_camera_effect` and `patch_sfx`/`remove_sfx`, writing into
  `EditorCommitRequest.camera_effects`/`.sound_effects`.

No changes to `src/apps/api/app/agents/edit_copilot.py` or any prompt file —
the parser already had full op/family plumbing for `sfx`/`effect`; the gap
was entirely in what the server snapshot advertised and what the compiler
could turn into a commit.

## Test plan

`cd src/apps/api && <venv>/bin/python -m pytest -q tests/services/test_kria_editor_ops.py tests/test_edit_copilot.py tests/test_edit_copilot_visual_media.py tests/evals/test_edit_copilot_evals.py tests/routes/test_creation_editor_actions.py tests/routes/test_creation_editor_actions_integration.py tests/routes/test_creation_threads.py -p no:cacheprovider`

All 700+ tests pass, including 12 new tests in `test_kria_editor_ops.py`
covering: title-family gating (both directions), music.removable
(capability-aware and legacy-preserving), `editor_limits`, `source_pool`
exposure and an end-to-end parse→compile of `add_unused_sources`, the sfx
section (empty catalog, path-free), `patch_sfx`/`remove_sfx` compile, an
end-to-end proof that `add_sfx` fails closed at parse time (never reaches an
uncompilable-op error), and an end-to-end parse→compile of
`add_camera_effect`/`patch_camera_effect`/`remove_camera_effect`.
