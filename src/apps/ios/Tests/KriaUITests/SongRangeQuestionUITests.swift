import XCTest

/// KRI-588: Kria asks "Which part of the song did you film?" in the chat and the creator answers with a card.
/// KRIA_CHAT_SONG_RANGE_Q: "1" = the server advertises song_range_questions; "off" = an older server (the question
/// text still shows, with no card); "invalid" / "stale" = every range answer is refused with 422 / 409.
@MainActor
final class SongRangeQuestionUITests: XCTestCase {
    private func launch(mode: String, sendFailure: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_SONG_RANGE_Q"] = mode
        app.launchEnvironment["KRIA_CHAT_SEND_FAILURE"] = sendFailure
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
        app.buttons["format-montage"].tap()
        return app
    }

    /// Sends the attached clips, which makes the fixture server ask the question.
    private func askTheQuestion(_ app: XCUIApplication) {
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func questionText(_ app: XCUIApplication) -> XCUIElement {
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Which part of the song did you film?")).firstMatch
    }

    /// The composer floats over the bottom of the conversation, so scroll until the element clears it.
    private func bringIntoView(_ target: XCUIElement, in app: XCUIApplication) {
        let conversation = app.descendants(matching: .any)["Conversation history"].firstMatch
        let composer = app.buttons["chat-send-message"]
        func isClearOfComposer() -> Bool {
            target.isHittable && (!composer.exists || target.frame.maxY <= composer.frame.minY)
        }
        for _ in 0..<8 where !isClearOfComposer() { conversation.swipeUp(velocity: .slow) }
        XCTAssertTrue(isClearOfComposer(), "\(target.identifier) must be on screen, above the composer")
    }

    private func wheel(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        let picker = element(app, id)
        XCTAssertTrue(picker.waitForExistence(timeout: 5), "\(id) must exist")
        let inner = picker.pickerWheels.firstMatch
        return inner.exists ? inner : app.pickerWheels[id]
    }

    private func choose(_ app: XCUIApplication, _ id: String, _ time: String) {
        wheel(app, id).adjust(toPickerWheelValue: time)
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func waitForTheCard(_ app: XCUIApplication) {
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the question shows as the assistant's message")
        XCTAssertTrue(element(app, "song-range-card").waitForExistence(timeout: 5))
        bringIntoView(app.buttons["song-range-card-use"], in: app)
    }

    func testUnconfirmedRangeRetryKeepsTheRejectionOnTheCard() {
        let app = launch(mode: "invalid", sendFailure: "offline-structured")
        askTheQuestion(app)
        waitForTheCard(app)
        choose(app, "song-range-card-from", "0:40")
        choose(app, "song-range-card-to", "1:20")
        app.buttons["song-range-card-use"].tap()
        let retry = app.buttons["chat-retry-message"]
        XCTAssertTrue(retry.waitForExistence(timeout: 15))
        retry.tap()
        let error = element(app, "song-range-card-error")
        XCTAssertTrue(error.waitForExistence(timeout: 10))
        bringIntoView(error, in: app)
        XCTAssertEqual(error.label, "That starts after your song ends.")
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.exists)
        guard let draft = composer.value as? String else {
            XCTFail("The composer must expose its draft after rejection")
            return
        }
        XCTAssertFalse(draft.contains("I filmed"))
        XCTAssertFalse(retry.exists)
        attach(app, "Retry rejection stays on the song card")
    }

    func testPickingAPartSendsItAndTheCardCollapsesToWhatWasChosen() {
        let app = launch(mode: "1")
        askTheQuestion(app)
        waitForTheCard(app)
        XCTAssertGreaterThanOrEqual(app.buttons["song-range-card-use"].frame.height, 44)
        XCTAssertGreaterThanOrEqual(app.buttons["song-range-card-whole"].frame.height, 44)
        XCTAssertEqual(wheel(app, "song-range-card-from").value as? String, "0:00", "From starts at the beginning")
        XCTAssertEqual(wheel(app, "song-range-card-to").value as? String, "1:30", "To starts at the song's end")
        XCTAssertTrue(app.buttons["song-range-card-use"].isEnabled)
        attach(app, "Active card")

        choose(app, "song-range-card-from", "0:40")
        choose(app, "song-range-card-to", "1:20")
        XCTAssertFalse(element(app, "song-range-card-error").exists)
        app.buttons["song-range-card-use"].tap()

        XCTAssertTrue(app.staticTexts["You: I filmed from 0:40 to 1:20"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "range[40-80]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "the server received start_s 40 and end_s 80 as song_range")
        let answered = element(app, "song-range-card-answered")
        XCTAssertTrue(answered.waitForExistence(timeout: 5), "the card collapses once the stored message echoes the answer")
        XCTAssertEqual(answered.label, "Filmed from 0:40 to 1:20")
        XCTAssertFalse(app.buttons["song-range-card-use"].exists)
        XCTAssertFalse(element(app, "song-range-card").exists)
        attach(app, "Answered card")
    }

    func testUseTheWholeSongSendsNullEndsAndShowsWholeSong() {
        let app = launch(mode: "1")
        askTheQuestion(app)
        waitForTheCard(app)
        bringIntoView(app.buttons["song-range-card-whole"], in: app)
        app.buttons["song-range-card-whole"].tap()

        XCTAssertTrue(app.staticTexts["You: Use the whole song"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "range[whole]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "both ends reached the server as null")
        let answered = element(app, "song-range-card-answered")
        XCTAssertTrue(answered.waitForExistence(timeout: 5))
        XCTAssertEqual(answered.label, "Whole song")
    }

    func testUnderFiveSecondsDisablesUseThisPartAndSaysWhy() {
        let app = launch(mode: "1")
        askTheQuestion(app)
        waitForTheCard(app)
        choose(app, "song-range-card-from", "0:40")
        choose(app, "song-range-card-to", "0:43")
        XCTAssertFalse(app.buttons["song-range-card-use"].isEnabled)
        XCTAssertTrue(app.buttons["song-range-card-whole"].isEnabled, "the whole song is always a valid answer")
        let reason = element(app, "song-range-card-error")
        XCTAssertTrue(reason.waitForExistence(timeout: 3))
        XCTAssertEqual(reason.label, "Pick at least five seconds of your song.")
        choose(app, "song-range-card-to", "0:45")
        XCTAssertTrue(app.buttons["song-range-card-use"].isEnabled, "exactly five seconds is allowed")
        XCTAssertFalse(reason.exists)
    }

    func testMovingFromPastToPushesToOut() {
        let app = launch(mode: "1")
        askTheQuestion(app)
        waitForTheCard(app)
        choose(app, "song-range-card-to", "0:30")
        choose(app, "song-range-card-from", "0:50")
        XCTAssertEqual(wheel(app, "song-range-card-to").value as? String, "0:55")
        XCTAssertTrue(app.buttons["song-range-card-use"].isEnabled)
    }

    func testAnOlderServerShowsTheQuestionTextWithNoCard() {
        let app = launch(mode: "off")
        askTheQuestion(app)
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the message is never hidden")
        XCTAssertFalse(element(app, "song-range-card").exists)
        XCTAssertFalse(app.buttons["song-range-card-use"].exists)
        XCTAssertFalse(app.buttons["song-range-card-whole"].exists)
        XCTAssertFalse(element(app, "song-range-card-answered").exists)
    }

    func testAStaleAnswerClosesTheCardWithoutAnAlert() {
        let app = launch(mode: "stale")
        askTheQuestion(app)
        waitForTheCard(app)
        bringIntoView(app.buttons["song-range-card-whole"], in: app)
        app.buttons["song-range-card-whole"].tap()
        XCTAssertTrue(element(app, "song-range-card").waitForNonExistence(timeout: 10), "the closed question is not offered again")
        XCTAssertTrue(questionText(app).exists, "the message stays")
        XCTAssertFalse(app.alerts.firstMatch.exists)
        XCTAssertFalse(app.staticTexts["You: Use the whole song"].exists, "the refused answer is not left in the transcript")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "couldn’t confirm")).firstMatch.exists)
    }

    func testARefusedRangeShowsTheServersReasonOnTheCardAndKeepsItOpen() {
        let app = launch(mode: "invalid")
        askTheQuestion(app)
        waitForTheCard(app)
        choose(app, "song-range-card-from", "0:10")
        choose(app, "song-range-card-to", "0:30")
        app.buttons["song-range-card-use"].tap()
        let reason = element(app, "song-range-card-error")
        XCTAssertTrue(reason.waitForExistence(timeout: 10))
        XCTAssertEqual(reason.label, "That starts after your song ends.")
        XCTAssertTrue(element(app, "song-range-card").exists, "the card stays open for another try")
        XCTAssertFalse(app.staticTexts["You: I filmed from 0:10 to 0:30"].exists, "the refused answer is not left in the transcript")
        choose(app, "song-range-card-to", "0:35")
        XCTAssertFalse(element(app, "song-range-card-error").exists, "changing the range clears the old reason")
    }

    func testDraggingAcrossTheCardDoesNotOpenTheProjectsDrawer() {
        let app = launch(mode: "1")
        askTheQuestion(app)
        waitForTheCard(app)
        let card = element(app, "song-range-card")
        let width = app.windows.firstMatch.frame.width
        let y = card.frame.midY
        func drag(fromX: CGFloat) {
            let start = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: fromX, dy: y))
            let end = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: width * 0.85, dy: y))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .fast, thenHoldForDuration: 0)
        }
        drag(fromX: card.frame.minX + 6)
        XCTAssertFalse(app.buttons["drawer-new-chat"].isHittable, "a drag that starts on the card must not open the drawer")
        XCTAssertTrue(element(app, "song-range-card").exists)
        // The same swipe from the screen edge, outside the card, still opens it: the exclusion is only the card.
        drag(fromX: 2)
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 3), "a swipe from outside the card still opens the drawer")
    }
}
