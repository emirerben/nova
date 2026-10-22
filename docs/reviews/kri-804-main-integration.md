# KRI-804 current-main integration and source-binding repair

Original author head: fc0dba1ec6d6cc92c4bcffe65f9706c4d1adfaf7. Preserved by normal merge into a fresh origin/main worktree. Original behavior asks for a shorter label when necessary and allows supported creator-approved captions.

## Reproduced defect and independent controls

The original multi-clip caption builder emitted role=any, dropping both requested source IDs. A real typed phone recipe containing only unrelated footage and the requested words passed verify_phone_recipe. The reproduction used the author's brief/caption fixtures and the existing portable-text recipe; neither requested source was present.

Failure criteria were recorded before repair: retain every requested source; accept shared layers over neighboring requested clips and separate correct layers; reject unrelated footage, missing members, absent sources, an unrelated intervening clip and extension outside the requested group. Before product edits, six of nine builder/phone cases failed; three positive controls passed. The same interval controls were extended to the real cloud publication verifier before changing its adapter: shared-adjacent failed and seven controls passed.

## Repair

The builder now emits the existing per-clip requirement for each requested source. Both phone and cloud verifiers use the same source-group predicate: all named sources must exist, visible text must stay within connected requested-source spans, and it must overlap every occurrence of each required source. One shared layer may cross adjacent members. Existing single-source behavior, schema version and serialized field set remain unchanged. No opaque unpersisted grouping field was added.

The wrong role=any assertion was tightened to require both source bindings. No acceptance assertion, unsafe-caption refusal, stale-write protection or single-clip guard was relaxed.

## Verification and limits

- 202 focused label, creator/cloud render contract, phone-editor contract, KRI-766 binding and KRI-678 long-label tests passed. This includes 16 independent phone/cloud interval cases and the builder source-binding check. Ruff and format checks passed.
- Original source head's hosted production-image overlay gate passed (39675859953). It is historical evidence only. The repaired head must get its own hosted production-image report.json and montage.png, inspected before merge.
- Local isolated production-image build failed during apt dependency installation because its cache had insufficient disk capacity. No shared cache/image pruning, production change or gate waiver was performed. Hosted current-head rendering is the available required verification path.
- Full Don Julio request fulfillment, physical phone rendering and exported result review remain pending under KRI-802. Offline contract controls do not prove these. No paid model call or device operation was made.

