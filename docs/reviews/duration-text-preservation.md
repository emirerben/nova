# Preserve or retime sequential text from an editing request

Duration-only edits preserve independent text at its absolute times. An explicit instruction not to touch text protects every text property. When the resulting video cannot contain the existing text, the turn offers a sufficient video duration or an explicit sequence retime. It stages no conflicting replacement.

A clear whole-video request such as “fill the text the whole video” or “fit text to video length” retimes the complete selected sequence in one operation. The existing brief extractor and editor copilot interpret the request and emit structured facts and operations. Server code does not infer timing intent from words. The normal planner retains ownership, revision, pending-copy and brief-version guards. Compound styling requests retain all requested operations.

The sequence keeps its identities, words, ordinal order and appearance. Constrained proportional allocation retains uneven pacing with a 0.25-second minimum per word. Interior gaps scale by default. Explicit gap closing removes them; explicit fixed gaps retain their current durations. Canonical pacing metadata makes compression followed by expansion reversible. Manual timing edits replace that baseline. Multiple candidate groups or insufficient readable duration require clarification. Independent layers remain separate.

## Runtime and verification boundaries

The compiler resolves whole-video timing after footage fitting and against the actual timeline duration. It refuses an unreachable explicit numeric target. Version-bound brief updates replace prior duration or sequence policies instead of appending contradictory requirements. Retry and Undo reconcile the ledger against the compiled draft, including an older restored snapshot. Collective sequence checks verify ordered intervals and the requested span rather than requiring every word to cover the whole video.

The internal `retime_text_sequence` opcode fails closed on an older worker. The edit-copilot contract is versioned at `2026-10-30-v93`; the brief extractor is `2026-10-30-v43`. Strict boolean preservation facts and operation fields distinguish explicit preservation from default absolute timing. Whole-video and explicit bounded-span policies are checked separately. Hand-authored offline golden metadata is explicitly repinned, not represented as newly captured provider output. No paid provider replay was performed.

Newly introduced or increased same-anchor overlap in a sequence is rejected. Unchanged overlaps and intentional independent layers remain editable. Clip-bound labels and pins retain attachment semantics unless all text is explicitly protected.

## Timeline clock

The duration allocator quantizes to frames. Legacy native projection consumes millisecond boundaries. Independently rounding 28 durations could turn a 12-second edit into 11.991 seconds. The writer allocates legacy millisecond durations from cumulative boundaries. Source-window-marked timelines retain their existing six-decimal frame clock and source-bound checks. A 1-picosecond arithmetic budget tolerance handles exact readability-floor subtraction; it does not relax source bounds or permit actual unreadable spans.

## Reproduction

Run the backend modules through the repository's documented disposable PostgreSQL setup. The journey manifest selects preservation, natural-intent, pacing, ledger, contract and runtime tests for this change. Database fixtures stub only the existing model transports with explicitly hand-authored JSON. They render real prompts and exercise the real parsers, normal planner, compiler and persistence. They prove contract handling, not live model interpretation.

Regenerate the synthetic native fixture from the repository root:

```sh
src/apps/api/.venv/bin/python scripts/ios/duration-text-preservation-fixtures.py
```

Run `KriaTests/DurationTextPreservationTests` using the repository native test workflow. Positive cases cover preservation, compression, uneven expansion, expansion after compression, JSON reopening, preview and export. A deliberately stacked visible-text control must fail the single-word content oracle. The generator uses explicit synthetic typed intent, the public operation parser and compiler. It does not simulate natural-language understanding. Native fixtures contain synthetic media and words.

The required `make verify-editor-timeline` gate covers browser and backend behavior. Mobile browser viewports and simulator exports do not establish real-device acceptance. Local evidence does not establish app Save/relaunch, original-media fulfillment, production rollout or the user's phone result. The user's report that the opening error is gone remains distinct from attribution to a particular build. KRI-1060 tracks remaining acceptance and the separate reopening repair.

## Retained local native result

The six-case simulator rerun passed 270 exact OCR observations and 104 blank-gap checks. Exports contain 600 frames for each 20-second case and 360 for each 12-second case at 30 fps. The stacked negative control remains effective. Input hashes, case details and limits are retained in [the synthetic native evidence](duration-text-native-evidence.json). The subsequent fixed-gap-only allocation correction is covered by pure regressions, not these default scaled-gap exports.

## Full-CI findings

The first published head failed the language-policy guard, a short opening-title reflow case, the closed provider wire schema and an outdated timing-policy test spy. The correction removes phrase-based shortcuts rather than exempting them. Opening titles retain their existing edge-anchor semantics, while independent text and explicit preserve-all controls still refuse incompatible shortening. The provider schema recognizes only the declared boolean and gap-policy values. Independent review also supplied regression cases for preservation precedence, selected sequence lineages and bounded timing policies. These failures remain part of the verification history.
