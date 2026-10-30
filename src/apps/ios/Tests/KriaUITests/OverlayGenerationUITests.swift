import XCTest

@MainActor final class OverlayGenerationUITests: XCTestCase {
    func testVisualsPlansAndAddsAnEditableNumberOverlay() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-overlays"]
        app.launch()
        let visuals = app.buttons["native-editor-tool-visuals"]
        XCTAssertTrue(visuals.waitForExistence(timeout: 20))
        visuals.tap()
        let generate = app.buttons["native-editor-generate-media"]
        XCTAssertTrue(generate.waitForExistence(timeout: 5))
        XCTAssertTrue(generate.isEnabled)
        generate.tap()
        XCTAssertTrue(app.navigationBars["Generate media"].waitForExistence(timeout: 5))
        let input = app.textFields["overlay-generation-prompt"].exists
            ? app.textFields["overlay-generation-prompt"] : app.textViews["overlay-generation-prompt"]
        input.tap()
        input.typeText("Show 10 when I say number ten")
        app.buttons["overlay-generation-plan"].tap()
        let use = app.buttons["overlay-generation-use-DCC14763-5702-4AE3-8510-8C72845E6B15"]
        for _ in 0..<5 where !use.isHittable { app.scrollViews.firstMatch.swipeUp() }
        XCTAssertTrue(use.waitForExistence(timeout: 10))
        XCTAssertTrue(use.isEnabled)
        let review = XCTAttachment(screenshot: app.screenshot())
        review.name = "overlay-plan-review-fixture"
        review.lifetime = .keepAlways
        add(review)
        use.tap()
        XCTAssertTrue(app.staticTexts["Added to your edit"].waitForExistence(timeout: 10))
        let applied = XCTAttachment(screenshot: app.screenshot())
        applied.name = "overlay-added-fixture"
        applied.lifetime = .keepAlways
        add(applied)
    }
}
