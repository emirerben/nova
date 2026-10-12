# KRI-577 session audit: 2026-10-04 to 10-11

Emir's ask (Slack, 2026-10-11): Yasin's agents open many small fix PRs; find out
whether the bugs could be handled in a complete way, where time is lost, and change
the workflow, testing or gates so work lands without so many follow-ups.

This document records what the Claude Code session logs on Yasin's Mac show for
that week, maps sessions to issues, PRs and re-tests, and names the changes made in
the KRI-577 PR plus the gaps left open. **OBSERVED** facts cite a session id and a
UTC time from the log; **HYPOTHESIS** is marked as such. Nothing here is estimated
where the logs do not support it.

## Sources and access gaps

| Source | Coverage | Gap |
| --- | --- | --- |
| Claude Code transcripts, `~/.claude/projects/-Users-yasinberkyesilyurt-projects-nova/*.jsonl` | 54 sessions active between 2026-10-04 00:00Z and 10-12 00:00Z (123 files were modified in the window; 69 of those are older sessions re-opened). All worktree sessions log under the primary project directory. | None for Claude Code. |
| Codex sessions (`~/.codex/sessions`, `~/.codex/state_5.sqlite` threads, `thread_history_1.sqlite` turns) | Last rollout file and last thread update are **2026-09-22**. | **No Codex session exists on this Mac for Oct 4–11.** Either Codex was not used that week or it ran elsewhere (another machine or the cloud). Codex work in this window is unaudited. |
| GitHub (`gh pr list --author ybyesilyurt`) | 62 PRs created since 2026-10-03; 55 in the window. | Reviewer comments and CI logs were not mined; CI rounds are counted from what the sessions saw. |
| Linear | The 17 issues linked from KRI-577. | Not every session names its issue; 9 sessions have a PR but no KRI id. |

Extraction method: a stdlib indexer turned each transcript into a timeline of user
prompts, pytest runs with their result line, pushes, PR creations, CI texts,
production checks and device builds. Two artefacts were corrected by hand in every
cluster: `failed=5432` is the Postgres **port** parsed out of `FATAL: role "postgres"
does not exist`, not a test count; and "CI failure" text hits are mostly memory-file
reads, so real CI rounds were re-counted from the raw logs.

## Session to PR map

Full per-session detail is in the five cluster reports kept with the extraction
(`scratchpad/timelines/<session>.md` during the audit); the condensed map:

| Day | Sessions (8-char ids) | Issues | PRs opened | Follow-ups found later |
| --- | --- | --- | --- | --- |
| Oct 4 | 73d4363e, fd6b0933, d26c679b, 9ac0ab59, 66a07da9, 372495ba, 86444967 | KRI-422, KRI-430, "still checking" | #1378, #1380, #1381, #1389 | "still checking" had a second emitter (the clip-check budget) hit on the next prod retry → KRI-433 (86444967 09:50Z); 9ac0ab59 spent 10.8 h wall, ~1 h work, nothing shipped (budget-override waits + two prompt-version conflicts) |
| Oct 5 | 0554277c, bec1385b, 3022657b, a392dde9, 86eb49df, 7ae7a1a3 | KRI-433, KRI-454, KRI-455, KRI-456 | #1394, #1395, #1400, #1403, #1404, #1415 | #1395 → phone re-test failed 9 min after "live" → #1400 (same session); KRI-456 needed #1416, #1423, #1429 across 4 phone re-sends; KRI-455 title view-only → KRI-465 |
| Oct 6 | 1d2dfedd, af6c66a4, 68c276d3, d44b90c8, 6e323c87, 92a3914a, 7fcc472e, e3a26fe7, a8979b58, 0113ceec, f70d3d65, 31919790, 5feba970, d394fa5d | KRI-465, KRI-467, KRI-472, text-Save chain | #1416–#1419, #1421, #1423–#1426, #1428–#1430, #1432, #1433, #1438, #1443, #1445 | #1419 merged into a dead base → re-landed by #1426; Talking chain #1433 → #1443 (user failure 3 min after install) → #1445 (5 min after "live") → prod rolled back by a stale deploy re-run |
| Oct 7 | 5ce3c43e, b0baf1e2, 4d57ac86, 257847da, 576a5aa5, 49930b07, a8cd336c, bc7165c4, 759cff90, c27fa6ac | KRI-508, 510, 511, 514–521 | #1468, #1469, #1471–#1478, #1482–#1484 | Oct 8 re-runs of the same three chats filed 9 issues (KRI-531, 532/533, 537, 540–542, 544–552) |
| Oct 8–11 | d9400c5e, f3ca4ba6, aefab6ba, b4ce631e, 600a1d9e, 82425804, eb3af074, f3328f61, f755a868, 4341f276, a8370af6, 966d8066 | KRI-531–533, 537, 540–552, 568 | #1488, #1490–#1495, #1501–#1507, #1531 | Receipts fixed five times in five hours (#1491, #1494, #1495, #1503, #1504), each for one fact source; #1506/#1507 refused by the new journey gate added to main while they were open |

