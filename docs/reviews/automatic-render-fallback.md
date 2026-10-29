# Automatic supported-version recovery

Request: when the iPhone TestFlight app cannot perform part of a video request,
create the supported version and explain specifically what was left out. Related
historical incident: KRI-610 (per-clip explanations prevented any draft); binding
and readiness boundaries: KRI-698 and KRI-721. This change is not a production
rollout or a claim that every possible renderer failure can be recovered.

## Acceptance and failure matrix

The regression fixtures specify these expectations independently of the result:

- A Talking video with a supported title/closing line and unsupported shot labels
  keeps the title, closing line, captions, source clips and original audio.
- Multiple unsupported decorations are removed individually; no repeated model
  call or second “simpler version?” choice is needed.
- A removed title's duration does not become a new blocker. A missing optional
  licensed sound effect does not prevent the remaining edit.
- The reply names the omitted text and explains the limitation. It never repeats
  the model's claim that an omitted label will appear, or says “as agreed” before
  the creator agrees.
- Only proven capability limits may be superseded automatically in the brief:
  unquoted per-clip text on native/Talking edits and the typed footage-brightness
  request. Superseded by KRI-1009: text the creator quoted (exact compiler-removed
  copy included) is asked about before the draft, never dropped automatically
  ([review](kri-1009-talking-top-text-title.md)). A merely missing supported label and a
  missing spoken trigger stay unresolved. Unknown evidence, missing sources,
  source-audio/mute restrictions, selection, order and timing retain their checks.
- Newly received requirements survive rollback if a genuine choice still blocks
  approval. The accepted binding contains the exact effective brief and revision.
- The normal render approval is still required. Automatic simplification removes
  the additional simplification question; it does not invent render approval.
- The phone recipe retains the supported title, clip order and narration. Both an
  unsimplified contract and a recipe missing the retained title must be rejected.

## Root cause and boundaries

`strategy_policy.check_strategy_for_runtime_v2` returned a question when a typed
renderer limitation prevented an optional text/SFX field. The runtime planner now
opts into bounded recompilation: remove the specific failing optional field,
record its exact omission, then compile again. Every iteration must remove a new
allowlisted field. Generic validation errors, media failures and ambiguous choices
are never caught as successful recovery.

Separately, `tasks.kria_runtime._complete_draft_turn` rolled back a partial draft
unless a previous simplification question had been explicitly accepted. The new
automatic branch combines receipt/readiness classifications with explicit
capability evidence, keeps the
creator's original brief revision, appends a superseding omission revision inside
the locked completion transaction, reassesses readiness and binds the supported
request. Exact compiler-removed copy is carried as server-only metadata into the
runtime so a missing title receipt cannot contradict the deliberate omission;
a different brief literal is never waived by it. The renderer and publication
verification remain enforced.

The stored model-transport replay in `test_approval_readiness_postgres.py` exercises
the real parser, planner, runtime and approval state in EN/TR for phone and cloud.
The native recipe journey replays a planned strategy; it does not test a live model.
Neither test sends private footage or makes paid provider calls.

## Repeatable verification

From the repository root, create an isolated database with
`bash scripts/dev/test-db.sh`, then run `python3 scripts/reliability-gates.py run --changed`.
The gate includes the automatic strategy, runtime omission and readiness tests.
`test_runtime_simpler_version_loop.py` separately replays historical explicit offers.
Only its historical native export case disables automatic classification to replay
an already offered choice; normal guided-label cases use the production classifier.

For an actual export through the shared native AVFoundation engine:

```bash
src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py prepare .dev/native-fallback --automatic
KRIA_AUDIO_PARITY_DIR="$PWD/.dev/native-fallback" swift test --package-path src/apps/ios/Packages/KriaMediaEngine --filter AudioParityFixtureTests
src/apps/api/.venv/bin/python scripts/ios/kri-698-simpler-version.py compare .dev/native-fallback
```

The script generates identifiable synthetic source clips and narration. It exports
the exact approved phone recipe and a no-title control. The comparison checks the
retained title, duration, ordered clips and audio. Media remain outside git.

The 22 October 2026 run passed all seven export checks and the shared engine's
`AudioParityFixtureTests`. The montage was inspected: the title is visible over
clip 1, absent over clips 2/3, and the three identifiable colors appear in order.
The audio check measures the synthetic narration tone throughout the six-second
export; this does not claim review of human speech. The retained report is
[`automatic-render-fallback-native.json`](automatic-render-fallback-native.json).
The original persisted brief rejects the omitted rule lines before simplification,
and the effective contract rejects a recipe missing the retained title.

Independent review found and corrected two gaps: `not_possible` alone also means
a supported label was missing, and compiler-removed titles still remained in the
brief. The regression suite now covers both boundaries plus mismatched title text.
Final local validation: 877 mapped API tests passed with zero environment skips;
88 gate-registry tests and 47 CI selector/journey tests passed. The title omission
case also asserts the saved readiness report is ready and retains the supported
caption requirement. Ruff and the diff whitespace check passed.

Evidence level and remaining gaps: offline replay and macOS AVFoundation native
output using the iOS app's shared renderer. Physical
iPhone/TestFlight and production replays remain pending; no backend deployment or
TestFlight build is performed by this PR. A model that refuses without proposing a
strategy, unknown/unclassified optional capabilities, and failures discovered only
after approval remain outside this bounded recovery path.

## Compound title/brightness repair acceptance

Before changing runtime code, the confirmed failure is a single text/title requirement
containing the literal `Match day` and `footage_light: brighter`: the supported draft
keeps the title, but automatic omission deletes its persistent requirement. Repair must
keep the title live in the saved brief and approval binding, name only the brightness
change as omitted, and keep the original compound request in immutable history.
The persisted regression uses the existing `Gameday.` phone fixture through the real
planner and readiness gate, replacing only provider transport. Both brighter and darker must behave alike. A draft missing the supported title must
still stop before approval; source/audio, unknown-evidence and genuine-choice guards
remain in force. Existing original-footage/native acceptance gaps remain open.

The corrected provider-replay baseline fails all three new cases on the original
runtime. With the repair, all three pass: the current brief and approval binding
retain the title, only `footage_light` is omitted, the original compound row remains
in the prior immutable version, and missing supported text still blocks approval.
All 109 fallback/consent/choice/real-Postgres readiness and native-recipe controls
pass. The initial synthetic-strategy probes lacked the relevant cohort/format
setup and are not counted as end-to-end omission evidence. No provider call was
made. This repair preserves copy; it does not add a brightness renderer or prove
physical-device output.

## Current-main policy test-double correction

Hosted run39771161500 failed24 API cases and one repeated KRI-612 offline journey case because narrow policy test doubles rejected the newly supplied simplify_unsupported keyword. The same24 failures reproduced locally before correction (104 other cases passed). Five test files now accept that explicit keyword with the real False default; the sixth affected suite imports the corrected shared helper. No broad catch-all, production fallback removal or assertion deletion was introduced. The existing kept-lipsync planner test also requires policy call modes [True, False]: simplify on initial compile, ordinary recheck for the kept strategy.

After correction, all128 cases across the six previously failing suites pass on current-main integration. Existing strategy-policy, automatic fallback, runtime simplification, historical simpler-version and real-PostgreSQL approval-readiness controls are run separately. This is a CI contract repair; it does not close the original KRI-698 physical voiceover/request/output gap or replace current-head CI.
