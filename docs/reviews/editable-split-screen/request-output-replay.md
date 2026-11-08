# Authored request-to-native-output replay

This harness connects the production creator parser and effective policy strategy to
initial split lowering, editable draft projection, the phone compiler, and native
preview/export. It is authored offline replay. The ordinary initial timeline and
approved source catalogue are supplied inputs. It does not exercise a live provider,
creator orchestration, dispatch transport, database persistence, iPhone UI, physical
device or deployment.

The independent oracle is
[the scenario fixture](../../../src/apps/ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests/Fixtures/split-screen-request-output.json).
It specifies synthetic source colors and clock changes before any recipe exists.
The English case places a video above a still. The Turkish case places independently
clocked videos above a still. Both use a section from 0.5 through 2.5 seconds inside
four seconds of ordinary white footage. Samples include the frames immediately
before and at each clock change and the section end, plus full-screen return.

The per-channel tolerance is 80. It was authored as 45 before the harness first ran. The first execution (13aad28fa) measured saturated primaries from the shared untagged `SolidVideoFixture` shifted identically in preview and export, independent of panel position (red reads 255/43/0, blue 3/57/255, magenta 255/73/255; worst channel 73). Panel placement and content were correct in every sample. The tolerance covers that fixture conversion error only: wrong-content samples differ by at least 200 on some channel, and every control must still be rejected.

Each wrong-content control swaps two authored source selections while preserving
section timing, weights, panel count and total duration. The same fixed RGB
comparator must accept every positive sample, reject each designated wrong-content
sample, and retain matching ordinary/background samples. Every control must decode
and successfully render in both preview and export before its rejection counts.
A missing source or fingerprint failure cannot satisfy the control.

Native preparation generates the originals and probes their actual metadata. The
Python bridge independently verifies their bytes, then forwards the parser output
through `compile_strategy_to_plan` and its effective strategy into
`compile_initial_split_screen`. The native consumer accepts those exact recipe bytes
and checks receipts, recipe fingerprints and manifest fingerprints without repinning.
It requires the same clean Git revision as compilation and records both revisions.
The policy path is exercised; these cases do not demonstrate an observable repair
of a malformed strategy.

## Prepared execution

Heavy execution requires the shared lane. Run from a clean, reviewed repository
root after allocation. The local bounded wrapper is
`.dev/split-screen/request-output-command.sh <exact-head>`; it checks the head between
steps, uses two build jobs and limits each native stage to twenty minutes. It has
not been executed. Its three stages are:

```bash
export KRIA_SPLIT_REPLAY_DIR="$(mktemp -d "$PWD/.dev/split-screen/replay-XXXXXX")"
swift test --package-path src/apps/ios/Packages/KriaMediaEngine --jobs 2 \
  --filter SplitScreenRequestOutputTests.testPrepareSourceReceipts
src/apps/api/.venv/bin/python scripts/verify-split-screen-request-output.py \
  --artifacts "$KRIA_SPLIT_REPLAY_DIR"
swift test --package-path src/apps/ios/Packages/KriaMediaEngine --jobs 2 --skip-build \
  --filter SplitScreenRequestOutputTests.testRenderCompiledRequests
```

Retain the source receipts, compiled index, authored/parsed/policy/request artifacts,
recipes, native report, four MP4s and sampled PNGs. Inspect the frames before recording
acceptance. The opt-in tests skip without the artifact environment variable; ordinary
package CI therefore cannot establish this output acceptance by merely compiling them.

## Current evidence

Independent read-only review found and corrected two harness gaps before native
execution. Preceding-frame samples now detect a one-frame early source clock. Native
consumption now verifies actual clean-checkout provenance rather than labeling its
result solely with the compiler's SHA. Focused re-review found no remaining definite
issue. Three documentation blocks were checked without a required comment deletion.

At base `93133072598e8d929e8adc8e45702004fe0c7358` plus these additions, the bridge and
related API suites passed 98 tests with no skips or failures. This includes five new
bridge cases for production compilation, stale bytes, stale scenario, missing source
and evidence preservation. Their small byte fixtures establish compiler contracts,
not playable media. Selector checks passed 49 tests. Ruff and whitespace checks passed.
Logs are `.dev/split-screen/request-output-{related,selector}-check.log`.

Native preparation, Swift compilation, actual source probing, preview/export, pixel
comparisons and artifact inspection remain unrun. This addition prepares the remaining
output check; it does not close it or establish full feature acceptance. Formal
correspondence, the pending Save-error native batch and physical-device acceptance
remain separate gates in the feature acceptance matrix.
