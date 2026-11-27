import XCTest

/// CA-6 (KRI-1645): the research draft asks which sourced facts stay, in the chat, before the approval card offers
/// "Create this video". KRIA_CHAT_RESEARCH_SELECTION: "1" = three sourced facts with ids, the server's real event
/// order on a narrowing; "stale" = the first answer meets a newer card; "fail-once" = the first answer is lost on the
/// network and its retry must be the same body; "long-tr" = long Turkish facts. KRIA_CHAT_SEND_DELAY holds the answer.
///
/// Failure modes pinned here: the approve action offered before the ask is answered; a tick that does not change the
/// count on the primary; none ticked sending an empty answer without the confirm; "Use all 3" writing a draft the
/// server never made; the saved choice leaving the old approval card up, or the receipt missing; a stale answer shown
/// as an error, or without the refreshed list; a retry that sends a different choice; a source host that does nothing;
/// a long Turkish fact cutting off the primary; a card that blanks the chat at the largest text size.
@MainActor
final class ResearchSelectionUITests: XCTestCase {
    private func localized(_ key: String, _ language: String?) -> String {
        guard let language else { return key }
        let catalog = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Kria/Resources/Localizable.xcstrings")
        let strings = (try? JSONSerialization.jsonObject(with: Data(contentsOf: catalog)) as? [String: Any])?["strings"] as? [String: Any]
        let unit = ((strings?[key] as? [String: Any])?["localizations"] as? [String: Any])?[language] as? [String: Any]
        return (unit?["stringUnit"] as? [String: Any])?["value"] as? String ?? key
    }

