import XCTest

/// Steer now (Green 5, 2026-11-04): the creator tapped the "Suggest an edit." start, typed the real request while
/// Kria was still working on it, and approved the chip's generic card because nothing said a newer message was
/// waiting. Emir's direction: do it the way Codex does. A message sent while Kria works waits in a bar attached to the
/// top of the input box (not in the chat): "↳ Steer" switches Kria to it now, the trash removes it, and "⋯" (or holding
/// the bar) offers Edit message / Steer instead / Cancel message. No Steer while the video is being made.
///
/// The fake server (`KRIA_CHAT_STEER_NOW`) mirrors `runtime.submit_turn`'s one queue slot, `cancel_turn(steer=True)`
/// and the plain cancel of a waiting message. "Send clips" sends the same stock message the chip sends. Failure
/// modes: docs/pipelines/steer-now.md.
@MainActor
final class SteerNowUITests: XCTestCase {
    static let typedRequest = "Me small at the bottom right. Captions small, white, lowercase, in the middle. No music."
    static let firstSummary = "Open on the laugh and keep the pacing quick."
    static let typedSummary = "Your request: you small at the bottom right, white captions in the middle, no music."
    static let stopLine = "Kria: Stopped. Working on your newer message instead."

    override func setUp() {
        continueAfterFailure = false
    }

    // MARK: - The bar while Kria plans (Green 5)

