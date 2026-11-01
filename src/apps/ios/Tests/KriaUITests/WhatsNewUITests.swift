import XCTest

@MainActor final class WhatsNewUITests: XCTestCase {
    private func launch(mode: String, largeText: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-account"]
        app.launchEnvironment["KRIA_ACCOUNT_TEST_MODE"] = mode
        // The root environment override does not reach a presented sheet, so the system size is set for the whole process.
        if largeText { app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"] }
        app.launch()
        return app
    }

    /// Scrolls the page until the element has a hit point inside the window.
    private func reveal(_ element: XCUIElement, app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        for _ in 0..<14 where !(element.exists && element.isHittable) { app.swipeUp() }
        for _ in 0..<14 where !(element.exists && element.isHittable) { app.swipeDown() }
        XCTAssertTrue(element.exists && element.isHittable, "Not reachable: \(element.identifier)", file: file, line: line)
    }

    private func element(_ id: String, in app: XCUIApplication) -> XCUIElement { app.descendants(matching: .any)[id] }

    private func openSettings(_ app: XCUIApplication) {
        let manage = app.buttons["Manage account"]
        reveal(manage, app: app)
        manage.tap()
    }

    private func openGuide(_ app: XCUIApplication) {
        openSettings(app)
        let entry = app.buttons["account-whats-new"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5), "What’s New leads Settings")
        reveal(entry, app: app)
        entry.tap()
        XCTAssertTrue(app.navigationBars["What’s New"].waitForExistence(timeout: 5))
    }

    private func back(_ app: XCUIApplication) { app.navigationBars.buttons.firstMatch.tap() }

    private func attachScreenshot(_ name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }

    func testGuideOpensFromSettingsShowsNewestFirstAndTryItReachesTextPreferences() {
        let app = launch(mode: "whats-new")
        openSettings(app)
        let settings = app.navigationBars["Settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Account"].exists, "The Account heading is gone")
        attachScreenshot("settings-top", app: app)
        let entry = app.buttons["account-whats-new"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5))
        XCTAssertLessThan(entry.frame.minY, app.buttons["account-text-preferences"].frame.minY, "What’s New is the first row")
        entry.tap()

        XCTAssertTrue(app.navigationBars["What’s New"].waitForExistence(timeout: 5))
        let newest = element("whats-new-date-2026-11-01", in: app)
        XCTAssertTrue(newest.waitForExistence(timeout: 5), "The newest day leads")
        XCTAssertTrue(newest.label.contains("November"), newest.label)
        XCTAssertTrue(element("whats-new-card-lipsync", in: app).exists, "Lip-sync is offered to this account")
        attachScreenshot("guide-top", app: app)
        XCTAssertFalse(element("whats-new-date-2026-10-28", in: app).exists, "Older days stay folded away")

        let previous = app.buttons["whats-new-previous"]
        reveal(previous, app: app)
        previous.tap()
        let oldest = element("whats-new-date-2026-10-28", in: app)
        XCTAssertTrue(oldest.waitForExistence(timeout: 5))
        XCTAssertTrue(element("whats-new-card-camera-roll-events", in: app).waitForExistence(timeout: 5))
        attachScreenshot("previous-expanded", app: app)

        let tryIt = app.buttons["whats-new-try-text-save-all"]
        reveal(tryIt, app: app)
        tryIt.tap()
        XCTAssertTrue(app.navigationBars["Text preferences"].waitForExistence(timeout: 5))
        back(app)
        XCTAssertTrue(app.navigationBars["What’s New"].waitForExistence(timeout: 5))
        back(app)
        XCTAssertTrue(settings.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["account-whats-new"].exists)
    }

    func testCardsThatNeedAFeatureAreAbsentWhenTheAccountCannotConfirmIt() {
        let app = launch(mode: "normal")
        openGuide(app)
        XCTAssertTrue(element("whats-new-date-2026-10-30", in: app).waitForExistence(timeout: 5),
                      "Days with only unavailable features are skipped")
        XCTAssertFalse(element("whats-new-date-2026-11-01", in: app).exists)
        XCTAssertFalse(element("whats-new-date-2026-10-31", in: app).exists)

        let previous = app.buttons["whats-new-previous"]
        reveal(previous, app: app)
        previous.tap()
        XCTAssertTrue(element("whats-new-card-message-receipt", in: app).waitForExistence(timeout: 5), "Cards with no gate still show")
        XCTAssertTrue(element("whats-new-card-format-examples", in: app).exists)
        XCTAssertFalse(element("whats-new-card-lipsync", in: app).exists)
        XCTAssertFalse(element("whats-new-card-camera-roll-events", in: app).exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "whats-new-try-")).firstMatch.exists)
    }

    func testEveryActionStaysReachableAtLargestTextSize() {
        let app = launch(mode: "whats-new", largeText: true)
        openSettings(app)
        let entry = app.buttons["account-whats-new"]
        XCTAssertTrue(entry.waitForExistence(timeout: 5))
        reveal(entry, app: app)
        attachScreenshot("settings-accessibility5", app: app)
        entry.tap()
        XCTAssertTrue(app.navigationBars["What’s New"].waitForExistence(timeout: 5))
        attachScreenshot("guide-top-accessibility5", app: app)

        let tryIt = app.buttons["whats-new-try-text-save-all"]
        reveal(tryIt, app: app)
        let previous = app.buttons["whats-new-previous"]
        reveal(previous, app: app)
        previous.tap()
        XCTAssertTrue(element("whats-new-date-2026-10-28", in: app).waitForExistence(timeout: 5))
        attachScreenshot("previous-expanded-accessibility5", app: app)
    }

    /// The audit measures pixels, so a row near the translucent navigation bar or the screen edge can read as a contrast
    /// failure while it is only partly on screen. The first pass is strict at the top of the guide. The second pass, scrolled
    /// to the Try it button, uses the same colors and checks everything except contrast.
    func testGuidePassesTheAccessibilityAudit() throws {
        let app = launch(mode: "whats-new")
        openGuide(app)
        XCTAssertTrue(element("whats-new-card-lipsync", in: app).waitForExistence(timeout: 5))
        let top = app.navigationBars["What’s New"].frame.maxY
        let bottom = app.frame.maxY
        var findings: [String] = []
        func audit(_ name: String, includesContrast: Bool) throws {
            attachScreenshot(name, app: app)
            try app.performAccessibilityAudit { issue in
                guard let frame = issue.element?.frame else { return false }
                if frame.minY < top || frame.maxY > bottom - 1 { return true }
                if issue.auditType == .contrast && !includesContrast { return true }
                findings.append("\(name) | \(issue.auditType) | \(issue.element?.identifier ?? "-") | \(issue.element?.label ?? "-") | \(issue.compactDescription)")
                return true
            }
        }
        try audit("audit-top", includesContrast: true)
        let tryIt = app.buttons["whats-new-try-text-save-all"]
        reveal(tryIt, app: app)
        for _ in 0..<4 where tryIt.frame.maxY > bottom - 40 { app.swipeUp(velocity: .slow) }
        try audit("audit-try-it", includesContrast: false)
        XCTAssertTrue(findings.isEmpty, findings.joined(separator: "\n"))
    }
}
