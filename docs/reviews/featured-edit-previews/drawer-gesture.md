# Drawer gesture CI correction

CI failed `CreationUITests/testLaunchEntersChatFirstWorkspace` at its swipe-open assertion on three attempts. The drawer button succeeded before the failing swipe. The same failure reproduced locally before the correction.

The native accessibility snapshot records a 402 by 874 point window and the format carousel at y=225.7 through y=484. The test's fixed 55% screen-height swipe starts at y=480.7, inside the carousel. The drawer intentionally yields horizontal drags to that scroll view. The new 228-point preview height moved the test gesture onto that protected surface.

The test now starts its drawer gesture 20 points above the carousel and verifies that this remains below the header button. It still checks that carousel swipes do not open the drawer, the button toggles it, workspace swipes open and close it, and the drawer can navigate to Gallery. No production code, timeout, expected label, or behavioral assertion was removed or relaxed.

The corrected journey passed three consecutive simulator runs. `drawer-open.png` shows the resulting open drawer. Earlier evidence and the Release email-sign-in check remain unchanged.

Reproduce from `src/apps/ios` with the built Debug UI bundle:

```sh
xcodebuild -project Kria.xcodeproj -scheme Kria -destination 'platform=iOS Simulator,id=BD43D2A9-660A-46B5-9EE0-54E576B8B7EC' -derivedDataPath .derived-data -parallel-testing-enabled NO -only-testing:KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace -test-iterations 3 CODE_SIGNING_ALLOWED=NO test-without-building
```

## Before correction

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
      "failedTests" : 1,
      "passedTests" : 0,
      "skippedTests" : 0,
      "testPlanConfiguration" : {
        "configurationId" : "1",
        "configurationName" : "Test Scheme Action"
      }
    }
  ],
  "environmentDescription" : "Kria · Built with macOS 26.4.1",
  "expectedFailures" : 0,
  "failedTests" : 1,
  "finishTime" : 1793208138.781,
  "passedTests" : 0,
  "result" : "Failed",
  "skippedTests" : 0,
  "startTime" : 1793208081.796,
  "statistics" : [

  ],
  "testFailures" : [
    {
      "failureText" : "XCTAssertTrue failed",
      "targetName" : "KriaUITests",
      "testIdentifier" : 1,
      "testIdentifierString" : "CreationUITests/testLaunchEntersChatFirstWorkspace()",
      "testIdentifierURL" : "test://com.apple.xcode/Kria/KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace",
      "testName" : "testLaunchEntersChatFirstWorkspace()"
    }
  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 1
}

```

## After correction

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
      "passedTests" : 3,
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
  "finishTime" : 1793208292.372,
  "passedTests" : 1,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793208212.076,
  "statistics" : [
    {
      "subtitle" : "3 test runs",
      "title" : "1 configuration ran with test repetitions"
    }
  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 1
}

```

## Neighboring journeys

Seven further native checks passed with no skips or failures. They cover attachment creation for both runtimes, editor back navigation and playback, all three featured-preview journeys, and Debug sign-in visibility.

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
      "passedTests" : 7,
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
  "finishTime" : 1793208438.767,
  "passedTests" : 7,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793208313.921,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 7
}

```
