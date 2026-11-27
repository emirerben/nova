# Chat asks redesign plan

Kria's chat asks four things today, each in its own way. This program gives them one lifecycle, one order, and one look. Reordering clips, picking research facts, choosing a text style and making overlays all become cards in the chat thread. The creator sees one question at a time, and the next one opens only after the last is answered. Overlay images still open in the existing results sheet. The design is the initial grey-fill card design on the canvas at https://claude.ai/artifact/T63PwRt4GGouGLezyaE6Pj. The PR ids in stack order are CA-1 to CA-8.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

The program runs `pstack/skills/poteto-mode/playbooks/autopilot-stack.md`. The root builds and verifies the chain and the operator lands it. Every UI PR (CA-1, CA-2, CA-3, CA-4, CA-6, CA-8) is review-gated and stops at STACK-READY for the operator's click. CA-5 and CA-7 are API only and are not review-gated.

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] State the protocol and this plan to the operator, then stop. Start execution only on the operator's explicit go.
- [ ] Read these from trunk at program start. Re-read them at every tick.
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/autopilot-stack.md`
  - [ ] `git show origin/main:pstack/skills/swarm/SKILL.md`
  - [ ] `git show origin/main:.claude/skills/verify-kria-ios/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/opening-a-pr.md`
  - [ ] `git show origin/main:docs/runbooks/request-to-output-standard.md`
  - [ ] `git show origin/main:DESIGN.md`
- [ ] On the operator's go, arm the audit tick as `/loop 1h` with the tick prompt below. Never leave the cadence to memory.
- [ ] Use this tick prompt, verbatim. "Re-read the execution playbook from trunk. Audit the operation against it and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a short status message to the operator in chat only when the audit found a tracked change that no earlier status message reported, such as a PR opened, a code-ready head, a round launched or closed, a verdict, a merge, a stuck agent and the action taken, a blocker added or cleared, or a decision only the operator can make. Name every such change and nothing else. Do not repeat a table, the merged list, or an unchanged blocker. If the audit found none, end the turn with no reply text. Either way, log this tick's row in your decision trail. The row names the items reported, or none."
- [ ] On the operator's hold or stand-down, send every owner a zero-writes order at once.
- [ ] Create the Linear project "Chat asks redesign" with one issue per PR id, each assigned to Emir, labelled with the iOS or API domain and a Feature type. Follow `docs/runbooks/linear-workflow.md`.

### Spawn owners

- [ ] Spawn one owner per PR with the full lifecycle the execution playbook names.
- [ ] Follow this dependency graph. The stack is linear in id order, and an owner branches from its parent's branch, not from trunk.
  - [ ] CA-1 and CA-5 and CA-7 have no dependency. CA-1 is the stack root.
  - [ ] CA-2 after CA-1. CA-3 and CA-4 after CA-2.
  - [ ] CA-6 after CA-1 and CA-5.
  - [ ] CA-8 after CA-1 and CA-7.
- [ ] Hold the file boundaries. CA-5 and CA-7 touch only `src/apps/api/`. CA-1 to CA-4, CA-6 and CA-8 touch only `src/apps/ios/` plus `DESIGN.md` and tests.
- [ ] Hold the review gate. CA-1, CA-2, CA-3, CA-4, CA-6 and CA-8 change an interaction. They wait for the operator's review in chat with screenshots and a video before landing.
- [ ] Keep web-editor code out of every PR. This program is iPhone and iOS-required backend only.

### PR mechanics, for every PR

- [ ] Work in its own worktree from the parent branch, created with `git worktree add -b codex/<topic> <path> <parent-branch>`. Never touch `/Users/emirerben/Projects/nova` directly.
- [ ] Resolve the forge once. Default to `gh`; if `command -v origin` succeeds and Origin can resolve the repository, use `origin pr` for every PR operation. Record any fallback to `gh`. Never require `gt`.
- [ ] Open the PR ready, never draft, per **Opening a PR**, with `gh pr create --base <parent-branch>`. The body carries the `request-evidence` block and ends with the attribution line.
- [ ] Write the failure-mode list before code for any PR that is the third fix on a ticket. Prefer E2E evidence. Never write unit tests after the code that restate it.
- [ ] Update `DESIGN.md` in the same PR as any visual change.
- [ ] Run `bash scripts/preship-check.sh` and `make ios-verify` (iOS PRs) or `cd src/apps/api && ruff check . && ruff format --check . && pytest` (API PRs) before the PR-facing push.
- [ ] Run `/deslop` before each commit and `/no-comments` before review.
- [ ] Triage every Bugbot and security-reviewer comment per `../references/bugbot-triage.md`.
- [ ] Report STACK-READY with the tip and the intended parent. Never rebase onto another branch, retarget, merge or arm auto-merge.

### Verdict and merge, for every PR

- [ ] A full swarm runs once, at the code-ready head. Another full swarm runs only for new behavior, a redesign, or a large new diff. A rebase, a merge-conflict resolution, or a minor fix gets a single verifier at the new head. Run each full swarm per `pstack/skills/swarm/SKILL.md`. One gates lane. The ten live lanes from the PR's **Verify, live** block. The perf lane from its **Verify, perf** block. Two or more audit lanes, each with its own focus, that read the diff and the receipts and distrust the PR body. The root audits the receipts in the STACK-READY report before the verdict.
- [ ] Clean only when every lane is `PASS`. Findings go back to the owner, including a defect that a lane filed as a note. A new head gets a fresh verdict, except for results that stay valid under the patch-id rule in `playbooks/shipping.md`.
- [ ] The root appends each clean PR to the stack in dependency order with its base set to its parent's branch. Nobody writes to the chain once landing starts, except the post-merge retarget that `playbooks/shipping.md` sanctions.
- [ ] After each landing, update Linear. Move the issue to Done only when the PR merged into `main`, and comment with the PR number.

### Boot recipe, for every live lane

Each live lane drives the real surface on its own simulator or local stack at the PR head. iOS lanes use the `verify-kria-ios` skill. API lanes use the local stack from `./scripts/dev-auto.sh` and `scripts/admin.py`.

- [ ] `git fetch origin <head-branch> && git checkout <head SHA>` in a fresh worktree.
- [ ] iOS. Run `make ios-verify` once, then launch the app with the chat UI-test transport fixtures (`ChatUITestTransport.swift`). Set `Config/Local.xcconfig` per the signing note in `docs/runbooks/ios-development.md`.
- [ ] API. Run `./scripts/dev-auto.sh`, wait for the health check, and replay with `make kria-replay FIXTURE=<fixture>`.
- [ ] Deliver input only through the control skill's commands. The read-only diagnostics are `.dev/<service>.log` and the simulator screenshot command.
- [ ] Save every screenshot to `/tmp/swarm-<pr-id>/worker-<n>/<slug>.png` and return the paths with the report. API lanes save a terminal capture under the same name.

## Give every ask one lifecycle and one sequence (CA-1)

**Depends on.** None.

**Files.**

- [ ] Create `src/apps/ios/Kria/Core/AskFold.swift`.
- [ ] Edit `src/apps/ios/Kria/DesignSystem/KriaAnswers.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ChatWorkspaceView.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/CanonicalChatComponents.swift`.
- [ ] Edit `DESIGN.md`.

**Build.**

- [ ] Add `AskPhase<Answer>` with the cases open, submitting, answered, failed and stale, in `AskFold.swift`. There is no superseded case.
- [ ] Add a pure `AskFold` that reads the ordered transcript and returns the one open ask. A later unanswered ask renders nothing until the earlier ask is answered.
- [ ] Delete the per-type supersession in `ChatWorkspaceView` (`xMode(for:)` and the choice-replaces-choice rule). A re-issued ask with the same `question_id` replaces the open one.
- [ ] Extend the typed-answer lock (`isAnsweringOwn`) from choice asks to every ask, so `ChatComposer` reads "Type your answer" with a close button while any ask is open.
- [ ] Write the shared card rules into `DESIGN.md` section 2 (heading, controls, state text only, no helper copy, one primary).

**You see.**

- [ ] A thread with two queued asks shows one card. The second appears right after the first is answered.
- [ ] The composer shows "Type your answer" with a close button whenever a card is open.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Create `src/apps/ios/Tests/KriaTests/AskFoldTests.swift` with these cases. Two unanswered asks give one open. Answered then next opens. Same `question_id` re-issue replaces. Empty transcript. Event order wins over type. Run `make ios-verify`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Run the thread with no ask at trunk and head. Save `ca1-regression-plain-thread.png`. Pass when the transcript and composer are identical.
- [ ] Lane 2. Open a fixture with one choice ask. Save `ca1-one-ask.png`. Pass when one card shows and the composer reads "Type your answer".
- [ ] Lane 3. Open a fixture with a choice ask followed by a moment ask. Save `ca1-two-asks.png`. Pass when only the choice card shows.
- [ ] Lane 4. Answer the choice ask in that fixture. Save `ca1-next-opens.png`. Pass when the moment card appears and the choice card is a receipt.
- [ ] Lane 5. Tap the close button on the composer. Save `ca1-composer-close.png`. Pass when the composer returns to "Add instructions" and the card stays open.
- [ ] Lane 6. Type an answer while a choice ask is open and send. Save `ca1-typed-answer.png`. Pass when the card closes and the typed text is a normal turn.
- [ ] Lane 7. Re-issue the open ask with the same `question_id`. Save `ca1-reissue.png`. Pass when one card shows with the new content.
- [ ] Lane 8. Re-send an answered ask event. Save `ca1-idempotent.png`. Pass when the receipt does not duplicate.
- [ ] Lane 9. Turn on Reduce Motion and open a card. Save `ca1-reduce-motion.png`. Pass when the card appears without animation.
- [ ] Lane 10. Set Dynamic Type to the largest size. Save `ca1-large-type.png`. Pass when no text is clipped and the buttons stay at 44 points or more.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Main-thread time to fold a 400-event transcript.
- [ ] Probe. A `measure` block in `AskFoldTests` run at trunk (existing `xMode` scans) and at head, interleaved, five runs each.
- [ ] Baseline. Record the trunk median first.
- [ ] Rule. Fail if head is slower than trunk by more than 10 percent.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-1-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-1-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Move the existing cards onto the shared lifecycle (CA-2)

**Depends on.** CA-1

**Files.**

- [ ] Edit `src/apps/ios/Kria/Features/ChoiceQuestionCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/MomentOrderCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/SongOrderCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ClipOrderCardView.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ChatWorkspaceView.swift`.

**Build.**

- [ ] Replace the four private mode enums with `AskPhase`. They sit at the top of each card file.
- [ ] Route every card through the shared `KriaQuestionCard` shell, `KriaInlineFailure` and `KriaAnsweredReceipt`.
- [ ] Delete the `KriaSupersededLine` call sites and the per-type `xMode(for:)` helpers.
- [ ] Remove helper copy from these cards (for example "Hold and drag..." lines).

**You see.**

- [ ] Choice, moment, song and clip-order cards look and behave as before, with one failure style and one receipt style.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Run the existing card tests unchanged with `make ios-verify`. They must pass without edits to their expectations.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Open the choice card at trunk and head. Save `ca2-regression-choice.png`. Pass when the card renders the same rows, previews and button.
- [ ] Lane 2. Open the moment order card. Save `ca2-moment.png`. Pass when rows, windows and the primary match trunk.
- [ ] Lane 3. Open the song order card. Save `ca2-song.png`. Pass when rows and the primary match trunk.
- [ ] Lane 4. Open the clip order strip card. Save `ca2-clip.png`. Pass when the strip and the primary match trunk.
- [ ] Lane 5. Force a moment overlap rejection. Save `ca2-overlap.png`. Pass when the red inline failure shows and the primary is disabled.
- [ ] Lane 6. Force a moment stale rejection. Save `ca2-stale.png`. Pass when the slate notice shows with the latest list.
- [ ] Lane 7. Cut the network while sending a choice. Save `ca2-send-fail.png`. Pass when a red failure and a Try again button show.
- [ ] Lane 8. Tap Try again after reconnecting. Save `ca2-retry.png`. Pass when the same answer is sent once.
- [ ] Lane 9. Answer each card type. Save `ca2-receipts.png`. Pass when each becomes a receipt in the same style.
- [ ] Lane 10. Read each card with VoiceOver. Save `ca2-voiceover.png`. Pass when every control has a label and the open card is announced.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Render time of the transcript with one open card.
- [ ] Probe. The `ios-perf` bench harness (`bench.sh`) at trunk and head, interleaved.
- [ ] Baseline. Record the trunk median first.
- [ ] Rule. Fail if head is slower than trunk by more than 10 percent.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-2-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-2-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Redesign clip reordering (CA-3)

**Depends on.** CA-2

**Files.**

- [ ] Edit `src/apps/ios/Kria/Features/ClipOrderCardView.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/MomentOrderCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Core/ClipOrder.swift`.
- [ ] Edit `DESIGN.md`.

**Build.**

- [ ] Make the clip strip a horizontal scroller with the next clip peeking at the edge. Each clip is a button.
- [ ] Open `ClipOrderLargePreview` (already in `ClipOrderCardView.swift`) when a clip is tapped. Show "Clip 1 of 7" and the previous position.
- [ ] Show the "order moved" slate notice when a newer order is rebased under the creator's move, and the failed-send state with a retry.
- [ ] Keep the sheet for drag reordering. Remove its helper lines.

**You see.**

- [ ] The strip scrolls, a tap opens the large preview, and a failed send shows a red notice with Try again.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Edit `src/apps/ios/Tests/KriaTests/ClipOrderTests.swift` to cover the rebase notice and the outbox retry. Run `make ios-verify`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Open a 7-clip order card at trunk and head. Save `ca3-regression-strip.png`. Pass when trunk shows all clips, head scrolls with the fifth peeking.
- [ ] Lane 2. Swipe the strip to the end. Save `ca3-scroll.png`. Pass when the seventh clip is reachable.
- [ ] Lane 3. Tap clip 3. Save `ca3-enlarge.png`. Pass when the large preview opens with "Clip 3 of 7".
- [ ] Lane 4. Close the preview. Save `ca3-close.png`. Pass when the strip is where it was.
- [ ] Lane 5. Drag a clip in the sheet. Save `ca3-drag.png`. Pass when the moved clip shows a "was" label.
- [ ] Lane 6. Send the order. Save `ca3-send.png`. Pass when the card shrinks to a receipt.
- [ ] Lane 7. Cut the network and send. Save `ca3-fail.png`. Pass when a red notice and Try again show.
- [ ] Lane 8. Rebase a newer order under the move. Save `ca3-rebase.png`. Pass when the slate "order moved" notice shows.
- [ ] Lane 9. Use Dynamic Type at the largest size. Save `ca3-large-type.png`. Pass when the strip and buttons stay usable.
- [ ] Lane 10. Use VoiceOver on a clip. Save `ca3-voiceover.png`. Pass when each clip reads its position and opens the preview.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Scroll hitch rate of the strip with 12 clips.
- [ ] Probe. The `bench.sh` scroll probe at trunk (non-scrolling strip is skipped) and head. Isolate the strip scroll alone.
- [ ] Baseline. Record the trunk transcript scroll hitch rate first.
- [ ] Rule. Fail if the strip scroll drops below 55 frames per second, or if the transcript scroll hitch rate rises by more than 10 percent.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-3-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-3-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Redesign style selection (CA-4)

**Depends on.** CA-2

**Files.**

- [ ] Edit `src/apps/ios/Kria/Features/ChoiceQuestionCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/DesignPreviewStrip.swift`.
- [ ] Edit `DESIGN.md`.

**Build.**

- [ ] Size the preview tiles so three fit inside the card without clipping. Keep the strip scrollable for four or more.
- [ ] Add the selected, submitting (others at 40 percent), failed and typed-own states from the canvas.
- [ ] Show the chosen style as a small preview in the answered receipt.
- [ ] Keep the rows fallback for options without previews.

**You see.**

- [ ] Three style previews fit the card. The receipt shows the chosen style.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Edit `src/apps/ios/Tests/KriaTests/ChoiceQuestionTests.swift` for the receipt preview and the rows fallback. Run `make ios-verify`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Open a 3-option style card at trunk and head. Save `ca4-regression-strip.png`. Pass when trunk clips the third tile, head shows all three.
- [ ] Lane 2. Open a 5-option style card. Save `ca4-five.png`. Pass when the strip scrolls.
- [ ] Lane 3. Select a style. Save `ca4-selected.png`. Pass when the primary reads "Use <style>".
- [ ] Lane 4. Send the selection. Save `ca4-submitting.png`. Pass when unchosen tiles fade to 40 percent and the button shows a spinner.
- [ ] Lane 5. Finish the send. Save `ca4-receipt.png`. Pass when the receipt shows the style preview.
- [ ] Lane 6. Cut the network and send. Save `ca4-fail.png`. Pass when a red notice and Try again show.
- [ ] Lane 7. Tap Type my own. Save `ca4-type-own.png`. Pass when the card locks and the composer takes the answer.
- [ ] Lane 8. Open an older plan without previews. Save `ca4-rows.png`. Pass when plain rows show with the same lifecycle.
- [ ] Lane 9. Switch to Turkish. Save `ca4-turkish.png`. Pass when labels fit without clipping.
- [ ] Lane 10. Use VoiceOver on the tiles. Save `ca4-voiceover.png`. Pass when each tile reads its style name and selected state.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. First render time of the preview strip.
- [ ] Probe. `DesignPreviewRenderer` timing in `bench.sh` at trunk and head, interleaved.
- [ ] Baseline. Record the trunk median first.
- [ ] Rule. Fail if head is slower than trunk by more than 10 percent.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-4-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-4-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Let the server accept a research selection (CA-5)

**Depends on.** None.

**Files.**

- [ ] Edit `src/apps/api/app/kria/research_preview.py`.
- [ ] Edit `src/apps/api/app/routes/kria_runtime.py`.
- [ ] Edit `src/apps/api/app/tasks/kria_runtime.py`.
- [ ] Edit `src/apps/api/app/kria/runtime.py`.
- [ ] Edit `src/apps/api/app/routes/creation_threads.py`.
- [ ] Create `src/apps/api/tests/kria/test_research_selection.py`.
- [ ] Edit `docs/pipelines/kria-agent-runtime.md`.

**Build.**

- [ ] Mint a stable `fact_id` for each shown fact in `preview_payload` and add `selection_id` equal to `basis_digest`.
- [ ] Accept a turn body key `research_selection` with `selection_id` and `kept_fact_ids`. Check the basis against `for_draft`. Reject a mismatch with 409 `research_selection_stale`.
- [ ] Filter the prepared research to the kept facts with no search and no model call. Save the draft again with the filtered binding so the approval check and fingerprint stay valid.
- [ ] Add the creator-waived receipt for an empty selection, because `pinned_research_operations` raises on zero texts.
- [ ] Advertise a capability bool and gate the key behind a minimum client protocol, default off.

**You see.**

- [ ] Posting a selection of 2 of 3 facts saves a new draft with two sourced facts and a new approval card.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Create the Postgres turn test. A subset keeps the binding digest valid. A stale basis returns 409. A fact id the client never saw is dropped. An empty selection writes the waived receipt. The flag off returns the legacy shape. Run `cd src/apps/api && pytest tests/kria/test_research_selection.py`.
- [ ] Run the KRI-881 replay eval with `cd src/apps/api && pytest tests/evals/ -v`. It must pass unchanged.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Replay a research draft at trunk and head with no selection. Save `ca5-regression-plain.png`. Pass when the approval card and binding are byte-identical.
- [ ] Lane 2. Post a selection of all facts. Save `ca5-all.png`. Pass when the draft saves and the approval stays valid.
- [ ] Lane 3. Post a selection of 2 of 3. Save `ca5-subset.png`. Pass when the draft holds two facts and the approval fingerprint changed.
- [ ] Lane 4. Post the selection twice. Save `ca5-idempotent.png`. Pass when the second post makes no new draft revision.
- [ ] Lane 5. Post a stale `selection_id`. Save `ca5-stale.png`. Pass when the response is 409 `research_selection_stale`.
- [ ] Lane 6. Post an unknown `fact_id`. Save `ca5-unknown.png`. Pass when the unknown id is dropped and the rest applies.
- [ ] Lane 7. Post an empty selection. Save `ca5-empty.png`. Pass when a waived receipt is written and the draft has no research text.
- [ ] Lane 8. Approve after a selection. Save `ca5-approve.png`. Pass when the approval accepts the new binding.
- [ ] Lane 9. Call with a client below the minimum protocol. Save `ca5-old-client.png`. Pass when the key is refused with 426.
- [ ] Lane 10. Turn the flag off and restart. Save `ca5-flag-off.png`. Pass when the key is ignored and the legacy flow runs.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall time of the selection turn.
- [ ] Probe. Time the turn against the local stack with the replay fixture at head. Trunk has no selection turn, so also time a plain draft save at trunk.
- [ ] Baseline. Record the trunk plain draft save first.
- [ ] Rule. Fail if the selection turn exceeds twice the trunk draft save, or makes any provider call.

**Review gate.** None. CA-5 is not review-gated.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Pick research facts in chat (CA-6)

**Depends on.** CA-1 and CA-5

**Files.**

- [ ] Edit `src/apps/ios/Kria/Core/ResearchPreview.swift`.
- [ ] Create `src/apps/ios/Kria/Features/ResearchSelectionCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ResearchPreviewCard.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ChatWorkspaceView.swift`.
- [ ] Edit `src/apps/ios/Kria/Core/Services.swift`.
- [ ] Edit `DESIGN.md`.

**Build.**

- [ ] Read `fact_id` and `selection_id` in `ResearchPreview`. Keep the current display when the server sends neither.
- [ ] Build `ResearchSelectionCard` on `AskPhase` with a tick per fact, the on-screen window and the source host, and a primary that names the count ("Use 2 of 3").
- [ ] Add the none-ticked confirm ("Continue without facts") and the stale notice that re-lists the current facts.
- [ ] Send `research_selection` as a turn through `Services.swift`. Show the new approval card after it saves.

**You see.**

- [ ] The research ask shows three facts with ticks. Unticking one and tapping the primary shows a new approval card with two.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Create `src/apps/ios/Tests/KriaTests/ResearchSelectionTests.swift`. Parse with and without ids. Count label for all, some and none. Stale response handling. Run `make ios-verify`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Open a research draft with an older server at trunk and head. Save `ca6-regression-display.png`. Pass when the legacy preview shows, with no ticks.
- [ ] Lane 2. Open a research draft with ids. Save `ca6-ask.png`. Pass when three facts show with ticks and a "Use all 3" primary.
- [ ] Lane 3. Untick one fact. Save `ca6-two-of-three.png`. Pass when the primary reads "Use 2 of 3".
- [ ] Lane 4. Untick all facts. Save `ca6-none.png`. Pass when the confirm and "Continue without facts" show.
- [ ] Lane 5. Send 2 of 3. Save `ca6-saving.png`. Pass when a spinner shows and the other controls dim.
- [ ] Lane 6. Finish the save. Save `ca6-approval.png`. Pass when the receipt "You kept 2 of 3 facts" and the new approval card show.
- [ ] Lane 7. Return a stale response. Save `ca6-stale.png`. Pass when the slate notice shows the refreshed list.
- [ ] Lane 8. Cut the network and send. Save `ca6-fail.png`. Pass when a red notice and Try again show.
- [ ] Lane 9. Tap a source host. Save `ca6-source.png`. Pass when the source opens in Safari.
- [ ] Lane 10. Use VoiceOver on a fact row. Save `ca6-voiceover.png`. Pass when the row reads its text, window, source and ticked state.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Time from the primary tap to the new approval card.
- [ ] Probe. Run the fixture and a local stack at head, ten times. Trunk has no selection, so also time trunk approval card render.
- [ ] Baseline. Record the trunk approval render first.
- [ ] Rule. Fail if the turn plus render exceeds 2 seconds locally.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-6-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-6-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Put overlay generation on the thread (CA-7)

**Depends on.** None.

**Files.**

- [ ] Edit `src/apps/api/app/routes/overlay_generation.py`.
- [ ] Edit `src/apps/api/app/agents/overlay_generation.py`.
- [ ] Edit `src/apps/api/app/routes/creation_threads.py`.
- [ ] Edit `src/apps/api/app/kria/runtime.py`.
- [ ] Create `src/apps/api/tests/routes/test_overlay_thread_events.py`.
- [ ] Edit `docs/pipelines/kria-agent-runtime.md`.

**Build.**

- [ ] Emit an `overlay_set` thread event for ask, compose and per-overlay progress, built on the card-event id and `stage(...)` helpers.
- [ ] Store the compose answer and per-overlay state on the thread so a relaunch resumes without a second charge.
- [ ] Gate it behind `kria_suggestion_generated_overlay_enabled`, a capability bool and a minimum client protocol.

**You see.**

- [ ] A run writes thread events for the ask, three overlay states and a done receipt.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Create the Postgres event test. The flag off emits nothing. Events replay in order. A duplicate request makes one set. A failed overlay is retryable on its own. Run `cd src/apps/api && pytest tests/routes/test_overlay_thread_events.py`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Run an overlay request at trunk and head with the flag off. Save `ca7-regression-off.png`. Pass when the response and events are unchanged.
- [ ] Lane 2. Turn the flag on and ask. Save `ca7-ask.png`. Pass when an ask event is on the thread.
- [ ] Lane 3. Submit two prompts. Save `ca7-compose.png`. Pass when a compose answer is stored.
- [ ] Lane 4. Read the thread while generating. Save `ca7-progress.png`. Pass when each overlay has a making state.
- [ ] Lane 5. Finish generation. Save `ca7-done.png`. Pass when a done receipt event lists the overlay ids.
- [ ] Lane 6. Fail one overlay. Save `ca7-partial.png`. Pass when that overlay is failed and the rest are ready.
- [ ] Lane 7. Retry the failed overlay. Save `ca7-retry.png`. Pass when only that overlay is generated again.
- [ ] Lane 8. Send the same request twice. Save `ca7-duplicate.png`. Pass when one set exists and one charge is recorded.
- [ ] Lane 9. Read the thread after a restart. Save `ca7-resume.png`. Pass when the state matches before the restart.
- [ ] Lane 10. Call with an old client. Save `ca7-old-client.png`. Pass when the events are not sent.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Thread read latency with overlay events.
- [ ] Probe. Time `GET` thread at trunk and head on a 200-event thread, interleaved.
- [ ] Baseline. Record the trunk median first.
- [ ] Rule. Fail if head is slower than trunk by more than 10 percent.

**Review gate.** None. CA-7 is not review-gated.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Make overlays in chat and keep the results sheet (CA-8)

**Depends on.** CA-1 and CA-7

**Files.**

- [ ] Edit `src/apps/ios/Kria/Features/GeneratedOverlaysSheet.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/GeneratedOverlaySet.swift`.
- [ ] Create `src/apps/ios/Kria/Features/OverlayAskCards.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/CreationAttachments.swift`.
- [ ] Edit `src/apps/ios/Kria/Features/ChatWorkspaceView.swift`.
- [ ] Edit `DESIGN.md`.

**Build.**

- [ ] Add the ask, compose and progress cards in `OverlayAskCards.swift` on `AskPhase`, driven by the `overlay_set` events.
- [ ] Start `GeneratedOverlaysSheet` at Results. Keep Results, Edit and Remade as they are.
- [ ] Show the done card with "Review images" that opens the sheet, and a retry on a failed overlay.
- [ ] Point the attachments tray entry and the editor entry at the chat flow when the flag is on.

**You see.**

- [ ] Asking for overlays in chat shows the ask, the compose card, per-image progress and a done card that opens the existing results sheet.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Create `src/apps/ios/Tests/KriaTests/OverlayAskCardsTests.swift`. Phase mapping from events. Partial failure state. Resume from a half-done set. Run `make ios-verify`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `inherit-parent` at the PR head, per the boot recipe.

- [ ] Lane 1. Open the attachments tray overlay entry at trunk and head with the flag off. Save `ca8-regression-flag-off.png`. Pass when the six-screen sheet opens exactly as at trunk.
- [ ] Lane 2. Turn the flag on and ask for overlays. Save `ca8-ask.png`. Pass when the ask card shows "Let Kria decide" and "Describe them".
- [ ] Lane 3. Tap Describe them. Save `ca8-compose.png`. Pass when the compose card shows prompt rows and a price.
- [ ] Lane 4. Add a third prompt row. Save `ca8-add-row.png`. Pass when the row appears and the price updates.
- [ ] Lane 5. Start generation. Save `ca8-progress.png`. Pass when each overlay shows a real making state, with no fake percent.
- [ ] Lane 6. Fail one overlay. Save `ca8-partial.png`. Pass when a red row with Retry shows and the others show Ready.
- [ ] Lane 7. Tap Review images. Save `ca8-sheet.png`. Pass when the existing sheet opens at Results.
- [ ] Lane 8. Relaunch the app mid-generation. Save `ca8-resume.png`. Pass when the card resumes with no second charge.
- [ ] Lane 9. Turn on Reduce Transparency. Save `ca8-reduce-transparency.png`. Pass when cards and sheet render with solid fills.
- [ ] Lane 10. Use VoiceOver across the flow. Save `ca8-voiceover.png`. Pass when each card and row is labeled and progress is announced.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Time from the done event to the results sheet showing images.
- [ ] Probe. The `bench.sh` probe at trunk (sheet from the tray) and head (sheet from the done card), interleaved.
- [ ] Baseline. Record the trunk median first.
- [ ] Rule. Fail if head is slower than trunk by more than 10 percent.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane screenshots into `docs/reviews/chat-asks/CA-8-review-<slug>.png`.
- [ ] Record a 30 to 60 second simulator video of the change. Save it as `docs/reviews/chat-asks/CA-8-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator's click.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Appended to the stack with its base set to its parent's branch, patch-id unchanged.
- [ ] The operator lands the stack bottom-up.

