import XCTest

/// Steer now (Green 5, 2026-11-04): the creator tapped the "Suggest an edit." start, typed the real request while
/// Kria was still working on it, and approved the chip's generic card because nothing said a newer message was
/// waiting. A message sent while Kria works now shows as "Up next" with a "Steer now" button; tapping it stops the
/// earlier turn (with a line in the chat) and Kria works on the waiting message instead. No button once the video
/// is being made.
///
/// The fake server (`KRIA_CHAT_STEER_NOW`) mirrors `runtime.submit_turn`'s one queue slot and
/// `cancel_turn(steer=True)`. "Send clips" sends the same stock message the chip sends. Failure modes:
/// docs/pipelines/steer-now.md (F3, F4, F8, F10, F12, F17, F18 here; the rest are server tests).
@MainActor
final class SteerNowUITests: XCTestCase {
    static let typedRequest = "Me small at the bottom right. Captions small, white, lowercase, in the middle. No music."
    static let firstSummary = "Open on the laugh and keep the pacing quick."
    static let typedSummary = "Your request: you small at the bottom right, white captions in the middle, no music."
    static let stopLine = "Kria: Stopped. Working on your newer message instead."

    override func setUp() {
        continueAfterFailure = false
    }

    // MARK: - Up next while Kria plans (Green 5)

    func testUpNextAndSteerNowWhileKriaPlans() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "planning"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10), "Kria is working on the first message")

        send(app, Self.typedRequest)

        XCTAssertTrue(element(app, "chat-up-next").waitForExistence(timeout: 10), "the typed request waits as Up next")
        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 5))
        XCTAssertEqual(steer.label, "Steer now")
        attach(app, "steer-1-up-next-while-planning")

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10), "the stopped turn says so in the chat")
        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "chat-up-next").exists })
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 20), "one card arrives")
        XCTAssertTrue(app.staticTexts[Self.typedSummary].exists, "the card is for the typed request")
        XCTAssertFalse(app.staticTexts[Self.firstSummary].exists)
        attach(app, "steer-2-one-card-for-the-request")
    }

    // MARK: - Up next while the first card is showing

    func testSteerNowClosesTheCardAndSaysWhy() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts[Self.firstSummary].exists)

        send(app, Self.typedRequest)

        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))
        XCTAssertTrue(element(app, "chat-up-next").exists)
        attach(app, "steer-3-up-next-while-the-card-shows")

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10))
        XCTAssertTrue(eventually(timeout: 10) { !app.staticTexts[Self.firstSummary].exists }, "the stopped card closes")
        XCTAssertTrue(app.staticTexts[Self.typedSummary].waitForExistence(timeout: 20), "the new card is for the request")
        XCTAssertFalse(app.buttons["chat-steer-now"].exists)
        attach(app, "steer-4-new-card-after-steer")
    }

    // MARK: - No Steer now once the video is being made

    func testNoSteerNowWhileTheVideoIsBeingMade() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "rendering"])
        let create = app.buttons["Create this video"]
        XCTAssertTrue(create.waitForExistence(timeout: 15))
        create.tap()
        XCTAssertTrue(eventually(timeout: 10) { !self.element(app, "direction-stage").exists }, "the video is being made")

        send(app, Self.typedRequest)

        XCTAssertTrue(element(app, "chat-up-next").waitForExistence(timeout: 10), "the message still shows as Up next")
        XCTAssertFalse(app.buttons["chat-steer-now"].exists, "an approved video is never stopped")
        attach(app, "steer-5-up-next-while-the-video-is-made")
    }

    // MARK: - A third message

    func testThirdMessageStaysInComposerWithANote() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "planning"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10))
        send(app, Self.typedRequest)
        XCTAssertTrue(app.buttons["chat-steer-now"].waitForExistence(timeout: 10))

        send(app, "Also trim the ending")

        let note = element(app, "chat-steer-note")
        XCTAssertTrue(note.waitForExistence(timeout: 5), "a plain note, not an error card")
        XCTAssertEqual(note.label, "One message is already up next. Tap Steer now, or send this after Kria answers.")
        XCTAssertEqual(app.textFields["Message Kria"].value as? String, "Also trim the ending", "the text is kept")
        XCTAssertFalse(app.staticTexts["You: Also trim the ending"].exists)
        XCTAssertFalse(app.staticTexts["Something went wrong"].exists)
        attach(app, "steer-6-third-message-kept")
    }

    // MARK: - Races the app must survive

    func testSteerNowRetriesOnceAfterAStaleRevision() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "stale"])
        XCTAssertTrue(element(app, "chat-thinking").waitForExistence(timeout: 10))
        send(app, Self.typedRequest)
        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))

        steer.tap()

        XCTAssertTrue(app.staticTexts[Self.stopLine].waitForExistence(timeout: 10), "one refresh-and-retry, no error")
        XCTAssertFalse(app.staticTexts["Something went wrong"].exists)
    }

    func testSteerNowAfterTheVideoStartedHidesTheButton() {
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
        XCTAssertTrue(element(app, "chat-up-next").exists, "the message still waits")
        attach(app, "steer-7-too-late-video-started")
    }

    // MARK: - Language and switch

    func testTurkishChatGetsTurkishSteerWords() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card", "KRIA_CHAT_REPLY_LANGUAGE": "tr"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))
        send(app, "Altyazılar küçük ve beyaz olsun")

        let steer = app.buttons["chat-steer-now"]
        XCTAssertTrue(steer.waitForExistence(timeout: 10))
        XCTAssertEqual(steer.label, "Şimdi buna geç")
        XCTAssertEqual(element(app, "chat-up-next").label, "Sırada")
        attach(app, "steer-8-turkish")

        steer.tap()

        XCTAssertTrue(app.staticTexts["Kria: Durdurdum. Bunun yerine yeni mesajın üzerinde çalışıyorum."].waitForExistence(timeout: 10))
    }

    func testSwitchOffLooksLikeToday() {
        let app = launch(["KRIA_CHAT_STEER_NOW": "card", "KRIA_CHAT_STEER_NOW_CAP": "0"])
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15))

        send(app, Self.typedRequest)

        XCTAssertTrue(app.staticTexts["You: \(Self.typedRequest)"].waitForExistence(timeout: 10))
        XCTAssertFalse(element(app, "chat-up-next").exists)
        XCTAssertFalse(app.buttons["chat-steer-now"].exists)
        attach(app, "steer-9-switch-off-as-today")
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
