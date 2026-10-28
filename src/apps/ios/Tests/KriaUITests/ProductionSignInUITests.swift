import XCTest

@MainActor final class ProductionSignInUITests: XCTestCase {
    func testBuildControlsDevelopmentSignIn() {
        let app = XCUIApplication()
        #if DEV_AUTH
        app.launchArguments = ["-ui-testing-brand"]
        app.launchEnvironment["KRIA_BRAND_STATE"] = "signin"
        #endif
        app.launch()
        XCTAssertTrue(app.buttons["signin.google"].waitForExistence(timeout: 10))
        #if DEV_AUTH
        XCTAssertTrue(app.buttons["signin.email"].exists)
        XCTAssertTrue(app.buttons["signin.local"].exists)
        #else
        XCTAssertFalse(app.buttons["signin.email"].exists)
        XCTAssertFalse(app.buttons["signin.local"].exists)
        #endif
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Production sign-in"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