## Close the program

- [ ] Every box above is checked with its evidence.
- [ ] Reply to the operator with the Autopilot-stack report. Give the stack root and tip links, a one-line verdict per link, and anything parked with the reason.

## Appendix A. Prototype evidence

The design canvas at https://claude.ai/artifact/T63PwRt4GGouGLezyaE6Pj is the prototype. It settled the card lifecycle (asking, editing, submitting, answered, stale, failed, typed own), the sequential flow, the scrolling clip strip with tap to enlarge, the research fact ticks, the style preview strip and the in-chat overlay flow with the existing results sheet. Glass, colour and block-layout variants were drawn and parked. The initial grey-fill design was chosen on 2026-11-26.

Unproven so far.

- The `research_selection` contract against the real approval binding (CA-5 proves it with a Postgres test).
- The shape of the `overlay_set` thread event (CA-7 settles it).
- The exact protocol number and capability names for both new keys.
- Behavior on a real device. The canvas was rendered in a headless browser only.

## Appendix B. Alternatives rejected

- Supersede or batch asks. Rejected by the operator. Asks come one after the other.
- Glass, colour wallpaper and floating panels. Rejected as hard to read and not in the chat.
- One discriminated enum for all card fields on `ChatTranscriptMessage`. A message can carry two asks, answers live on user messages, and three card kinds are not fields at all. A shared `AskPhase` and `AskFold` remove the real duplication at lower cost.
- Per-source ticks for research. Each fact was reviewed against its sources, so ticking facts is the unit. Per-source is out of scope.
- A phone-only overlay row. It would not survive a relaunch or show in the thread, so CA-7 adds a server event.
- Reassurance or helper lines on cards. Rejected by the operator.

