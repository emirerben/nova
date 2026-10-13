# Kria iPhone verification map

Read this index and the affected feature before selecting tests. These recipes
describe user-visible behavior; the existing XCUITest bodies contain the exact
taps and assertions. All five features are mapped, not automatically verified.

[journeys.json](journeys.json) lists the regression journeys local background
hooks select for committed native changes. Update paths/tests there with these
recipes. Shared or unmapped UI files run all five areas, plus required smoke,
with duplicate test IDs removed. The broad selection contains 18 UI tests;
the full creation/editor regression suites remain separate existing CI policy.
Hardware, live backend/model, and output-media inspection retain the limits
documented in each recipe.

## Baseline

- Use Launch/Doctor/Cleanup from [SKILL.md](../SKILL.md). They define
  `KRIA_VERIFY_CONTROL` and a unique `KRIA_VERIFY_RUN` at the worktree root.
- Each run starts with an empty owned simulator; selected tests seed their own
  fixture account, capabilities, project/media and transport responses.
- One `drive` selection per launch. Combine tests listed in a feature when
  checking multiple entry points. Another selection needs another fresh run.
- Do not source a production `.env` or use the paired physical iPhone for these
  commands. Offline fixture authentication does not validate live sign-in.

## Driving and proof

- Prefer accessibility identifiers and native button labels. Gesture coordinates
  in existing tests are relative to discovered controls, not fixed screen pixels.
- Pair the user action with the observable result and a second read of any stored
  value. Inspect exported screenshots, result coverage and flaky-test reports.
- Record the feature ID, selected entry points, HEAD/fingerprint and substituted
  boundaries. A mock ready card or successful Photos banner is not an output file.
- Missing media/capabilities/auth are unmet preconditions. Keep the journey failed
  or unverified; never weaken it or claim another entry point covers it.
- Physical recording, actual phone exports, authenticated API and live model
  behavior require separately authorized runs using the iOS development runbook.

## Features

- [Projects and new chats](projects.md): drawer, gallery, fresh chat, actions.
- [Creation and media](creation.md): Photos/library intake, format, confirmation,
  ready/error/retry paths and voiceover attachment.
- [Talking and teleprompter](teleprompter.md): script persistence, camera failure,
  adjustable reading window and recording recovery.
- [Native video editor](editor.md): playback, trim/text/drawing, Save/reopen, export.
- [Slide posts](slides.md): creation/drawer/gallery entry, size/text/look/drawing,
  Save and export.

Maintain entries with exactly the four H2s used below. Add new entry points as
the app changes; `/maintain-verification-skill` is the maintenance workflow.