## Totals (OBSERVED)

| | Oct 4–5 (8) | Oct 5 (5) | Oct 6 (14) | Oct 7 (10) | Oct 8–11 (12) | Week |
| --- | --- | --- | --- | --- | --- | --- |
| PRs opened | 5 | 8 | 15 | 13 | 15 | 55 (+1 never pushed) |
| pytest runs / non-green outputs | 93 / 28 | 57 / 13 | 185 / 49 | 170 / 58 | ≈85 / 21 | ≈590 / ≈169 |
| of which environment (DB role, disk, budget gate, PYTHONPATH) | ≈15 | 6 | ≈9+ | ≈20 | ≈8 | ≈60 |
| of which deliberate red-first / red control | 6 | 3 | ≈6 | ≈11 | ≈9 | ≈35 |
| real CI failure rounds | 0 | 1 (runner cancel) | 0 | 3 | ≈4 | ≈8 |
| merge-conflict rounds | 3 | 1 | 4 (+1 dead-base re-land) | 5 | ≈5 | ≈19 |
| subagents spawned | 0 | 5 (1 stalled) | 56 (29 review specialists, 3 stalled) | ≈12 | 17 | ≈90 |
| phone re-send before the PR | 0 | 0 | 0 | 0 | 0 | **0** |

Real code defects caught by the agent's own tests before pushing: about 5 in the
Oct 4–5 cluster, 2 on Oct 5, a handful on Oct 7 (TDD iterations). Most non-green
outputs were environment or intentional.

## Where time went (ranked by evidence)

1. **Environment failures in nearly every session (≈15 sessions, 5–20 min each, plus
   misread baselines).** The default test URL uses a `postgres` role that does not
   exist on this Mac; each session re-discovered it and ran `createdb nova_<topic>_test`
   by hand (b0baf1e2 18:57Z, 4d57ac86 19:44Z, 49930b07 20:50Z, 759cff90 21:32Z,
   c27fa6ac 21:45Z, 3022657b 14:41Z, 0554277c 10:30Z and more). Worse, the resulting
   "92 pre-existing failures" became a baseline that let bc7165c4 push with 9 red
   tests (09:35Z) and hid real results. Other environment losses: disk at ~99 %
   (5ce3c43e 18:46Z "350 MB", 257847da turned into a cleanup session, f755a868
   removed 17 worktrees), the dev AI-budget gate (four refused live runs on Oct 4;
   9ac0ab59 waited ≈10 h for overrides), the shared venv importing the primary
   checkout (b4ce631e 11:22Z), stalled subagents (5feba970 16:35–17:23Z; 86eb49df
   17:59–18:19Z) and a Fly deploy-guard lock (5feba970 14:03–14:45Z).
