import XCTest

/// CA-1: asks come one after the other. KRIA_CHAT_ASK_SEQUENCE: "1" = the first turn is answered with a choice ask
/// and then a moments ask, both unanswered.
///
/// Failure modes pinned here: both asks drawn at once; the second ask missing after the first is answered; the
/// composer offering no way to type an answer while an ask is open; closing that lock closing the card or locking
/// it again on the next redraw; a typed answer leaving the choice open.
@MainActor
final class AskSequenceUITests: XCTestCase {
    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_ASK_SEQUENCE"] = "1"
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
        app.chooseFormat("montage")
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        return app
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

    private func composer(_ app: XCUIApplication) -> XCUIElement {
        app.textFields["Message Kria"].exists ? app.textFields["Message Kria"] : app.textViews["Message Kria"]
    }

    private func waitForTheChoiceCard(_ app: XCUIApplication) {
        XCTAssertTrue(element(app, "choice-card").waitForExistence(timeout: 20), "the first ask is drawn")
    }

    func testOnlyTheFirstAskIsDrawnAndTheNextOpensWhenItIsAnswered() {
        let app = launch()
        waitForTheChoiceCard(app)
        XCTAssertFalse(element(app, "moment-order-card").exists, "the second ask waits")
        XCTAssertFalse(app.staticTexts["Put your moments in order and show me where each one is."].exists, "a waiting ask draws nothing")
        XCTAssertTrue(element(app, "chat-answering").exists, "the composer is ready for a typed answer")
        XCTAssertEqual(composer(app).placeholderValue, "Type your answer")
        attach(app, "ca1-one-card-open")

        let option = app.buttons["choice-option-short"]
        XCTAssertTrue(option.waitForExistence(timeout: 5))
        option.tap()
        XCTAssertTrue(element(app, "moment-order-card").waitForExistence(timeout: 15), "the second ask opens once the first is answered")
        XCTAssertTrue(element(app, "choice-card-answered").exists, "the first ask is a receipt")
        XCTAssertFalse(element(app, "choice-card").exists)
        attach(app, "ca1-next-card-after-answer")
        XCTAssertTrue(element(app, "chat-answering").exists, "the lock moves to the ask that is open now")
        attach(app, "ca1-composer-type-your-answer")
    }

    func testClosingTheComposerLockLeavesTheCardOpen() {
        let app = launch()
        waitForTheChoiceCard(app)
        let close = app.buttons["chat-answering-cancel"]
        XCTAssertTrue(close.waitForExistence(timeout: 5))
        close.tap()
        XCTAssertFalse(element(app, "chat-answering").exists)
        XCTAssertNotEqual(composer(app).placeholderValue, "Type your answer")
        XCTAssertTrue(element(app, "choice-card").exists, "the card stays open")
        attach(app, "ca1-composer-closed")
    }

    func testATypedAnswerClosesTheChoiceAndOpensTheNextAsk() {
        let app = launch()
        waitForTheChoiceCard(app)
        let field = composer(app)
        field.tap()
        field.typeText("Short, with the date")
        app.buttons["chat-send-message"].tap()
        XCTAssertTrue(element(app, "moment-order-card").waitForExistence(timeout: 15), "the typed answer closed the choice")
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "received[text]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "the server got the typed text as an ordinary turn, with no choice payload")
        XCTAssertFalse(element(app, "choice-card").exists, "the choice is no longer open")
        attach(app, "ca1-typed-answer")
    }
}
