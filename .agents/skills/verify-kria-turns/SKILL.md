---
name: verify-kria-turns
description: Model-check Kria chat turns (send, queued follow-up, cancel, worker claim/lease, completion, promotion, reconciler) with TLA+ and replay the bugs on Postgres. Use before changing kria/runtime.py submit/cancel/promotion, tasks/kria_runtime.py claim/lease/complete/reconcile, the planner's thread-history load, or the revision fence in creation_thread_titles.py.
---

# Verify Kria chat turns

**Promise:** you can always send a message to Kria. Kria works on one message at a time,
answers each message from what you had said up to it, and starts a waiting follow-up once
the current one is done, even if a server restarts in between.

**Harm when broken:** the chat refuses every message with "A follow-up is already
waiting", the same request is acted on twice, or two answers are planned at once.

## Run

```bash
make verify-kria-turns             # or: python3 formal/kria_turns/verify.py [--no-replay]
```

It takes about 1 minute. It needs:
- **Java 11+**: `brew install openjdk@21`, then `export JAVA=/opt/homebrew/opt/openjdk@21/bin/java`.
- **TLC v1.8.0**: downloaded to `.dev/formal-kria-turns/` and checked by sha256. Set `TLA2TOOLS` to use your own copy.
- **A worktree test database** for the replays: `bash scripts/dev/test-db.sh`. Without one, the replays are skipped and the run says so.

Output is in `.dev/formal-kria-turns/`: `report.json`, one TLC log per rule, and `replay.log`.

| Exit | Meaning |
| --- | --- |
| 0 | PASS |
| 1 | FAIL, with the reason printed |
| 3 | PASS, but with DRIFT to review |

A pass requires all five checks:
1. `KriaTurns.tla` keeps all four rules at 2 and at 3 messages, with 2 workers.
2. `KriaTurnsBefore.tla` (origin/main at bf7be5f92, before the fix) breaks exactly `NoStuckFollowUp` and `AnswersOnlyUpToItsMessage`.
3. Each rule fails on a copy of the model with its guard removed. These "teeth" mutations live in `verify.py`'s `MUTATIONS`.
4. The six replays in `src/apps/api/tests/kria/test_turn_follow_up_postgres.py` pass.
5. No covered function has drifted (see below).

## The rules (in `formal/kria_turns/KriaTurns.tla`)

| Rule | In plain words | Guard in the code |
| --- | --- | --- |
| `OneAtATime` | At most one turn of a thread is pending or planning. | Both promotions re-check under the Thread lock that no turn runs (`_promote_queued_successor`, `_turn_running_sync`). |
| `NoStaleCommit` | Only the current lease holder saves a reply. | `_owns_turn_lease` checks the epoch and expiry. The owner is the Celery task id, which every run of a turn shares, so the **epoch** is the real guard. |
| `NoStuckFollowUp` | Once nothing can run, no follow-up stays queued. | `_release_stranded_successors`, which runs at the start of every reconcile sweep (every 60 s). |
| `AnswersOnlyUpToItsMessage` | A turn plans only from user messages up to its own. | `answering_through_sequence` bounds `_load_thread_events`. The revision fence forgives a queued follow-up's `user_message`. |

## Does my change touch this?

It does if your diff changes any function listed in `formal/kria_turns/coverage.json`, or:
- adds a new place that marks a turn finished and then promotes its successor,
- adds a turn status, or a new writer of `CreatorAgentTurn.status`,
- changes the lease, lock order, or reconcile cadence.

`verify.py` hashes each covered function. If any changed since `sync_commit`, it prints **DRIFT** and exits 3.

## When it fails or drifts

1. **Read the diff** of each drifted function since `sync_commit`: `git diff <sync_commit> -- <file>`.
2. **Map each change to the model.** One TLA+ action is one database transaction. Every guard has a `\* file:line` comment, so update both the action and its citation.
   - A new commit-then-promote site needs no new action: the stranded release covers it. Still, give the site a replay test if users can reach it.
   - A promotion that skips `_turn_running_sync` breaks `OneAtATime` at 3 messages.
3. **Tell a real bug from a modelling error.**
   - The TLC log ends in a trace of named actions. Replay it as a Postgres test that drives only `submit_turn`, `cancel_turn`, `run_kria_turn` and the reconciler, and asserts what the creator sees.
   - If the trace does not reproduce, assume the model is wrong (a guard you did not model, or a step that is really two transactions). Fix the model, not the rule.
   - Never weaken a rule to get a pass.
4. **When the model again matches the code**, run `python3 formal/kria_turns/verify.py --resync`. This rewrites the hashes and `sync_commit` to HEAD. Commit that only after step 2.

## Charts for a PM

`formal/kria_turns/statechart/` holds `chart.png`, `bug-1.png`, `bug-2.png` and `stories.md`, which use the screen words in `KriaTurnsBefore.map.mjs`. To regenerate them with the pstack statechart skill, run from a scratch folder that holds copies of `KriaTurnsBefore.*` and `KriaTurns.*`:

```bash
node <pstack>/skills/statechart/scripts/statechart.mjs from-spec KriaTurnsBefore.tla \
  --fixed KriaTurns.tla --label "the fix" \
  --expect-broken NoStuckFollowUp,AnswersOnlyUpToItsMessage \
  --title "Kria chat: sending messages" --png
```

## What this does not cover

- The model has one thread, 2 to 3 messages and 2 workers. A pass covers these bounds, not every size.
- `executing` / `awaiting_approval` / `observing` are folded into "running". Approval decide/expiry and render observation are not modelled step by step; they are covered only because they use the same guarded promotion.
- Not covered: LLM content, brief-version requeues (`brief_expected_version`), continuation (KRI-709), claims-exhausted failure, editor Save while a turn runs, and thought-stream events.
- Time is abstract: a lease can lapse at any step, and there is no clock.
