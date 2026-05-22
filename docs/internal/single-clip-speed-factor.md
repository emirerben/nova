# Single-Clip Speed Factor Verification

Generated for the one-video beat-sync test job plan.

## Speed Factor Convention

`speed_factor` follows **Convention B**:

```text
source_seconds_needed = output_seconds * speed_factor
```

A `speed_factor` of `2.0` means a `2.0s` source segment renders as `1.0s` of output video. Equivalently, a slot that needs `1.0s` of output requires `2.0s` of source footage.

## Evidence

- `src/apps/api/app/tasks/template_orchestrate.py:2383-2384` reads `speed_factor` from the slot and computes `source_duration = slot_target_dur * speed_factor`. This is the source trim length selected for the slot.
- `src/apps/api/app/tasks/template_orchestrate.py:2473-2474` advances the clip cursor by `source_duration` and caps `end_s` at `start_s + source_duration`, confirming that the multiplied value is the consumed source window.
- `src/apps/api/app/tasks/template_orchestrate.py:2511-2514` stores `start_s`, `end_s`, and `speed_factor` together in `SlotPlan`, preserving that source window for render.
- `src/apps/api/app/tasks/template_orchestrate.py:2750-2752` forwards the same source window and `speed_factor` into `SinglePassInput`.
- `src/apps/api/app/pipeline/single_pass.py:267-275` documents and computes the output duration as `(inp.end_s - inp.start_s) / inp.speed_factor`; the inline example says clips at `speed_factor=2.0` occupy half their source duration in the output.
- `src/apps/api/app/pipeline/reframe.py:590-592` applies `setpts=PTS/{speed_factor}`, so values greater than `1.0` speed playback up and shorten output duration.

## Worked Example

For a beat-sync slot with:

- `target_output_s = 1.0`
- `speed_factor = 2.0`

The planner computes:

```text
source_duration = 1.0 * 2.0 = 2.0s
```

The renderer then computes output duration as:

```text
output_duration = 2.0 / 2.0 = 1.0s
```

So Bead 3 should encode this acceptance case: a `1.0s` target output with `speed_factor=2.0` requires `2.0s` of raw source footage.

## Output For Next Bead

Convention string: `B`
