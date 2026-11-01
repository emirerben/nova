import XCTest

/// KRI-717 on a phone Talking edit: the server keeps the timeline closed and opens only Range
/// (`clips.source_window`). Drives the real sheet on a Talking segment, saves through the stub commit transport
/// (the real request encoder) and reopens to find the saved window; then checks a closed leaf says why.
@MainActor
final class NativeTalkingRangeUITests: XCTestCase {
    private func launch(reset: Bool, closed: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-talking-range", "-ui-testing-editor-live-preview"]
        if reset { app.launchArguments += ["-ui-testing-editor-talking-range-reset"] }
        if closed { app.launchArguments += ["-ui-testing-editor-talking-range-closed"] }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        return app
    }

    private func selectMiddleSegment(_ app: XCUIApplication) {
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = name
        capture.lifetime = .keepAlways
        add(capture)
    }

    func testRangeOnATalkingSegmentSavesAndReopensWithTheSavedWindow() {
        let app = launch(reset: true)
        selectMiddleSegment(app)
        let range = app.buttons["native-editor-clip-range"]
        XCTAssertTrue(range.waitForExistence(timeout: 5))
        XCTAssertTrue(range.isEnabled, "the open leaf unlocks Range although the timeline is closed")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-range-unavailable"].firstMatch.exists)
        range.tap()

        let label = app.staticTexts["kria-source-window-label"]
        XCTAssertTrue(label.waitForExistence(timeout: 10))
        let before = label.label
        let trailing = app.descendants(matching: .any)["kria-source-window-trailing"].firstMatch
        XCTAssertTrue(trailing.waitForExistence(timeout: 5))
        let grip = trailing.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        grip.press(forDuration: 0.05, thenDragTo: grip.withOffset(CGVector(dx: -40, dy: 0)))
        let edited = label.label
        XCTAssertNotEqual(edited, before, "the trailing handle shortens the segment")
        attach(app, "Range sheet on a Talking segment after shortening it")
        app.buttons["native-editor-inspector-done"].tap()

        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled, "a Range edit on a Talking segment is an unsaved edit")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)

        app.terminate()
        let reopened = launch(reset: false)
        selectMiddleSegment(reopened)
        reopened.buttons["native-editor-clip-range"].tap()
        let reopenedLabel = reopened.staticTexts["kria-source-window-label"]
        XCTAssertTrue(reopenedLabel.waitForExistence(timeout: 10))
        XCTAssertEqual(reopenedLabel.label, edited, "the saved window comes back")
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
        attach(reopened, "Reopened Talking segment with the saved range")
    }

    func testClosedLeafKeepsRangeVisibleAndSaysWhy() {
        let app = launch(reset: true, closed: true)
        let notice = app.descendants(matching: .any)["native-editor-range-unavailable"].firstMatch
        XCTAssertTrue(notice.waitForExistence(timeout: 20), "a closed Range is explained, never a silent dead end")
        XCTAssertTrue(notice.label.contains("single-clip talking videos"), notice.label)
        selectMiddleSegment(app)
        let range = app.buttons["native-editor-clip-range"]
        XCTAssertTrue(range.waitForExistence(timeout: 5))
        XCTAssertFalse(range.isEnabled)
        attach(app, "Talking edit with Range closed and the reason shown")
    }
}
