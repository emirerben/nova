# KRI-988 — length-check failure modes (written before the fix)

Run: Don Julio lunch M2, 2026-10-27, thread `4C536AFE`, Fly v1877 (`af613077f`).
Repro: `~/Downloads/kria-julio345-test/M2_steak_only_missing_dessert_montage/runs/2026-10-27_4C536AFE/repro_no_usable_sources.py`
reproduces on `c4f0aa826` (origin/main tip): `[('no_usable_sources', 'Some selected clips no longer have usable source metadata.')]`.

## Root cause (KRI-989)

`length_planning.build_length_plan` computes `missing_ids = set(selected_ids) - set(by_id)` where
`by_id` comes only from `media_snapshot["clip_assignments"]` (the item's own footage rows).
Visuals-pool photos are addressed as `asset-<uuid>` in `strategy.selected_media_ids` (the designed
contract: `creator_policy.normalize_creator_strategy_media`, `unified_montage.selected_visual_ids`,
`generative_build.ready_visual_rows`) but never have a clip row, so every explicit selection that
includes a pool photo is reported as "no longer attached". The planner already knows those ids
(`planner._held_visual_pool` → `visual_budget`), it just never tells the length plan.

Sibling check (every caller of `build_length_plan` / `selected_visual_ids`): one caller of
`build_length_plan` (`planner._enrich_best_parts_snapshot`); `selected_visual_ids` callers
(`generative_build`, `content_plan_build`, `phone_montage_preflight`, `planner._held_visual_pool`)
all treat `asset-*` ids as pool photos correctly. The render contract (`_edit_rows`) filters rows
by selected ids without refusing unknown ones. No sibling comparison refuses.

## Decision: do photos add seconds?

**No new seconds come from the length plan.** The length plan sizes footage only. Pool photos
already take their seconds off the creator's total through `asset_holds.visual_budget`
(KRI-824/825): a creator-held photo its hold, a group-held photo the group length, the rest the
montage's own pacing (`unheld_still_frames`). The fix must keep that: a pool photo in the selection
is "attached, placed by the worker, budgeted by `visual_budget`", never a length-plan source row and
never a missing source.

## Media kinds the length check can receive

| Input | Where it lives | Expected length-plan outcome |
|---|---|---|
| Own video clip (`analysis-proxy-…`) | `clip_assignments`, kind `video` | source row; counts seconds |
| Own camera-roll photo | `clip_assignments`, kind `image` | source row as a still (1.2 s preferred / 3 s capacity, or its hold) |
| Visuals-pool photo (`asset-…`, kind image) | manifest media only, NOT a clip row | **attached**; not a source row; not missing; seconds via `visual_budget` |
| Visuals-pool video (`asset-…`, kind video) | manifest media only | attached; not missing (placed by the worker like a Visual) |
| Sticker / drawing | slide-post / overlay lanes, never in `selected_media_ids` | not a length input; unchanged |
| Song | `licensed_music` / `audio_strategy`, never a media id | not a length input; unchanged |
| Voiceover | `voiceover_duration_s`, never a media id | unchanged |
| Id in selection, in neither clip rows nor manifest | removed / replaced clip | `no_usable_sources` → "no longer attached" (honest) |
| Own video in manifest but no clip row yet | still analysing | `no_usable_sources` today (honest enough: can't read its footage); unchanged |
| Selection = photos only (no video) | — | must NOT say "no longer attached"; whatever conflict arises is about footage amount |
| Pool photo selected in a voiceover montage | — | refused earlier by `creator_policy` (`PhoneMediaUnavailableError`); never reaches the planner |

## Dead end (KRI-990) — general trap, not one screen

* `_ask_limit` = 1 for every timing kind (`duration_vs_count`, `voice_vs_duration`,
  `length_vs_natural`, `source_coverage`). After the one card, any creator message that is not
  an option match makes `resolve_choices` put the choice in `exhausted`, and the planner answers
  with `length_inputs_waiting`'s plain progress text ("Your length choice is still waiting…").
* That reply carries no card and closes the open question (`assistant_response` closes after a
  user reply); the iPhone marks the earlier card `.answered` as soon as any later user message
  exists (`ChatWorkspaceView.choiceQuestionMode(for:)`). So after one non-answer there is no
  button to tap and no label to type: the only exit is a message that changes the strategy so the
  conflict disappears ("Forget the steak photo").
* `source_coverage` has a single option, "Update the selected clips", which itself only waits for
  new footage (`length_inputs_waiting`'s second branch). No way forward without new footage even
  on the first ask.

Fix shape: while a timing conflict stays unresolved, every turn whose message is not an answer
re-offers the question as a live card (lead: "still waiting"), and the source-coverage question
gets a no-footage exit ("Leave those clips out") that drops the offending ids from the selection
when the server can name them (`no_usable_sources`: `missing_source_ids` the plan now records;
`source_below_minimum`: sources under 0.1 s).

## Pass / fail

* Pass: repro script → `[]` conflicts and the plan's `sources` are the three clips (photo neither a
  source nor missing). Replay of the 4C536AFE turn-4 inputs reaches a draft, not a
  `source_coverage` question. A typed non-answer while a timing card is pending gets the card again
  (tappable), and "Leave those clips out" (tap or typed) yields a draft without the gone clip.
* Fail: any "no longer attached" / "still waiting" with all media attached; a photo accepted at
  the gate but refused at dispatch; the photo counted twice (visual budget AND source row).
* Final acceptance (owed, after deploy): M2 re-run on the iPhone with the steak photo in the
  render (6/6).
