# KRI-806 combined main integration

Fresh fetched main: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
Original source: `9344d04a4c8488812904b175efb3e9c39236a3dd` (#1738).
Dependency sources preserved by normal merges:
- #1724 `133c0812d2764483989e00dfb30a1aa8f208ef91`, including #1727/#1733.
- #1739 `efd8216612bc3865453c2c18d9ce06b12c52ecdf`.

Merge those dependencies before this PR. This branch preserves their author
history; no source branch was force-pushed or its substantive assertions removed.

## Acceptance and reconciliation

An optional editorial plan that cannot run must be dropped without losing the
valid strategy's kept sound, selection, title, duration or story. A valid plan
must still cover exactly the selected clips after normalization; invented,
omitted and skipped clips cannot survive in that plan. Malformed duplicate
beats remain schema errors. Counts-only diagnostics must not leak creator text.
The original recorded-input tests cover these requirements without a paid call.

The code merge preserves the authored parser repair. Conflicts were limited to
prompt version pins/hashes and append-only reliability entries. Both caption
`spellings` and spoken-text `triggers`/`speaker` instructions remain, alongside
the new editorial-plan audio-pairing rule. Main Creator is `2026-10-22-v67`;
extractor remains the dependency's `2026-10-22-v14`.

All four no-song prompt hashes change because KRI-806 changes the shared prompt.
The two brief-off hashes exactly match the original KRI-806 source. The two
brief-on hashes also include the reviewed caption and timed-text instructions.
All semantic assertions remain unchanged. All current-main and dependency gate
IDs are preserved; KRI-806 and KRI-805 are appended once each.

## Executed verification

- `bash scripts/check-api.sh test tests/agents/test_main_creator_editorial_plan.py tests/agents/test_main_creator_agent.py tests/agents/test_main_creator_user_song.py tests/tasks/test_kri791_spoken_text_trigger.py tests/kria/test_kri793_caption_spellings.py tests/kria/test_kri805_brief_change_answer.py tests/agents/test_brief_extractor_version_binding.py tests/kria/test_creative_brief.py -q --tb=short`: **345 passed** on the completed dependency tree.
- `bash scripts/check-api.sh test tests/evals/ -q --tb=short`: **2122 passed, 15 skipped, 7 expected failures**, offline replay only. The known limitations remain explicit; no live model or judge was run.
- Database: isolated migrated `nova_pr_1738_integration_test`.

Committed preship and exact-head hosted CI are still required before merge.
The inherited #1724 finite-frame helper needs its actual native Gameday proof;
Python replay is not that proof. #1724 already records the six timeline decisions
and its completed local timeline gate; this PR adds no new timeline mutation.

This is a bounded parser and prompt repair. Live model adherence and the full
Don Julio request/export remain unproven, owned by KRI-802 with KRI-803/804 and
other siblings. No paid call, phone operation, rollout or manual release occurred.

## Actual post-picker main reconciliation

Old sourcec8769456 completed CI39731609276 successfully, including native validation. Merged actualmain1bf6d14a6 normally. The six caption/prompt/assertion files conflicted because the original caption commits had since squash-landed. Proved each current-main blob byte-identical to7eaf217cb, then retained the already reviewed complete caption + timed-text + editorial union; no semantic assertion/hash/version changes were made. The new main picker/planner code merged automatically. Every common incident gate was equal as parsed JSON; all unique entries retained in actual main order.

Consumed #1739 source33115c07a (the later a3f5f90d3 adds only its report). Combined parser/brief/caption/spoken-trigger/editorial + actual unmatched-picker checks:364passed. Core prompts and iOS files remain identical to the previously verified c876 candidate. MainCreator67/BriefExtractor14 remain candidate versions; any later landed trim/named-cut instruction must be preserved and deliberately versioned in the eventual union.

Do not merge until actual #1724 and #1739 land and all newly consumed product inputs are verified. New exact-head checks and normal preship remain required; the old successful native result is retained as scoped historical evidence, not forged as a new-head status.

## Final cumulative prompt and bounded native synchronization candidate

The current published trim producer and named-cut source034aae1c1 are included with actual main after #1724/#1743/#1739, preserving Yasin's concurrent3d06 author merge. Main Creator69 / Brief Extractor16 retain every caption spelling, spoken-text trigger/speaker, quoted timing bound, named cut and editorial audio rule. Both changed Python modules are AST-identical to the private320587731 union that passed2123 free recorded evals (15 skips,7 known expected failures); prompt string bytes are identical. Current combined original parser/trim/cut/caption/picker/editorial controls:416 passed.

The prior published7f8 native run39741259795 failed the real Gameday UI test: the add-video control existed but was not hittable in its existing5s limit. This was not CoreData or the receipt CLI. Downloaded its actual result bundle and recorded simulator video. The test waits for title data, which changes before the inspector's outgoing transition finishes; that is not a panel-dismissal barrier. A four-line test-only candidate now requires the text inspector and connected panel to disappear before opening the next add menu. Existing menu/hittability limits and all fifth-clip, undo/redo, save/reopen/export assertions remain unchanged. No app code, retries, fixed sleeps, force taps or timeout increases. This is a bounded synchronization hypothesis, not yet confirmed: the exact new-head Gameday must execute and pass before merge.

Current hosted checks are required. Merge after actual producer/named-cut parents; no prior failed or old passing run is relabeled as the new source proof. Full Don Julio output/export acceptance remains pending.

## Final shared parent and actual Gameday barrier result

Normal merge9a0c88b1d includes published #1728 source291675a83 and exclusive #1730 consumerbc4, preserving the complete canonical69/16 prompt union. The only merge conflict was a version-history comment; all prompt strings and hash assertions remain the reviewed69 baseline. The new shared receipt formatting correction addresses the independently reproduced KRI793 title-row truncation on combined named-cut/trim replies. No native application/test bytes changed relative to publishedd a069.

207 combined editorial/producer/named-cut/caption-reply/take-window controls pass. Full free replay evals:2123 passed,15 explicit skips,7 existing expected failures. Old publishedd a069's actual native job119737388048 in run39751825532 completed SUCCESS, including the real Gameday fifth-clip/reopen/export, immutable evidence and artifact upload. This verifies execution with the stronger inspector/panel disappearance prerequisite; it is not a claim about a later source head. Actual #1730/#1728 merges and final current-head hosted checks remain required. Preserve the pending original Don Julio actual output/export acceptance.

## Fresh current-main integration check — 22 October

Fresh main was `12d4e7662`; the unchanged PR source was
`a069537d6df4f2a7316c220e29d24e0b8ed1c5da`. A normal no-commit merge of
current main into that source conflicted only in the two prompt-version modules
and their three version/hash assertions. The resolved union retains current
main's quoted one-take trim instruction and the PR's named-cut instruction and
editorial-plan repair: Main Creator is `2026-10-22-v69`, Brief Extractor is
`2026-10-22-v16`, and the four reviewed prompt hashes remain the PR's v69
values. No prompt wording was invented or changed during conflict resolution.

Offline verification on this exact no-commit merge:

- Focused parser, caption, spoken-trigger, quoted-trim, named-cut and brief
  recovery controls: **397 passed**.
- Full free recorded evaluation suite: **2133 passed, 15 skipped, 7 xfailed**.

The inherited Gameday UI synchronization change is still not native evidence on
this head. No paid model call, device operation, production operation, commit,
push or deployment occurred in this check.

## Final reconciliation after #1728 merged

The final candidate preserves author source `8f2dd811` by normal history. #1728 actually merged as `5a09fdff`, and its release-metadata continuation is `b3c1d4fda`. Normal merge `f2f4694d0` integrates that actual parent. The incoming Main Creator module and two version/hash tests were byte-identical to the validated #1728 source `74ffb1e4`; conflicts were limited to the cumulative version and hash pins. The reviewed Main Creator 69 / Brief Extractor 16 union remains intact, including caption spellings, spoken-text triggers and speaker, quoted trim bounds, named-cut honesty and editorial audio pairing. The incoming v68 history comment was retained.

The resolved module and both tests have exactly the same Python AST as the previously validated private candidate `9e83660f`. No prompt wording or semantic assertion changed. The other integration changes are release metadata and the full KRI-792 evidence continuation. All 49 incident gate IDs and 62 journey IDs are the exact unique parent union.

The unchanged behavioral candidate passed 234 focused parser, source-audio, quoted-window, named-cut, label and real PostgreSQL recovery/refusal checks. The recorded evaluation baseline passed 2,135 cases, with 15 explicit skips and 7 existing expected failures; its reviewed prompt strings and relevant module AST remain unchanged. These are component and integration results, not a new live model run. Current committed preship and all final published-head hosted checks remain required before merge.

The complete original Don Julio video, final picture/audio, correction/reopen and physical export remain pending under KRI-802/KRI-829. Earlier native successes are retained as scoped historical evidence and do not replace the final new-head checks. No paid provider, model, judge, phone, flag or manual release action was performed in this reconciliation.
