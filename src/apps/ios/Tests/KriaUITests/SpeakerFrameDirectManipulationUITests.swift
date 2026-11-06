import XCTest

/// Tap the corner speaker of a Talk-to-camera green-screen edit on the preview, then drag to move and pinch to resize
/// them. Rows SF1, SF2, SF3, SF6 in `docs/reviews/speaker-frame-direct-manipulation-failure-modes.md`. The fixture
/// opens `speaker_background_windows` and starts from a plain-colour fill with the speaker at the bottom right
/// (`small_bottom_right`: width 42%, centre 76% / 80%). Save goes through the stub commit transport, which keeps the
/// saved lane across relaunches.
@MainActor
final class SpeakerFrameDirectManipulationUITests: XCTestCase {
    private struct Placement: Equatable { var x: Int; var y: Int; var size: Int }

    private func launch(reset: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        // `-ui-testing-editor-live-preview` builds the editable source preview (the bundled clip as the take), which is
        // where the speaker is a preview object; a colour fill needs no pool media.
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-speaker-background",
                               "-ui-testing-editor-speaker-background-windows", "-ui-testing-editor-speaker-frame",
                               "-ui-testing-editor-live-preview"]
            + (reset ? ["-ui-testing-editor-speaker-background-reset"] : [])
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20))
        return app
    }

    private func speaker(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-preview-speaker-speaker"].firstMatch
    }

    /// The video canvas (the preview's own frame is exactly the 9:16 canvas).
    private func canvas(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any)["native-editor-preview"].firstMatch
    }

    /// "0:00 to 30:00, position 76%, 80%, size 42%, selected" -> (76, 80, 42).
    private func placement(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) -> Placement? {
        let value = speaker(app).value as? String ?? ""
        let numbers = value.components(separatedBy: "position ").dropFirst().first.map { tail in
            tail.components(separatedBy: CharacterSet.decimalDigits.inverted).compactMap { Int($0) }
        } ?? []
        guard numbers.count >= 3 else { XCTFail("no placement in \(value)", file: file, line: line); return nil }
        return Placement(x: numbers[0], y: numbers[1], size: numbers[2])
    }

    private func isSelected(_ app: XCUIApplication) -> Bool { (speaker(app).value as? String ?? "").hasSuffix("selected") }

    /// Waits for the editable preview to offer the framed speaker: movable, so its value carries a position.
    private func waitForSpeaker(_ app: XCUIApplication) {
        XCTAssertTrue(speaker(app).waitForExistence(timeout: 20), "the framed speaker is a preview object")
        let ready = NSPredicate { _, _ in (self.speaker(app).value as? String ?? "").contains("position") }
        expectation(for: ready, evaluatedWith: nil)
        waitForExpectations(timeout: 30)
    }

    /// Where the speaker is drawn: their stored centre on the canvas (a person taps what they see).
    private func onSpeaker(_ app: XCUIApplication) -> XCUICoordinate {
        let place = placement(app) ?? Placement(x: 50, y: 50, size: 0)
        return canvas(app).coordinate(withNormalizedOffset: CGVector(dx: Double(place.x) / 100, dy: Double(place.y) / 100))
    }

    private func select(_ app: XCUIApplication) {
        onSpeaker(app).tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-you-panel"].firstMatch.waitForExistence(timeout: 5),
                      "tapping the speaker opens their panel")
        XCTAssertTrue(isSelected(app))
    }

    /// Drags from the speaker's centre by `offset` points.
    private func drag(_ app: XCUIApplication, by offset: CGVector) {
        let start = onSpeaker(app)
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(offset))
    }

    private func saveAndWait(_ app: XCUIApplication) {
        // Save sits in the editor header, which an open tool panel replaces: close the speaker's panel first.
        let done = app.buttons["native-editor-you-done"]
        if done.exists { done.tap() }
        XCTAssertFalse(isSelected(app), "Done deselects the speaker")
        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertTrue(save.isEnabled, "there is something to save")
        save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save)
        waitForExpectations(timeout: 10)
    }

    private func preview(_ app: XCUIApplication) -> String {
        app.descendants(matching: .any)["native-editor-preview"].firstMatch.value as? String ?? ""
    }

    /// SF1, SF3, SF4: a tap selects the speaker; a drag moves them and the safe-zone guide shows while it does; a pinch
    /// resizes them; what is saved is what the reopened editor shows.
    func testTapDragAndPinchMoveAndResizeTheSpeaker() {
        let app = launch(reset: true)
        waitForSpeaker(app)
        XCTAssertEqual(placement(app), Placement(x: 76, y: 80, size: 42), "the seeded corner")
        select(app)

        drag(app, by: CGVector(dx: -120, dy: -200))
        let moved = placement(app)
        XCTAssertNotNil(moved)
        if let moved {
            XCTAssertLessThan(moved.x, 70, "the speaker moved left: \(moved)")
            XCTAssertLessThan(moved.y, 70, "and up: \(moved)")
            XCTAssertEqual(moved.size, 42, "a move keeps the size")
        }
        XCTAssertTrue(preview(app).contains("dangerGuideSamples:") && !preview(app).contains("dangerGuideSamples:0;"),
                      "the safe-zone guide showed during the drag: \(preview(app))")

        canvas(app).pinch(withScale: 1.6, velocity: 1)
        let resized = placement(app)
        XCTAssertNotNil(resized)
        if let resized, let moved {
            XCTAssertGreaterThan(resized.size, moved.size + 5, "the pinch made the speaker bigger: \(resized)")
            XCTAssertEqual(resized.x, moved.x, accuracy: 1, "a pinch keeps the centre")
            XCTAssertEqual(resized.y, moved.y, accuracy: 1)
            let size = app.sliders["native-editor-speaker-size"]
            XCTAssertTrue(size.exists, "the panel's size slider")
            XCTAssertTrue((size.value as? String ?? "").hasPrefix("\(resized.size) percent"), "the slider follows the pinch: \(String(describing: size.value))")
        }

        saveAndWait(app)
        app.terminate()

        let reopened = launch(reset: false)
        waitForSpeaker(reopened)
        XCTAssertEqual(placement(reopened), resized, "the saved place and size come back")
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled, "nothing unsaved after reopening")
    }

    /// SF6: a whole drag is one undo step.
    func testOneDragIsOneUndoStep() {
        let app = launch(reset: true)
        waitForSpeaker(app)
        select(app)
        drag(app, by: CGVector(dx: -100, dy: -160))
        XCTAssertNotEqual(placement(app), Placement(x: 76, y: 80, size: 42), "the drag moved the speaker")
        // Undo lives in the timeline bar, which the speaker's panel covers.
        app.buttons["native-editor-you-done"].tap()
        let undo = app.buttons["native-editor-undo"]
        XCTAssertTrue(undo.waitForExistence(timeout: 5))
        undo.tap()
        XCTAssertEqual(placement(app), Placement(x: 76, y: 80, size: 42), "one undo puts the speaker back where the drag began")
    }

    /// SF3: past every limit the frame stays inside the wire's range (size 20%...100%, centre on the canvas), and that
    /// is what is saved.
    func testDragAndPinchPastTheLimitsSaveAFrameInRange() {
        let app = launch(reset: true)
        waitForSpeaker(app)
        select(app)
        // Two ordinary pinches each way overshoot both limits (42% x 3 x 3; 100% x 0.3 x 0.3).
        canvas(app).pinch(withScale: 3, velocity: 2)
        canvas(app).pinch(withScale: 3, velocity: 2)
        XCTAssertEqual(placement(app)?.size, 100, "the largest speaker is the full width")
        canvas(app).pinch(withScale: 0.3, velocity: -2)
        canvas(app).pinch(withScale: 0.3, velocity: -2)
        XCTAssertEqual(placement(app)?.size, 20, "the smallest is a fifth of the width")
        // Far past the canvas's bottom-left corner (the screen's bottom-left), keeping the touch on screen.
        let start = onSpeaker(app)
        start.press(forDuration: 0.15, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.01, dy: 0.98)))
        let corner = placement(app)
        XCTAssertEqual(corner?.x, 0, "the centre stops at the left edge")
        XCTAssertEqual(corner?.y, 100, "and at the bottom edge")
        saveAndWait(app)
        app.terminate()
        let reopened = launch(reset: false)
        waitForSpeaker(reopened)
        XCTAssertEqual(placement(reopened), corner, "the clamped frame is what was saved")
    }

    /// SF2: Full screen takes the speaker out of the preview objects (it would swallow tap-to-expand) and closes the
    /// panel; a corner button brings them back.
    func testFullFrameSpeakerIsNotSelectable() {
        let app = launch(reset: true)
        waitForSpeaker(app)
        select(app)
        app.buttons["native-editor-speaker-spot-full"].tap()
        XCTAssertFalse(speaker(app).waitForExistence(timeout: 2), "a full-frame speaker is not a preview object")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-you-panel"].firstMatch.exists, "the panel closed")
        app.buttons["native-editor-undo"].tap()
        waitForSpeaker(app)
        XCTAssertEqual(placement(app), Placement(x: 76, y: 80, size: 42), "undo brings the corner speaker back")
    }
}
