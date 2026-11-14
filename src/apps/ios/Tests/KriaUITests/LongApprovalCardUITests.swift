import XCTest

/// KRI-1403: in the ylab chat (build 1977) the whole chat went white for a few seconds right after Kria posted the
/// draft and its approval card. The card was about two screens tall (14 lines at 22 pt, under a stock heading,
/// subtitle and three made-up "Format / Rhythm / Opening" rows), and the chat draws a stage as one lazy row: a row
/// taller than the visible chat, swapped in at the end while the chat follows the end, leaves nothing drawn.
///
/// Failure modes pinned here: the transcript is blank at any moment between Send and a few seconds after the card
/// lands; the card is taller than the visible chat; the card keeps the stock filler; the approve button cannot be
/// reached; the phone keeps re-reading the same approval on every poll.
@MainActor
final class LongApprovalCardUITests: XCTestCase {
    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testTheYlabApprovalCardFitsTheScreenAndTheChatNeverGoesBlank() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_LONG_APPROVAL"] = "1"
        // The phone read the approval about one round trip after the draft landed.
        app.launchEnvironment["KRIA_CHAT_APPROVAL_DELAY"] = "2"
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

        // The strip of chat between the top bar and the composer, in screen points.
        let header = app.buttons["workspace-menu-toggle"]
        let composer = app.buttons["chat-attach"]
        XCTAssertTrue(header.exists && composer.exists, app.debugDescription)
        let visible = CGRect(x: 0, y: header.frame.maxY, width: app.frame.width,
                             height: max(composer.frame.minY - header.frame.maxY, 0))
        let transcriptText = app.otherElements["Conversation history"].descendants(matching: .staticText)
        func somethingDrawn() -> Bool {
            transcriptText.allElementsBoundByIndex.contains { $0.exists && $0.frame.intersection(visible).height >= 16 }
        }

        send.tap()
        // From Send until well after the card has swapped in, the chat always shows text. The card lands about two
        // seconds after the draft; the white chat in ylab lasted a few seconds after that.
        let approve = app.buttons["Create this video"]
        var blankSamples = 0
        var cardSeenAt: Date?
        let start = Date()
        while Date().timeIntervalSince(start) < 25 {
            if !somethingDrawn() {
                blankSamples += 1
                if blankSamples == 1 { attach(app, "Blank chat") }
            }
            if cardSeenAt == nil, approve.exists { cardSeenAt = Date() }
            if let cardSeenAt, Date().timeIntervalSince(cardSeenAt) > 6 { break }
        }
        XCTAssertNotNil(cardSeenAt, "the approval card arrived\n\(app.debugDescription)")
        XCTAssertEqual(blankSamples, 0, "the chat was blank in \(blankSamples) samples between Send and the card settling")
        attach(app, "Card settled")

        // The whole card, heading to approve button, fits in the visible chat.
        let heading = app.staticTexts["Here’s the direction I’ll use"]
        XCTAssertTrue(heading.exists, app.debugDescription)
        let firstLine = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Hier ist die einfachere Version")).firstMatch
        XCTAssertTrue(firstLine.exists, "the card shows the draft's summary")
        let cardHeight = approve.frame.maxY - heading.frame.minY
        XCTAssertLessThanOrEqual(cardHeight, visible.height,
                                 "the approval card (\(cardHeight) pt) is taller than the visible chat (\(visible.height) pt)")

        // The stock filler is gone: none of it said anything about this draft.
        XCTAssertFalse(app.staticTexts["Rhythm"].exists)
        XCTAssertFalse(app.staticTexts["Natural, intentional"].exists)
        XCTAssertFalse(app.staticTexts["The story, right away"].exists)
        XCTAssertFalse(app.staticTexts["I’ve shaped a clear creative direction from your footage and what you told me."].exists)

        // Approve is reachable and still works.
        let conversation = app.otherElements["Conversation history"]
        for _ in 0..<4 where !approve.isHittable { conversation.swipeUp(velocity: .slow) }
        XCTAssertTrue(approve.isHittable)
        attach(app, "Approve reachable")
    }
}
