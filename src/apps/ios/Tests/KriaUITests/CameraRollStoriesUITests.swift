import XCTest

final class CameraRollStoriesUITests: XCTestCase {
    @MainActor func testReviewReplaceAndRejectWithoutRendering() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-story-review"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Boat journey"].waitForExistence(timeout: 10))
        app.buttons["story-replace-0"].tap()
        app.buttons["story-alternative-33333333-3333-3333-3333-333333333333"].tap()
        XCTAssertTrue(app.staticTexts["The final arrival"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["A ferry crossing"].exists, "The unrelated second choice must remain")
        for _ in 0..<3 where !app.switches["story-sound"].isHittable { app.swipeDown() }
        app.switches["story-sound"].switches.firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Muted footage"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["The final arrival"].exists)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Corrected recommendation preserves other footage and separate approval"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        for _ in 0..<4 where !app.buttons["Not this story"].exists { app.swipeUp() }
        XCTAssertTrue(app.buttons["story-choose-11111111-1111-1111-1111-111111111111"].exists)
        app.buttons["Not this story"].tap()
        XCTAssertTrue(app.staticTexts["No story to suggest"].waitForExistence(timeout: 5))
    }
}