    func testWaitingBarAndSteerWhileKriaPlans() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "planning"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10), "Kria is working on the first message")

        send(app, Self.typedRequest)

        XCTAssertTrue(element(app, "chat-waiting-bar").waitForExistence(timeout: 10), "the typed request waits in the bar")
        XCTAssertEqual(element(app, "chat-waiting-text").label, "Waiting message: \(Self.typedRequest)", "VoiceOver reads it as waiting")
        XCTAssertFalse(chatShows(app, Self.typedRequest), "the waiting message is not in the chat (it would show twice)")
        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.exists)
        XCTAssertEqual(steer.label, "Steer")
        XCTAssertTrue(app.buttons["chat-waiting-delete"].exists)
        XCTAssertTrue(app.buttons["chat-waiting-more"].exists)
        attach(app, "steer-1-bar-while-planning")

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10), "the stopped turn says so in the chat")
        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "chat-waiting-bar").exists })
        XCTAssertTrue(eventually(timeout: 10) { self.chatShows(app, Self.typedRequest) }, "the steered message joins the chat")
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 20), "one card arrives")
        XCTAssertTrue(app.staticTexts[Self.typedSummary].exists, "the card is for the typed request")
        XCTAssertFalse(app.staticTexts[Self.firstSummary].exists)
        attach(app, "steer-2-after-steer-one-card")
    }

    // MARK: - The bar while the first card is showing

    func testSteerClosesTheCardAndSaysWhy() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts[Self.firstSummary].exists)

        send(app, Self.typedRequest)

        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))
        XCTAssertFalse(chatShows(app, Self.typedRequest))
        attach(app, "steer-3-bar-while-the-card-shows")

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10))
        XCTAssertTrue(eventually(timeout: 10) { !app.staticTexts[Self.firstSummary].exists }, "the stopped card closes")
        XCTAssertTrue(app.staticTexts[Self.typedSummary].waitForExistence(timeout: 20), "the new card is for the request")
        XCTAssertFalse(element(app, "chat-waiting-bar").exists)
        attach(app, "steer-4-new-card-after-steer")
    }

    // MARK: - No Steer once the video is being made

    func testNoSteerWhileTheVideoIsBeingMade() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "rendering"])
        let create = app.buttons["Create this video"]
        XCTAssertTrue(create.waitForExistence(timeout: 15))
        create.tap()
        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "direction-stage").exists }, "the video is being made")

        send(app, Self.typedRequest)

        XCTAssertTrue(element(app, "chat-waiting-bar").waitForExistence(timeout: 10), "the message still waits in the bar")
        XCTAssertFalse(app.buttons["chat-steer-now"].exists, "an approved video is never stopped")
        XCTAssertTrue(app.buttons["chat-waiting-delete"].exists, "the waiting message can still be removed")
        XCTAssertTrue(app.buttons["chat-waiting-more"].exists)
        attach(app, "steer-5-bar-while-the-video-is-made")
    }

    // MARK: - Trash, Edit and the menu

    func testTrashRemovesTheWaitingMessage() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, Self.typedRequest)
        let delete = app.buttons["chat-waiting-delete"]
        XCTAssertTrue(delete.waitForExistence(timeout: 10))
        XCTAssertEqual(delete.label, "Delete waiting message")

        delete.tap()

        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "chat-waiting-bar").exists }, "the bar goes away")
        XCTAssertFalse(chatShows(app, Self.typedRequest), "a removed message never shows in the chat")
        XCTAssertTrue(app.staticTexts[Self.firstSummary].exists, "the first card is untouched")
        attach(app, "steer-8-after-trash")
    }

    func testEditFromTheMenuPutsTheTextBackInTheBox() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, Self.typedRequest)
        let more = app.buttons["chat-waiting-more"]
        XCTAssertTrue(more.waitForExistence(timeout: 10))

        more.tap()

        XCTAssertTrue(app.buttons["Edit message"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Steer instead"].exists)
        XCTAssertTrue(app.buttons["Cancel message"].exists)
        attach(app, "steer-6-menu")
        app.buttons["Edit message"].tap()

        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "chat-waiting-bar").exists })
        XCTAssertTrue(eventually(timeout: 5) {
            (app.textFields["Message Kria"].value as? String) == Self.typedRequest
        }, "the words are back in the box to change")
        XCTAssertFalse(chatShows(app, Self.typedRequest))
    }

    func testHoldingTheBarOpensTheSameMenu() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, Self.typedRequest)
        let text = element(app, "chat-waiting-text")
        XCTAssertTrue(text.waitForExistence(timeout: 10))

        text.press(forDuration: 1.0)

        XCTAssertTrue(app.buttons["Edit message"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Steer instead"].exists)
        XCTAssertTrue(app.buttons["Cancel message"].exists)
    }

    // MARK: - A third message

    func testThirdMessageStaysInComposerWithANote() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "planning"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10))
        send(app, Self.typedRequest)
        XCTAssertTrue(element(app, "chat-waiting-bar").waitForExistence(timeout: 10))

        send(app, "Also trim the ending")

        let note = element(app, "chat-steer-note")
        XCTAssertTrue(note.waitForExistence(timeout: 5), "a plain note, not an error card")
        XCTAssertEqual(note.label, "One message is already waiting. Steer it, or send this after Kria answers.")
        XCTAssertEqual(app.textFields["Message Kria"].value as? String, "Also trim the ending", "the text is kept")
        XCTAssertFalse(chatShows(app, "Also trim the ending"))
        XCTAssertFalse(app.staticTexts["Something went wrong"].exists)
        attach(app, "steer-7-third-message-kept")
    }

    // MARK: - Races the app must survive

    func testSteerRetriesOnceAfterAStaleRevision() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "stale"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10))
        send(app, Self.typedRequest)
        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10), "one refresh-and-retry, no error")
        XCTAssertFalse(app.staticTexts["Something went wrong"].exists)
    }

    func testSteerAfterTheVideoStartedHidesTheButton() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "refused"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, Self.typedRequest)
        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))

        steer.tap()

        let note = element(app, "chat-steer-note")
        XCTAssertTrue(note.waitForExistence(timeout: 10))
        XCTAssertEqual(note.label, "Your video is already being made. Kria reads your message next.")
        XCTAssertTrue(eventually(timeout: 5) { !app.buttons["chat-steer-now"].exists })
        XCTAssertTrue(element(app, "chat-waiting-bar").exists, "the message still waits")
        attach(app, "steer-9-too-late-video-started")
    }

    // MARK: - Language and switch

    func testTurkishChatGetsTurkishSteerWords() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card", "KRIA_CHAT_REPLY_LANGUAGE": "tr"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, "Altyazılar küçük ve beyaz olsun")

        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))
        XCTAssertEqual(steer.label, "Yönlendir")
        XCTAssertEqual(app.buttons["chat-waiting-delete"].label, "Bekleyen mesajı sil")
        attach(app, "steer-10-turkish")

        steer.tap()

        XCTAssertTrue(app.staticTexts["Kria: Durdurdum. Bunun yerine yeni mesajın üzerinde çalışıyorum."].waitForExistence(timeout: 10))
    }

    func testSwitchOffLooksLikeToday() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card", "KRIA_CHAT_STEER_NOW_CAP": "0"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))

        send(app, Self.typedRequest)

        XCTAssertTrue(app.staticTexts["You: \(Self.typedRequest)"].waitForExistence(timeout: 10))
        XCTAssertFalse(element(app, "chat-waiting-bar").exists)
        XCTAssertFalse(app.buttons["chat-steer-now"].exists)
        attach(app, "steer-11-switch-off-as-today")
    }

    // MARK: - Helpers

    private func launch(_ environment: [String: String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment.merge(environment) { _, new in new }
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
        // "Send clips" sends the same stock message as the "Suggest an edit" chip.
        let sendClips = app.buttons["Send clips"]
        XCTAssertTrue(sendClips.waitForExistence(timeout: 5))
        sendClips.tap()
        return app
    }

    private func send(_ app: XCUIApplication, _ text: String) {
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText(text)
        let send = app.buttons["Send message"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        XCTAssertTrue(eventually(timeout: 5) { send.isEnabled }, "the creator can send while Kria works")
        send.tap()
    }

    /// The chat shows the creator's message as a bubble (the bar's copy has its own identifier).
    private func chatShows(_ app: XCUIApplication, _ text: String) -> Bool {
        app.staticTexts.matching(NSPredicate(format: "label == %@", "You: \(text)")).count > 0
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func eventually(timeout: TimeInterval = 3, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else { return false }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return true
    }
}
