import XCTest

/// KRI-374: the song attachment step and the take-order card, driven through the fixture chat transport.
@MainActor
final class SongOrderUITests: XCTestCase {
    private func launch(songOrder: String?, media: Bool = true) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        if media { app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1" }
        app.launchEnvironment["KRIA_CHAT_SONG_ORDER"] = songOrder
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

    private func sendClips(_ app: XCUIApplication) {
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
    }

    private func bringIntoView(_ element: XCUIElement, in app: XCUIApplication) {
        let conversation = app.descendants(matching: .any)["Conversation history"].firstMatch
        for _ in 0..<8 where !element.isHittable { conversation.swipeUp(velocity: .slow) }
        XCTAssertTrue(element.isHittable, "\(element.identifier) must be on screen before it is tapped")
    }

    func testOrderCardShowsBadgesReordersAndSendsTheConfirmedOrder() {
        let app = launch(songOrder: "1")
        sendClips(app)
        let card = app.descendants(matching: .any)["song-order-card"]
        XCTAssertTrue(card.waitForExistence(timeout: 15))
        // Proposed order, with a "?" caption on the two takes Kria could not place confidently.
        let first = app.buttons["song-order-take-fixture-clip"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertTrue(first.label.contains("position 1 of 4"), first.label)
        XCTAssertTrue(app.buttons["song-order-take-fixture-clip-2"].label.contains("Could fit a few places"))
        XCTAssertTrue(app.buttons["song-order-take-fixture-clip-3"].label.contains("couldn’t place"))
        XCTAssertFalse(app.buttons["song-order-up-fixture-clip"].isEnabled, "the first take cannot move up")
        XCTAssertTrue(app.buttons["song-order-take-fixture-clip-3"].label.contains("used as filler"), "an unplaced take says what happens to it")
        XCTAssertFalse(app.buttons["song-order-up-fixture-clip-3"].exists, "an unplaced take is not reorderable")
        XCTAssertFalse(app.buttons["song-order-down-fixture-clip-3"].exists)
        XCTAssertFalse(app.buttons["song-order-reset"].exists)
        // Reorder with the arrow buttons (the accessible path): take 1 down one place.
        bringIntoView(app.buttons["song-order-down-fixture-clip"], in: app)
        app.buttons["song-order-down-fixture-clip"].tap()
        XCTAssertTrue(app.buttons["song-order-take-fixture-clip"].label.contains("position 2 of 4"))
        XCTAssertTrue(app.buttons["song-order-take-fixture-clip-2"].label.contains("position 1 of 4"))
        XCTAssertTrue(app.buttons["song-order-reset"].exists)
        // Tapping a take opens the inline preview; this fixture has no local original, so it says so.
        app.buttons["song-order-take-fixture-clip-3"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["song-order-preview"].waitForExistence(timeout: 3))
        bringIntoView(app.buttons["song-order-use"], in: app)
        app.buttons["song-order-use"].tap()
        XCTAssertTrue(app.staticTexts["You: Use this order: clips 2, 1, 3, 4"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "order[fixture-clip-2+fixture-clip+fixture-clip-3+fixture-clip-4]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "server received the confirmed order as song_order")
        XCTAssertTrue(app.descendants(matching: .any)["song-order-answered"].waitForExistence(timeout: 5), "the card collapses once answered")
        XCTAssertTrue(app.descendants(matching: .any)["song-order-answered"].label.contains("Order confirmed"), "the stored message echoed the song_order")
        XCTAssertFalse(app.buttons["song-order-use"].exists)
    }

    func testOrderQuestionFallsBackToTextWhenServerDoesNotAdvertiseIt() {
        let app = launch(songOrder: "legacy")
        sendClips(app)
        XCTAssertTrue(app.staticTexts["Kria: I couldn't place a few of your clips against the song. Check the order."].waitForExistence(timeout: 15))
        XCTAssertFalse(app.descendants(matching: .any)["song-order-card"].exists)
        XCTAssertFalse(app.buttons["song-order-use"].exists)
    }

    func testAddYourSongStepAppearsOnlyWhenTheServerAdvertisesIt() {
        let on = launch(songOrder: "1", media: false)
        let attach = on.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 10))
        attach.tap()
        XCTAssertTrue(on.buttons["attachment-next"].waitForExistence(timeout: 5), "footage then song")
        on.buttons["attachment-next"].tap()
        XCTAssertTrue(on.staticTexts["Add your song"].waitForExistence(timeout: 5))
        XCTAssertTrue(on.staticTexts["song-rights-note"].exists)
        XCTAssertTrue(on.staticTexts["Only use songs you have the rights to."].exists)
        XCTAssertTrue(on.buttons["Choose a song from Files"].exists)
        XCTAssertTrue(on.buttons["song-skip"].exists, "a song is optional")
        XCTAssertFalse(on.buttons["Choose from Photos"].exists, "a song is an audio file, not a Photos pick")
        on.terminate()

        let off = launch(songOrder: nil, media: false)
        let attachOff = off.buttons["Attach footage"]
        XCTAssertTrue(attachOff.waitForExistence(timeout: 10))
        attachOff.tap()
        XCTAssertTrue(off.buttons["attachment-next"].waitForExistence(timeout: 5))
        off.buttons["attachment-next"].tap()
        XCTAssertTrue(off.staticTexts["Add overlays"].waitForExistence(timeout: 5), "without media.song the step after footage is overlays")
        XCTAssertFalse(off.staticTexts["Add your song"].exists)
        XCTAssertFalse(off.buttons["song-skip"].exists)
    }
}
