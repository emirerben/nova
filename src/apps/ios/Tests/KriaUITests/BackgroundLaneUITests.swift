import XCTest

/// KRI-1162 (failure mode S45): the timed backgrounds of a Talk-to-camera edit live on their own BACKGROUND lane,
/// and what the lane shows, what the inspector reads and what Save sends are one thing. The fixture opens
/// `speaker_background_windows` and starts from a photo fill with two windows: a photo 0-2 s and a video 2.5-5 s on
/// a 6 s edit. Save goes through the stub commit transport, which keeps the lane it was sent across relaunches.
@MainActor
final class BackgroundLaneUITests: XCTestCase {
    private let windowKind = "speaker_background_window"

    private func launch(reset: Bool, windows: Bool = true) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speaker-background"]
            + (windows ? ["-ui-testing-editor-speaker-background-windows"] : [])
            + (reset ? ["-ui-testing-editor-speaker-background-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        return app
    }

    private func block(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.buttons["native-editor-timeline-\(windowKind)-\(id)"]
    }

    private func seconds(_ app: XCUIApplication, _ edge: String) -> Double {
        let element = app.staticTexts["native-editor-selected-\(windowKind)-\(edge)"]
        XCTAssertTrue(element.waitForExistence(timeout: 5), "the inspector shows the window's \(edge)")
        return Double(element.value as? String ?? "") ?? -1
    }

    /// Opens the inspector of a window by tapping its block, reads its times, and closes it again.
    private func readTimes(_ app: XCUIApplication, _ id: String) -> (start: Double, end: Double) {
        let target = block(app, id)
        XCTAssertTrue(target.waitForExistence(timeout: 10), "the BACKGROUND lane has a block for \(id)")
        target.tap()
        let times = (seconds(app, "start"), seconds(app, "end"))
        app.buttons["native-editor-inspector-done"].tap()
        return times
    }

    private func openBackgroundTool(_ app: XCUIApplication) {
        let tool = app.buttons["native-editor-tool-background"]
        XCTAssertTrue(tool.waitForExistence(timeout: 5))
        tool.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-panel"].firstMatch.waitForExistence(timeout: 5))
    }

    private func saveAndWait(_ app: XCUIApplication) {
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled, "there is something to save")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
    }

    func testDraggingAWindowMovesTheInspectorAndTheSave() {
        let app = launch(reset: true)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-lane-background"].firstMatch.waitForExistence(timeout: 10),
                      "a project with windows has the BACKGROUND lane")
        XCTAssertTrue(app.buttons["native-editor-timeline-background-fill"].exists, "and the fill as a strip on it")
        let seeded = readTimes(app, "bg-1")
        XCTAssertEqual(seeded.start, 2.5, accuracy: 0.001)
        XCTAssertEqual(seeded.end, 5.0, accuracy: 0.001)

        // Hold, then drag the block's body a little to the right (a short drag keeps it inside the first 3 s the timeline
        // shows at the start, so the reopened lane can find it without scrolling).
        let body = block(app, "bg-1").coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        body.press(forDuration: 0.7, thenDragTo: body.withOffset(CGVector(dx: 16, dy: 0)))
        let moved = readTimes(app, "bg-1")
        XCTAssertGreaterThan(moved.start, seeded.start + 0.1, "the block moved later")
        XCTAssertEqual(moved.end - moved.start, 2.5, accuracy: 0.002, "a move keeps the window's length")
        XCTAssertLessThanOrEqual(moved.end, 6.0 + 0.001, "and stays inside the video")
        XCTAssertEqual(readTimes(app, "bg-0").end, 2.0, accuracy: 0.001, "the neighbour did not move")

        saveAndWait(app)
        app.terminate()

        // What was saved is what the reopened lane shows: the Save carried the dragged times, not the seeded ones.
        let reopened = launch(reset: false)
        let after = readTimes(reopened, "bg-1")
        XCTAssertEqual(after.start, moved.start, accuracy: 0.0015)
        XCTAssertEqual(after.end, moved.end, accuracy: 0.0015)
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
    }

    func testTheSequenceTilesAddAndRemoveWindowsAndTheSpeakerControlSetsThePreset() {
        let app = launch(reset: true)
        openBackgroundTool(app)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-sequence-bg-0"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-sequence-bg-1"].exists)
        XCTAssertTrue(app.buttons["native-editor-background-speaker-full"].isSelected, "no corner frame yet")

        let add = app.buttons["native-editor-background-sequence-add-fixture-bg-photo"]
        XCTAssertTrue(add.exists, "the pool tiles of the Sequence add a window")
        add.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-sequence-bg-2"].waitForExistence(timeout: 5),
                      "the new window joins the end of the sequence")

        app.buttons["native-editor-background-sequence-remove-bg-0"].tap()
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-background-sequence-bg-0"].waitForExistence(timeout: 2), "the x removes its window")

        let corner = app.buttons["native-editor-background-speaker-small_bottom_right"]
        corner.tap()
        XCTAssertTrue(corner.isSelected)
        XCTAssertFalse(app.buttons["native-editor-background-speaker-full"].isSelected)

        app.buttons["native-editor-background-done"].tap()
        saveAndWait(app)
        app.terminate()

        let reopened = launch(reset: false)
        openBackgroundTool(reopened)
        XCTAssertTrue(reopened.buttons["native-editor-background-speaker-small_bottom_right"].waitForExistence(timeout: 5))
        XCTAssertTrue(reopened.buttons["native-editor-background-speaker-small_bottom_right"].isSelected, "the saved corner comes back")
        XCTAssertTrue(reopened.descendants(matching: .any)["native-editor-background-sequence-bg-1"].exists)
        XCTAssertTrue(reopened.descendants(matching: .any)["native-editor-background-sequence-bg-2"].exists || reopened.descendants(matching: .any)["native-editor-background-sequence-bg-0"].exists,
                      "the added window is still there; ids are renumbered at load")
    }

    func testAProjectWithoutWindowsKeepsItsLanesAndHidesTheWindowControls() {
        // The windows gate closed: the single background works as before and nothing new shows.
        let closed = launch(reset: true, windows: false)
        openBackgroundTool(closed)
        XCTAssertTrue(closed.buttons["native-editor-background-visual-fixture-bg-photo"].waitForExistence(timeout: 5))
        XCTAssertFalse(closed.descendants(matching: .any)["native-editor-background-sequence-title"].exists)
        XCTAssertFalse(closed.buttons["native-editor-background-speaker-full"].exists)
        closed.buttons["native-editor-background-visual-fixture-bg-photo"].tap()
        closed.buttons["native-editor-background-done"].tap()
        XCTAssertFalse(closed.descendants(matching: .any)["native-editor-lane-background"].exists, "a fill alone has no lane")
        closed.terminate()

        // The gate open and windows seeded: now the lane appears.
        let open = launch(reset: true, windows: true)
        XCTAssertTrue(open.descendants(matching: .any)["native-editor-lane-background"].waitForExistence(timeout: 10), "windows give the lane")
    }
}