2. **Narrow symptom patches with the sibling path left open (drove most follow-up
   PRs).** Receipts: #1491 (narrated order/timing/captions) → #1494 (beats, mix) →
   #1495 (captions, pause cut) → #1503 (montage order/duplicate/closing) → #1504
   (caption language, spelling); each session read the previous one (grep counts in
   the Oct 8 report) and still added one checker family. KRI-456: #1404 then #1416,
   #1423, #1429, #1490. "Still checking" (66a07da9) fixed one of two emitters.
   0113ceec fixed one of four callers and the user asked for "the other two"; the
   agent then found a third. KRI-515: the harness in #1474 parametrized the empty
   selection as *correct* ("an empty pick means everything"), which is exactly the
   KRI-544 case (a8cd336c; U-Bahn sha256 was already in the ticket text).
3. **Parallel PRs on one hot file.** `brief_checks.py` was edited by three sessions in
   one hour on Oct 8 (b4ce631e 11:51Z and 12:39Z conflicts, 82425804 13:28Z false
   alarm, f755a868 twice on Oct 10–11). `MAIN_CREATOR_PROMPT_VERSION` was contended by
   five PRs in 24 h (v47→v51); bc7165c4 ran four Auto-fix conflict rounds (07:39,
   07:44, 08:50, 09:31Z) and 9ac0ab59 had to redo paid evals twice. #1419 was stacked
   on an unmerged branch as a draft and still merged into the dead base; #1426 re-landed
   it a session later.
