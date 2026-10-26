---
name: verify-kria-ios
description: Drive Kria iPhone creation, attachments, editor, delivery and slides with existing XCUITests. Use for launch checks and maintaining the native feature map.
---

# Verify Kria on iPhone

## Launch

Run from this task's worktree. Xcode, XcodeGen and an installed iPhone simulator
runtime are required. No production account or paid model call is needed for the
fixture UI pass. This pass does not establish production or physical-device behavior.

```bash
bash .agents/skills/verify-kria-ios/scripts/verify.sh launch
```

The helper creates its own simulator, boots it and builds the app and test runner.
It records its media-independent iOS input fingerprint. Never adopt another chat's
simulator. A successful build plus doctor permits driving. A build alone proves no
journey. If launch fails, preserve the log and run cleanup before trying again.

## Doctor

```bash
bash .agents/skills/verify-kria-ios/scripts/verify.sh doctor
```

Doctor requires the recorded simulator to be booted, a built test runner and the
same iOS inputs. Run before every drive and after any surprising failure. If a UI
test wedges, cleanup and launch again. Never retry on an unknown UI state.

## Drive

```bash
bash .agents/skills/verify-kria-ios/scripts/verify.sh drive chat
bash .agents/skills/verify-kria-ios/scripts/verify.sh drive media
bash .agents/skills/verify-kria-ios/scripts/verify.sh drive editor
bash .agents/skills/verify-kria-ios/scripts/verify.sh drive delivery
bash .agents/skills/verify-kria-ios/scripts/verify.sh drive slides
```

Each drive launches through XCUITest and checks visible actions and results. Read
[the feature index](features/README.md) for selectors, source and proof limits.
The transport fixtures replace backend and provider responses. In the delivery
UI fixture the exporter/upload are stand-ins. Use real native export checks too:

```bash
swift test --package-path src/apps/ios/Packages/KriaMediaEngine
bash scripts/check-api.sh test tests/services/test_launch_clarification_replay.py tests/services/test_kri948_single_take_resolution.py tests/services/test_kri950_picker_inventory.py tests/pipeline/test_launch_listed_members.py -q
```

## Evidence

Keep build logs, each drive's log and `.xcresult`, screenshots, fingerprints and
the simulator id in `.dev/verify-kria-ios/`. Set `KRIA_VERIFY_EVIDENCE` to an
absolute path before launch to retain a run elsewhere. Never reuse a drive name
as a replacement for a failed artifact. Each attempt gets a separate folder.

Read the result bundle. Record failures and skipped tests. Fixture UI, offline
backend replay, native export and production are separate claims. A ready card
does not prove the exported content. A share sheet does not prove publication.
Physical iPhone checks require a connected unlocked phone and the named release
candidate. Actual Photos writes, HEVC/HDR, interruption and thermal behavior stay
unverified until exercised there. Never mark these verified-unreachable without
recording the concrete route attempted and prerequisite.

## Cleanup

```bash
bash .agents/skills/verify-kria-ios/scripts/verify.sh cleanup
```

Cleanup shuts down and deletes only the recorded simulator this helper created.
It preserves all evidence. Check the result bundles still exist afterward.

## Helpers

`scripts/verify.sh` owns launch, doctor, serial drives and teardown. Its exact
invocations are above. Extend the feature map when a new native journey appears.
Run the maintenance skill after changes to this map or its driving instructions.
