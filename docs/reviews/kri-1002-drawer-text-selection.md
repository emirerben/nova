# KRI-1002: text-selection handles vs the projects drawer

iPhone 17 Pro simulator, iOS 26.5. Test: `CreationUITests.testDraggingASelectionHandleInTheComposerDoesNotOpenTheDrawer`
types text in the chat composer, long-presses, taps Select All, then drags the start handle to the right.

## Red control (fix absent)

```
CreationUITests.swift:142: error: XCTAssertFalse failed - Dragging a selection handle must not open the drawer
```

The post-drag screenshot shows the Recent chats drawer open and the selection unchanged.

## Green (fix in `DrawerGestureTracking.startsOnOwnDrag`)

The test passes. The post-drag screenshot shows the start handle moved into the word "delta" and the drawer closed.
`testLaunchEntersChatFirstWorkspace` (swipe on the chat area still opens and closes the drawer), the slide strip, song range,
moment order, object mark and camera roll drawer tests also pass (18 tests, 0 failures in the second run).

Not covered: a device run, and selections in text fields other than the chat composer (same code path: any first-responder
`UITextInput` with a non-empty selection).