4. **Validation that faked the boundary that failed.** KRI-454: resolution tests
   stubbed the resolver's `run()`, so the parser that discarded an allowed empty value
   was never exercised (#1395 body says so); the phone-proxy fidelity (640×360
   analyses) was reproduced only after the re-test failed (3022657b 15:49Z). Three
   prompt bumps merged without the live eval the CLAUDE.md rule requires (#1484,
   #1483, #1490: "has not run… about $0.10", aefab6ba 11:09Z). #1507 was "proven" by
   blocking an import with `python -I`, then failed on CI the same way.
5. **Post-deploy regressions found by Yasin's phone within minutes.** #1433 → failure
   reported 3 min after install; #1443 → 5 min after "live"; #1395 → 9 min; #1488 →
   5 min (caused by #1485's `thinking=high`, found and measured in f3ca4ba6 11:25Z:
   `finish_reason=MAX_TOKENS`, 315 output tokens). No session ran a phone re-send
   before opening its PR; the phone was the first real end-to-end check every time.
6. **Waiting.** Merge latency and budget approvals dominate wall time: ≈21 h in the
   Oct 4 cluster, ≈20 of 25 h in 5feba970, 44.7 h for an unanswered question in
   4341f276 (its fix never became a PR). Not agent inefficiency, but it is where
   batching follow-ups into one PR would have paid off.
7. **CI churn that was process, not code.** /ship review passes of 20 and 9
   specialists added 3 h (1d2dfedd) and 1 h 22 m (68c276d3) to small fixes; the
   KRI-559 journey gate landed on main while #1506/#1507 were open and refused them;
   `libEGL` on the Linux runner and a Swift type-check timeout each cost a round.

Honesty of claims: deploy claims were almost always evidence-backed (fly image SHA).
Three exceptions: 3022657b 15:39Z promised the behaviour ("should go straight to a
plan…") and it failed 9 min later; 5feba970 14:45Z said a deploy with the fix was
running but did not watch the older re-run that rolled prod back (found from the
user's 14:56Z report); 4341f276 17:35Z marked an iOS card "done" with `swiftc -parse`
only. HYPOTHESIS: the "pre-existing failures" habit is the mechanism behind most of
the premature green signals, more than any single wrong claim.

Application-runtime retries are separate from the above: KRI-433's 18 vision calls
and KRI-552's resend are product behaviour (cache and idempotency gaps), not agent
loops; they are listed as gaps below.

## What this PR changes

| Change | Why (which sessions it would have shortened) |
| --- | --- |
| `scripts/dev/test-db.sh` + `check-api.sh`/`preship-check.sh` export the recorded URL | The missing `postgres` role in ≈15 sessions; stops the "92 pre-existing" baseline (bc7165c4, c27fa6ac, f3ca4ba6, 600a1d9e, 966d8066, 3022657b, …) |
| `tests/gates/reliability-gates.json` + `test_reliability_gates.py` | One ledger per incident: boundary, fix PRs, tests, harness realness, evidence level, open neighbours. Documents which fixes already satisfy the gates (25 rows: 21 merged, 2 open PRs, 2 gaps). |
| `scripts/reliability-gates.py` (`select`, `run`, `red-control`, `journey`) | `select` names the incidents that touched a diff (the sibling check 0113ceec, a8cd336c and the receipts chain skipped); `run` separates environment skips from failures; `red-control` proves the pre-fix tree fails the gate. |
| CI journey `kri-577-reliability-gates` | Every merged gate runs in CI when one of its 37 incident paths changes (`scripts/ci/journey-manifest.json`, `ci.yml`). |
| `preship-check.sh` step 6 | Runs the mapped gates before a PR. |
| PR template "Reliability gates" block + `docs/runbooks/pr-acceptance-checklist.md` | The five acceptance lines: incident replay, neighbours, platforms/evidence level, resource envelope, environment vs regressions. |
| `agent-navigation.md`, CLAUDE.md pointer, `agents/DECISIONS.md` | The workflow rules: replay first, enumerate siblings, one hot file per PR, no stacked PRs, device re-send before "done". |

### Exercised on this HEAD

- `python3 scripts/reliability-gates.py run --all` with the scratch DB: 59 test files,
  **2,597 passed, 1 skipped, 2 xfailed, 13 s**, 0 environment skips.
- Red control: `kri-515-selected-clips` **verified** (the pre-fix dispatch rejects
  the test's call); `kri-544-duplicate-uploads` and `kri-546-montage-receipts` are
  **weak** (the tests import helpers the fix added, so the pre-fix tree only fails at
  import). Both are recorded in the ledger; a behaviour-level red control for those
  two needs a kill switch or a fixture that does not import the new module.
- `scripts/ci` selection tests (31) pass with the new journey; `app/kria/planner.py`
  now maps to three journeys, `content_plan_build.py` and `duplicate_uploads.py` to
  the new one.

## Gaps kept open, in priority order

1. **Render-ready facts as a typed contract (ticket item 1).** `PlanFacts` is still
   built per archetype from whichever variant fields a fix happened to persist. A
   table-driven test that fails when a requirement kind has no checker for an
   archetype would have collapsed five receipts PRs into one. Owner: next receipts
   change; the gate `voiceover-render-receipts` lists it.
2. **Replay records across parser → resolution → contract (item 2).** KRI-454 and
   KRI-568 are the same representation-mismatch class; the incident corpus
   (`tests/incidents`) can hold them as `contract_pin` records once the resolver
   output is captured. KRI-515's harness should parametrize what the model may
   return (empty selection, duplicates, `include` vs `exclude`, TR cue forms).
3. **Device evidence before merge (item 3).** No tool exists to run a phone re-send
   from a session; the checklist makes it a human step with a recorded job id. The
   iOS simulator journeys (kri-524, kri-548) are the pattern to extend for crop and
   caption geometry (KRI-547, KRI-553).
4. **Repeat-work instrumentation (item 4).** Calls per turn, cache hits, token
   exhaustion and cost are visible only in the admin proposal trace by hand
   (f3ca4ba6 did it manually). KRI-552's race needs a trace before any orchestration
   change; the answer cache (#1394) still has no coalescing of identical questions
   in one turn.
5. **Prompt-version collisions.** Five PRs contended one literal in 24 h. A content
   hash, or a CI check that rejects a reused version, removes the paid re-evals and
   four conflict rounds (bc7165c4, 9ac0ab59, d26c679b, c27fa6ac).
6. **Deploy watch.** After "live", check the fleet image SHA again a few minutes
   later; a stale workflow re-run rolled prod back once (5feba970, Oct 7 14:49Z).
7. **Codex sessions.** None on this Mac after 2026-09-22; if Codex was used that
   week, its logs live elsewhere and must be exported before they can be audited.
