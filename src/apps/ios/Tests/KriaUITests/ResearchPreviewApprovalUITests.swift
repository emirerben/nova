import XCTest

/// KRI-881: before approval the server sends the draft's sourced wording and its sources on
/// `draft_applied.research_preview`. The approval card shows them, and a source opens in an in-app Safari view.
///
/// Failure modes pinned here: the payload is ignored (sources only in reply text); a row with no public source is
/// shown as if it were sourced; an unknown key breaks decoding; a grounding redirect link shows the redirect host
/// instead of the real domain; a source tap does nothing or leaves the app; a draft WITHOUT the payload grows an
/// empty sources card; a Turkish chat gets English card words.
@MainActor
final class ResearchPreviewApprovalUITests: XCTestCase {
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
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 15), "the approval card is offered")
        return app
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    /// The composer floats over the bottom of the conversation, so scroll until the element clears it.
    private func bringIntoView(_ target: XCUIElement, in app: XCUIApplication) {
        let conversation = app.descendants(matching: .any)["Conversation history"].firstMatch
        let composer = app.buttons["chat-send-message"]
        func isClear() -> Bool {
            target.isHittable && (!composer.exists || target.frame.maxY <= composer.frame.minY)
                && target.frame.minY >= conversation.frame.minY
        }
        for _ in 0..<8 where !isClear() {
            if target.frame.minY < conversation.frame.minY { conversation.swipeDown(velocity: .slow) } else { conversation.swipeUp(velocity: .slow) }
        }
        XCTAssertTrue(isClear(), "\(target.identifier) must be on screen, above the composer")
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testSourcedWordingAndSourcesShowBeforeApprovalAndASourceOpensInApp() {
        let app = launch(["KRIA_CHAT_RESEARCH_PREVIEW": "1"])
        // DirectionStage's own identifier is inherited by everything inside it, so the card is found by its words.
        XCTAssertTrue(app.staticTexts["SOURCES"].waitForExistence(timeout: 5), "the sources card reads draft_applied.research_preview")
        XCTAssertTrue(app.staticTexts["“Galata Tower was built in 1348 by the Genoese.”"].exists, "the exact sourced wording")
        XCTAssertTrue(app.staticTexts["On screen 0:03–0:07"].exists, "where it sits in the video")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Unsourced claim")).firstMatch.exists,
                       "a row without a public source is never shown as sourced")

        let britannica = app.buttons["Open source: Galata Tower | History, britannica.com"]
        XCTAssertTrue(britannica.exists, "title and domain")
        XCTAssertTrue(app.buttons["Open source: en.wikipedia.org"].exists, "a grounding redirect shows the real domain, not vertexaisearch")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "vertexaisearch")).firstMatch.exists)
        XCTAssertTrue(app.buttons["Create this video"].exists, "approval is still offered")
        bringIntoView(britannica, in: app)
        attach(app, "Approval card with sources")

        britannica.tap()
        // SFSafariViewController's own close button: present only once the in-app Safari view is up.
        let safariDone = app.buttons["Done"].exists ? app.buttons["Done"] : app.buttons["Close"]
        let opened = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in app.buttons["Done"].exists || app.buttons["Close"].exists }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [opened], timeout: 15), .completed, "the source opens in an in-app Safari view")
        XCTAssertEqual(app.state, .runningForeground, "checking a source never leaves Kria")
        XCTAssertFalse(app.buttons["Create this video"].isHittable, "the browser covers the card")
        attach(app, "Source opened in app")
        (app.buttons["Done"].exists ? app.buttons["Done"] : safariDone).tap()
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 10))
        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "hittable == true"), object: app.buttons["Create this video"])
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 10), .completed, "closing the browser returns to the approval card")
    }

    func testADraftWithoutResearchLooksUnchanged() {
        let app = launch([:])
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["SOURCES"].exists, "no payload, no sources card")
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Open source")).firstMatch.exists)
        attach(app, "Approval card without research")
    }

    func testTurkishChatGetsTurkishCardWords() {
        let app = launch(["KRIA_CHAT_RESEARCH_PREVIEW": "1", "KRIA_CHAT_REPLY_LANGUAGE": "tr"])
        XCTAssertTrue(app.staticTexts["KAYNAKLAR"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Ekranda 0:03–0:07"].exists)
        XCTAssertTrue(app.buttons["Kaynağı aç: Galata Tower | History, britannica.com"].exists)
        XCTAssertFalse(app.staticTexts["SOURCES"].exists)
        attach(app, "Turkish approval card with sources")
    }
}