    private func launch(_ mode: String = "1", language: String? = nil, textSize: String? = nil, extra: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        if let textSize { app.launchArguments += ["-UIPreferredContentSizeCategoryName", textSize] }
        if let language { app.launchArguments += ["-AppleLanguages", "(\(language))", "-AppleLocale", "\(language)_\(language.uppercased())"] }
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_RESEARCH_SELECTION"] = mode
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launchEnvironment.merge(extra) { _, new in new }
        app.launch()
        let openProjects = app.buttons[localized("Open projects", language)]
        XCTAssertTrue(openProjects.waitForExistence(timeout: 20))
        openProjects.tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts.firstMatch.waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
        app.chooseFormat("montage")
        let send = app.buttons[localized("Send clips", language)]
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

    private func row(_ app: XCUIApplication, _ index: Int) -> XCUIElement { app.buttons["research-fact-\(index)"] }

    private func waitForTheCard(_ app: XCUIApplication) {
        XCTAssertTrue(element(app, "research-selection-card").waitForExistence(timeout: 20), "the research ask is drawn")
    }

    private func bringIntoView(_ target: XCUIElement, in app: XCUIApplication, language: String? = nil) {
        let conversation = app.descendants(matching: .any)[localized("Conversation history", language)].firstMatch
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

    func testTheAskShowsThreeFactsAndTheApprovalWaits() {
        let app = launch()
        waitForTheCard(app)
        XCTAssertTrue(app.staticTexts["Use these facts?"].exists, "the card's one heading")
        for index in 0..<3 {
            XCTAssertTrue(row(app, index).exists, "fact \(index + 1) is a row")
            XCTAssertTrue(row(app, index).isSelected, "every fact starts ticked")
        }
        XCTAssertEqual(row(app, 0).label, "Galata Tower was built in 1348 by the Genoese., On screen 0:03–0:07, britannica.com")
        XCTAssertTrue(app.buttons["Use all 3"].exists, "the primary names the count")
        XCTAssertFalse(app.buttons["Create this video"].exists, "approval waits for the ask")
        XCTAssertFalse(app.staticTexts["Sources"].exists, "the facts are drawn once")
        bringIntoView(app.buttons["Use all 3"], in: app)
        attach(app, "ca6-ask")
    }

    func testUntickingOneNamesTheCountAndSavingShowsANewApprovalCard() {
        let app = launch(extra: ["KRIA_CHAT_SEND_DELAY": "1.5"])
        waitForTheCard(app)
        row(app, 1).tap()
        XCTAssertFalse(row(app, 1).isSelected)
        XCTAssertTrue(app.buttons["Use 2 of 3"].waitForExistence(timeout: 5), "the primary names the count")
        bringIntoView(app.buttons["Use 2 of 3"], in: app)
        attach(app, "ca6-two-of-three")
        app.buttons["Use 2 of 3"].tap()
        let saving = app.buttons["Saving your choice"]
        XCTAssertTrue(saving.waitForExistence(timeout: 5), "a spinner button while the answer is in flight")
        XCTAssertFalse(app.buttons["Create this video"].exists)
        attach(app, "ca6-saving")
        XCTAssertTrue(element(app, "research-selection-receipt").waitForExistence(timeout: 15), "the answer is a receipt")
        XCTAssertEqual(element(app, "research-selection-receipt").label, "You kept 2 of 3 facts")
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 10), "the new approval card offers approval")
        XCTAssertFalse(element(app, "research-selection-card").exists, "the question is gone")
        XCTAssertTrue(app.staticTexts["“Galata Tower was built in 1348 by the Genoese.”"].exists, "the new card shows the kept fact")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "tallest structure")).firstMatch.exists, "the dropped fact is gone")
        attach(app, "ca6-approval")
    }

    func testNoneTickedAsksToContinueWithoutFacts() {
        let app = launch()
        waitForTheCard(app)
        for index in 0..<3 { row(app, index).tap() }
        XCTAssertFalse(app.buttons["Use all 3"].exists)
        XCTAssertTrue(app.buttons["Continue without facts"].waitForExistence(timeout: 5), "a waiver is its own confirm")
        XCTAssertTrue(element(app, "research-selection-notice").exists, "a slate notice names what is dropped")
        XCTAssertTrue(app.buttons["Pick facts"].exists)
        bringIntoView(app.buttons["Continue without facts"], in: app)
        attach(app, "ca6-none")
        app.buttons["Pick facts"].tap()
        XCTAssertTrue(app.buttons["Use all 3"].waitForExistence(timeout: 5), "Pick facts gives the ticks back")
        XCTAssertTrue(row(app, 0).isSelected && row(app, 2).isSelected)
        for index in 0..<3 { row(app, index).tap() }
        app.buttons["Continue without facts"].tap()
        XCTAssertTrue(element(app, "research-selection-receipt").waitForExistence(timeout: 15))
        XCTAssertEqual(element(app, "research-selection-receipt").label, "You chose Continue without facts")
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Sources"].exists, "a waived draft shows no sourced facts")
    }

    func testKeepingEveryFactIsAnsweredWithoutANewDraft() {
        let app = launch()
        waitForTheCard(app)
        app.buttons["Use all 3"].tap()
        XCTAssertTrue(element(app, "research-selection-receipt").waitForExistence(timeout: 15))
        XCTAssertEqual(element(app, "research-selection-receipt").label, "You kept all 3 facts")
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 10), "approve is offered on the card the server kept")
        XCTAssertTrue(app.staticTexts["“Galata Tower was built in 1348 by the Genoese.”"].exists, "all three facts are still on the card")
    }

    func testAStaleAnswerShowsTheRefreshedListInSlate() {
        let app = launch("stale")
        waitForTheCard(app)
        row(app, 2).tap()
        app.buttons["Use 2 of 3"].tap()
        let notice = element(app, "research-selection-notice")
        XCTAssertTrue(notice.waitForExistence(timeout: 15), "a slate notice, not a failure")
        XCTAssertFalse(element(app, "research-selection-failed").exists)
        XCTAssertTrue(app.buttons["Use all 2"].waitForExistence(timeout: 5), "the refreshed list has two facts")
        XCTAssertFalse(row(app, 2).exists)
        XCTAssertFalse(app.buttons["Create this video"].exists, "the new card is a new ask")
        bringIntoView(app.buttons["Use all 2"], in: app)
        attach(app, "ca6-stale")
        app.buttons["Use all 2"].tap()
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 15), "picking again on the new card works")
    }

    func testAFailedSendRetriesTheSameChoice() {
        let app = launch("fail-once")
        waitForTheCard(app)
        row(app, 1).tap()
        app.buttons["Use 2 of 3"].tap()
        let failed = element(app, "research-selection-failed")
        XCTAssertTrue(failed.waitForExistence(timeout: 15), "a red notice")
        XCTAssertTrue(app.buttons["Try again"].exists, "an ink Try again")
        XCTAssertFalse(row(app, 1).isSelected, "the choice is kept")
        bringIntoView(app.buttons["Try again"], in: app)
        attach(app, "ca6-fail")
        app.buttons["Try again"].tap()
        XCTAssertTrue(element(app, "research-selection-receipt").waitForExistence(timeout: 15), "the same body went through; a different one would be a 409")
        XCTAssertEqual(element(app, "research-selection-receipt").label, "You kept 2 of 3 facts")
        XCTAssertTrue(app.buttons["Create this video"].waitForExistence(timeout: 10))
    }

    func testTheSourceHostOpensInApp() {
        let app = launch()
        waitForTheCard(app)
        let host = app.buttons["research-fact-source-0"]
        XCTAssertTrue(host.exists)
        XCTAssertEqual(host.label, "Open source: britannica.com")
        bringIntoView(host, in: app)
        attach(app, "ca6-source-before")
        host.tap()
        let opened = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in app.buttons["Done"].exists || app.buttons["Close"].exists }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [opened], timeout: 15), .completed, "the source opens in an in-app Safari view")
        XCTAssertEqual(app.state, .runningForeground)
        XCTAssertTrue(row(app, 0).isSelected, "opening a source never changes the pick")
        attach(app, "ca6-source")
        (app.buttons["Done"].exists ? app.buttons["Done"] : app.buttons["Close"]).tap()
        XCTAssertTrue(element(app, "research-selection-card").waitForExistence(timeout: 10))
    }

    func testLongTurkishFactsKeepEveryControlReachable() {
        let app = launch("long-tr", language: "tr")
        waitForTheCard(app)
        XCTAssertTrue(app.staticTexts["Bu bilgiler kullanılsın mı?"].exists, "Turkish heading")
        let primary = element(app, "research-selection-use")
        XCTAssertTrue(primary.waitForExistence(timeout: 5))
        XCTAssertEqual(primary.label, "3 bilginin hepsini kullan", "the primary names the count in Turkish")
        row(app, 0).tap()
        XCTAssertEqual(primary.label, "3 bilgiden 2 tanesini kullan")
        bringIntoView(primary, in: app, language: "tr")
        XCTAssertTrue(primary.isHittable, "the primary stays reachable under three long facts")
        attach(app, "ca6-turkish")
    }

    func testLargestTextSizeKeepsTheCardScrollable() {
        let app = launch(textSize: "UICTContentSizeCategoryAccessibilityXXXL")
        waitForTheCard(app)
        row(app, 0).tap()
        let primary = element(app, "research-selection-use")
        bringIntoView(primary, in: app)
        XCTAssertTrue(primary.isHittable)
        attach(app, "ca6-largest-text")
    }
}
