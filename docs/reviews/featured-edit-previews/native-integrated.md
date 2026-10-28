# Native integration verification

After integrating main at 060922cae, all five focused native checks passed.
They cover the drawer journey, centered playback, four-edit onboarding sequence,
Slider timing and pause, and Debug sign-in visibility. The test-inventory conflict
was resolved by retaining both branches’ entries. All 117 native tooling checks
also passed and the discovered UI test inventory matches the manifest.

Unedited JSON from `xcresulttool get test-results summary`.

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
      "passedTests" : 5,
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
  "finishTime" : 1793222145.404,
  "passedTests" : 5,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793221908.492,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 5
}

```
