import XCTest

@MainActor final class AccountPrivacyUITests: XCTestCase {
    private func launch(mode: String = "normal", largeText: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-account"]
        app.launchEnvironment["KRIA_ACCOUNT_TEST_MODE"] = mode
        if largeText { app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5" }
        app.launch()
        return app
    }

    private func reveal(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<12 where !element.isHittable { app.swipeUp() }
        XCTAssertTrue(element.waitForExistence(timeout: 3))
        XCTAssertTrue(element.isHittable)
    }

    func testAIConsentRequiresExplicitChoiceAndDeclineReturnsToSignInAtLargeText() {
        let app = launch(largeText: true)
        XCTAssertTrue(app.staticTexts["Choose how you create."].waitForExistence(timeout: 5))
        let proceed = app.buttons["ai-consent-continue"]
        reveal(proceed, app: app)
        XCTAssertFalse(proceed.isEnabled)
        let decline = app.buttons["ai-consent-decline"]
        reveal(decline, app: app)
        decline.tap()
        XCTAssertTrue(app.buttons["Continue with Google"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["ai-consent-continue"].exists)
    }

    func testAccountDeletionIsReachableBeforeAIConsentAndShowsServiceFailure() {
        let app = launch(mode: "unavailable")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        let delete = app.buttons["account-delete"]
        reveal(delete, app: app)
        delete.tap()
        app.buttons["account-deletion-request"].tap()
        XCTAssertTrue(app.staticTexts["account-deletion-error"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.textFields["account-deletion-code"].exists)
        app.buttons["Cancel"].tap()
        XCTAssertTrue(app.buttons["account-delete"].waitForExistence(timeout: 3))
    }

    func testAcceptingAIConsentEntersWorkspace() {
        let app = launch()
        let toggle = app.switches["ai-consent-toggle"]
        reveal(toggle, app: app)
        toggle.tap()
        let proceed = app.buttons["ai-consent-continue"]
        reveal(proceed, app: app)
        // reveal() only waits for hittable, and a disabled button is still
        // hittable, so on a slow runner the switch's state may not have
        // committed yet. Wait for enabled before tapping.
        expectation(for: NSPredicate(format: "isEnabled == true"), evaluatedWith: proceed)
        waitForExpectations(timeout: 5)
        XCTAssertTrue(proceed.isEnabled)
        proceed.tap()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 5))
        XCTAssertFalse(proceed.exists)
    }

    func testConfirmedAccountDeletionReturnsToSignIn() {
        let app = launch()
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        let delete = app.buttons["account-delete"]
        reveal(delete, app: app)
        delete.tap()
        app.buttons["account-deletion-request"].tap()
        let code = app.descendants(matching: .any)["account-deletion-code"].firstMatch
        XCTAssertTrue(code.waitForExistence(timeout: 5))
        code.tap()
        code.typeText("fixture-confirmation")
        let confirm = app.buttons["account-deletion-confirm"]
        reveal(confirm, app: app)
        confirm.tap()
        app.alerts["Permanently delete your account?"].buttons["Delete account"].tap()
        XCTAssertTrue(app.buttons["Continue with Google"].waitForExistence(timeout: 5))
        XCTAssertFalse(confirm.exists)
    }

    func testDeletionRequiresConfirmationAndFailedCodeKeepsAccountOpen() {
        let app = launch(mode: "invalid-code")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        let delete = app.buttons["account-delete"]
        reveal(delete, app: app)
        delete.tap()
        app.buttons["account-deletion-request"].tap()
        let code = app.descendants(matching: .any)["account-deletion-code"].firstMatch
        XCTAssertTrue(code.waitForExistence(timeout: 5))
        code.tap()
        code.typeText("invalid-confirmation")
        let confirm = app.buttons["account-deletion-confirm"]
        reveal(confirm, app: app)
        confirm.tap()
        let alert = app.alerts["Permanently delete your account?"]
        XCTAssertTrue(alert.waitForExistence(timeout: 3))
        alert.buttons["Cancel"].tap()
        XCTAssertFalse(app.staticTexts["account-deletion-error"].exists)
        confirm.tap()
        alert.buttons["Delete account"].tap()
        XCTAssertTrue(app.staticTexts["account-deletion-error"].waitForExistence(timeout: 5))
        XCTAssertTrue(code.exists)
    }
}
