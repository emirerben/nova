import XCTest

/// KRI-1656: every Talk-to-camera background is a bar on the timeline. The fixture (`-ui-testing-editor-speaker-background-bars`)
/// opens the timed-background and section gates over a lane made of one photo fill and nothing else on a 6 s edit, the common
/// case: one whole-video bar. Save goes through the stub commit transport, which keeps the lane it was sent across relaunches.
@MainActor
final class BackgroundBarsUITests: XCTestCase {
    private let windowKind = "speaker_background_window"

    private func launch(reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speaker-background", "-ui-testing-editor-speaker-background-bars"]
            + (reset ? ["-ui-testing-editor-speaker-background-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        return app
    }

    private func block(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.buttons["native-editor-timeline-\(windowKind)-\(id)"]
    }

    private func anyBar(_ app: XCUIApplication) -> XCUIElement {
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "native-editor-timeline-\(windowKind)-")).firstMatch
    }

    private func times(_ app: XCUIApplication) -> (start: Double, end: Double) {
        let start = app.staticTexts["native-editor-selected-\(windowKind)-start"], end = app.staticTexts["native-editor-selected-\(windowKind)-end"]
        XCTAssertTrue(start.waitForExistence(timeout: 5), "the inspector shows the bar's times")
        return (Double(start.value as? String ?? "") ?? -1, Double(end.value as? String ?? "") ?? -1)
    }

    private func saveAndWait(_ app: XCUIApplication) {
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled, "there is something to save")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testAWholeVideoBackgroundIsOneBarOnTheLane() {
        let app = launch(reset: true)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-lane-background"].firstMatch.waitForExistence(timeout: 10),
                      "a background with no timed window still has the BACKGROUND lane")
        let bar = block(app, "fill-0")
        XCTAssertTrue(bar.waitForExistence(timeout: 5), "the fill is one bar")
        attach(app, "A whole-video background is one bar")
        bar.tap()
        let span = times(app)
        XCTAssertEqual(span.start, 0, accuracy: 0.001)
        XCTAssertEqual(span.end, 6, accuracy: 0.01, "over the whole 6 s video")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-selected-background-crop"].exists, "Crop replaces Aim")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-selected-background-replace"].exists)
        XCTAssertTrue(app.buttons["native-editor-selected-background-remove"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-background-aim-fill-left"].exists, "no Aim control remains")
    }

    func testThePanelIsColourOnePoolGridAndTheSpeakerAndNothingElse() {
        let app = launch(reset: true)
        let tool = app.buttons["native-editor-tool-background"]
        XCTAssertTrue(tool.waitForExistence(timeout: 5))
        tool.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-panel"].firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-background-color-title"].exists, "Colour")
        XCTAssertTrue(app.buttons["native-editor-background-visual-fixture-bg-photo"].exists, "one pool grid")
        XCTAssertTrue(app.buttons["native-editor-background-visual-fixture-bg-photo"].isSelected, "lit: the bar under the playhead shows it")
        XCTAssertFalse(app.buttons["native-editor-background-none"].exists, "removing a bar is its inspector's job")
        XCTAssertTrue(app.buttons["native-editor-background-speaker-full"].exists, "the speaker presets")
        for gone in ["native-editor-background-fill-title", "native-editor-background-sequence-title", "native-editor-background-sequence-empty",
                     "native-editor-background-aim-fill-left", "native-editor-background-fill-zoom"] {
            XCTAssertFalse(app.descendants(matching: .any)[gone].exists, "\(gone) is gone")
        }
        attach(app, "The Background tool")
        app.swipeUp()
        attach(app, "The Background tool, scrolled")
    }

    func testTrimmingTheBarShortensItAndTheFreedSpanSavesAsHerOwnTake() {
        let app = launch(reset: true)
        let bar = block(app, "fill-0")
        XCTAssertTrue(bar.waitForExistence(timeout: 10))
        bar.tap()
        app.buttons["native-editor-inspector-done"].tap()

        // The bar is selected: its leading handle is on screen (the timeline starts at 0 s). Drag it right.
        let handle = app.descendants(matching: .any)["native-editor-trim-leading"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5), "a selected bar shows its trim handles")
        let grip = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        grip.press(forDuration: 0.2, thenDragTo: grip.withOffset(CGVector(dx: 60, dy: 0)))

        let shortened = anyBar(app)
        XCTAssertTrue(shortened.waitForExistence(timeout: 5))
        shortened.tap()
        let span = times(app)
        XCTAssertGreaterThan(span.start, 0.3, "the bar's start moved later")
        XCTAssertEqual(span.end, 6, accuracy: 0.01, "and its end stayed")
        app.buttons["native-editor-inspector-done"].tap()
        attach(app, "The bar trimmed shorter")

        saveAndWait(app)
        app.terminate()

        // What was saved is what comes back: one shorter bar, and no left-over fill bar for the freed span.
        let reopened = launch(reset: false)
        let saved = anyBar(reopened)
        XCTAssertTrue(saved.waitForExistence(timeout: 10))
        saved.tap()
        let after = times(reopened)
        XCTAssertEqual(after.start, span.start, accuracy: 0.0015)
        XCTAssertEqual(after.end, span.end, accuracy: 0.0015)
        reopened.buttons["native-editor-inspector-done"].tap()
        XCTAssertEqual(reopened.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "native-editor-timeline-\(windowKind)-fill-")).count, 0,
                       "the freed span is her own take, not a second bar")
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
    }

    func testRemovingTheOnlyBarClearsTheBackground() {
        let app = launch(reset: true)
        let bar = block(app, "fill-0")
        XCTAssertTrue(bar.waitForExistence(timeout: 10))
        bar.tap()
        app.buttons["native-editor-selected-background-remove"].tap()
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-lane-background"].firstMatch.waitForExistence(timeout: 2),
                       "no bar left: the lane goes")
        saveAndWait(app)
        app.terminate()

        let reopened = launch(reset: false)
        XCTAssertTrue(reopened.buttons["native-editor-save"].waitForExistence(timeout: 10))
        XCTAssertFalse(reopened.descendants(matching: .any)["native-editor-lane-background"].firstMatch.exists, "the cleared background stays cleared")
    }

    func testCropOpensFromTheBarsInspector() {
        let app = launch(reset: true)
        let bar = block(app, "fill-0")
        XCTAssertTrue(bar.waitForExistence(timeout: 10))
        bar.tap()
        let crop = app.descendants(matching: .any)["native-editor-selected-background-crop"].firstMatch
        XCTAssertTrue(crop.waitForExistence(timeout: 5))
        crop.tap()
        // The fixture's pool photo has no file to draw on, so the honest "couldn't load" state shows; with a real photo the
        // crop editor opens (its gestures are the shared KriaCropEditor's, covered by the clip crop tests).
        let opened = app.descendants(matching: .any)["native-editor-background-crop"].firstMatch
        let unavailable = app.descendants(matching: .any)["native-editor-background-crop-unavailable"].firstMatch
        XCTAssertTrue(opened.waitForExistence(timeout: 5) || unavailable.waitForExistence(timeout: 5), "the Crop screen is up")
        attach(app, "The Crop screen")
        app.navigationBars.buttons.firstMatch.tap()   // back to the bar's inspector
        app.buttons["native-editor-inspector-done"].tap()
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertFalse(save.isEnabled, "opening Crop writes nothing")
    }
}
