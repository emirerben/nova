import XCTest

/// Re-record on the offline fixture server (`KRIA_TALKING_RERECORD`). The real app code runs the whole path: the
/// ready video's action, the teleprompter, proxy transcode, reserve, PUT, the swap route and the follow-up turn.
/// The fixture answers a plain attach with the real clip-cap refusal, so a swap that attached a second speaker fails.
@MainActor final class TalkRerecordUITests: XCTestCase {
    private static let oldTake = "Old take.mov"

    func testReRecordSwapsTheTakeAndMakesAFreshPlan() {
        let app = launch(mode: "ready")
        openRerecordCamera(app)
        let notice = app.staticTexts["teleprompter-replacement-notice"]
        XCTAssertTrue(notice.waitForExistence(timeout: 10))
        XCTAssertTrue(notice.label.contains(Self.oldTake), "names the take that is replaced")
        XCTAssertTrue(notice.label.contains("won’t carry over"), "says what is lost before the creator commits")
        XCTAssertEqual(app.buttons["teleprompter-use-take"].label, "Replace take")
        capture(app, "Replace notice before committing")
        app.buttons["teleprompter-use-take"].tap()

        XCTAssertTrue(app.staticTexts["Take replaced"].waitForExistence(timeout: 90))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Made it again from your new take."].waitForExistence(timeout: 30), "a fresh plan follows the swap")
        XCTAssertTrue(app.buttons["ready-rerecord"].waitForExistence(timeout: 20), "the new video can be re-recorded again")
        XCTAssertFalse(app.buttons["rerecord-make-again"].exists, "the plan turn was sent once, not left owed")
        capture(app, "Fresh video after re-record")

        openAddMedia(app)
        XCTAssertTrue(app.buttons["Remove Recorded take.mov"].waitForExistence(timeout: 10), "the new take is attached")
        XCTAssertFalse(app.buttons["Remove \(Self.oldTake)"].exists, "the old take is gone")
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Remove '")).count, 1, "one speaker, not two")
        capture(app, "Add media after the swap shows one take")

        app.buttons["attachment-close"].tap()
        app.buttons["Open projects"].tap()
        XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label CONTAINS 'Talking to camera'")).firstMatch.waitForExistence(timeout: 10))
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "label CONTAINS 'Untitled project'")).count, 0, "re-recording and reopening Add media stay in this chat; a new chat would be Untitled project")
    }

    func testRefusedSwapKeepsTheOldTakeAndStartsNoPlan() {
        let app = launch(mode: "no-audio")
        openRerecordCamera(app)
        app.buttons["teleprompter-use-take"].tap()
        // The error is a notice card: the identifier is on the card, the words on its text.
        let error = app.otherElements["teleprompter-review-error"]
        XCTAssertTrue(error.waitForExistence(timeout: 60))
        XCTAssertTrue(error.staticTexts.matching(NSPredicate(format: "label CONTAINS 'no sound'")).firstMatch.exists)
        XCTAssertTrue(error.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Your current take is unchanged.'")).firstMatch.exists)
        capture(app, "Swap refused, take kept")
        app.buttons["teleprompter-close"].tap()

        XCTAssertTrue(app.staticTexts["rerecord-failure"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["rerecord-retry"].exists, "the same bytes cannot succeed, so no retry is offered")
        XCTAssertFalse(app.staticTexts["Made it again from your new take."].exists)
        XCTAssertFalse(app.buttons["rerecord-make-again"].exists)

        openAddMedia(app)
        XCTAssertTrue(app.buttons["Remove \(Self.oldTake)"].waitForExistence(timeout: 10), "the old take is still attached")
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Remove '")).count, 1)
        capture(app, "Old take intact after a refused swap")
    }

    func testBusyServerOffersRetryThenSwapsWithTheSameTake() {
        let app = launch(mode: "busy-twice")
        openRerecordCamera(app)
        app.buttons["teleprompter-use-take"].tap()
        // The error is a notice card: the identifier is on the card, the words on its text.
        let error = app.otherElements["teleprompter-review-error"]
        XCTAssertTrue(error.waitForExistence(timeout: 60))
        XCTAssertTrue(error.staticTexts.matching(NSPredicate(format: "label CONTAINS 'still being made'")).firstMatch.exists)
        let use = app.buttons["teleprompter-use-take"]
        XCTAssertTrue(use.isEnabled, "the take is kept and can be sent again")
        use.tap()
        XCTAssertTrue(app.staticTexts["Take replaced"].waitForExistence(timeout: 90))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.staticTexts["Made it again from your new take."].waitForExistence(timeout: 30))
    }

    func testFullSlotOffersReplaceInsteadOfRefusing() {
        let app = launch(mode: "slot-full", maxClips: "1")
        let attach = app.buttons["chat-attach"]
        XCTAssertTrue(attach.waitForExistence(timeout: 20))
        attach.tap()
        app.buttons["attach-record"].tap()
        let use = app.buttons["teleprompter-use-take"]
        XCTAssertTrue(use.waitForExistence(timeout: 20))
        XCTAssertTrue(use.isEnabled, "a full slot is not a refusal when replace is offered")
        XCTAssertEqual(use.label, "Replace take")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'This Talking edit already has footage'")).firstMatch.exists)
        use.tap()
        XCTAssertTrue(app.staticTexts["Take replaced"].waitForExistence(timeout: 60))
        app.buttons["Done"].tap()
        XCTAssertTrue(app.buttons["Remove Recorded take.mov"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Remove \(Self.oldTake)"].exists)
    }

    func testFlagOffHidesReRecord() {
        let app = launch(mode: "no-replace")
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["ready-rerecord"].exists, "a server that does not offer replace shows no action")
        XCTAssertTrue(app.buttons["chat-attach"].isEnabled, "reopening Add media on a ready video still works")
    }

    /// Add media on a ready video: the composer plus, Add script, record without a script, then close the camera. The
    /// camera was opened from the Add media step, so closing it lands on the clip list.
    private func openAddMedia(_ app: XCUIApplication) {
        let attach = app.buttons["chat-attach"]
        XCTAssertTrue(attach.waitForExistence(timeout: 20))
        attach.tap()
        app.buttons["attach-script"].tap()
        let noScript = app.buttons["teleprompter-no-script"]
        XCTAssertTrue(noScript.waitForExistence(timeout: 20))
        noScript.tap()
        let close = app.buttons["teleprompter-close"]
        XCTAssertTrue(close.waitForExistence(timeout: 20))
        close.tap()
        XCTAssertTrue(app.buttons["attachment-close"].waitForExistence(timeout: 10))
    }

    private func openRerecordCamera(_ app: XCUIApplication) {
        let action = app.buttons["ready-rerecord"]
        XCTAssertTrue(action.waitForExistence(timeout: 20))
        action.tap()
        let confirm = app.buttons["Record new take"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5), "the trade-off is shown before anything changes")
        confirm.tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 20))
    }

    private func launch(mode: String, maxClips: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
        app.launchEnvironment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] = "1"
        app.launchEnvironment["KRIA_TALKING_RERECORD"] = mode
        if let maxClips { app.launchEnvironment["KRIA_CHAT_TALKING_MAX_CLIPS"] = maxClips }
        app.launch()
        return app
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
