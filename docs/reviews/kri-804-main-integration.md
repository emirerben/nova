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