KRI-805 (#1739) supplies the safe shorter-answer binding path and must land before this change. Later main integration and preship results are recorded below.

## Current-main and brief dependency integration

Merged origin/main 7eaf217cb15bc8ebd8d2fea552bbcfe3ee7b0fea and #1739 source ed0523dac2398be815eb33c3616d868a3dd16b31 normally. Only the appended gate ledger conflicted; all common entries were byte-equal as parsed JSON and both unique incident gates were retained. No source/assertion conflict occurred. Combined focused suite: 231 passed, including the real KRI-805 parser/binder/planner path. Exact-head hosted checks and overlay artifact inspection remain required.

## Hosted artifact gap found on repaired head

Run39737293281 on head97790d6c passed actual production-image rendering: PASS=124, WARN=0, FAIL=0, SKIPPED=0, including the deliberate fallback negative control. Its upload step then warned that no files were found. It used upload-artifact v4 with include-hidden-files=false against the hidden .overlay-verify directory, so no reviewable report/montage was retained. A successful render log alone cannot replace required visual inspection.

The existing upload step now names only report.json and montage.png, includes these hidden-directory outputs, and fails if none exist. This is a fail-closed artifact fix, not a test waiver. New-head rendering and downloaded report/montage inspection are still required before merge.

## Actual post-picker planner integration

Merged #1739 currenta3f5f90d3, including main1bf6d14a6. Real conflicts overlapped the resolution gate and internal result fields: KRI-804 adds label reply options, while KRI-807 moves exclusion-category augmentation ahead of the gate. Choosing incoming logic alone reproduced the original missing label-answer buttons (one failed/one passed); the independent red-cap + exclusion compound picker still passed.

The final union uses the post-exclusion resolution gate AND forwards the original reply_options; it keeps both internal fields and every incident gate. No creator answer is invented and no exclude is silently applied. KRI-804 now maps the cloud verifier boundary too.250 combined label/parser/phone/cloud/editor/picker controls passed, including wrong-source and exact compound picker/label outcomes. Scoped formatting/whitespace checks passed.

Oldhead2475 production-image run39738175253 report/montage were downloaded and viewed (124PASS); that remains correctly scoped historical evidence after this source merge. Current-head hosted rendering/artifact inspection and fullCI remain required, along with actual #1739 first. Renderer/fixture code is unchanged, but no old artifact is relabeled as the new head.

## Actual spoken-text and parser parents landed

The actual #1724/#1743/#1739 main prefix is now included. One real contract overlap retained both the source-bound caption-group/label-question branches and main's shared missing-clip helper (its displayed text is unchanged). The spoken-text cue exception from #1724 remains intact. Combined source-affinity phone/cloud, label, parser, picker and spoken-trigger controls passed278 cases. The actual #1739 squash produced only a gate ordering overlap: every common entry was identical and the unique KRI-804 gate was preserved.

Previous published986003 production-image run39743933582 retained report.json and montage.png (artifact12410901190). All124 render checks passed, with no failures/warnings/skipped overlays; draw_frame mode did not run OCR. The downloaded montage and enlarged six sauce/place examples were visually inspected: center/left/right text is readable and inside the frame. That remains scoped to that exact previous head; the necessary current-main reconciliation requires fresh CI and hosted evidence. Full Don Julio output/export remains pending.

## Current main reconciliation

Merged current `origin/main` `12d4e766231f8326e96eb6bf7336a956c43b0167` normally into the preserved PR branch. The sole source conflict was the `cloud_render_contract` import list: KRI-804's `text_source_group` stays alongside main's `unresolved_field_path`, so the cloud verifier retains source-bound label groups and the KRI-794 unresolved-order field path. No picker, choice, caption, source-label, or requested clip-ID behavior was discarded.

The combined focused suite passed **274 tests**, with no skips, against the worktree's isolated Postgres database. This includes real database execution of choice re-answer and refusal recovery; the initial sandbox-only connection failure was resolved by running the scratch database setup and tests with local-service access. The completed controls cover KRI-667, KRI-766, KRI-794, KRI-795, KRI-804, cloud render contract, KRI-678 labels, suggestion chips, KRI-789, and KRI-788. Scoped Ruff and formatter checks passed; `git diff --check` passed.

Local production-image overlay rendering could not be attempted: the `docker` executable is unavailable in this worktree. The required current-head hosted `verify-overlays` run must retain and have its `report.json` and `montage.png` inspected. Hosted CI and the iPhone/full Don Julio result remain pending; offline contract checks do not establish either.
## Actual recovery merge reconciliation

After #1732 landed as6d734e34, actualmain12d4e7662 merged normally. The only conflict was two adjacent cloud-contract imports: retain BOTH text_source_group and unresolved_field_path. All source-affinity and newer answer/timing logic remains.206 combined phone/cloud source-group, label, persisted reanswer/refusal and one-take producer controls passed; no semantic assertion was relaxed. Renderer bytes are unchanged. Old6c current production-render artifact39755620710/12415752642 passed124 cases and its actual sauce labels were visually inspected, but a refreshed head still requires exact hosted checks/render evidence. Old986 full native success remains historical component proof;6c's in-progress native result is not claimed complete. This necessary mergeable-source update supersedes that stale integration run through normal PR automation; no manual cancellation, skip or status waiver. Full original Don Julio export remains pending.

Both current-main reconciliations produced identical product/test code. The normal merge retains both evidence records above; no source change was required after the 274-test suite and 1,921 mapped incident gates passed.
