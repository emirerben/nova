import XCTest

/// KRI-717: Range picks which part of the original clip a slot plays. Drives the real sheet: open it from the
/// context strip, slide the window, drag each handle, and read the result back from the label.
@MainActor
final class NativeSourceWindowUITests: XCTestCase {
    func testRangeSheetSlidesAndResizesTheWindow() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()

        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        let range = app.buttons["native-editor-clip-range"]
        XCTAssertTrue(range.waitForExistence(timeout: 5))
        XCTAssertTrue(range.isHittable, "Range must be reachable in the context strip")
        range.tap()

        let label = app.staticTexts["kria-source-window-label"]
        XCTAssertTrue(label.waitForExistence(timeout: 10), "the Range sheet shows the window label")
        let body = app.descendants(matching: .any)["kria-source-window-body"].firstMatch
        XCTAssertTrue(body.waitForExistence(timeout: 5))
        let before = label.label

        // Slide the whole window right: the label (source range, then length) must change its range, not its length.
        body.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.05, thenDragTo: body.coordinate(withNormalizedOffset: CGVector(dx: 1.4, dy: 0.5)))
        let slid = label.label
        XCTAssertNotEqual(slid, before, "sliding the window moves the in-point")
        XCTAssertEqual(slid.components(separatedBy: " · ").last, before.components(separatedBy: " · ").last, "a slide keeps the length")

        // Drag the trailing handle left: the length shrinks.
        let trailing = app.descendants(matching: .any)["kria-source-window-trailing"].firstMatch
        XCTAssertTrue(trailing.exists)
        trailing.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.05, thenDragTo: trailing.coordinate(withNormalizedOffset: CGVector(dx: -1.5, dy: 0.5)))
        XCTAssertNotEqual(label.label.components(separatedBy: " · ").last, slid.components(separatedBy: " · ").last, "a handle changes the length")

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "KRI-717 Range sheet after slide and resize"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
