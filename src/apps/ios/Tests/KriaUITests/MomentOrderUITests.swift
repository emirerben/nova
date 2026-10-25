import XCTest

/// KRI-723: "six moments from it, in this order: ...". Before approval Kria asks "put your moments in order and show
/// me where each one is" and the creator answers with a card: reorder the moments, and per moment point at a window
/// in the take (the KRI-717 Range picker), say it isn't in their clips, or let Kria pick.
/// KRIA_CHAT_MOMENT_ORDER: "1" = the server advertises moment_order_questions; "off" = an older server (the question
/// text still shows, with no card); "reject" = the first answer is refused (422 moment_order_overlap); "stale" = the
/// first answer meets a question the server has since replaced (409 moment_order_stale).
///
/// Failure modes pinned here: the answer reaches the server in another order or shape than the card shows (rows
/// re-sorted, a window from the picker changed, a skipped / delegated row sent with a window); an incomplete answer
/// can be sent (the server would 422 it); approval is offered before the moments are answered; a refused answer
/// loses the creator's choices or closes the card silently; a stale answer is dropped without saying so or leaves
/// the old card answerable; an older server hides the question.
@MainActor
final class MomentOrderUITests: XCTestCase {
    private func launch(mode: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_MOMENT_ORDER"] = mode
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
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
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        return app
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func questionText(_ app: XCUIApplication) -> XCUIElement {
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "I found 1 of your 3 moments")).firstMatch
    }

    /// The composer floats over the bottom of the conversation, so scroll until the element clears it.
    private func bringIntoView(_ target: XCUIElement, in app: XCUIApplication) {
        let conversation = app.descendants(matching: .any)["Conversation history"].firstMatch
        let composer = app.buttons["chat-send-message"]
        func isClearOfComposer() -> Bool {
            target.isHittable && (!composer.exists || target.frame.maxY <= composer.frame.minY)
        }
        for _ in 0..<8 where !isClearOfComposer() { conversation.swipeUp(velocity: .slow) }
        for _ in 0..<8 where !target.isHittable { conversation.swipeDown(velocity: .slow) }
        XCTAssertTrue(isClearOfComposer(), "\(target.identifier) must be on screen, above the composer")
    }

    private func tap(_ id: String, in app: XCUIApplication) {
        let button = app.buttons[id]
        XCTAssertTrue(button.waitForExistence(timeout: 5), id)
        bringIntoView(button, in: app)
        button.tap()
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func order(_ app: XCUIApplication) -> String { element(app, "moment-order-list").value as? String ?? "" }
    private func window(_ app: XCUIApplication, _ key: String) -> String { element(app, "moment-order-row-\(key)").value as? String ?? "" }

    private func waitForTheCard(_ app: XCUIApplication) {
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the question shows as the assistant's message")
        XCTAssertTrue(element(app, "moment-order-card").waitForExistence(timeout: 5))
        XCTAssertEqual(order(app), "m-tray,m-wave,m-bite", "Kria's order: the creator's own")
    }

    /// Leaves the tray out and lets Kria pick the wave; the bite stays on its evidence.
    private func answerWithoutPointing(_ app: XCUIApplication) {
        tap("moment-order-skip-m-tray", in: app)
        tap("moment-order-delegate-m-wave", in: app)
    }

    func testReorderPointAndSendExactlyThatAnswerThenApprovalIsOffered() {
        let app = launch(mode: "1")
        waitForTheCard(app)
        XCTAssertFalse(element(app, "direction-stage").exists, "no approval while moments are unanswered")
        XCTAssertEqual(window(app, "m-bite"), "5.00-6.50", "the grounded moment starts pointed at its evidence")
        XCTAssertEqual(window(app, "m-tray"), "", "an unlocated moment is never guessed")
        XCTAssertFalse(app.buttons["moment-order-send"].isEnabled, "two moments still need an answer")
        attach(app, "Moments card")

        // Reorder: the tray goes after the wave.
        tap("moment-order-down-m-tray", in: app)
        XCTAssertEqual(order(app), "m-wave,m-tray,m-bite")

        // Point at the tray with the Range picker: slide the default window right.
        tap("moment-order-point-m-tray", in: app)
        let body = element(app, "kria-source-window-body")
        XCTAssertTrue(body.waitForExistence(timeout: 15), "the KRI-717 Range picker opens on the take")
        let label = app.staticTexts["kria-source-window-label"]
        XCTAssertTrue(label.waitForExistence(timeout: 5))
        let before = label.label
        body.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.05, thenDragTo: body.coordinate(withNormalizedOffset: CGVector(dx: 1.2, dy: 0.5)))
        XCTAssertNotEqual(label.label, before, "the window slid")
        attach(app, "Pointing at the tray")
        app.buttons["moment-order-window-done"].tap()
        let tray = element(app, "moment-order-row-m-tray")
        XCTAssertTrue(tray.waitForExistence(timeout: 5))
        let pointed = window(app, "m-tray")
        let bounds = pointed.split(separator: "-").compactMap { Double($0) }
        XCTAssertEqual(bounds.count, 2, "the tray row holds the picked window: \(pointed)")
        if bounds.count == 2 {
            XCTAssertGreaterThan(bounds[0], 0, "slid off the start")
            XCTAssertEqual(bounds[1] - bounds[0], 2.0, accuracy: 0.01, "a slide keeps the default length")
            XCTAssertLessThanOrEqual(bounds[1], 5.0, "clear of the bite")
        }

        // Let Kria pick the wave.
        tap("moment-order-delegate-m-wave", in: app)
        let send = app.buttons["moment-order-send"]
        bringIntoView(send, in: app)
        XCTAssertTrue(send.isEnabled)
        XCTAssertGreaterThanOrEqual(send.frame.height, 44)
        attach(app, "Moments answered")
        send.tap()

        XCTAssertTrue(app.staticTexts["You: I showed you where each of my moments is."].waitForExistence(timeout: 10),
                      "the message is the server's neutral echo: no times or digits for a model to read")
        let expected = "moments[m-wave:delegate|m-tray:point:fixture-clip:\(pointed)|m-bite:point:fixture-clip:5.00-6.50]"
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", expected)).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "the server received the creator's order and exactly the windows the card showed")
        let answered = element(app, "moment-order-answered")
        XCTAssertTrue(answered.waitForExistence(timeout: 5), "the card collapses once the stored message echoes the answer")
        XCTAssertEqual(answered.label, "3 moments set, in your order")
        XCTAssertFalse(app.buttons["moment-order-send"].exists)
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 10), "approval is offered after the answer")
        attach(app, "Accepted, approval offered")
    }

    func testARefusedAnswerKeepsTheCardAndTheChoicesAndSaysWhy() {
        let app = launch(mode: "reject")
        waitForTheCard(app)
        answerWithoutPointing(app)
        tap("moment-order-send", in: app)
        let error = element(app, "moment-order-error")
        XCTAssertTrue(error.waitForExistence(timeout: 10))
        XCTAssertEqual(error.label, "Two of your moments overlap. Move one of them.")
        XCTAssertEqual(window(app, "m-bite"), "5.00-6.50", "the creator's choices are kept")
        XCTAssertTrue(app.buttons["moment-order-skip-m-tray"].isSelected)
        XCTAssertTrue(app.buttons["moment-order-delegate-m-wave"].isSelected)
        XCTAssertFalse(element(app, "direction-stage").exists)
        attach(app, "Refused answer")
        tap("moment-order-send", in: app)
        XCTAssertTrue(element(app, "moment-order-answered").waitForExistence(timeout: 10), "the retry goes through")
    }

    func testAStaleAnswerRefreshesToTheNewestCardAndSaysSo() {
        let app = launch(mode: "stale")
        waitForTheCard(app)
        answerWithoutPointing(app)
        tap("moment-order-send", in: app)
        let notice = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "that answer is out of date")).firstMatch
        XCTAssertTrue(notice.waitForExistence(timeout: 10), "a stale answer is never dropped silently")
        XCTAssertFalse(element(app, "moment-order-answered").exists, "the stale answer closed nothing")
        let cards = app.descendants(matching: .any).matching(identifier: "moment-order-card")
        let one = XCTNSPredicateExpectation(predicate: NSPredicate(format: "count == 1"), object: cards)
        XCTAssertEqual(XCTWaiter.wait(for: [one], timeout: 10), .completed, "only the newest question is answerable")
        XCTAssertFalse(app.buttons["moment-order-send"].isEnabled, "the newest card starts fresh")
        attach(app, "Stale answer, newest card")
        answerWithoutPointing(app)
        tap("moment-order-send", in: app)
        XCTAssertTrue(element(app, "moment-order-answered").waitForExistence(timeout: 10), "the newest card is answered")
    }

    func testAnOlderServerShowsTheQuestionTextWithNoCard() {
        let app = launch(mode: "off")
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the message is never hidden")
        XCTAssertFalse(element(app, "moment-order-card").exists)
    }
}
