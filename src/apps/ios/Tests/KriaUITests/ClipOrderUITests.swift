import XCTest

/// KRI-1591: the clip-order card. Under the draft message Kria shows the order the clips will play in as a numbered
/// strip; "Change order" opens a sheet to reorder them, "Use this order" sends the whole order as ONE turn.
/// KRIA_CHAT_CLIP_ORDER picks the server: "1" (4 clips), "many19" / "many40", "hidden" (the card is not shown),
/// "locked" (a video exists: read-only), "unknown" (a newer server: unknown source, missing and extra keys), "two"
/// (an older draft carries a card too), "rebase" (the first drag is not applied as sent), "error" (the confirm closes
/// the approval with an `assistant_error` that carries the card), "lost" (the first confirm never gets an answer),
/// "caps-off" (the switch is off). KRIA_CHAT_CLIP_ORDER_ECHO=1 makes the server say what a turn carried, as other
/// fixtures do, so a test can pin the wire; without it the transcript must not change at all.
///
/// Failure modes pinned here (the list lives in `ClipOrderTests`): no card / shown:false draws nothing; 19 and 40 clips
/// scroll without breaking the strip or the sheet; dragging then closing sends nothing; confirm sends exactly one
/// whole-order turn and a failed one retries with the same client event id; a rebased answer is just the server's
/// card; a locked card has no buttons; a tapped clip opens larger, and a clip whose original is gone says so without
/// crashing; no chat bubble appears for the drag or its answer; an `assistant_error` card is drawn; an older card
/// collapses; a newer server's card still reads.
@MainActor
final class ClipOrderUITests: XCTestCase {
    private func launch(mode: String, echo: Bool = false, extra: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_CLIP_ORDER"] = mode
        if echo { app.launchEnvironment["KRIA_CHAT_CLIP_ORDER_ECHO"] = "1" }
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        for (key, value) in extra { app.launchEnvironment[key] = value }
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

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.descendants(matching: .any)[id].firstMatch }

    /// The composer floats over the bottom of the conversation, so scroll until the element clears it.
    private func bringIntoView(_ target: XCUIElement, in app: XCUIApplication) {
        let conversation = element(app, "Conversation history")
        let composer = app.buttons["chat-send-message"]
        func isClearOfComposer() -> Bool { target.isHittable && (!composer.exists || target.frame.maxY <= composer.frame.minY) }
        for _ in 0..<8 where !isClearOfComposer() { conversation.swipeUp(velocity: .slow) }
        for _ in 0..<8 where !target.isHittable { conversation.swipeDown(velocity: .slow) }
        XCTAssertTrue(isClearOfComposer(), "\(target.identifier) must be on screen, above the composer")
    }

    private func tap(_ id: String, in app: XCUIApplication) {
        let button = app.buttons[id]
        XCTAssertTrue(button.waitForExistence(timeout: 10), id)
        bringIntoView(button, in: app)
        button.tap()
    }

    private func order(_ app: XCUIApplication, _ id: String = "clip-order-strip") -> [String] {
        (element(app, id).value as? String ?? "").split(separator: ",").map(String.init)
    }

    private func waitForStrip(_ app: XCUIApplication, count: Int? = nil) {
        XCTAssertTrue(element(app, "clip-order-strip").waitForExistence(timeout: 20), "the strip sits under the draft message")
        if let count { XCTAssertEqual(order(app).count, count) }
    }

