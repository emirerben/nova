# Integrated native verification

Unedited JSON from `xcresulttool get test-results summary` after integrating
main at `db42721ce34e38aea41303ec4775945305fa439a`. The feature source was
unchanged by the integration. All four focused playback/build-condition checks
passed on the integrated branch.

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
      "passedTests" : 4,
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
  "finishTime" : 1793199381.499,
  "passedTests" : 4,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793199298.459,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria",
  "topInsights" : [

  ],
  "totalTestCount" : 4
}
```
