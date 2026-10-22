# KRI-792 current-main named-cut receipt integration

Original author sourceb86d48ee692d12fdf72e8f3fd20ff5d697c3a7c4 is preserved by normal merge into freshly fetched main658fa0eab3d3c439021b21193c597b9a28b2248e. The change makes receipts honest about named spoken phrases: ordinary speech cleanup does not prove those words were cut. It does not implement phrase cutting.

The original recorded T1 brief runs through plan_facts_from_strategy -> build_receipts -> reply_from_receipts, with cleanup on/off/offered/unknown and post-render applied/no-change. Original structured cut_lines, single quotes, apostrophe negatives, Turkish and unrelated caption/clip exclusions remain unchanged. No original semantic or acceptance assertion was relaxed.

## Actual prompt collision and deliberate union

Main already taught caption spellings at MainCreator65/BriefExtractor13. The original source independently used those same versions for cut_lines. The merged prompts retain BOTH caption spellings and exact creator-authored cut_lines, including the distinction from order start/end. Actual union versions are MainCreator2026-10-22-v66 and BriefExtractor2026-10-22-v14. These are candidate versions, not claims about landed main. Timed-text/trim siblings must be consumed in the final actual union without dropping instructions; advance the version if their actual prompt differs.

Only two brief-on hash assertions changed, after the original hash tests produced exactly those two hash deltas plus the expected version guard. Both brief-off hashes stayed byte-identical. Version guards remain tied to AgentSpec. No default model/provider changed.

## Verification

196 focused named-cut, structured facts, Main Creator/song, brief extractor and landed caption-spelling checks passed. Mandatory free offline evals:2121passed,15explicit skips,7retained expected failures. No live provider/judge call or paid evaluation was run. Source and assertion changes outside deliberate version/hash updates were not needed.

Full original T1 request replay, actual physical output and export remain pending under KRI-787; #1723 is the final compound replay after siblings land. A partial receipt is the correct result until actual phrase removal can be independently established. Preship and exact-head hosted CI are required before merge.

## Cumulative prompt reconciliation with current trim producer

Consumed the exclusive trim owner's published cdb8d5f6 producer, including actual main after #1724/#1743/#1739. All five version/hash conflicts preserve the reviewed full instruction union: caption spellings, spoken-text triggers/speaker, quoted start_line/end_line timing, and named cut_lines. The old named-cut parenthetical incorrectly called spoken start/end bounds clip order; it now agrees with the trim rule, while route/clip sequencing keeps order start/end. Named phrase cuts remain honestly partial editor work.

Candidate Main Creatorv68 / Brief Extractorv16 advance beyond the producer's67/15. Only the two brief-on prompt hashes change; brief-off hashes remain4684c634/9ae7a033. Consuming the refreshed producer changes only a version-history comment in Main Creator relative to the already tested private c733 union; its actual prompt bytes remain pinned. All383 combined original trim/named-cut/parser/caption/spoken-trigger/dispatch/security controls passed. Free recorded evals:2122 passed,15 skipped,7 known expected failures; no live model or judge.

The original source and both owners' history are preserved by normal merges. This PR must merge after actual #1731, then the editorial union deliberately advances to69/16. Current-source CI is required; the old7017 head's successful checks are historical proof only.

## Actual producer merge and combined reply overflow

#1731 actually landed as5b91de97e at09:50:30UTC. Its tree equals testedcdb8d5f6. Reconciling that squash into this PR conflicted only in five version/history/hash regions; all five resolved modules are AST-identical to the reviewed034aae1c source, including full prompt strings/assertions. The full canonical68/16 instruction union remains. The current published trim consumerbc4fc03b is also normally merged unchanged; actual #1730 must land first.

New combined-source CI on sibling #1744 (2923, run39755712620/API0) exposed an actual interaction: longer named-cut/trim explanations made reply_from_receipts slice the title requirement out at the existing1200-character cap. The unchanged KRI793 positive fails locally,1FAIL26PASS. Failure modes and stress controls were written before the correction. The first two new stress cases had a test-type error (receipts are Pydantic, not dataclasses); after correcting that prewritten setup, the old function was rerun in memory: all three relevant cases failed on missing request text, not exceptions.

On overflow in ordinary judged-receipt replies, the display now reserves every complete request label and status before dividing the remaining budget among explanations. Explicit ellipses show shortened display; stored receipt reasons and decision/approval semantics remain unchanged. The existing cap is not raised. Already-fitting replies are byte-identical. This bounded path does not compact mixed outcomes/inferences/unchecked receipts; labels that alone exceed the cap still use the existing final hard cap. No universal lossless-display claim.

The original six-row incident plus budget/status/immutable-receipt stress and existing brief/reaction/render-spelling suites pass283 cases. Original named-cut/trim-window/producer/prompt suites pass167 cases. Full free replay evals pass2122,15 explicit skips,7 existing expected failures; no paid/live/judge call. The final rsplit only selects the known generated body after any model summary; the final preship reruns mapped receipt gates on that source. Exact new-head hosted CI remains required. Old034's full native success is historical; this source must get its own required checks. Full phrase cutting and original physical export remain incomplete under KRI829/KRI787.
