import XCTest

/// Baseline diagnostic, before collection work limits are chosen. Does not upload.
final class CameraRollBenchmarkUITests: XCTestCase {
    func testMetadataBenchmarkStartsWithNoAuthorizedAssets() {
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-benchmark"]
        app.launch()
        XCTAssertTrue(app.buttons["camera-roll-benchmark-choose"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["No collection selected"].exists)
        XCTAssertTrue(app.staticTexts["No media uploads or AI calls"].exists)
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = "Metadata-only collection benchmark before explicit selection"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
