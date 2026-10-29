# Production sign-in

Final Release check after the user retained email sign-in for Apple reviewer access.
Email is present. Local-account access is absent. No DEV_AUTH or DEBUG condition was added.

Unedited JSON from `xcresulttool get test-results summary`.

```json
{
  "devicesAndConfigurations" : [
    {
      "device" : {
        "architecture" : "arm64",
        "deviceId" : "50596C09-D082-4819-B74C-765D5480F6F5",
        "deviceName" : "iPhone 17 Pro",
        "modelName" : "iPhone 17 Pro",
        "osBuildNumber" : "23F77",
        "osVersion" : "26.5",
        "platform" : "iOS Simulator"
      },
      "expectedFailures" : 0,
      "failedTests" : 0,
      "passedTests" : 1,
      "skippedTests" : 0,
      "testPlanConfiguration" : {
        "configurationId" : "1",
        "configurationName" : "Test Scheme Action"
      }
    }
  ],
  "environmentDescription" : "Kria Production SignIn Check · Built with macOS 26.4.1",
  "expectedFailures" : 0,
  "failedTests" : 0,
  "finishTime" : 1793202096.415,
  "passedTests" : 1,
  "result" : "Passed",
  "skippedTests" : 0,
  "startTime" : 1793201412.215,
  "statistics" : [

  ],
  "testFailures" : [

  ],
  "title" : "Test - Kria Production SignIn Check",
  "topInsights" : [

  ],
  "totalTestCount" : 1
}

```
