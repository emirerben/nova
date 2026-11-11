import XCTest

/// KRI-1160 bake-later, through the real screen on the re-record fixture server (`KRIA_TALKING_RERECORD=ready`).
/// `KRIA_TELEPROMPTER_BAKE_FIXTURE=<s>` makes the review take owe a Cinematic bake that takes that long, so Review opens
/// on the raw take at once, Use take waits with progress, and the swap then goes through unchanged.
/// The real Cinematic render needs a physical iPhone (`TeleprompterHardwareUITests.testPhysicalCinematicTake`).
@MainActor final class TalkBakeUITests: XCTestCase {
    func testReviewOpensAtOnceAndUseTakeWaitsForTheBakeThenSwaps() {
        let app = launch(bakeSeconds: 8)
        openRerecordCamera(app)

        let use = app.buttons["teleprompter-use-take"]
        XCTAssertTrue(use.isEnabled, "the raw take is reviewable and usable while the bake runs")
        let note = app.staticTexts["teleprompter-review-cinematic"]
        XCTAssertTrue(note.waitForExistence(timeout: 5))
        XCTAssertTrue(note.label.contains("Applying your Cinematic look"), "Review says the look is still being applied: \(note.label)")
        capture(app, "Review while the Cinematic look bakes")

        use.tap()
        let progress = app.staticTexts["teleprompter-review-progress"]
        XCTAssertTrue(progress.waitForExistence(timeout: 5), "Use take shows that it is waiting")
        XCTAssertTrue(progress.label.hasPrefix("Finishing your Cinematic look"), progress.label)
        XCTAssertFalse(use.isEnabled, "a second tap cannot start a second wait")
        XCTAssertTrue(app.buttons["teleprompter-retake"].isEnabled, "the wait can be abandoned")
        capture(app, "Use take waiting for the Cinematic look")

        XCTAssertTrue(app.staticTexts["Take replaced"].waitForExistence(timeout: 90), "the swap goes through once the bake settles")
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Made it again from your new take."].waitForExistence(timeout: 30))
    }

    func testReviewNoteBecomesFinalWhenTheBakeSettlesAndUseTakeThenDoesNotWait() {
        let app = launch(bakeSeconds: 3)
        openRerecordCamera(app)
        let note = app.staticTexts["teleprompter-review-cinematic"]
        XCTAssertTrue(note.waitForExistence(timeout: 5))
        let settled = NSPredicate(format: "label == 'Recorded in Cinematic.'")
        expectation(for: settled, evaluatedWith: note)
        waitForExpectations(timeout: 30)
        capture(app, "Review after the Cinematic look settled")

        app.buttons["teleprompter-use-take"].tap()
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'Finishing your Cinematic look'")).firstMatch.exists,
                       "a settled take is attached without a wait")
        XCTAssertTrue(app.staticTexts["Take replaced"].waitForExistence(timeout: 90))
    }

    private func launch(bakeSeconds: Int) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
        app.launchEnvironment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] = "1"
        app.launchEnvironment["KRIA_TELEPROMPTER_BAKE_FIXTURE"] = String(bakeSeconds)
        app.launchEnvironment["KRIA_TALKING_RERECORD"] = "ready"
        app.launch()
        return app
    }

    private func openRerecordCamera(_ app: XCUIApplication) {
        let action = app.buttons["ready-rerecord"]
        XCTAssertTrue(action.waitForExistence(timeout: 20))
        action.tap()
        let confirm = app.buttons["Record new take"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        confirm.tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 20))
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
