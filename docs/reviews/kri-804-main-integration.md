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
