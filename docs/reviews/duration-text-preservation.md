# Preserve authored text during duration edits

Duration-only edits keep ordinary authored text at its existing absolute times. An explicit text-preservation instruction protects every text property. If those intervals exceed the proposed video length, the turn asks whether to keep a sufficient duration or retime the words. It stages no replacement and starts no render.

A complete word sequence can be explicitly retimed in one operation. That operation preserves identities, wording, order and appearance. It allocates contiguous intervals with a 0.25-second minimum. Partial or ambiguous groups are refused. Newly introduced or increased overlap at the same anchor is rejected. Existing independent layers and unchanged overlaps remain editable.

The internal `retime_text_sequence` opcode fails closed on an older worker. No model prompt changed. The deterministic route accepts complete timing requests and defers compound styling requests to normal planning. Existing clip-bound labels and pins retain their attachment semantics unless all text is explicitly protected.

## Timeline clock

The duration allocator quantizes to frames. Legacy native timeline projection consumes millisecond boundaries. Independently rounding 28 durations could turn a 12-second edit into 11.991 seconds and clip the final word. The writer now allocates legacy millisecond durations from cumulative boundaries. Source-window-marked timelines retain their existing six-decimal frame clock and source-bound checks.

## Local evidence

- Two retained incident operation replays pass against immutable local copies. No original project was changed.
- The focused compiler, planner, parser and neighboring timeline suite passes 95 tests.
- Five real PostgreSQL cases pass without skips. They cover refusal without mutation, Undo and retry, malicious paragraph replacement, explicit sequence recovery, and real submit/planner/compiler/persistence/receipt integration. Provider entrypoints are blocked in the complete integration case. The complete integration case does not establish brief-enabled routing.
- The required `make verify-editor-timeline` gate passes 209 web assertions, 28 desktop browser cases, 42 mobile browser cases and 585 backend cases. Mobile browser viewports are not real-device tests.
- Four native simulator cases pass. The real preview compositor and exporter preserve all 27 word identities, styles and intervals. All 162 exact OCR observations pass. Exported 1080×1920, 30-fps videos contain 600 frames for 20 seconds and 360 frames for each 12-second case.
- A deliberately stacked control renders visible text, measures concurrency 27 and fails the single-word content oracle.
- Both intermediate rounding failures are retained locally rather than hidden by tolerance changes.

Regenerate the synthetic native fixture from the repository root:

```sh
src/apps/api/.venv/bin/python scripts/ios/duration-text-preservation-fixtures.py
```

Run the three new backend test modules from `src/apps/api` using the documented disposable PostgreSQL setup. Run `KriaTests/DurationTextPreservationTests` through the repository's native test workflow. The journey manifest selects these checks for relevant changes.

The initial broader reliability run exposed an unmapped section scope for the new opcode. Adding it to the existing text-operation scope cleared all 845 cases without skips. The patch was subsequently rebased onto newer main, so exact-head integration checks are being repeated before readiness.

This evidence proves local behavior and synthetic simulator output. It does not prove app Save/relaunch, actual phone playback/export, the original media's visual fulfillment, production exposure or deployment. The user's report that the opening error is gone remains distinct from attribution to a particular build.
