# KRI-985: sideways rows vs the projects drawer

Run on iPhone 17 Pro simulator, iOS 26.5, `xcodebuild ... test` against `src/apps/ios/Kria.xcodeproj`.

## Red control

Fix removed (format carousel opt-out deleted, no UIKit hit test):

```
CreationUITests.swift:108: error: testLaunchEntersChatFirstWorkspace :
  XCTAssertEqual failed: ("Close projects") is not equal to ("Open projects") - Format-card swipes must not open the drawer
```

## Green

With `DrawerGestureTracking.startsInSidewaysScroller` in `ProjectsDrawerContainer`:

| Test | Result |
| --- | --- |
| `CreationUITests.testLaunchEntersChatFirstWorkspace` (swipe on carousel keeps drawer closed; swipe on chat area opens and closes it) | passed |
| `SlidePostUITests.testStripScrollsWithAShortPressFingerDrag` | passed |
| `SongRangeQuestionUITests` (8) | passed |
| `MomentOrderUITests` (4) | passed |
| `ObjectMarkUITests` (4) | passed |
| `CameraRollCollectionUITests.testDrawerOpensTheCameraRollFullScreen` | passed |

19 tests, 0 failures. Not covered here: a device run, and a dedicated test for plan clip filmstrips.

## Hit-test facts (from view-hierarchy dumps)

- A SwiftUI horizontal `ScrollView` nested in the page is a `HostingScrollView` (`UIScrollView`) and is an ancestor of the touched view, so the superview walk finds it.
- A row whose content fits the screen is not a scroller for this check and does not block the drawer swipe.
