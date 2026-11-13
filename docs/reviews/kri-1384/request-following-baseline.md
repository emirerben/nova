# Request-following baseline (KRI-1389, parent KRI-1384)

The score every later KRI-1384 PR re-runs against. Recorded live on the current production copilot model before any model or contract change. Per-thread detail: [request-following-baseline-scores.md](request-following-baseline-scores.md) (generated, pinned by `test_committed_baseline_report_matches_the_replay`).

## The three numbers

Over every recorded thread (the KRI-185 prod capture, the 30 authored briefs, 38 per-action threads).

| Number | Value |
|---|---|
| Requirements met | **71%** (82 of 115; 7 partial, 26 unmet) |
| Overclaiming replies | **14** (13 in conversation threads, 1 in the per-action set) |
| Turn time (copilot model call) | **p50 10.9 s, p95 37.7 s** over 79 live calls |

| Set | Threads | Requirements | Met | Partial | Unmet | Met share | Overclaims |
|---|---:|---:|---:|---:|---:|---:|---:|
| Conversation threads (east_run + 30 briefs) | 31 | 77 | 52 | 7 | 18 | 68% | 13 |
| Per-action set | 38 | 38 | 30 | 0 | 8 | 79% | 1 |

The 30 new briefs alone: 48 of 68 met (71%). East Run (KRI-185 prod capture, unchanged): 4 of 9 met (44%).

**Per-action set.** 38 threads cover all 35 `agent_ops` groups of `app/kria/editor_capability_parity.py`, plus the look preset, custom effect and music swap actions that share a group. 30 met. The 8 unmet:

| Thread | Why |
|---|---|
| `action_camera_effects` | `effect` family is closed on a device edit (`kria_editor_ops._allowed_families`). |
| `action_carousel` | Not in the phone's op families. |
| `action_clip_look_and_transition__custom_effect` | No phone compile handler; parser refuses `apply_custom_effect`. |
| `action_clip_look_and_transition__look_preset` | `look_preset` is closed for a device timeline (`timeline_patch_capabilities`). |
| `action_history` | Undo/redo not in the phone's op families. |
| `action_music__swap` | The snapshot never lists swap candidates. |
| `action_visual_blocks_and_motion` | Motion blocks not in the phone's op families. |
| `action_split_screen_section_order` | Reachable, but the model answered "move the split screen to the end" with `reorder_clips_by` instead of `move_split_screen`. The only per-action miss that is the model's, not the contract's. |

