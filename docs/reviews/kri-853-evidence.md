# KRI-853 verification evidence

Safe retained summary; full local logs and result bundles are named below. Tests bind to the code trees and native input fingerprint; subsequent evidence-only documentation commits do not change these inputs.

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
    "command": "DATABASE_URL from .dev/test-db-url; python3 scripts/reliability-gates.py run kri-729-camera-roll-approval --log .dev/kri-853-camera-roll-gate-final.log",
    "result": "106 passed, zero environment skips/errors; 95.93 seconds after integrating origin/main",
    "provider_calls": "recorded/replayed transport only; no paid live calls",
    "log": ".dev/kri-853-gate-final-summary.log"
  },
  "native_units": {
    "command": "KRIA_SIMULATOR_ID from .dev/kri-853-simulator-id; KRIA_IOS_TEST_MODE=prepare-ui make ios-verify",
    "result": "1896 executed; 25 skipped; zero failures",
    "bundle": "test-results/ios/prepare-ui.kheWpZ/unit.xcresult",
    "log": ".dev/kri-853-native-corrected.log"
  },
  "native_ui": {
    "command": "KRIA_IOS_TEST_MODE=ui KRIA_IOS_UI_GROUPS from .dev/kri-853-ui-groups; make ios-verify",
    "status": "passed",
    "result": "12 selected tests passed; zero failures and zero retries needed; 174 seconds",
    "bundle": "test-results/ios/ui.4dZKoS/ui.xcresult",
    "log": ".dev/kri-853-ui-final.log",
    "screenshot": ".dev/kri-853-final-attachments/E9EDCD5A-FD6E-4665-B647-61B72F528661.png",
    "visual_review": "Inspected final iPhone 13 Pro simulator screenshot: title, previews, explicit Use this idea and collapsed clip details. Synthetic media placeholders; no export or physical-device claim."
  },
  "limitations": [
    "No production or affected-account reproduction claimed",
    "No physical-device output or rollout claimed",
    "Renderer, prompts, cohort and cache-only policy unchanged"
  ],
  "native_input_fingerprint": "45179fcc1cfeb6398b1dd2da0db7e361ad06ac0e95011cd6523c7d756aacdeff",
  "tested_code_trees": {
    "src/apps/ios": "778fe8655d531f37b29d24cb4e16071ae51d4e3b",
    "src/apps/api": "2b2621d311bf56e3c4d02e98fa1893ed362e9f93"
  },
  "tested_revision": "e38f6df388af9fd6676d9acab5707eda48b46048",
  "verification_date_utc": "2026-10-23"
}
```
