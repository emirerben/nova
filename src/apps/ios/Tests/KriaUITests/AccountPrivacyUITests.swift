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
            var top = app.navigationBars.firstMatch.exists ? app.navigationBars.firstMatch.frame.maxY : app.frame.minY + 60
            var bottom = app.frame.maxY - 50
            // A form row can be reported hittable behind the fixed Save inset.
            // Only the stable parent page owns that frame: child font pickers
            // must never query the disappearing parent's Save button.
            if app.navigationBars["Text preferences"].exists {
                // The video frame and role chips stay pinned above the settings.
                let header = app.otherElements["text-preferences-header"]
                if header.exists, header.frame.maxY.isFinite { top = max(top, header.frame.maxY) }
                let save = app.buttons["text-preferences-save"]
                if save.exists {
                    let frame = save.frame
                    if frame.minY.isFinite, frame.height > 0 {
                        bottom = min(bottom, frame.minY - 8)
                    }
                }
            }
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

    /// The role chips scroll sideways, so a chip off the edge has no hit point until the row is swiped.
    @discardableResult private func selectRole(_ role: String, app: XCUIApplication) -> XCUIElement {
        let chip = app.buttons["text-preference-role-" + role]
        XCTAssertTrue(chip.waitForExistence(timeout: 5), role)
        let row = app.otherElements["text-preference-role"].firstMatch
        for _ in 0..<4 where !(chip.frame.minX >= 0 && chip.frame.maxX <= app.frame.maxX - 4) { row.swipeLeft() }
        for _ in 0..<4 where !(chip.frame.minX >= 0 && chip.frame.maxX <= app.frame.maxX - 4) { row.swipeRight() }
        chip.tap()
        return chip
    }

    private func scrollForm(_ app: XCUIApplication, down: Bool) {
        // Whole-screen flings can jump a row from below the fixed Save bar to
        // above the navigation bar, then back again. Move within the form in
        // small steps so reveal converges instead of oscillating on CI devices.
        var top = app.frame.height * 0.4, bottom = app.frame.height * 0.65
        // On Text preferences the pinned header and Save bar cover the screen's
        // middle, so a drag must start and end between them to move the form.
        let header = app.otherElements["text-preferences-header"], save = app.buttons["text-preferences-save"]
        if app.navigationBars["Text preferences"].exists, header.exists, header.frame.maxY.isFinite {
            top = header.frame.maxY + 12
            bottom = (save.exists && save.frame.minY.isFinite ? save.frame.minY : app.frame.maxY * 0.9) - 12
            let step = (bottom - top) * 0.35, mid = (top + bottom) / 2
            top = mid - step; bottom = mid + step
        }
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: (down ? top : bottom) / app.frame.height))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: (down ? bottom : top) / app.frame.height))
        // A held, slow drag keeps a flick's momentum from carrying a row over the short window between header and Save bar.
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.3)
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
        let shadow = app.otherElements["text-preference-shadow"]
        reveal(shadow, app: app); shadow.buttons["On"].tap()
        selectRole("title", app: app)
        XCTAssertTrue(app.staticTexts["The big opening line in the first seconds."].waitForExistence(timeout: 5))
        selectRole("all_text", app: app)
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["On"].isSelected, "Changing scope must retain the unsaved draft")
    }

    func testOneSavePersistsEveryEditedTextTypeAcrossReopen() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        selectRole("title", app: app)
        let shadow = app.otherElements["text-preference-shadow"]
        reveal(shadow, app: app); shadow.buttons["On"].tap()
        selectRole("label", app: app)
        let outline = app.otherElements["text-preference-outline"]
        reveal(outline, app: app); outline.buttons["On"].tap()
        let save = app.buttons["text-preferences-save"]
        reveal(save, app: app); save.tap()
        XCTAssertTrue(app.staticTexts["Saved for future videos."].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        selectRole("title", app: app)
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["On"].isSelected, "Title shadow must survive one Save and reopening")
        selectRole("label", app: app)
        reveal(outline, app: app)
        XCTAssertTrue(outline.buttons["On"].isSelected, "Label outline must survive one Save and reopening")
    }

    func testUnsavedTextTypesAreMarkedAndLeavingAsksBeforeDiscarding() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let shadow = app.otherElements["text-preference-shadow"]
        selectRole("title", app: app)
        reveal(shadow, app: app); shadow.buttons["On"].tap()
        selectRole("label", app: app)
        let outline = app.otherElements["text-preference-outline"]
        reveal(outline, app: app); outline.buttons["On"].tap()
        XCTAssertEqual(app.buttons["text-preference-role-title"].value as? String, "unsaved changes")
        XCTAssertEqual(app.buttons["text-preference-role-label"].value as? String, "unsaved changes")
        XCTAssertNotEqual(app.buttons["text-preference-role-caption"].value as? String, "unsaved changes")
        XCTAssertTrue(app.staticTexts["Unsaved changes in 2 types"].exists)
        let marked = XCTAttachment(screenshot: app.screenshot())
        marked.name = "Two types with unsaved changes"; marked.lifetime = .keepAlways; add(marked)

        app.buttons["text-preferences-back"].tap()
        XCTAssertTrue(app.buttons["Discard changes"].waitForExistence(timeout: 5))
        let dialog = XCTAttachment(screenshot: app.screenshot())
        dialog.name = "Leave with unsaved changes"; dialog.lifetime = .keepAlways; add(dialog)
        // Anchored to the toolbar the dialog is a popover: it has no Cancel button, a tap outside keeps editing.
        let keepEditing = app.buttons["Keep editing"]
        if keepEditing.exists { keepEditing.tap() } else { app.otherElements["PopoverDismissRegion"].firstMatch.tap() }
        XCTAssertTrue(app.staticTexts["Unsaved changes in 2 types"].waitForExistence(timeout: 5), "Keep editing stays on the screen with the edits")

        app.buttons["text-preferences-back"].tap()
        app.buttons["Discard changes"].tap()
        app.buttons["account-text-preferences"].tap()
        selectRole("title", app: app)
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["Same"].isSelected, "Discarding must not save the edit")
        shadow.buttons["On"].tap()
        app.buttons["text-preferences-back"].tap()
        app.buttons["Save and leave"].tap()
        app.buttons["account-text-preferences"].tap()
        selectRole("title", app: app)
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["On"].isSelected, "Save and leave must persist the edit")
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
        XCTAssertTrue(font.label.contains("Kria picks"))
        let outline = app.otherElements["text-preference-outline"]
        reveal(outline, app: app)
        XCTAssertTrue(outline.buttons["Off"].isSelected, "Outline shows its real default before anything is saved")
        outline.buttons["On"].tap()
        outline.buttons["Off"].tap()
        let shadow = app.otherElements["text-preference-shadow"]
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["Off"].isSelected, "Shadow shows its real default before anything is saved")
        shadow.buttons["On"].tap()
        shadow.buttons["Off"].tap()
        let save = app.buttons["text-preferences-save"]
        reveal(save, app: app)
        save.tap()
        XCTAssertTrue(app.staticTexts["Saved for future videos."].waitForExistence(timeout: 5))
        app.navigationBars.buttons.firstMatch.tap()
        app.buttons["account-text-preferences"].tap()
        reveal(app.buttons["text-preference-font"], app: app)
        XCTAssertTrue(app.buttons["text-preference-font"].label.contains("Kria picks"))
        reveal(app.otherElements["text-preference-outline"], app: app)
        XCTAssertTrue(app.otherElements["text-preference-outline"].buttons["Off"].isSelected)
        reveal(app.otherElements["text-preference-shadow"], app: app)
        XCTAssertTrue(app.otherElements["text-preference-shadow"].buttons["Off"].isSelected)
    }

    func testEachTextRoleExplainsItselfAndKeepsThePreview() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let descriptions = [
            "all_text": "Every kind of text below, unless you set one differently.",
            "title": "The big opening line in the first seconds.",
            "label": "A short tag like a name, place or score.",
            "pinned": "A small note that stays in a corner for the whole video.",
            "caption": "Your spoken words, shown as you say them.",
            "lyric": "Song words, shown in time with the music.",
        ]
        XCTAssertTrue(app.otherElements["text-preferences-preview"].waitForExistence(timeout: 5))
        for role in ["title", "label", "pinned", "caption", "lyric", "all_text"] {
            let chip = selectRole(role, app: app)
            XCTAssertTrue(app.staticTexts[descriptions[role]!].waitForExistence(timeout: 5), "\(role) description")
            XCTAssertTrue(app.otherElements["text-preferences-preview"].exists, "\(role) preview")
            XCTAssertTrue(chip.isSelected, "\(role) chip selected")
        }
    }

    func testSavedCustomColorShowsSelectedAndAutoClearsIt() {
        let app = launch(mode: "text-preferences-custom-color")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        let custom = app.descendants(matching: .any)["text-preference-color-custom"]
        reveal(custom, app: app)
        XCTAssertTrue(custom.isSelected, "A saved color outside the presets selects the custom swatch")
        XCTAssertTrue(custom.label.hasPrefix("Custom color"), custom.label)
        XCTAssertEqual(custom.value as? String, "#7A3FD1")
        XCTAssertTrue(app.otherElements["text-preferences-preview"].exists)
        XCTAssertFalse(app.buttons["White"].isSelected)
        let auto = app.buttons["text-preference-color-auto"]
        XCTAssertFalse(auto.isSelected)
        auto.tap()
        XCTAssertTrue(auto.isSelected, "Auto is a visible chip and clears the color")
        XCTAssertFalse(custom.isSelected)
        let white = app.buttons["White"]
        reveal(white, app: app)
        white.tap()
        XCTAssertTrue(white.isSelected)
        XCTAssertFalse(auto.isSelected)
    }

    func testEnlargedPreviewShowsTheSelectedTypeAndClosesWithDone() {
        let app = launch(mode: "text-preferences")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app); manage.tap()
        app.buttons["account-text-preferences"].tap()
        selectRole("title", app: app)
        let enlarge = app.buttons["text-preferences-preview-enlarge"]
        XCTAssertTrue(enlarge.waitForExistence(timeout: 5))
        enlarge.tap()
        let done = app.buttons["text-preferences-preview-done"]
        XCTAssertTrue(done.waitForExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts["text-preference-sheet-description"].label, "The big opening line in the first seconds.")
        let large = app.otherElements["text-preferences-preview-large"]
        XCTAssertTrue(large.exists)
        XCTAssertGreaterThan(large.frame.width, 280, "The enlarged preview is big enough to read")
        XCTAssertEqual(large.value as? String, "Title")
        done.tap()
        XCTAssertTrue(done.waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.otherElements["text-preferences-preview"].exists)
    }

    func testTextPreferencesStaleWriteKeepsEditsAndOffersReload() {
        let app = launch(mode: "text-preferences-stale")
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
        app.buttons["account-text-preferences"].tap()
        let shadow = app.otherElements["text-preference-shadow"]
        reveal(shadow, app: app)
        shadow.buttons["On"].tap()
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
        selectRole("title", app: app)
        let placement = app.buttons["text-preference-placement"]
        // The form keeps its scroll offset across roles; the learning section left it far below Placement.
        for _ in 0..<10 where !placement.exists { scrollForm(app, down: true) }
        reveal(placement, app: app)
        XCTAssertTrue(placement.label.contains("Top Left"))
        let shadow = app.otherElements["text-preference-shadow"]
        reveal(shadow, app: app)
        XCTAssertTrue(shadow.buttons["Off"].isSelected)
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
        XCTAssertTrue(app.buttons["text-preference-role-title"].waitForExistence(timeout: 5),
                      "Loaded preferences must appear before checking persistence")
        XCTAssertFalse(dismiss.exists)
        let reset = app.buttons["text-learning-reset"]
        reveal(reset, app: app)
        let saveBarButton = app.buttons["text-preferences-save"]
        // Reproduce the CI edge: the row is partly behind the fixed bottom action.
        // Its XCUI hittability alone must not make reveal return at that position.
        let shift = saveBarButton.frame.minY + 12 - reset.frame.maxY
        if shift > 0 && shift < app.frame.height * 0.35 {
            let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.4))
            let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.4 + shift / app.frame.height))
            start.press(forDuration: 0.05, thenDragTo: end)
        }
        reveal(reset, app: app)
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
