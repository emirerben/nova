import XCTest

/// Tap the corner speaker of a Talk-to-camera green-screen edit on the preview, then drag to move and pinch to resize
/// them. Rows SF1, SF2, SF3, SF6 in `docs/reviews/speaker-frame-direct-manipulation-failure-modes.md`. The fixture
/// opens `speaker_background_windows` and starts from a plain-colour fill with the speaker at the bottom right
/// (`small_bottom_right`: width 42%, centre 76% / 80%). Save goes through the stub commit transport, which keeps the
/// saved lane across relaunches.
@MainActor
final class SpeakerFrameDirectManipulationUITests: XCTestCase {
    func testScopedSpeakerSaveReopenAndFullResetThroughClipInspector() {
        func launchSplit(reset: Bool) -> XCUIApplication {
            let app = XCUIApplication()
            app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-split-speaker",
                "-ui-testing-editor-speaker-background", "-ui-testing-editor-speaker-background-windows",
                "-ui-testing-editor-speaker-frame", "-ui-testing-editor-live-preview"]
            if reset { app.launchArguments.append("-ui-testing-editor-speaker-background-reset") }
            app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
            app.launch()
            return app
        }
        func occurrence(_ app: XCUIApplication, _ panel: String) -> XCUIElement {
            app.descendants(matching: .any)["native-editor-preview-speaker-split-speaker-" + panel].firstMatch
        }
        func geometry(_ element: XCUIElement) -> String {
            (element.value as? String ?? "").replacingOccurrences(of: ", selected", with: "")
        }
        let app = launchSplit(reset: true)
        let top = occurrence(app, "top"), bottom = occurrence(app, "bottom")
        XCTAssertTrue(bottom.waitForExistence(timeout: 30))
        let peer = geometry(top)
        let original = geometry(bottom)
        bottom.tap()
        app.buttons["native-editor-speaker-spot-small_top_left"].tap()
        app.sliders["native-editor-speaker-size"].adjust(toNormalizedSliderPosition: 0.6)
        let custom = geometry(bottom)
        XCTAssertNotEqual(custom, original)
        XCTAssertEqual(geometry(top), peer)
        saveAndWait(app, speakers: [top, bottom])
        app.terminate()

        let reopened = launchSplit(reset: false)
        let savedTop = occurrence(reopened, "top"), savedBottom = occurrence(reopened, "bottom")
        XCTAssertTrue(savedBottom.waitForExistence(timeout: 30))
        XCTAssertEqual(geometry(savedBottom), custom, "Actual committed timeline override survives relaunch")
        XCTAssertEqual(geometry(savedTop), peer)
        XCTAssertFalse(reopened.buttons["native-editor-save"].isEnabled)
        savedBottom.tap()
        reopened.buttons["native-editor-speaker-spot-full"].tap()
        XCTAssertFalse(savedBottom.exists, "Full panel mode removes only the scoped preview object")
        saveAndWait(reopened, speakers: [savedTop])
        reopened.terminate()

        let full = launchSplit(reset: false)
        XCTAssertTrue(occurrence(full, "top").waitForExistence(timeout: 30))
        XCTAssertFalse(occurrence(full, "bottom").exists)
        XCTAssertEqual(geometry(occurrence(full, "top")), peer)
        let clip = full.descendants(matching: .any)["native-editor-clip-2"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 5))
        clip.tap()
        XCTAssertTrue(clip.isSelected)
        full.buttons["native-editor-adjust"].tap()
        let position = full.buttons["native-editor-clip-speaker-position"]
        for _ in 0..<6 where !position.isHittable { full.swipeUp() }
        XCTAssertTrue(position.isHittable)
        position.tap()
        let reset = full.buttons["native-editor-speaker-use-default"]
        XCTAssertTrue(reset.waitForExistence(timeout: 5))
        reset.tap()
        XCTAssertTrue(occurrence(full, "bottom").waitForExistence(timeout: 5))
        XCTAssertEqual(geometry(occurrence(full, "bottom")), original)
        XCTAssertEqual(geometry(occurrence(full, "top")), peer)
    }

    func testRepeatedSpeakerPanelsSelectAndDragTheChosenOccurrence() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-split-speaker",
                               "-ui-testing-editor-speaker-background", "-ui-testing-editor-speaker-background-windows",
                               "-ui-testing-editor-speaker-frame", "-ui-testing-editor-speaker-background-reset",
                               "-ui-testing-editor-live-preview"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        let top = app.descendants(matching: .any)["native-editor-preview-speaker-split-speaker-top"].firstMatch
        let bottom = app.descendants(matching: .any)["native-editor-preview-speaker-split-speaker-bottom"].firstMatch
        XCTAssertTrue(top.waitForExistence(timeout: 30))
        XCTAssertTrue(bottom.waitForExistence(timeout: 30))
        XCTAssertLessThan(top.frame.maxY, bottom.frame.minY)
        top.tap()
        XCTAssertTrue((top.value as? String)?.hasSuffix("selected") == true)
        bottom.tap()
        XCTAssertTrue((bottom.value as? String)?.hasSuffix("selected") == true)
        XCTAssertFalse((top.value as? String)?.hasSuffix("selected") == true)
        let peerValue = top.value as? String
        let peerFrame = top.frame
        let before = bottom.value as? String
        let start = bottom.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: -20, dy: -20)))
        XCTAssertNotEqual(bottom.value as? String, before)
        XCTAssertTrue((bottom.value as? String)?.hasSuffix("selected") == true)
        XCTAssertLessThan(top.frame.midY, bottom.frame.midY)
        XCTAssertEqual(top.value as? String, peerValue, "Dragging the lower occurrence must not edit its peer")
        XCTAssertEqual(top.frame, peerFrame)
        let beforeResize = bottom.value as? String
        // On the lower speaker, not the canvas centre: the panel divider band sits there and owns a touch that starts on it.
        bottom.pinch(withScale: 1.3, velocity: 1)
        XCTAssertNotEqual(bottom.value as? String, beforeResize)
        XCTAssertEqual(top.value as? String, peerValue, "Resizing the lower occurrence must not edit its peer")
        XCTAssertEqual(top.frame, peerFrame)
        app.sliders["native-editor-speaker-size"].adjust(toNormalizedSliderPosition: 0.5)
        XCTAssertEqual(top.value as? String, peerValue)
        app.buttons["native-editor-speaker-spot-small_top_left"].tap()
        XCTAssertEqual(top.value as? String, peerValue)
        app.buttons["native-editor-speaker-spot-full"].tap()
        let disappeared = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: bottom)
        XCTAssertEqual(XCTWaiter.wait(for: [disappeared], timeout: 5), .completed)
        XCTAssertFalse(bottom.exists)
        XCTAssertTrue(top.exists)
        XCTAssertEqual(top.value as? String, peerValue)
        app.buttons["native-editor-speaker-use-default"].tap()
        XCTAssertTrue(bottom.waitForExistence(timeout: 5))
        XCTAssertEqual(top.value as? String, peerValue)
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "Lower speaker reset after scoped edits with unchanged upper peer"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

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

    private func saveAndWait(_ app: XCUIApplication, speakers: [XCUIElement]? = nil) {
        // Save sits in the editor header, which an open tool panel replaces: close the speaker's panel first.
        let done = app.buttons["native-editor-you-done"]
        if done.exists { done.tap() }
        for element in speakers ?? [speaker(app)] {
            XCTAssertTrue(element.exists, "The remaining framed speaker stays visible")
            XCTAssertFalse((element.value as? String ?? "").hasSuffix("selected"), "Done deselects every visible speaker")
        }
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
