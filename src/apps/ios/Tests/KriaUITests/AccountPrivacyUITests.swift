import XCTest

@MainActor final class AccountPrivacyUITests: XCTestCase {
    private func launch(mode: String = "normal", largeText: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-account"]
        app.launchEnvironment["KRIA_ACCOUNT_INPUT_TRACE"] = "1"
        app.launchEnvironment["KRIA_ACCOUNT_TEST_MODE"] = mode
        for key in ["KRIA_ACCOUNT_DISABLE_RESPONSE_DELAY_MS", "KRIA_ACCOUNT_WRONG_POSITION_CONTROL"] {
            if let value = ProcessInfo.processInfo.environment[key] { app.launchEnvironment[key] = value }
        }
        if largeText { app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5" }
        app.launch()
        return app
    }

    private func reveal(_ element: XCUIElement, app: XCUIApplication, requiresEnabled: Bool = false) {
        for _ in 0..<12 where !element.isHittable { app.swipeUp() }
        if requiresEnabled {
            waitUntilReady(element)
        } else {
            XCTAssertTrue(element.waitForExistence(timeout: 3))
            XCTAssertTrue(element.isHittable)
        }
    }

    private var guardedInput: Bool {
        ProcessInfo.processInfo.environment["KRIA_ACCOUNT_PROBE_GUARD"] == "1"
    }

    private func observedTap(_ element: XCUIElement, key: String, app: XCUIApplication) {
        let exists = element.exists
        var record: [String: Any] = ["action": key, "stage": "before", "time": Date().timeIntervalSince1970,
                                     "guarded": guardedInput, "exists": exists]
        if exists {
            record["enabled"] = element.isEnabled
            record["hittable"] = element.isHittable
            let frame = element.frame
            record["frame"] = ["x": Double(frame.origin.x), "y": Double(frame.origin.y),
                               "width": Double(frame.width), "height": Double(frame.height)]
            record["label"] = element.label
            record["identifier"] = element.identifier
        }
        if let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]),
           let line = String(data: data, encoding: .utf8) { print("[ACCOUNT-ACTION] " + line) }
        element.tap() // The original action is performed exactly once, without a retry.
        print("[ACCOUNT-ACTION-END] \(key) time=\(Date().timeIntervalSince1970)")
        if key == "title-select" || key == "reenter-preferences" {
            let role = app.buttons["text-preference-role"]
            let result: [String: Any] = ["action": key, "stage": "after", "time": Date().timeIntervalSince1970,
                                         "roleExists": role.exists, "roleLabel": role.exists ? role.label : "missing",
                                         "menuTitleExists": app.buttons["Title"].exists]
            if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
               let line = String(data: data, encoding: .utf8) { print("[ACCOUNT-ACTION] " + line) }
            print("[ACCOUNT-ACTION-HIERARCHY] \(key)\n" + String(app.debugDescription.prefix(40000)))
        }
    }

    private func waitUntilReady(_ element: XCUIElement) {
        let ready = NSPredicate(format: "exists == true AND isHittable == true AND isEnabled == true")
        let expectation = XCTNSPredicateExpectation(predicate: ready, object: element)
        XCTAssertEqual(XCTWaiter.wait(for: [expectation], timeout: 3), .completed)
    }

    func testTextPreferencesSaveOffEffectsAndReopen() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        app.buttons["account-text-preferences"].tap()
        XCTAssertTrue(app.staticTexts["Text preferences"].waitForExistence(timeout: 5))
        let font = app.buttons["text-preference-font"]
        reveal(font, app: app)
        font.tap()
        app.buttons["Automatic"].tap()
        let outline = app.buttons["text-preference-outline"]
        reveal(outline, app: app)
        outline.tap()
        app.buttons["Off"].tap()
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
        reveal(app.buttons["text-preference-font"], app: app)
        XCTAssertTrue(app.buttons["text-preference-font"].label.contains("Automatic"))
        reveal(app.buttons["text-preference-outline"], app: app)
        XCTAssertTrue(app.buttons["text-preference-outline"].label.contains("Off"))
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

    func testFinishedEditLearningConsentAcceptAndDisableKeepsApprovedPreference() {
        let app = launch(mode: "text-preferences-learning")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app, requiresEnabled: guardedInput); manage.tap()
        let preferences = app.buttons["account-text-preferences"]
        if guardedInput { reveal(preferences, app: app, requiresEnabled: true) }
        observedTap(preferences, key: "initial-preferences", app: app)
        let learn = app.switches["text-learning-enabled"]
        reveal(learn, app: app, requiresEnabled: guardedInput)
        XCTAssertEqual(learn.value as? String, "0")
        learn.switches.firstMatch.tap()
        let count = app.staticTexts["Based on 3 finished videos"]
        reveal(count, app: app)
        let accept = app.buttons["text-learning-accept-learning-position"]
        reveal(accept, app: app, requiresEnabled: guardedInput); accept.tap()
        XCTAssertFalse(app.buttons["text-learning-accept-learning-position"].exists)
        observedTap(app.navigationBars.buttons.firstMatch, key: "back-to-account", app: app)
        let reopenedPreferences = app.buttons["account-text-preferences"]
        if guardedInput { reveal(reopenedPreferences, app: app, requiresEnabled: true) }
        observedTap(reopenedPreferences, key: "reenter-preferences", app: app)
        reveal(learn, app: app, requiresEnabled: guardedInput)
        XCTAssertEqual(learn.value as? String, "1")
        observedTap(learn.switches.firstMatch, key: "disable-learning", app: app)
        if guardedInput { reveal(learn, app: app, requiresEnabled: true) }
        XCTAssertEqual(learn.value as? String, "0")
        XCTAssertFalse(app.staticTexts["Based on 3 finished videos"].exists)
        let role = app.buttons["text-preference-role"]
        reveal(role, app: app, requiresEnabled: guardedInput)
        observedTap(role, key: "open-role-menu", app: app)
        let title = app.buttons["Title"]
        if guardedInput { waitUntilReady(title) }
        observedTap(title, key: "title-select", app: app)
        let placement = app.buttons["text-preference-placement"]
        reveal(placement, app: app)
        XCTAssertTrue(placement.label.contains("Top Left"))
        let shadow = app.buttons["text-preference-shadow"]
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.label.contains("Off"))
    }

    func testFinishedEditLearningDismissAndResetHistory() {
        let app = launch(mode: "text-preferences-learning")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let learn = app.switches["text-learning-enabled"]
        reveal(learn, app: app); learn.switches.firstMatch.tap()
        let dismiss = app.buttons["text-learning-dismiss-learning-position"]
        reveal(dismiss, app: app); dismiss.tap()
        XCTAssertFalse(dismiss.exists)
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        XCTAssertFalse(dismiss.exists)
        let reset = app.buttons["text-learning-reset"]
        reveal(reset, app: app); reset.tap()
        XCTAssertTrue(app.staticTexts["Learning history reset."].waitForExistence(timeout: 5))
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