Seven of the eight are blocked at parse for any model (`reference_compiles: false`, with the parser's reason in `blocked_reason`). Their replies were refusals, so none counts as an overclaim.

KRI-1388 moved those seven groups to `agent_unsupported` and renamed four threads (`action_clip_transition`, `action_clip_look`, `action_clip_look__custom_effect`, `action_music_swap`; same recordings, same statuses). It added one live-recorded thread, `action_clip_add_remove__visual_media` (met), so the regenerated scores file reads 31 of 39 per-action threads met. Verdicts and evidence: [parity-on-phone.md](parity-on-phone.md).

## What was measured, and what was not

- **Path.** `EditCopilotAgent` (real prompt, real model) -> `_honest_outcome` -> `compile_editor_ops` (`runner.replay_v1_copilot`). Planner routing in front of the copilot and the repair retry behind it are not in it. Nothing was rendered. Turn time is the wall time of the live model call (retries included), not the 66 to 389 s chat turn: queueing, planner, DB, and render are outside it.
- **Snapshots.** Built by the real `build_editor_snapshot` over a synthetic device variant (`phone_snapshot.py`), with the clip facts and "what the clip shows" the chat path hands the copilot. Footage is synthetic (invented places). Per-action snapshots pin the capability map the phone advertises when the lane's gate is open (`author_action_fixtures.PHONE_CAPS` and per-lane predicates), because the rollout settings of production cannot be read back from Fly.
- **Per-action scoring.** `op_applied`, stage `parsed`: the model's op survived the real parser on the phone snapshot. The harness compile rebuilds a variant from the snapshot alone and cannot hold split sections, backgrounds or cards, so it is not used here. Instead each thread freezes what the real compiler does with a hand-written reference op on the real variant (`reference_probe`), and a group the parser or compiler refuses is scored unmet.
- **Not measured.** Main Creator (planning, creation). Its output is an inert strategy with no edit to score. Config at record time: `gemini-3.1-pro-preview`, thinking `high`, prompt `mc-4db205a18cd9`. The request-to-edit step for the 30 briefs is the chat copilot over a first render, as in East Run.
- **Overclaim rule.** Requirements that name `claim_terms` (East Run) use them. The authored briefs name none, so the general rule applies: the turn applied ops, a requirement was missed, and the reply has no "not done" marker. A turn that applied nothing claims nothing. This is a behaviour change to `scorer.reply_overclaims`; before it, the 30 briefs could not overclaim at all.
- **Known fixture artifacts.** `sport_title_font_only.labels-keep-font` expects Inter on labels, but the editor's default for a new text is Playfair Display, so the requirement is unmet whatever the model does. `harbor_route_reversed`, `harbor_title_from_brief` and the receipt requirements (`reply_states`) grade reply wording by regex.

## Versions

| Item | Value |
|---|---|
| Model (recorded, all 79 calls) | `gemini-3.1-pro-preview` (code default; no `EDIT_COPILOT_MODEL` secret on Fly) |
| Copilot thinking level | `high` (code default) |
| Copilot prompt version | `2026-11-11-v122` (`EDIT_COPILOT_PROMPT_VERSION`) |
| Main Creator prompt version | `mc-4db205a18cd9` (not exercised) |
| Base commit | `b2df43ca51386a6b71700284438950a08ad944c4` (`origin/main` when recorded; fixtures land in the KRI-1389 PR on top of it) |
| Flags | Code defaults, no `.env` flags. Fly secret values cannot be read back, so production flag drift is not captured. |
| Tokens | 2,226,730 in, 19,101 out, 111,016 thought |

## Ledger cost

All runs: `--usage-purpose live_eval --max-cost-usd 2 --approve-reservation`, development ledger (`AI_USAGE_ENVIRONMENT=development`, scratch Postgres). Priced first with `record_live --price-only`: 41 calls on the 30 briefs, 79 with the per-action set, estimated $9.08 (pessimistic: input at bytes/3, 7k output tokens). Actual **$5.92 over 79 calls**, split into five runs so none passed $2. The recorder stops before the call whose reservation could cross the cap.

| Run (`rf-baseline-20261113-`) | Calls | Settled cost |
|---|---:|---:|
| `smoke` | 1 | $0.0683 |
| `a` | 20 | $1.5385 |
| `b` | 22 | $1.5574 |
| `c` | 19 | $1.5446 |
| `d` | 17 | $1.2150 |
| Total | 79 | $5.9238 |

No Anthropic judge was used.

## Rerun

```bash
cd src/apps/api
bash ../../../scripts/dev/test-db.sh            # scratch DB with the ledger tables
export DATABASE_URL=$(cat ../../../.dev/test-db-url) GEMINI_API_KEY=...

# 1. Price, no spend.
python -m tests.evals.request_following.record_live --price-only

# 2. Re-record (only threads with an unrecorded turn run; delete a thread's copilot.raw_text
#    to force it). One test-run-id per batch; the run stops before crossing the cap.
NOVA_EVAL_MODE=live python -m tests.evals.request_following.record_live \
  --usage-purpose live_eval --test-run-id rf-<topic>-<date>-a \
  --max-cost-usd 2 --approve-reservation

# 3. Per-action snapshots and reference probe (keeps recordings whose input is unchanged).
python -m tests.evals.request_following.author_action_fixtures [--probe]

# 4. Re-pin statuses (offline) and regenerate the scored report.
python -c "from tests.evals.request_following.runner import *; \
[stamp_baseline(p) for p in discover_fixture_paths() if not p.stem.startswith(('v2_', 'east_run'))]"
python -m tests.evals.request_following.report --phase baseline \
  --out ../../../docs/reviews/kri-1384/request-following-baseline-scores.md

# 5. Offline replay (CI).
pytest tests/evals/request_following -q
```

To score a later PR, re-record with the new model or contract under a new `--test-run-id`, regenerate the scores file, and diff it against this one. `baseline` in each thread pins the statuses recorded here; a code change that moves one fails the replay until the movement is reviewed and re-stamped.
