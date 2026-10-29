# Native verification

The initial New Chat alignment assertion failed before the fix: the Montage card began at x=123 while the heading began at x=16. After the fix, all 10 selected UI checks passed without skips on an iPhone 17 Pro simulator running iOS 26.5.

The checks cover initial heading alignment, leftmost playback ownership, a visible right-hand video staying paused, Voiceover and Talk to camera labels, Slider timing and pause, five simultaneous signup players, resuming after the reviewer sheet, provider/legal actions, accessibility text sizes, Reduce Motion, and sign-in notices. Signup and carousel screenshots were inspected. This is native simulator evidence, not a physical-device or production rollout claim.

The app keeps the existing muted bundled media. The new hero renders both portrait edits beside the three landscape edits, preserving their aspect ratios. The player completion policy loops every signup edit independently. Existing format server identifiers and authentication build guards remain unchanged. Existing historical preview evidence describes the prior centered/sequential design and is superseded by this acceptance record.

Reproduce after building the native Debug UI bundle with `xcodebuild test-without-building -scheme Kria -only-testing:KriaUITests/CreationUITests/testLaunchEntersChatFirstWorkspace -only-testing:KriaUITests/FeaturedEditPreviewUITests -only-testing:KriaUITests/SignInUITests -only-testing:KriaUITests/ProductionSignInUITests`.

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
  "finishTime" : 1793255432.137,
  "passedTests" : 0,
  "result" : "Failed",
  "skippedTests" : 0,
  "startTime" : 1793255324.857,
  "statistics" : [

  ],
  "testFailures" : [
    {
      "failureText" : "XCTAssertEqualWithAccuracy failed: (\"123.0\") is not equal to (\"16.0\") +/- (\"2.0\")",
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
      "passedTests" : 10,
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
  "finishTime" : 1793256011.819,
  "passedTests" : 10,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793255893.324,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 10
}

```

## Capacity warning label correction

CI ran 2,104 unit tests with one failure and 26 explicit skips. The failing existing test still expected the previous name, “Talking supports 1 clip.” The displayed warning correctly used the creator-requested “Talk to camera” name. The same mismatch reproduced locally. Only the literal expected wording changed; the test still accepts one clip and refuses two clips at a one-clip limit. All 45 ChatWorkspace tests then passed without skips. No app code changed for this correction.

Before correction, unedited `xcresulttool get test-results summary` JSON.

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
  "finishTime" : 1793258178.053,
  "passedTests" : 0,
  "result" : "Failed",
  "skippedTests" : 0,
  "startTime" : 1793258160.744,
  "statistics" : [

  ],
  "testFailures" : [
    {
      "failureText" : "XCTAssertEqual failed: (\"Optional(\"Talk to camera supports 1 clip. Keep your current format or remove extra footage first.\")\") is not equal to (\"Optional(\"Talking supports 1 clip. Keep your current format or remove extra footage first.\")\")",
      "targetName" : "KriaTests",
      "testIdentifier" : 1,
      "testIdentifierString" : "ChatWorkspaceTests/testFormatChangeRefusesAnOverCapacityTalkingCut()",
      "testIdentifierURL" : "test://com.apple.xcode/Kria/KriaTests/ChatWorkspaceTests/testFormatChangeRefusesAnOverCapacityTalkingCut",
      "testName" : "testFormatChangeRefusesAnOverCapacityTalkingCut()"
    }
  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 1
}

```

After correction, unedited `xcresulttool get test-results summary` JSON.

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
      "passedTests" : 45,
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
  "finishTime" : 1793258246.586,
  "passedTests" : 45,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793258233.909,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 45
}

```
