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

    func testTextPreferencesSaveOffEffectsAndReopen() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        app.buttons["account-text-preferences"].tap()
        XCTAssertTrue(app.staticTexts["Text preferences"].waitForExistence(timeout: 5))
        let shadow = app.buttons["text-preference-shadow"]
        reveal(shadow, app: app)
        shadow.tap()
        app.buttons["Off"].tap()
        let save = app.buttons["text-preferences-save"]
        reveal(save, app: app)
        save.tap()
        XCTAssertTrue(app.staticTexts["Saved for future videos."].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        reveal(app.buttons["text-preference-shadow"], app: app)
        XCTAssertTrue(app.buttons["text-preference-shadow"].label.contains("Off"))
    }

    func testTextPreferencesStaleWriteKeepsEditsAndOffersReload() {
        let app = launch(mode: "text-preferences-stale")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        app.buttons["account-text-preferences"].tap()
        let shadow = app.buttons["text-preference-shadow"]
        reveal(shadow, app: app)
        shadow.tap()
        app.buttons["Off"].tap()
        let save = app.buttons["text-preferences-save"]
        reveal(save, app: app)
        save.tap()
        reveal(app.buttons["text-preferences-reload"], app: app)
        XCTAssertFalse(app.staticTexts["Saved for future videos."].exists)
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