    private func waitForOrder(_ app: XCUIApplication, _ expected: [String], timeout: TimeInterval = 15, _ message: String = "") {
        let predicate = NSPredicate(format: "value == %@", expected.joined(separator: ","))
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: predicate, object: element(app, "clip-order-strip"))], timeout: timeout),
                       .completed, "strip order \(order(app)) != \(expected) \(message)")
    }

    /// Assistant messages are the "Kria: ..." texts; the drag and its answer must add none.
    private func assistantMessages(_ app: XCUIApplication) -> Int {
        app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'Kria: '")).count
    }

    private func wait(_ format: String, on element: XCUIElement, timeout: TimeInterval = 10) -> Bool {
        XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: format), object: element)], timeout: timeout) == .completed
    }

    private func echo(_ app: XCUIApplication) -> XCUIElement {
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "order[turn=")).firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    /// Drags a row of the sheet's list by its reorder handle to the row `rows` places away.
    private func drag(_ app: XCUIApplication, row id: String, by rows: Int) {
        let row = element(app, "clip-order-row-\(id)")
        XCTAssertTrue(row.waitForExistence(timeout: 10), id)
        let handle = app.buttons["Reorder " + element(app, "clip-order-thumb-\(id)").label]
        XCTAssertTrue(handle.waitForExistence(timeout: 10), "the row's reorder handle")
        let from = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        from.press(forDuration: 0.4, thenDragTo: from.withOffset(CGVector(dx: 0, dy: CGFloat(rows) * 72)))
    }

    private func openSheet(_ app: XCUIApplication) {
        tap("clip-order-change", in: app)
        XCTAssertTrue(element(app, "clip-order-sheet").waitForExistence(timeout: 10))
    }

    // MARK: the flow

    func testStripThenSheetReorderConfirmsOneWholeOrderTurnAndAddsNoChatMessage() {
        let app = launch(mode: "1", echo: true)
        waitForStrip(app, count: 4)
        let ids = ["fixture-clip", "fixture-clip-2", "fixture-clip-3", "fixture-clip-4"]
        XCTAssertEqual(order(app), ids, "the decided order, in play order")
        XCTAssertEqual(element(app, "clip-order-header").label, "Order · 4 clips")
        XCTAssertEqual(element(app, "clip-order-label-fixture-clip").label, "the map video, position 1 of 4")
        XCTAssertFalse(element(app, "clip-order-moved-fixture-clip").exists, "nothing moved yet")
        XCTAssertTrue(app.buttons["clip-order-change"].exists)
        XCTAssertTrue(app.buttons["clip-order-use"].exists)
        let before = assistantMessages(app)
        attach(app, "Strip")

        openSheet(app)
        XCTAssertEqual(element(app, "clip-order-sheet-count").label, "4 clips")
        XCTAssertEqual(order(app, "clip-order-sheet-list"), ids)
        for id in ids { XCTAssertTrue(element(app, "clip-order-row-\(id)").exists, id) }
        drag(app, row: "fixture-clip", by: 2)
        let reordered = order(app, "clip-order-sheet-list")
        XCTAssertEqual(reordered.count, 4)
        XCTAssertNotEqual(reordered, ids, "dragging a row moves it")
        attach(app, "Sheet after a drag")
        XCTAssertFalse(echo(app).exists, "dragging alone sends nothing")
        app.buttons["clip-order-sheet-use"].tap()
        XCTAssertTrue(wait("exists == false", on: element(app, "clip-order-sheet"), timeout: 10),
                      "confirming closes the sheet and returns to the chat")

        waitForOrder(app, reordered)
        let sent = echo(app)
        XCTAssertTrue(sent.waitForExistence(timeout: 10))
        XCTAssertTrue(sent.label.contains("turn=1 confirm=true"), sent.label)
        XCTAssertTrue(sent.label.contains("ids=" + reordered.joined(separator: "+")), "the whole order the creator saw: \(sent.label)")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "turn=2")).firstMatch.exists == false, "one turn")
        let movedTags = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'clip-order-moved-'"))
        XCTAssertGreaterThan(movedTags.count, 0, "the clips that moved say so, from the server's card")
        XCTAssertEqual(assistantMessages(app), before + 1, "only the fixture's echo is new: the drag adds no message of its own")
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'clip_order_ops'")).count, 0, "the placeholder never shows")
        XCTAssertTrue(element(app, "direction-stage").exists, "the approval card is still there for \"Create\"")
        attach(app, "Strip after confirming")
    }

    func testConfirmingFromTheStripSendsTheWholeOrderAndLocksTheButton() {
        let app = launch(mode: "1", echo: true)
        waitForStrip(app, count: 4)
        tap("clip-order-use", in: app)
        let sent = echo(app)
        XCTAssertTrue(sent.waitForExistence(timeout: 10))
        XCTAssertTrue(sent.label.contains("turn=1 confirm=true retry=false ids=fixture-clip+fixture-clip-2+fixture-clip-3+fixture-clip-4"), sent.label)
        let use = app.buttons["clip-order-use"]
        XCTAssertTrue(wait("label == 'Order confirmed'", on: use, timeout: 10))
        XCTAssertFalse(use.isEnabled)
    }

    func testClosingTheSheetWithoutConfirmingSendsNothing() {
        let app = launch(mode: "1", echo: true)
        waitForStrip(app, count: 4)
        let ids = order(app)
        openSheet(app)
        drag(app, row: "fixture-clip-4", by: -2)
        XCTAssertNotEqual(order(app, "clip-order-sheet-list"), ids)
        app.buttons["clip-order-sheet-close"].tap()
        XCTAssertTrue(wait("exists == false", on: element(app, "clip-order-sheet"), timeout: 10))
        XCTAssertEqual(order(app), ids, "the strip keeps the order the server holds")
        sleep(2)
        XCTAssertFalse(echo(app).exists, "nothing was sent")
        // The discarded drag does not come back: the sheet opens on the strip's order.
        openSheet(app)
        XCTAssertEqual(order(app, "clip-order-sheet-list"), ids)
    }

    func testPreviewOpensLargerAndAMissingOriginalSaysSoWithoutCrashing() {
        let app = launch(mode: "1", extra: ["KRIA_CHAT_CLIP_ORDER": "1"])
        waitForStrip(app, count: 4)
        openSheet(app)
        // This clip's original is on the phone (the fixture generates one): it plays larger.
        app.buttons["clip-order-thumb-fixture-clip"].tap()
        XCTAssertTrue(element(app, "clip-order-preview").waitForExistence(timeout: 10))
        XCTAssertTrue(element(app, "clip-preview-player-fixture-clip").waitForExistence(timeout: 10))
        XCTAssertFalse(element(app, "clip-preview-not-on-device").exists)
        attach(app, "Preview of a clip on this iPhone")
        element(app, "clip-order-preview").tap()
        XCTAssertTrue(wait("exists == false", on: element(app, "clip-order-preview"), timeout: 10), "a tap leaves")
        XCTAssertTrue(element(app, "clip-order-sheet").exists, "and returns to the sheet")
        // This one is gone from the phone: the poster and a short line, never blank.
        app.buttons["clip-order-thumb-fixture-clip-2"].tap()
        XCTAssertTrue(element(app, "clip-order-preview").waitForExistence(timeout: 10))
        let note = app.staticTexts["This clip isn’t on this iPhone"]
        XCTAssertTrue(note.waitForExistence(timeout: 10))
        attach(app, "Preview of a clip that is not on this iPhone")
        app.buttons["clip-order-preview-close"].tap()
        XCTAssertTrue(wait("exists == false", on: element(app, "clip-order-preview"), timeout: 10))
        XCTAssertTrue(element(app, "clip-order-sheet").exists)
    }

    // MARK: sizes

    func testNineteenClipsScrollInTheStripAndTheSheet() {
        let app = launch(mode: "many19", echo: true)
        waitForStrip(app, count: 19)
        XCTAssertEqual(element(app, "clip-order-header").label, "Order · 19 clips")
        XCTAssertTrue(element(app, "clip-order-overflow").exists, "more than fit are summarised as +N")
        XCTAssertEqual(element(app, "clip-order-overflow").label, "13 more clips")
        openSheet(app)
        XCTAssertEqual(element(app, "clip-order-sheet-count").label, "19 clips")
        let list = element(app, "clip-order-sheet-list")
        let last = element(app, "clip-order-row-clip-19")
        for _ in 0..<8 where !last.isHittable { list.swipeUp() }
        XCTAssertTrue(last.isHittable, "the last of 19 rows is reachable by scrolling")
        XCTAssertTrue(app.buttons["clip-order-sheet-use"].isHittable, "the confirm button stays pinned")
        app.buttons["clip-order-sheet-use"].tap()
        let sent = echo(app)
        XCTAssertTrue(sent.waitForExistence(timeout: 10))
        XCTAssertEqual(sent.label.components(separatedBy: "+").count, 19, "all 19 clips go back, in order: \(sent.label)")
    }

    func testFortyClipsAtTheLargestTextKeepTheStripAndSheetUsable() {
        let app = launch(mode: "many40", echo: true, extra: ["UI_TEST_DYNAMIC_TYPE_SIZE": "accessibility5"])
        waitForStrip(app, count: 40)
        XCTAssertEqual(element(app, "clip-order-header").label, "Order · 40 clips")
        XCTAssertTrue(app.buttons["clip-order-use"].exists)
        openSheet(app)
        XCTAssertEqual(element(app, "clip-order-sheet-count").label, "40 clips")
        let list = element(app, "clip-order-sheet-list")
        let last = element(app, "clip-order-row-clip-40")
        for _ in 0..<20 where !last.isHittable { list.swipeUp() }
        XCTAssertTrue(last.isHittable)
        attach(app, "40 clips")
        app.buttons["clip-order-sheet-use"].tap()
        XCTAssertTrue(echo(app).waitForExistence(timeout: 10))
        XCTAssertEqual(echo(app).label.components(separatedBy: "+").count, 40)
    }

    // MARK: server answers

    func testARebasedAnswerShowsTheServersCardWithoutAnError() {
        let app = launch(mode: "rebase", echo: true)
        waitForStrip(app, count: 4)
        let ids = order(app)
        openSheet(app)
        drag(app, row: "fixture-clip", by: 2)
        let dragged = order(app, "clip-order-sheet-list")
        XCTAssertNotEqual(dragged, ids)
        app.buttons["clip-order-sheet-use"].tap()
        XCTAssertTrue(echo(app).waitForExistence(timeout: 10))
        waitForOrder(app, ids, timeout: 15, "the server did not apply the list as sent: its card is the truth")
        XCTAssertFalse(element(app, "clip-order-failed").exists, "a rebase is not an error")
    }

    func testALostConfirmKeepsTheLocalOrderAndRetriesTheSameRequest() {
        let app = launch(mode: "lost", echo: true)
        waitForStrip(app, count: 4)
        openSheet(app)
        drag(app, row: "fixture-clip", by: 2)
        let dragged = order(app, "clip-order-sheet-list")
        app.buttons["clip-order-sheet-use"].tap()
        XCTAssertTrue(element(app, "clip-order-failed").waitForExistence(timeout: 15), "the creator is told, with the way to retry")
        XCTAssertEqual(order(app), dragged, "the local order stays")
        XCTAssertFalse(echo(app).exists)
        let retry = app.buttons["clip-order-use"]
        XCTAssertEqual(retry.label, "Try again")
        retry.tap()
        let sent = echo(app)
        XCTAssertTrue(sent.waitForExistence(timeout: 15), "the retry reached the server (a changed body under the same id would be a 409)")
        XCTAssertTrue(sent.label.contains("retry=true"), "the same client event id: \(sent.label)")
        XCTAssertTrue(sent.label.contains("ids=" + dragged.joined(separator: "+")), sent.label)
        XCTAssertFalse(element(app, "clip-order-failed").exists)
    }

    func testAnApprovalClosedOverAnOrderChangeShowsItsMessageAndTheCard() {
        let app = launch(mode: "error")
        waitForStrip(app, count: 4)
        tap("clip-order-use", in: app)
        let message = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "so I cancelled it")).firstMatch
        XCTAssertTrue(message.waitForExistence(timeout: 15), "the message says what happened")
        XCTAssertTrue(element(app, "clip-order-strip").exists, "and the card is drawn, under it")
        XCTAssertFalse(element(app, "clip-order-failed").exists, "no error text on the card")
    }

    // MARK: other states

    func testNoStripWhenTheCardIsNotShown() {
        let app = launch(mode: "hidden")
        XCTAssertTrue(element(app, "direction-stage").waitForExistence(timeout: 20), "the approval card still shows")
        XCTAssertFalse(element(app, "clip-order-strip").exists)
        XCTAssertFalse(app.buttons["clip-order-change"].exists)
    }

    func testALockedCardIsReadOnly() {
        let app = launch(mode: "locked")
        waitForStrip(app, count: 4)
        XCTAssertFalse(app.buttons["clip-order-change"].exists, "a video exists: the timeline owns the order")
        XCTAssertFalse(app.buttons["clip-order-use"].exists)
        XCTAssertEqual(element(app, "clip-order-readonly").label, "The order that plays")
    }

    func testTheSwitchBeingOffMakesTheStripReadOnly() {
        let app = launch(mode: "caps-off")
        waitForStrip(app, count: 4)
        XCTAssertFalse(app.buttons["clip-order-use"].exists, "a confirm would not be taken")
    }

    func testOlderCardsCollapseToOneLine() {
        let app = launch(mode: "two")
        waitForStrip(app, count: 4)
        XCTAssertTrue(element(app, "clip-order-superseded").exists, "the first draft's card is a one-line summary")
        XCTAssertEqual(app.descendants(matching: .any).matching(identifier: "clip-order-strip").count, 1, "one live strip")
    }

    func testANewerServersCardStillReads() {
        let app = launch(mode: "unknown", echo: true)
        waitForStrip(app, count: 4)
        XCTAssertTrue(element(app, "clip-order-card-fixture-clip-2").exists, "an unknown source and extra keys draw like any clip")
        tap("clip-order-use", in: app)
        XCTAssertTrue(echo(app).waitForExistence(timeout: 10))
    }

    func testLabelsAreTheServersWordsNeverAnIdentifier() {
        let app = launch(mode: "1")
        waitForStrip(app, count: 4)
        func assertNoIdentifiers() {
            let texts = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH 'clip-order-'")).allElementsBoundByIndex.map(\.label)
            XCTAssertFalse(texts.isEmpty)
            for text in texts {
                XCTAssertFalse(text.contains("fixture-clip"), "an id on screen: \(text)")
                XCTAssertNil(text.range(of: #"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"#, options: .regularExpression), "a UUID on screen: \(text)")
                XCTAssertNil(text.range(of: #"\.(mp4|mov)$"#, options: .regularExpression), "a file name on screen: \(text)")
            }
        }
        assertNoIdentifiers()
        XCTAssertEqual(element(app, "clip-order-label-fixture-clip-3").label, "the sunset, position 3 of 4")
        openSheet(app)
        XCTAssertTrue(element(app, "clip-order-row-fixture-clip-3").label.hasPrefix("the sunset, position 3 of 4"))
        assertNoIdentifiers()
    }
}
