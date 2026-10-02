import XCTest

@MainActor final class NativeUpdateRequiredUITests: XCTestCase {
    private func launch(routeArguments: [String] = [], accessibilitySize: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = routeArguments + ["-ui-testing-native-update"]
        if accessibilitySize {
            app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        }
        app.launch()
        XCTAssertTrue(app.staticTexts["Update Kria to continue"].waitForExistence(timeout: 5), app.debugDescription)
        return app
    }

    private func reveal(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<12 where !element.isHittable { app.swipeUp() }
        XCTAssertTrue(element.waitForExistence(timeout: 3))
        XCTAssertTrue(element.isHittable, "Expected \(element.identifier) to remain reachable: \(app.debugDescription)")
    }

    func testTyped426ReplacesEachRootRoute() {
        let routes: [([String], String)] = [
            ([], "signin.google"),
            (["-ui-testing-brand"], "format-carousel"),
            (["-ui-testing-chat"], "workspace-menu-toggle"),
            (["-ui-testing-editor"], "native-editor-fixture-sourceText"),
            (["-ui-testing-chat-bubbles"], "chat-bubbles-pasted"),
            (["-device-effects"], "device-effects-status"),
            (["-ui-testing-account"], "ai-consent-continue"),
        ]

        for (arguments, routeIdentifier) in routes {
            let app = launch(routeArguments: arguments)
            XCTAssertTrue(app.staticTexts["This version of Kria is no longer supported. Update the app from the App Store, then reopen it."].exists)
            XCTAssertFalse(app.descendants(matching: .any)[routeIdentifier].exists,
                           "The update requirement must replace the \(routeIdentifier) route")
            app.terminate()
        }
    }

    func testBlockingScreenScrollsAtAccessibilitySizeAndActionsUseFixtureSeam() {
        let app = launch(accessibilitySize: true)
        let appStore = app.descendants(matching: .any)["kria-update-app-store-link"].firstMatch
        let support = app.descendants(matching: .any)["kria-update-support-link"].firstMatch

        reveal(appStore, app: app)
        appStore.tap()
        XCTAssertEqual(app.staticTexts["kria-update-fixture-action"].label, "Opened app-store")

        reveal(support, app: app)
        support.tap()
        XCTAssertEqual(app.staticTexts["kria-update-fixture-action"].label, "Opened support")
    }
}
