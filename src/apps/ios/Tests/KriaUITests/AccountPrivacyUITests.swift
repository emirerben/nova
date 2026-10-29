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
        for _ in 0..<12 {
            let top = app.navigationBars.firstMatch.exists ? app.navigationBars.firstMatch.frame.maxY : app.frame.minY + 60
            // A child picker has no Save button. Do not query the disappearing parent's
            // frame during navigation; the target's own hit-testing handles fixed actions.
            let bottom = app.frame.maxY - 50
            if element.exists {
                let fixedAction = ["text-preferences-save", "text-preferences-reload"].contains(element.identifier)
                if element.isHittable && (fixedAction || (element.frame.minY >= top && element.frame.maxY <= bottom)) { break }
                if element.frame.minY < top { scrollForm(app, down: true); continue }
            }
            scrollForm(app, down: false)
        }
        XCTAssertTrue(element.waitForExistence(timeout: 3))
        XCTAssertTrue(element.isHittable, "Control must be visible: \(element.identifier)\n\(app.debugDescription)")
    }

    private func scrollForm(_ app: XCUIApplication, down: Bool) {
        // Whole-screen flings can jump a row from below the fixed Save bar to
        // above the navigation bar, then back again. Move within the form in
        // small steps so reveal converges instead of oscillating on CI devices.
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: down ? 0.4 : 0.65))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: down ? 0.65 : 0.4))
        start.press(forDuration: 0.05, thenDragTo: end)
    }

    func testExcludedFontSurvivesPickerReturnSaveAndReopen() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let excluded = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Excluded fonts")).firstMatch
        reveal(excluded, app: app); excluded.tap()
        XCTAssertTrue(app.buttons["text-preferences-save"].waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.navigationBars["Never use"].waitForExistence(timeout: 5))
        let search = app.searchFields.firstMatch
        XCTAssertTrue(search.waitForExistence(timeout: 5))
        search.tap(); search.typeText("Fraunces")
        let fontChoice = app.buttons["text-preference-font-Fraunces"]
        XCTAssertTrue(fontChoice.waitForExistence(timeout: 5))
        fontChoice.tap()
        let chosen = XCTNSPredicateExpectation(predicate: NSPredicate(format: "selected == true"), object: fontChoice)
        XCTAssertEqual(XCTWaiter.wait(for: [chosen], timeout: 5), .completed)
        let cancelSearch = app.buttons.matching(NSPredicate(format: "label IN %@", ["Cancel", "close"])).firstMatch
        XCTAssertTrue(cancelSearch.waitForExistence(timeout: 5))
        cancelSearch.tap()
        app.navigationBars.buttons.firstMatch.tap()
        let retained = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label CONTAINS %@", "Fraunces"), object: excluded)
        XCTAssertEqual(XCTWaiter.wait(for: [retained], timeout: 5), .completed, "Returning from the picker must keep the excluded font")
        let selected = XCTAttachment(screenshot: app.screenshot())
        selected.name = "Excluded Fraunces before saving"; selected.lifetime = .keepAlways; add(selected)
        let save = app.buttons["text-preferences-save"]
        reveal(save, app: app); save.tap()
        XCTAssertTrue(app.staticTexts["Saved for future videos."].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        reveal(excluded, app: app); excluded.tap()
        XCTAssertTrue(app.buttons["text-preferences-save"].waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.navigationBars["Never use"].waitForExistence(timeout: 5))
        let reopenedSearch = app.searchFields.firstMatch
        XCTAssertTrue(reopenedSearch.waitForExistence(timeout: 5))
        reopenedSearch.tap(); reopenedSearch.typeText("Fraunces")
        let fraunces = app.buttons["text-preference-font-Fraunces"]
        XCTAssertTrue(fraunces.waitForExistence(timeout: 5))
        XCTAssertTrue(fraunces.isSelected, "Saved exclusion must survive reopening settings")
        let persisted = XCTAttachment(screenshot: app.screenshot())
        persisted.name = "Persisted Fraunces exclusion"; persisted.lifetime = .keepAlways; add(persisted)
    }

    func testTextPreferenceDraftSurvivesRoleSwitch() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let shadow = app.buttons["text-preference-shadow"]
        reveal(shadow, app: app); shadow.tap(); app.buttons["Off"].tap()
        let role = app.buttons["text-preference-role"]
        for _ in 0..<5 where !role.isHittable { app.swipeDown() }
        reveal(role, app: app); role.tap(); app.buttons["Title"].tap()
        role.tap(); app.buttons["All Text"].tap()
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.label.contains("Off"), "Changing scope must retain the unsaved draft")
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
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let learn = app.switches["text-learning-enabled"]
        reveal(learn, app: app)
        XCTAssertEqual(learn.value as? String, "0")
        learn.switches.firstMatch.tap()
        let count = app.staticTexts["Based on 3 finished videos"]
        reveal(count, app: app)
        let accept = app.buttons["text-learning-accept-learning-position"]
        reveal(accept, app: app); accept.tap()
        XCTAssertTrue(app.buttons["text-learning-accept-learning-position"].waitForNonExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        reveal(learn, app: app)
        XCTAssertEqual(learn.value as? String, "1")
        learn.switches.firstMatch.tap()
        XCTAssertEqual(learn.value as? String, "0")
        XCTAssertFalse(app.staticTexts["Based on 3 finished videos"].exists)
        let role = app.buttons["text-preference-role"]
        for _ in 0..<6 where !role.isHittable { app.swipeDown() }
        reveal(role, app: app); role.tap(); app.buttons["Title"].tap()
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
        XCTAssertTrue(dismiss.waitForNonExistence(timeout: 5), "Dismiss must finish its asynchronous save and reload")
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        XCTAssertTrue(app.buttons["text-preference-role"].waitForExistence(timeout: 5),
                      "Loaded preferences must appear before checking persistence")
        XCTAssertFalse(dismiss.exists)
        let reset = app.buttons["text-learning-reset"]
        reveal(reset, app: app)
        let saveBarButton = app.buttons["text-preferences-save"]
        XCTAssertLessThanOrEqual(reset.frame.maxY, saveBarButton.frame.minY - 8,
                                 "Reset must be above the fixed Save bar before tapping")
        reset.tap()
        // The confirmation is inserted below Reset and may be virtualized offscreen.
        let resetConfirmation = app.staticTexts["Learning history reset."]
        reveal(resetConfirmation, app: app)
        XCTAssertTrue(resetConfirmation.exists)
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
