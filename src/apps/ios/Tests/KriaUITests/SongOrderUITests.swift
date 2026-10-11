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
        // The composer floats over the bottom of the conversation, so an element can report hittable while its
        // centre still sits under the composer pill and the tap lands there. Scroll until it clears the composer.
        let composer = app.buttons["chat-send-message"]
        func isClearOfComposer() -> Bool {
            element.isHittable && (!composer.exists || element.frame.maxY <= composer.frame.minY)
        }
        for _ in 0..<8 where !isClearOfComposer() { conversation.swipeUp(velocity: .slow) }
        XCTAssertTrue(isClearOfComposer(), "\(element.identifier) must be on screen, above the composer, before it is tapped")
    }

    /// "list" = the server advertises song_order_questions but not song_order_placements: the vertical order list.
    func testOrderCardShowsBadgesReordersAndSendsTheConfirmedOrder() {
        let app = launch(songOrder: "list")
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

    private func any(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.descendants(matching: .any)[id].firstMatch }

    /// The playhead in seconds, read from the transport's "0:04.0 of 1:30" value.
    private func playhead(_ app: XCUIApplication) -> Double {
        let value = (any(app, "song-timeline-current-time").value as? String) ?? ""
        let parts = value.components(separatedBy: " ").first?.split(separator: ":") ?? []
        guard parts.count == 2, let minutes = Double(parts[0]), let seconds = Double(parts[1]) else { return -1 }
        return minutes * 60 + seconds
    }

    /// The preview's accessibility value is "Clip 1 · 0:02.0, video at 0:02.1": the chip, then the real video position.
    private func previewVideoTime(_ app: XCUIApplication) -> Double {
        let value = (any(app, "song-timeline-preview").value as? String) ?? ""
        guard let tail = value.components(separatedBy: "video at ").last, value.contains("video at ") else { return -1 }
        let parts = tail.split(separator: ":")
        guard parts.count == 2, let minutes = Double(parts[0]), let seconds = Double(parts[1]) else { return -1 }
        return minutes * 60 + seconds
    }

    private func drawerIsOpen(_ app: XCUIApplication) -> Bool { app.buttons["drawer-new-chat"].exists }

    /// KRI-561: with `song_order_placements` the same question is the song timeline (KRI-575: editor-style).
    func testTimelineFillsAnEmptySpotFromTheTrayAndSendsPlacements() {
        let app = launch(songOrder: "1")
        sendClips(app)
        XCTAssertTrue(app.descendants(matching: .any)["song-timeline-card"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.descendants(matching: .any)["song-order-card"].exists, "the timeline replaces the vertical list")
        let first = app.buttons["song-timeline-block-fixture-clip"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertTrue(first.label.contains("Clip 1, starts at 0:04"), first.label)
        XCTAssertTrue(app.buttons["song-timeline-block-fixture-clip-4"].exists)
        XCTAssertTrue(app.buttons["song-timeline-tray-fixture-clip-3"].exists, "the unplaced clip waits in the tray")
        let gap = app.buttons["song-timeline-gap-0"]
        XCTAssertTrue(gap.exists)
        XCTAssertTrue(gap.label.contains("Empty spot 0:12 to 0:22"), gap.label)
        XCTAssertEqual(app.descendants(matching: .any)["song-timeline-footer-note"].label, "Clips in the tray will fill empty spots where they fit.")
        XCTAssertFalse(app.descendants(matching: .any)["song-timeline-audio-note"].exists, "the song downloaded")
        XCTAssertFalse(app.buttons["song-timeline-reset"].exists)
        XCTAssertEqual(playhead(app), 4, accuracy: 0.05, "the first paint shows the first clip, not blank song")

        // The preview shows the clip under the playhead (the fixture gives clip 1 a real, generated original).
        let videoPreview = any(app, "song-timeline-preview")
        XCTAssertTrue(videoPreview.exists)
        XCTAssertEqual(videoPreview.label, "Preview, clip 1")
        XCTAssertTrue((videoPreview.value as? String ?? "").hasPrefix("Clip 1 · 0:00.0"), videoPreview.value as? String ?? "nil")

        // The play button plays the song from the playhead and the playhead follows the audio; pause holds it.
        let play = app.buttons["song-timeline-play"]
        bringIntoView(play, in: app)
        XCTAssertEqual(play.label, "Play song")
        play.tap()
        let moving = NSPredicate { _, _ in self.playhead(app) > 4.6 }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: moving, object: nil)], timeout: 8), .completed, "the playhead advances while the song plays")
        XCTAssertEqual(play.label, "Pause song")
        // The video follows the song: its position tracks (playhead - 4 s, the clip's start) while playing.
        let videoMoving = NSPredicate { _, _ in self.previewVideoTime(app) > 0.3 }
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: videoMoving, object: nil)], timeout: 8), .completed, "the preview video plays")
        XCTAssertEqual(previewVideoTime(app), playhead(app) - 4, accuracy: 0.6, "the video stays in step with the song")
        play.tap()
        let held = playhead(app)
        Thread.sleep(forTimeInterval: 0.7)
        XCTAssertEqual(playhead(app), held, accuracy: 0.01, "paused: the playhead stays put")
        XCTAssertEqual(play.label, "Play song")

        // Previous clip jumps back to the first clip and selects it; its actions appear.
        let previous = app.buttons["song-timeline-prev"]
        previous.tap()
        XCTAssertEqual(playhead(app), 4, accuracy: 0.05)
        let preview = app.buttons["song-timeline-action-preview"]
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        preview.tap()
        XCTAssertTrue(app.descendants(matching: .any)["song-order-preview"].waitForExistence(timeout: 5), "the take preview (or its off-device note) is shown")
        app.buttons["song-timeline-sheet-done"].tap()

        // Moving a placed clip to the tray, then Reset, brings the server's proposal back.
        let toTray = app.buttons["song-timeline-action-to-tray"]
        XCTAssertTrue(toTray.waitForExistence(timeout: 5))
        bringIntoView(toTray, in: app)
        toTray.tap()
        XCTAssertTrue(app.buttons["song-timeline-tray-fixture-clip"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["song-timeline-block-fixture-clip"].exists)
        let reset = app.buttons["song-timeline-reset"]
        XCTAssertTrue(reset.exists)
        bringIntoView(reset, in: app)
        reset.tap()
        XCTAssertTrue(app.buttons["song-timeline-block-fixture-clip"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["song-timeline-tray-fixture-clip"].exists)
        XCTAssertFalse(reset.exists)

        // Next clip: the playhead steps to the empty spot and selects it; Fill opens the tray picker.
        let next = app.buttons["song-timeline-next"]
        bringIntoView(next, in: app)
        next.tap()
        XCTAssertEqual(playhead(app), 12, accuracy: 0.05)
        XCTAssertTrue(videoPreview.label.contains("Empty spot: nothing plays here"), "an empty spot is a calm tile, not a blank: \(videoPreview.label)")
        next.tap()
        XCTAssertEqual(playhead(app), 21.5, accuracy: 0.05)
        XCTAssertTrue(videoPreview.label.contains("This clip isn't on this iPhone"), "no local original: say so instead of spinning: \(videoPreview.label)")
        XCTAssertTrue((videoPreview.value as? String ?? "").hasPrefix("Clip 2 ·"))
        app.buttons["song-timeline-prev"].tap()
        XCTAssertEqual(playhead(app), 12, accuracy: 0.05)
        let fill = app.buttons["song-timeline-action-fill"]
        XCTAssertTrue(fill.waitForExistence(timeout: 5))
        bringIntoView(fill, in: app)
        fill.tap()
        let pick = app.buttons["song-timeline-pick-fixture-clip-3"]
        XCTAssertTrue(pick.waitForExistence(timeout: 5))
        pick.tap()
        XCTAssertTrue(app.buttons["song-timeline-block-fixture-clip-3"].waitForExistence(timeout: 5), "the clip is on the song now")
        XCTAssertFalse(app.buttons["song-timeline-tray-fixture-clip-3"].exists)
        XCTAssertTrue(app.buttons["song-timeline-reset"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["song-timeline-footer-note"].label.hasPrefix("Empty spots can't be filled"), "two small spots remain and the tray is empty")

        bringIntoView(app.buttons["song-timeline-use"], in: app)
        app.buttons["song-timeline-use"].tap()
        XCTAssertTrue(app.staticTexts["You: Use this arrangement: clip 1 at 0:04, clip 3 at 0:14, clip 2 at 0:22, clip 4 at 0:52"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@",
            "order[fixture-clip+fixture-clip-3+fixture-clip-2+fixture-clip-4]",
            "placements[fixture-clip@4.0;fixture-clip-3@14.0;fixture-clip-2@21.5;fixture-clip-4@52.0]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "server received ordered_media_ids and placements")
        let answered = app.descendants(matching: .any)["song-timeline-answered"]
        XCTAssertTrue(answered.waitForExistence(timeout: 5), "the card collapses once answered")
        XCTAssertTrue(answered.label.contains("Arrangement confirmed"), answered.label)
        XCTAssertFalse(app.buttons["song-timeline-use"].exists)
    }

    /// KRI-575 (TestFlight: "sliding in the block moves the screen to open the left bar"): a sideways drag anywhere
    /// on the timeline moves the playhead and never the projects drawer; a plain swipe on a clip pans instead of
    /// moving the clip; press-and-hold lifts the clip.
    func testSlidingTheTimelineScrubsInsteadOfOpeningTheDrawerAndHoldLiftsAClip() {
        let app = launch(songOrder: "1")
        sendClips(app)
        let lanes = any(app, "song-timeline-scroll")
        XCTAssertTrue(lanes.waitForExistence(timeout: 15))
        bringIntoView(lanes, in: app)
        func point(_ x: CGFloat, _ y: CGFloat) -> XCUICoordinate { lanes.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)) }
        let rulerY: CGFloat = 0.19

        // Rightward drag across the ruler (the direction that pulled the drawer open) moves the playhead EARLIER.
        let start = playhead(app)
        XCTAssertEqual(start, 4, accuracy: 0.05)
        point(0.15, rulerY).press(forDuration: 0.05, thenDragTo: point(0.6, rulerY))
        XCTAssertFalse(drawerIsOpen(app), "a drag on the timeline must not open the projects drawer")
        XCTAssertEqual(playhead(app), 0, accuracy: 0.05, "dragging right pulls earlier song under the playhead (clamped at the start)")
        XCTAssertEqual(any(app, "song-timeline-preview").label, "Preview. Before your first clip")
        // Leftward drag moves it later.
        point(0.85, rulerY).press(forDuration: 0.05, thenDragTo: point(0.3, rulerY))
        XCTAssertFalse(drawerIsOpen(app))
        XCTAssertGreaterThan(playhead(app), 5, "dragging left seeks later in the song")
        XCTAssertEqual(any(app, "song-timeline-preview").label, "Preview, clip 1", "scrubbing changes which clip the preview shows")

        // A swipe that starts ON a clip pans; it does not move the clip and does not open the drawer.
        app.buttons["song-timeline-prev"].tap()
        let first = app.buttons["song-timeline-block-fixture-clip"]
        XCTAssertTrue(first.isHittable)
        let before = playhead(app)
        first.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.5)).press(forDuration: 0.05, thenDragTo: first.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)))
        XCTAssertFalse(drawerIsOpen(app))
        XCTAssertNotEqual(playhead(app), before, "swiping on a clip seeks")
        XCTAssertTrue(first.label.contains("starts at 0:04"), "swiping a clip must not move it: \(first.label)")

        // Press and hold, then drag: the clip lifts and moves earlier by about two seconds.
        app.buttons["song-timeline-next"].tap()
        app.buttons["song-timeline-prev"].tap()
        XCTAssertEqual(playhead(app), 4, accuracy: 0.05)
        first.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.7, thenDragTo: first.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.5)))
        XCTAssertFalse(drawerIsOpen(app))
        XCTAssertFalse(first.label.contains("starts at 0:04"), "the lifted clip moved: \(first.label)")
        XCTAssertTrue(app.buttons["song-timeline-reset"].waitForExistence(timeout: 3), "the arrangement changed")
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
