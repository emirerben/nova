import XCTest

final class KriaUITests: XCTestCase {
    func testLaunchShowsKriaEntryPoint() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing"]
        app.launch()
        XCTAssertTrue(app.staticTexts["Make something\nworth sharing."].waitForExistence(timeout: 3) || app.staticTexts["Projects"].waitForExistence(timeout: 3))
    }
}
