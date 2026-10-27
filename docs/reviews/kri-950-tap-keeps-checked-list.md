# KRI-950: a picker tap keeps the instructions the last turn checked

Parent: KRI-946 (Don Julio walk-in M2, 2026-10-26). Claim: local fix, offline replay.

## Incident

Thread `545F53A9-2DE3-48F8-90C7-C8366E742B89`, prod `5a96e1d58`, iPhone protocol 22.

| Turn | Creator input | Result |
| --- | --- | --- |
| `6b961c32` | M2 kit prompt (six moments, ribeye photo with the text "Ojo de bife 🥩", Atakan ending, title, 15 s) | 10 clip intents planned; `caption_ribeye_photo:n=1:0.80`; picker for the queue (n=0) |
| `060429a3` | Tap: the only video for the queue (no typed text) | `stage=planning reason=planner_salvage drop_classes=[creator_text_not_source_backed] kept=6`, `intents=[]`: "I understood 6 of your clip instructions. I couldn't safely verify "label: the ribeye photo". Please restate just those so I can add them." |

Source: `python scripts/admin.py --prod GET creation-threads/<thread>/turns` (read-only).

## Cause (proven in code and replay)

1. Every proposing turn calls the clip-intent planner model (`plan_and_resolve_clip_intents`), including a turn whose only input is a `clip_selection`. Round 1's checked list was not kept anywhere.
2. Round 2's planner input still held the original words (`_full_creator_request`: prompt + the tap stand-in "I tapped the clips you asked about."). A scratch probe of `ClipIntentPlannerAgent.parse` with round 1's and round 2's exact inputs gives identical outcomes for every row shape and reproduces the prod reply byte for byte, so the tap did not change the check.
3. The new model answer wrote the photo text as `op=label` (preview "label: the ribeye photo"; round 1 had `caption_ribeye_photo`) and its `source_quote` did not contain the text, so `_validated_intent` raised `creator_text_not_source_backed`; the salvage path held all six good rows.
4. KRI-696 (#1618) fixed `_settle`, which runs after planning; this fails one step earlier.

Not provable: the exact row. The planner is `sensitive_io`, so its raw answer is not stored. Row shapes that reproduce the class: quote without the text, quote from elsewhere in the sentence, emoji with an added U+FE0F, another emoji, capitals.

## Fix

- The turn that ends on a clip picker keeps the planner's checked output on its own turn record (`KriaTurnPlan.clip_intent_inventory`, persisted in `creator_agent_turns.plan_json`). It is not copied into the chat event; the admin turns view shows only `intent_id` + `op` (`_scrub_plan`).
- A turn whose latest creator event is a `clip_selection` (tap or Skip) for the picker in the reply just before it, with no other answer on the event and no typed line between, loads that record (`_picker_answer_turn_id`, `_load_checked_inventory`). `_reused_inventory` reuses it only when its key matches: the words before the answer's stand-in line, clip count, CLIP_FACTS, reply language, exclusions flag and planner `prompt_version`. Otherwise the turn re-plans as before. Everything after the planner call (brief labels, kept labels, whole-video texts, selections, resolver) runs unchanged on the reused list.
- Parser safety net (`_validated_intent`): a quote that stops short of the creator's quoted text in the same sentence, with no other quoted text between, is widened to that span (`_quote_with_text`); provenance ignores emoji presentation selectors and the stored text takes the creator's own form (`_creator_form`).

## Case matrix (`src/apps/api/tests/kria/test_kri950_tap_keeps_checked_list.py`, `..._postgres.py`)

| Case | Expected |
| --- | --- |
| Round 1 → tap → round 2 (recorded re-plan row) | 1 planner call; queue on the tapped video; "Ojo de bife 🥩" on the photo (`creator_text` grounding) |
| Same with a slip the parser cannot repair (other emoji) | 1 planner call; caption kept |
| Red control: no saved list (asked before deploy) | 2 planner calls; prod reply byte for byte |
| No saved list, recorded row | 2 planner calls; parser repairs it, no restate |
| Typed correction; words changed (re-send); tap + choice card / moment order / mark; broken or unknown-version record | Re-plan |
| Skip; second picker in a row; Turkish chat; a server event (render ready, review) after the tap | Reuse |
| Chat event payload; admin view; plan without a picker | No list; ids + ops only; no new key |
| Postgres: picker turn through submit + worker, then a tap | List in `plan_json`, not in the chat event; the tap's turn finds and reads it |
| Parser: other emoji, capitals, paraphrase, longer text, text from another sentence, another quoted instruction's text | Still asked |
| Parser: U+FE0F added by the model / dropped by the model | Kept with the creator's form |

Red control: the file copied onto unchanged main `831781faa`: 11 failed, 16 passed (the 16 are guards that hold both ways). With the fix: 27 passed.

## Gaps

- The legacy v1 route (`routes/creator_agent.py`) still re-plans every turn; the iPhone uses runtime v2.
- The M2 iPhone re-send also needs KRI-947, KRI-948 and KRI-949 before it can reach a draft.
