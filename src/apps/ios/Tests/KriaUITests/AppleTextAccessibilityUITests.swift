import XCTest

@MainActor
final class AppleTextAccessibilityUITests: XCTestCase {
    func testTextInspectorKeepsEditingAndStyleControlsReachableAtAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        app.launchEnvironment["UI_TEST_EDITOR_WIDTH"] = "320"
        app.launch()

        let timelineText = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(timelineText.waitForExistence(timeout: 8))
        timelineText.tap()
        timelineText.tap()

        let panel = app.descendants(matching: .any)["native-editor-text-panel"].firstMatch
        XCTAssertTrue(panel.waitForExistence(timeout: 3))
        let window = app.windows.firstMatch.frame
        let viewport = CGRect(x: (window.width - 320) / 2, y: window.minY, width: 320, height: window.height)
        XCTAssertGreaterThanOrEqual(panel.frame.minX, viewport.minX)
        XCTAssertLessThanOrEqual(panel.frame.maxX, viewport.maxX)

        let font = app.buttons["native-editor-text-font"]
        let alignRight = app.buttons["Align text right"]
        let size = app.textFields["native-editor-text-size"]
        let inspector = app.scrollViews["native-editor-text-inspector-scroll"]
        attachScreenshot(app, name: "Large text inspector initial")
        for control in [font, alignRight, size] {
            reveal(control, in: inspector)
            if !control.isHittable { attachScreenshot(app, name: "Unreachable " + control.identifier) }
            XCTAssertTrue(control.waitForExistence(timeout: 3))
            XCTAssertTrue(control.isHittable, "\(control.identifier) should remain reachable at accessibility text size")
            XCTAssertGreaterThanOrEqual(control.frame.minX, viewport.minX)
            XCTAssertLessThanOrEqual(control.frame.maxX, viewport.maxX)
        }
        reveal(alignRight, in: inspector)
        alignRight.tap()

        app.buttons["Edit text"].tap()
        let content = app.descendants(matching: .any)["native-editor-text-content"].firstMatch
        XCTAssertTrue(content.waitForExistence(timeout: 3))
        content.tap()
        content.typeText(" accessible")
        XCTAssertTrue((content.value as? String ?? "").contains("accessible"))

        app.buttons["Style"].tap()
        let color = app.buttons["Text color #E7DDF5"]
        reveal(color, in: inspector)
        attachScreenshot(app, name: "Large text inspector color")
        XCTAssertTrue(color.waitForExistence(timeout: 3))
        XCTAssertTrue(color.isHittable)
        XCTAssertGreaterThanOrEqual(color.frame.minX, viewport.minX)
        XCTAssertLessThanOrEqual(color.frame.maxX, viewport.maxX)
    }

    private func reveal(_ control: XCUIElement, in inspector: XCUIElement) {
        for _ in 0..<24 where !control.isHittable {
            let moveUp = !control.exists || control.frame.midY > inspector.frame.midY
            let start = inspector.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.7 : 0.4))
            let end = inspector.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.4 : 0.7))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: 20, thenHoldForDuration: 0.3)
        }
    }

    private func attachScreenshot(_ app: XCUIApplication, name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
