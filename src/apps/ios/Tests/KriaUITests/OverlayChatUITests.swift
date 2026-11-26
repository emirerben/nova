import XCTest

/// CA-8: overlays are asked for, answered and made in the chat, and the finished images open the existing results sheet.
/// The offline server (`OverlaySetUITestFixture`) sends `overlay_set` events in the real shape. `KRIA_CHAT_OVERLAY_ECHO`
/// writes each overlay's generate requests and charged attempts into its title, so "no second charge" is read off the
/// screen. `KRIA_CHAT_OVERLAY_FAIL=third` fails the third overlay's first attempt; `KRIA_CHAT_OVERLAY_HOLD=1` leaves it
/// in flight.
///
/// Failure modes pinned here: the Generate tile still opening the six-screen sheet when the server offers the chat flow
/// (and the chat flow leaking in when it does not); the ask drawing twice or staying after the answer; the compose card
/// pricing or counting the wrong rows; a failed overlay hiding the ready ones, or Retry making the others again; a
/// relaunch mid-generation charging again; Review images opening anywhere but the sheet's results.
@MainActor
final class OverlayChatUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    private func launch(overlaySets: Bool = true, fail: Bool = false, hold: Bool = false, persist: String? = nil, relaunching app: XCUIApplication? = nil) -> XCUIApplication {
        let app = app ?? XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_FIXTURE_MEDIA": "1", "KRIA_CHAT_VOICEOVER_SCRIPT": "1",
                                 "KRIA_GENERATED_MEDIA": "1", "UI_TEST_REDUCE_MOTION": "1"]
        if overlaySets { app.launchEnvironment["KRIA_CHAT_OVERLAY_SETS"] = "1"; app.launchEnvironment["KRIA_CHAT_OVERLAY_ECHO"] = "1" }
        if fail { app.launchEnvironment["KRIA_CHAT_OVERLAY_FAIL"] = "third" }
        if hold { app.launchEnvironment["KRIA_CHAT_OVERLAY_HOLD"] = "1" }
        if let persist { app.launchEnvironment["KRIA_GENERATED_MEDIA_PERSIST"] = persist }
        app.launch()
        return app
    }

    private func startChat(_ app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)], timeout: 20), .completed)
        newChat.tap()
        app.chooseFormat("montage")
        XCTAssertTrue(app.buttons["Send clips"].waitForExistence(timeout: 10))
        app.buttons["Send clips"].tap()
        XCTAssertTrue(app.buttons["voiceover-script-record"].waitForExistence(timeout: 20))
    }

    private func tapGenerateInTray(_ app: XCUIApplication) {
        app.buttons["voiceover-script-record"].tap()
        XCTAssertTrue(app.buttons["voiceover-skip"].waitForExistence(timeout: 10))
        app.buttons["voiceover-skip"].tap()
        let generate = app.buttons["generate-image"]
        XCTAssertTrue(generate.waitForExistence(timeout: 10))
        generate.tap()
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.descendants(matching: .any)[id].firstMatch }

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func reveal(_ element: XCUIElement, in app: XCUIApplication) {
        for _ in 0..<5 where !element.isHittable { app.swipeUp() }
    }

    private func openAsk(_ app: XCUIApplication) {
        startChat(app)
        tapGenerateInTray(app)
        XCTAssertTrue(element(app, "overlay-ask-card").waitForExistence(timeout: 20), "the Generate tile asks in the chat")
        XCTAssertFalse(app.buttons["overlay-choice-decide"].exists, "the six-screen sheet is not opened")
    }

    func testTheGenerateTileAsksInTheChatAndTheCreatorCanDescribeTwoOverlays() {
        let app = launch()
        openAsk(app)
        XCTAssertTrue(app.buttons["overlay-ask-decide"].exists)
        XCTAssertTrue(app.buttons["overlay-ask-describe"].exists)
        XCTAssertEqual(element(app, "chat-answering").exists, true, "the composer is ready for a typed answer")
        shot("ca8-ask")

        app.buttons["overlay-ask-describe"].tap()
        XCTAssertTrue(element(app, "overlay-compose-card").waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["overlay-ask-decide"].exists)
        XCTAssertFalse(app.buttons["overlay-compose-make"].isEnabled, "nothing to make until a row is described")
        shot("ca8-compose")

        let first = app.textFields["overlay-compose-row-0"].exists ? app.textFields["overlay-compose-row-0"] : app.textViews["overlay-compose-row-0"]
        first.tap()
        first.typeText("A sticker of a mountain peak")
        app.buttons["overlay-compose-add"].tap()
        let second = app.textFields["overlay-compose-row-1"].exists ? app.textFields["overlay-compose-row-1"] : app.textViews["overlay-compose-row-1"]
        XCTAssertTrue(second.waitForExistence(timeout: 5), "the added row appears")
        second.tap()
        second.typeText("A hand-drawn compass")
        XCTAssertTrue(app.buttons["overlay-compose-make"].label.hasPrefix("Make 2 overlays"), "the price follows the filled rows: \(app.buttons["overlay-compose-make"].label)")
        XCTAssertTrue(app.buttons["overlay-compose-make"].label.contains("$0.08"))
        shot("ca8-add-row")

        app.buttons["overlay-compose-make"].tap()
        XCTAssertTrue(element(app, "overlay-status-card").waitForExistence(timeout: 20))
        XCTAssertFalse(element(app, "overlay-compose-card").exists, "the answer is spent")
        XCTAssertTrue(app.buttons["overlay-review"].waitForExistence(timeout: 20))
        XCTAssertTrue(app.staticTexts["Overlays ready"].waitForExistence(timeout: 20), "both images are made")
        XCTAssertEqual(app.buttons["overlay-review"].label, "Review images")
        shot("ca8-done")
    }

    func testAPartlyFailedSetKeepsTheReadyOnesRetriesOnlyTheFailedOneAndReviewOpensTheSheetAtResults() {
        let app = launch(fail: true)
        openAsk(app)
        app.buttons["overlay-ask-decide"].tap()
        XCTAssertTrue(app.staticTexts["Couldn’t make this one"].waitForExistence(timeout: 30), "the third overlay failed")
        let retry = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "overlay-retry-")).firstMatch
        XCTAssertTrue(retry.exists, "a failed, retryable overlay offers Retry")
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", "Ready")).count, 2, "the others stay ready")
        XCTAssertEqual(app.buttons["overlay-review"].label, "Review 2 images")
        XCTAssertTrue(app.buttons["overlay-review"].isEnabled)
        for title in ["Mountain peak sticker [charged 1 of 1]", "Hand-drawn compass [charged 1 of 1]"] {
            XCTAssertTrue(app.staticTexts[title].exists, "\(title): a ready overlay was charged once")
        }
        shot("ca8-partial")

        retry.tap()
        XCTAssertTrue(app.staticTexts["Overlays ready"].waitForExistence(timeout: 30), "the retry made the third overlay")
        XCTAssertFalse(app.staticTexts["Couldn’t make this one"].exists)
        XCTAssertEqual(app.buttons["overlay-review"].label, "Review images")

        app.buttons["overlay-review"].tap()
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 20), "the existing sheet opens at its results")
        XCTAssertTrue(app.staticTexts["Your 3 overlays"].waitForExistence(timeout: 20))
        XCTAssertFalse(app.buttons["overlay-choice-decide"].exists)
        let charged = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "[charged 1 of 1]"))
        XCTAssertEqual(charged.count, 2, "the two overlays that were never retried were charged once and asked for once")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "[charged 2 of 2]")).firstMatch.exists, "only the retried overlay was charged twice")
        shot("ca8-sheet")
    }

    func testRelaunchingMidGenerationMakesTheRestWithoutChargingAgain() {
        let persist = UUID().uuidString
        let app = launch(hold: true, persist: persist)
        openAsk(app)
        app.buttons["overlay-ask-decide"].tap()
        XCTAssertTrue(app.staticTexts["Rolled-up map [charged 1 of 1]"].waitForExistence(timeout: 30), "the third overlay is still being made")
        XCTAssertEqual(app.buttons["overlay-review"].label, "Review 2 images")
        shot("ca8-progress")
        app.terminate()

        _ = launch(persist: persist, relaunching: app)
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 25))
        XCTAssertTrue(app.staticTexts["Overlays ready"].waitForExistence(timeout: 40), "the relaunch asked for the one still being made")
        shot("ca8-resume")
        app.buttons["overlay-review"].tap()
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 20))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "[charged 1 of")).count, 3, "every overlay was charged exactly once")
    }

    func testCancellingTheComposeCardLeavesNoCardAndAFlagOffServerKeepsTheOldSheet() {
        let app = launch()
        openAsk(app)
        app.buttons["overlay-ask-describe"].tap()
        XCTAssertTrue(app.buttons["overlay-compose-cancel"].waitForExistence(timeout: 5))
        app.buttons["overlay-compose-cancel"].tap()
        XCTAssertFalse(element(app, "overlay-compose-card").exists)
        XCTAssertFalse(element(app, "overlay-ask-card").exists, "Cancel closes the ask; nothing is made")
        XCTAssertFalse(element(app, "chat-answering").exists, "the composer is free again")
    }

    func testWithTheCapabilityOffTheGenerateTileOpensTheSixScreenSheet() {
        let app = launch(overlaySets: false)
        startChat(app)
        tapGenerateInTray(app)
        XCTAssertTrue(app.buttons["overlay-choice-decide"].waitForExistence(timeout: 10), "the sheet opens exactly as before")
        XCTAssertTrue(app.buttons["overlay-choice-describe"].exists)
        XCTAssertFalse(element(app, "overlay-ask-card").exists)
        shot("ca8-flag-off")
    }
}
