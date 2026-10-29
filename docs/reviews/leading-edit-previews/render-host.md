# Isolate the hosted render batch from signup playback

Two CI attempts exhausted the existing 600-second typography batch allowance at different styles. The completed cases reported no assertion failures. The complete 28-case fixture batch passed locally with the same limits.

Temporary instrumentation in `FeaturedEditPlayback` confirmed five decorative signup players entered the playing state during a hosted native narration render test. The application root remained visible behind the offscreen renderer. This is confirmed competing media work; remote CI still determines whether removing it resolves the observed deadline.

The typography batch now temporarily replaces the unrelated application root controller and restores it with `defer`. Instrumentation then recorded zero decorative playback events during the batch. The production app, preview players, renderer, all 28 cases, and every render assertion remain unchanged. The preview comparisons read production video compositions using `AVAssetImageGenerator`; they do not read the application's root view. The existing 180/600-second test limits are unchanged. Temporary instrumentation was removed before final verification.

The clean build passed the full typography batch and five native UI checks. Those checks cover the normal signup's five concurrent players and reviewer-sheet recovery, leftmost carousel ownership and non-leading pause, Slider speed and pause, heading alignment and drawer gestures, and Reduce Motion. They show that normal signup playback remains enabled after the hosted render check.

The earlier partial CI retry was rejected before tests because its fixture producer had not run for that attempt. No artifact was renamed or rebound. Future reruns must regenerate producer and consumer inputs together.

Reproduce with `TEST_RUNNER_KRIA_E2E_DIR` set to the portable typography fixture directory and `xcodebuild test -scheme Kria -test-timeouts-enabled YES -default-test-execution-time-allowance 180 -maximum-test-execution-time-allowance 600 -only-testing:KriaTests/DeviceMontageRenderE2ETests/testGenerationPinnedTextStylesPreviewAndExport -only-testing:KriaUITests/FeaturedEditPreviewUITests -only-testing:KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace -only-testing:KriaUITests/SignInUITests/testSignInWithReduceMotionIsInteractiveImmediately`.

## Clean verification

Unedited `xcresulttool get test-results summary` JSON.

```json
{
  "devicesAndConfigurations" : [
    {
      "device" : {
        "architecture" : "arm64",
        "deviceId" : "BD43D2A9-660A-46B5-9EE0-54E576B8B7EC",
        "deviceName" : "Kria featured preview verification",
        "modelName" : "iPhone 17 Pro",
        "osBuildNumber" : "23F77",
        "osVersion" : "26.5",
        "platform" : "iOS Simulator"
      },
      "expectedFailures" : 0,
      "failedTests" : 0,
      "passedTests" : 6,
      "skippedTests" : 0,
      "testPlanConfiguration" : {
        "configurationId" : "1",
        "configurationName" : "Test Scheme Action"
      }
    }
  ],
  "environmentDescription" : "Kria · Built with macOS 26.4.1",
  "expectedFailures" : 0,
  "failedTests" : 0,
  "finishTime" : 1793265940.688,
  "passedTests" : 6,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793265780.525,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 6
}

```
