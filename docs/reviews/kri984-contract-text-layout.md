# Keep confirmed intro text readable and editable

The installed Cunda output plays in Kria and in Photos, but its opening hook
overlaps the creator's added facts. This repair addresses the overlap and the
first editor read dropping those added facts. KRI-984 remains open.

## Acceptance and failure modes

Keep both exact texts, their font, size, time windows, footage, and audio.
Reserve separate measured text bounds in portrait and landscape, in English
and Turkish. Keep the immutable approval unchanged. A saved manual editor
revision takes priority over the initial projection.

The check must reject the original fixed-offset overlap and incorrect words.
If the confirmed copy cannot fit, ask for a size or time choice. Never silently
shrink it, remove words, or move its time window. Existing small text must not
inherit a new minimum-size restriction. These criteria were recorded before
the implementation in the task's failure-mode record.

## Root causes and change

Contract repair used fixed vertical offsets for newly added text. Two wrapped
76 px texts occupied the same opening window, so the offsets put their ink on
top of each other. Repair now measures the actual bundled-font bounds and
existing animation envelopes. It places only the new text into available
space, with a 24 px gap. If no position fits, it returns the existing
`needs_choice` refusal.

The worker stores its repaired plan in `_phone_editor_plan_v1`. The initial
editor projection read the older approval plan instead. It now uses the
worker plan, while valid saved editor revisions still take priority.

## Verification

The overlap regression failed in all four English/Turkish and
portrait/landscape cases before the placement change. The first-editor-read
regression also failed before its route change. The manual-revision control
already passed. All failures remain in the local evidence folder.

On integrated main `19aa3b3cf40af8285c9305cd4af30919134afc10`:

```bash
bash scripts/check-api.sh test tests/routes/test_contract_text_initial_projection.py tests/pipeline/test_contract_text_layout.py tests/tasks/test_phone_plan_contract_repair.py tests/pipeline/test_phone_plan_repair.py tests/schemas/test_guided_edit_revision.py tests/routes/test_phone_speed_safety.py tests/services/test_creator_render_contract.py -q
```

Result: 173 passed, zero skips. A separate neighboring editor/narration suite
passed 63 checks before main integration. The new main changes none of those
editor route, schema, source-resolution, or native inputs.

`make verify-overlays` passed 138 overlays in a fresh build of the production
Dockerfile. No warnings, unexpected failures, or skipped overlays occurred.
OCR was unavailable, so this gate establishes frame bounds rather than words.
The default shared Docker disk was full. The same gate ran in a task-owned
isolated Docker VM, without deleting shared images or data.

The real native text store and compositor rendered all four public text cases
on an iPhone 17 simulator running iOS 26.5. One test passed with zero skips.
The eight before/after frames retain both texts. Every old placement overlaps;
every new placement has separate on-canvas ink bounds. The English landscape
and Turkish portrait frames were visually inspected. An earlier attempt failed
because Xcode replaced the fixture container; no app assertion ran in that
attempt. Restoring the fixture to the current container fixed the setup.

The native inputs regenerated after main integration differ only in the order
of the capability set. All other recipe fields match the consumed inputs.
The [safe proof summary](kri984-contract-text-layout-proof.json) retains the
bounds, source revisions, input hashes, image hashes, and control outcomes.
The private task folder retains the logs, result bundle, and synthetic PNGs.

An offline control replaces the facts with different words. The real contract
verifier rejects it. Another supplies valid but unplaceable long copy. Repair
returns `needs_choice` and leaves the input plan unchanged. The first control
attempt exceeded the schema's text limit and is retained as a harness failure;
the recorded placement control stays within that limit.

## Limits and remaining launch work

This is a local fix. It does not alter or rerender the existing published Cunda
video. A corrected production render and Photos playback remain unverified.
No new planning call, creative approval, or manual deployment was performed.

The main installed app's `RecipeError.invalidTimeline` editor preview failure
remains unresolved. Reconstructing its exact 21 sources passed on physical
iOS 27, but that replay does not exercise the installed app's document hydration.
The original 28.804-second timing request also remains unmet. A quiet source
segment missed a strict offline audio-correlation threshold; its cause has not
been isolated. None of these gaps is cleared by this text-only native proof.

Troya and marathon/açaí still need their original-media incident replays from
Emir's files. Tigre and Don Julio need Yasin's originals. Their exact-candidate,
native-output, and Photos checks remain open. Launch is not cleared.