## Appendix C. Risks

- CA-1 changes sequencing for every ask type. The owner watches the existing card tests and the two-ask fixture.
- CA-5 must keep the approval binding and fingerprint valid after a selection. The owner watches `tasks/kria_runtime.py` and `kria/runtime.py` approval checks.
- CA-5 and CA-7 are API changes that older app builds must not see. The owner gates both by capability and minimum protocol, default off.
- CA-8 moves a paid action into chat. The owner watches for duplicate charges on retry and relaunch.
- Line numbers in the exploration notes were read from `origin/main` at plan time. Owners re-locate by symbol name.
- Kria flags default off. Rollout is a separate step after the stack lands.

## Appendix D. Links and reading list

Read before editing.

- `DESIGN.md` sections 2, 5 and 9 and "Native iOS alignment".
- `docs/runbooks/ios-development.md`, `docs/runbooks/request-to-output-standard.md`, `docs/pipelines/kria-agent-runtime.md`.
- `src/apps/ios/Kria/DesignSystem/KriaAnswers.swift` for the card shell and the answer components.
- The memory notes on no reassurance copy and the Inter-only, no-borders rule.

CA-1 and CA-5 get `pstack/skills/how/SKILL.md` before building. CA-5 gets `pstack/skills/interrogate/SKILL.md` for the approval binding. Keep a decision trail per `pstack/skills/show-me-your-work/SKILL.md`, local unless stakes need a record.
