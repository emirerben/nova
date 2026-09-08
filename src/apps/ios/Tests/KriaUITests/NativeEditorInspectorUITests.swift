import XCTest

@MainActor
final class NativeEditorInspectorUITests: XCTestCase {
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
}
