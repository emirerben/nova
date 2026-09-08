import XCTest

@MainActor
final class NativeEditorInspectorUITests: XCTestCase {
    func testEditorChromeFitsViewportAndEveryToolRemainsReachable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        let timeline = app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch
        let toolRail = app.descendants(matching: .any)["native-editor-tool-rail"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertTrue(timeline.exists)
        XCTAssertTrue(toolRail.exists)

        XCTAssertLessThanOrEqual(preview.frame.height, 280)
        XCTAssertLessThanOrEqual(timeline.frame.maxY, toolRail.frame.minY + 1)
        XCTAssertLessThanOrEqual(toolRail.frame.maxY, app.frame.maxY + 1)

        let styles = app.buttons["native-editor-tool-styles"]
        if !styles.isHittable { toolRail.swipeLeft() }
        XCTAssertTrue(styles.waitForExistence(timeout: 2))
        XCTAssertTrue(styles.isHittable)
    }

    func testTimelineTextSelectionOpensTextInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()

        let text = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        text.tap()

        XCTAssertTrue(app.textFields["native-editor-selected-text-input"].waitForExistence(timeout: 3))
        let start = app.sliders["native-editor-selected-text-start"]
        let end = app.sliders["native-editor-selected-text-end"]
        // Form lazily instantiates rows below the current viewport. Keep this
        // bounded so a layout regression still fails instead of hanging.
        for _ in 0..<6 {
            if start.exists && end.exists { break }
            app.swipeUp()
        }
        XCTAssertTrue(start.waitForExistence(timeout: 3))
        XCTAssertTrue(end.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["native-editor-inspector-done"].exists)
    }

    func testCaptionSelectionOpensCaptionInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let captions = app.otherElements["Captions density"]
        XCTAssertTrue(captions.waitForExistence(timeout: 8))
        captions.tap()

        XCTAssertTrue(app.textFields["native-editor-selected-caption-input"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Caption settings"].exists)
    }

    func testAllPersistedLanesExposeStableTimelineIdentityAndInspector() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes"]
        app.launch()

        let cases: [(timelineID: String, inspectorID: String)] = [
            ("native-editor-timeline-music-00000000-0000-4000-8000-000000000350", "native-editor-selected-music-track"),
            ("native-editor-timeline-sound_effect-sfx-1", "native-editor-selected-sfx-placement"),
            ("native-editor-timeline-media_overlay-overlay-1", "native-editor-selected-overlay-display-mode"),
            ("native-editor-timeline-carousel-carousel-1", "native-editor-selected-carousel-position"),
            ("native-editor-timeline-visual_block-visual-1", "native-editor-selected-visual-preset"),
            ("native-editor-timeline-motion_scene-motion-1", "native-editor-capability-reason"),
            ("native-editor-timeline-camera_effect-camera-1", "native-editor-selected-camera-intensity"),
        ]

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 8))
        for value in cases {
            tapTimelineElement(value.timelineID, in: app)
            let inspectorElement = app.descendants(matching: .any)[value.inspectorID].firstMatch
            for _ in 0..<4 {
                if inspectorElement.exists { break }
                app.swipeUp()
            }
            XCTAssertTrue(
                inspectorElement.waitForExistence(timeout: 3),
                "Inspector \(value.inspectorID) did not follow \(value.timelineID)"
            )
            let done = app.buttons["native-editor-inspector-done"]
            XCTAssertTrue(done.exists)
            done.tap()
        }
    }

    func testPreviewDragIsOneUndoableTextEdit() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launch()

        let text = app.descendants(matching: .any)["native-editor-preview-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(text.waitForExistence(timeout: 8))
        let originalPosition = "position 50%, 28%"
        XCTAssertTrue((text.value as? String)?.contains(originalPosition) == true)
        // SwiftUI exposes the preview object as the canvas-sized accessibility
        // element. Address its authored 50%/28% canvas position; the canvas
        // gesture router resolves that point against the real object rect.
        let start = text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.28))
        start.press(forDuration: 0.15, thenDragTo: start.withOffset(CGVector(dx: 34, dy: 24)))

        expectation(for: NSPredicate(format: "NOT value CONTAINS %@", originalPosition), evaluatedWith: text)
        waitForExpectations(timeout: 3)
        let undo = app.buttons["native-editor-undo"]
        XCTAssertTrue(undo.isEnabled)
        undo.tap()
        expectation(for: NSPredicate(format: "value CONTAINS %@", originalPosition), evaluatedWith: text)
        waitForExpectations(timeout: 3)
    }

    func testStressFixtureRemainsReachableAtAccessibilityTypeWithReduceMotion() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-stress-71"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility3"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let fixture = app.descendants(matching: .any)["native-editor-fixture-stress-71"].firstMatch
        XCTAssertTrue(fixture.waitForExistence(timeout: 8))
        XCTAssertEqual(fixture.value as? String, "Dynamic type accessibility3; reduce motion on")
        XCTAssertTrue(app.buttons["native-editor-tool-text"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-mini-strip"].firstMatch.exists)
    }

    private func tapTimelineElement(_ identifier: String, in app: XCUIApplication) {
        let element = app.descendants(matching: .any)[identifier].firstMatch
        XCTAssertTrue(element.waitForExistence(timeout: 3), "Missing timeline item \(identifier)")
        // XCUITest scrolls a descendant of SwiftUI's lane ScrollView into view
        // as part of tap(). The inspector assertion at the call site verifies
        // that the auto-scroll reached the intended item.
        element.tap()
    }
}
