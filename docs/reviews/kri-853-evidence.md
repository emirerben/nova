# KRI-853 verification evidence

Safe retained summary; full local logs and result bundles are named below. Tests bind to the immutable code trees and native input fingerprints. The PR receipt binds the final commit; evidence-only documentation does not change consumed code. Earlier published-head CI is superseded after this integration.

```json
{
  "issue": "KRI-853",
  "claim": "local_fix",
  "source": "Linear KRI-853 description read on 2026-10-23; original affected collection/build not supplied",
  "negative_control": {
    "command": "bash scripts/check-api.sh test tests/integration/test_media_collections.py::test_story_candidates_corrections_and_rejection_are_separate_from_render -q --tb=short",
    "result": "FAILED before fix: unchanged resume returned revision 2 rather than 1",
    "log": ".dev/kri-853-red.log"
  },
  "backend": {
    "command": "DATABASE_URL from .dev/test-db-url; python3 scripts/reliability-gates.py run kri-729-camera-roll-approval --log .dev/kri-853-integration-camera-roll-gate.log",
    "result": "111 passed, zero environment skips/errors; 86.24 seconds on integrated main",
    "provider_calls": "recorded/replayed transport only; no paid live calls",
    "log": ".dev/kri-853-integration-gate-summary.log"
  },
  "native_units": {
    "command": "KRIA_SIMULATOR_ID from .dev/kri-853-simulator-id; KRIA_IOS_TEST_MODE=prepare-ui make ios-verify",
    "result": "1897 executed; 25 skipped; zero failures",
    "bundle": "test-results/ios/prepare-ui.cG6e44/unit.xcresult",
    "log": ".dev/kri-853-integration-native.log",
    "consumed_input_fingerprint": "468eb496866e6355e6a4ba9f534692c35f21b98aa4ee60f205386384268ad38f",
    "freshness": "After this run only the UI-test Done selector/assertion changed. App, engine, resources, configuration and unit-test source hashes excluding Tests/KriaUITests were verified identical before and after rebuilding the UI bundle."
  },
  "native_ui": {
    "command": "KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS from .dev/kri-853-integration-ui-groups; make ios-verify",
    "status": "passed",
    "result": "14 selected tests passed; zero failures and zero retries needed; 214 seconds",
    "bundle": "test-results/ios/ui.Kf25UP/ui.xcresult",
    "log": ".dev/kri-853-integration-ui-final.log",
    "screenshot": ".dev/kri-853-integration-attachments/5AB6F67C-A4F5-4612-B600-0304697EBB51.png",
    "visual_review": "Inspected final iPhone 13 Pro simulator screenshot: explicit excluded-video warning, idea title, previews, Use this idea and collapsed clip details. Synthetic placeholders; no physical-device/export claim."
  },
  "limitations": [
    "No production or affected-account reproduction claimed",
    "No physical-device output or rollout claimed",
    "Renderer, prompts, cohort and cache-only policy unchanged"
  ],
  "native_input_fingerprint": "70d048a0b12b82ba75880c4e8063fb6abaaa2d573847ffecaabed6b6063f9efc",
  "tested_code_trees": {
    "src/apps/ios": "ac9a5e7634b45cc2338a9afc5eca97c576a49b4b",
    "src/apps/api": "99b1ea22b29460b4c0f506c8d69fb63584c88061"
  },
  "verification_date_utc": "2026-10-23",
  "integration": {
    "base": "ab510e0ebc57b5a1fdd57a3b4b035f7cd14c2281",
    "preserved": [
      "500-item capacity",
      "explicit partial-analysis consent",
      "independent idea state and named native checks"
    ],
    "repair": "Saved partial consent is reused only for the same excluded-video IDs; review callbacks retain that context."
  },
  "integration_red_control": {
    "command": "KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS=smoke,CameraRollCollectionUITests/testSavedIdeasSurviveRelaunchWithoutReselectingFootage make ios-verify",
    "result": "Failed all three retries before partial-consent persistence repair: saved partial ideas did not reopen after process termination. Smoke passed.",
    "log": ".dev/kri-853-integration-red-ui.log",
    "native_input_fingerprint": "d2042cb883628ee381eb02513c4a797ab14adfe0f91a5bef750d5c626b0213ec"
  }
}
```
