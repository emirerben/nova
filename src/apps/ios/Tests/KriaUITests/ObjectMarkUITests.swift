import XCTest

/// KRI-901: "No text on her face or on the plate". Before approval Kria asks "Mark the plate on one frame" and the
/// creator answers with a card: pick a clip, draw a box around the plate on its frame, send.
/// KRIA_CHAT_MARK_OBJECT: "1" = the server advertises protected_region_marks; "off" = an older server (the question
/// text still shows, with no card); "reject" = the first mark is refused (422 protected_region_invalid).
///
/// Failure modes pinned here: the box reaches the server in another shape than the card shows (x/y/w/h sent as
/// corners, or not clamped); approval is offered before the plate is marked; a clip whose original is not on this
/// iPhone shows a blank frame and lets the creator send; a refused mark loses the box or closes the card; an older
/// server hides the question.
@MainActor
final class ObjectMarkUITests: XCTestCase {
    private func launch(mode: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_MARK_OBJECT"] = mode
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
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Mark the plate on one frame")).firstMatch
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

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func waitForTheCard(_ app: XCUIApplication) {
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the question shows as the assistant's message")
        XCTAssertTrue(element(app, "object-mark-card").waitForExistence(timeout: 5))
        XCTAssertTrue(element(app, "object-mark-rect").waitForExistence(timeout: 10), "the clip's frame loaded with the box on it")
        bringIntoView(app.buttons["object-mark-send"], in: app)
    }

    private func boxValue(_ app: XCUIApplication) -> String {
        element(app, "object-mark-rect").value as? String ?? ""
    }

    func testDrawingTheBoxSendsExactlyThatBoxAndThenApprovalIsOffered() {
        let app = launch(mode: "1")
        waitForTheCard(app)
        XCTAssertFalse(element(app, "direction-stage").exists, "no approval while the plate is unmarked")
        XCTAssertEqual(boxValue(app), "0.2500,0.4000,0.7500,0.7000", "the default box, as [left, top, right, bottom]")
        XCTAssertGreaterThanOrEqual(app.buttons["object-mark-send"].frame.height, 44)
        attach(app, "Mark card")

        // Move the box down and to the left, then grow it from its corner.
        let rect = element(app, "object-mark-rect")
        let start = rect.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: -30, dy: 40)))
        let resize = element(app, "object-mark-resize")
        let corner = resize.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        corner.press(forDuration: 0.1, thenDragTo: corner.withOffset(CGVector(dx: 20, dy: 10)))
        let drawn = boxValue(app)
        XCTAssertNotEqual(drawn, "0.2500,0.4000,0.7500,0.7000", "the box moved")
        let corners = drawn.split(separator: ",").compactMap { Double($0) }
        XCTAssertEqual(corners.count, 4)
        if corners.count == 4 {
            XCTAssertLessThan(corners[0], 0.25, "moved left")
            XCTAssertGreaterThan(corners[1], 0.40, "moved down")
            XCTAssertTrue(corners.allSatisfy { (0...1).contains($0) })
            XCTAssertGreaterThan(corners[2] - corners[0], 0.02)
            XCTAssertGreaterThan(corners[3] - corners[1], 0.02)
        }
        attach(app, "Drawn mark")

        bringIntoView(app.buttons["object-mark-send"], in: app)
        app.buttons["object-mark-send"].tap()
        XCTAssertTrue(app.staticTexts["You: Marked the plate"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "mark[plate|fixture-clip|0.0|\(drawn)]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "the server received the label, the clip, the moment and the exact box the card showed")
        let answered = element(app, "object-mark-answered")
        XCTAssertTrue(answered.waitForExistence(timeout: 5), "the card collapses once the stored message echoes the mark")
        XCTAssertEqual(answered.label, "Marked the plate")
        XCTAssertFalse(app.buttons["object-mark-send"].exists)
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 10), "approval is offered after the mark")
        attach(app, "Marked, approval offered")
    }

    func testAClipThatIsNotOnThisIPhoneCannotBeMarked() {
        let app = launch(mode: "1")
        waitForTheCard(app)
        let second = app.buttons["object-mark-clip-2"]
        XCTAssertTrue(second.exists)
        second.tap()
        XCTAssertTrue(element(app, "object-mark-missing").waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["object-mark-send"].isEnabled, "nothing to draw on, nothing to send")
        app.buttons["object-mark-clip-1"].tap()
        XCTAssertTrue(element(app, "object-mark-rect").waitForExistence(timeout: 10))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: app.buttons["object-mark-send"])
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 10), .completed)
    }

    func testARefusedMarkKeepsTheCardAndTheBoxAndSaysWhy() {
        let app = launch(mode: "reject")
        waitForTheCard(app)
        let drawn = boxValue(app)
        app.buttons["object-mark-send"].tap()
        let error = element(app, "object-mark-error")
        XCTAssertTrue(error.waitForExistence(timeout: 10))
        XCTAssertEqual(error.label, "That mark is too small or outside the frame. Draw a box around the object.")
        XCTAssertEqual(boxValue(app), drawn, "the box the creator drew is kept")
        XCTAssertFalse(element(app, "direction-stage").exists)
        attach(app, "Refused mark")
        bringIntoView(app.buttons["object-mark-send"], in: app)
        app.buttons["object-mark-send"].tap()
        XCTAssertTrue(element(app, "object-mark-answered").waitForExistence(timeout: 10), "the second send goes through")
    }

    func testAnOlderServerShowsTheQuestionTextWithNoCard() {
        let app = launch(mode: "off")
        XCTAssertTrue(questionText(app).waitForExistence(timeout: 15), "the message is never hidden")
        XCTAssertFalse(element(app, "object-mark-card").exists)
    }
}
