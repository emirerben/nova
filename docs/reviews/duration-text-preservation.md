# Preserve or retime sequential text from an editing request

Duration-only edits preserve independent text at its absolute times. An explicit instruction not to touch text protects every text property. When the resulting video cannot contain the existing text, the turn offers a sufficient video duration or an explicit sequence retime. It stages no conflicting replacement.

A clear whole-video request such as “fill the text the whole video” or “fit text to video length” retimes the complete selected sequence in one operation. The deterministic whole-message route runs before brief extraction and uses the existing operation parser. It retains ownership, revision, pending-copy and brief-version guards. Compound styling requests use normal planning rather than executing a recognized fragment.

The sequence keeps its identities, words, ordinal order and appearance. Constrained proportional allocation retains uneven pacing with a 0.25-second minimum per word. Interior gaps scale by default. Explicit gap closing removes them; explicit fixed gaps retain their current durations. Canonical pacing metadata makes compression followed by expansion reversible. Manual timing edits replace that baseline. Multiple candidate groups or insufficient readable duration require clarification. Independent layers remain separate.

## Runtime and verification boundaries

The compiler resolves whole-video timing after footage fitting and against the actual timeline duration. It refuses an unreachable explicit numeric target. Version-bound brief updates replace prior duration or sequence policies instead of appending contradictory requirements. Retry and Undo reconcile the ledger against the compiled draft, including an older restored snapshot. Collective sequence checks verify ordered intervals and the requested span rather than requiring every word to cover the whole video.

The internal `retime_text_sequence` opcode fails closed on an older worker. The edit-copilot contract is versioned at `2026-10-30-v92` and describes bulk sequence editing. Hand-authored offline golden metadata is explicitly repinned, not represented as newly captured provider output. No paid provider replay was performed.

Newly introduced or increased same-anchor overlap in a sequence is rejected. Unchanged overlaps and intentional independent layers remain editable. Clip-bound labels and pins retain attachment semantics unless all text is explicitly protected.

## Timeline clock

The duration allocator quantizes to frames. Legacy native projection consumes millisecond boundaries. Independently rounding 28 durations could turn a 12-second edit into 11.991 seconds. The writer allocates legacy millisecond durations from cumulative boundaries. Source-window-marked timelines retain their existing six-decimal frame clock and source-bound checks. A 1-picosecond arithmetic budget tolerance handles exact readability-floor subtraction; it does not relax source bounds or permit actual unreadable spans.

## Reproduction

Run the backend modules through the repository's documented disposable PostgreSQL setup. The journey manifest selects preservation, natural-intent, pacing, ledger, contract and runtime tests for this change. Provider entrypoints are blocked in the natural-intent database cases.

Regenerate the synthetic native fixture from the repository root:

```sh
src/apps/api/.venv/bin/python scripts/ios/duration-text-preservation-fixtures.py
```

Run `KriaTests/DurationTextPreservationTests` using the repository native test workflow. Positive cases cover preservation, compression, uneven expansion, expansion after compression, JSON reopening, preview and export. A deliberately stacked visible-text control must fail the single-word content oracle. The generator uses the real intent helper, parser and compiler. Native fixtures contain synthetic media and words.

The required `make verify-editor-timeline` gate covers browser and backend behavior. Mobile browser viewports and simulator exports do not establish real-device acceptance. Local evidence does not establish app Save/relaunch, original-media fulfillment, production rollout or the user's phone result. The user's report that the opening error is gone remains distinct from attribution to a particular build. KRI-1060 tracks remaining acceptance and the separate reopening repair.

## Retained local native result

The six-case simulator rerun passed 270 exact OCR observations and 104 blank-gap checks. Exports contain 600 frames for each 20-second case and 360 for each 12-second case at 30 fps. The stacked negative control remains effective. Input hashes, case details and limits are retained in [the synthetic native evidence](duration-text-native-evidence.json). The subsequent fixed-gap-only allocation correction is covered by pure regressions, not these default scaled-gap exports.
