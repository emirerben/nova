# KRI-785: reconcile the portable font journey with the merged fallback

## Verified intent and baseline

[Merged #1721](https://github.com/emirerben/nova/pull/1721) deliberately maps
Big Curls to Great Vibes when Turkish letters are missing, preserving the whole
text. Truly uncovered text still refuses; unknown fonts stay invalid.

The portable journey still expected `İstanbul` + Big Curls to raise
`needs_choice`. That stale assertion stopped fixture generation at
`scripts/ios/phone-text-style-e2e.py:400`, before native validation.
Affected hosted examples:

- [#1726 CI, job 119454315222](https://github.com/emirerben/nova/actions/runs/39666544360/job/119454315222)
- [#1700 CI, job 119455233508](https://github.com/emirerben/nova/actions/runs/39666907758/job/119455233508)

The KRI-721 owner independently reproduced the exact clause on immutable main
`110e87197ccaef219c5dfcfdd65f5f7b41dd712a`: no clarification, exact `İstanbul`
preserved, Great Vibes selected. Its diagnostic captured the observation, not a
passing old assertion. This repair starts from fresh main
`3519e7cf00303da7af69b2b1453ee82f5c5e7853`; the harness and relevant font
resolver/coverage files match that older main byte for byte. No other open PR
for `phone-text-style-e2e` was found before implementation.

## Acceptance and change

Failure modes and acceptance were recorded before editing in `.dev/acceptance.md`.
Replace only the stale journey expectation. The helper is called by the real
portable journey and contributes its result to `kri648-measurements.json`.
It asserts one row, exact id/text/timing preservation, the registered Great Vibes
partner, and no missing glyphs in the displayed text. It also requires
uncovered `東京` to retain the typed `needs_choice` / `phone_text_style` refusal
and rejects an unknown font. Product fallback and rendering code are unchanged.

## Focused validation

From the isolated worktree:

```sh
src/apps/api/.venv/bin/python -c 'import json, runpy; print(json.dumps(runpy.run_path("scripts/ios/phone-text-style-e2e.py")["verify_font_coverage"](), ensure_ascii=False))'
```

The real resolver returns:

```json
{"text": "İstanbul", "font_family": "Great Vibes", "missing_glyphs": []}
```

Eight in-memory negative controls were rejected by the actual helper: altered
text (`Istanbul`), id, start, end, or font (`Inter`); reported missing glyphs;
an uncovered-text call that incorrectly returns rows; and unknown-font
validation that incorrectly succeeds. The product code was not modified for
these controls. Local diagnostics are `.dev/font-coverage-check.json` and
`.dev/font-coverage-controls.json`. Scoped Ruff lint/format and whitespace
checks passed.

This is portable font-selection/coverage evidence, not a new rendered-video or
native-device pass. The full generator and hosted selected checks remain CI's
responsibility. No broad suite reruns, paid calls, phone operations, product
changes, merges or manual deploys were performed. The seven M2 assertions are
owned separately by draft #1735; this repair does not copy or replace them.
