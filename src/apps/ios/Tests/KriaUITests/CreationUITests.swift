import XCTest

@MainActor
final class CreationUITests: XCTestCase {
    func testChatBubblesPreserveShapeAndWrappingAtAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat-bubbles"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        app.launch()

        let shortBubble = app.descendants(matching: .any)["chat-message-short"].firstMatch
        let longBubble = app.descendants(matching: .any)["chat-message-long"].firstMatch
        XCTAssertTrue(shortBubble.waitForExistence(timeout: 8))
        XCTAssertTrue(longBubble.waitForExistence(timeout: 3))

        let viewport = app.windows.firstMatch.frame
        XCTAssertGreaterThan(longBubble.frame.height, shortBubble.frame.height)
        XCTAssertGreaterThanOrEqual(shortBubble.frame.minX, viewport.minX + 54)
        XCTAssertGreaterThanOrEqual(longBubble.frame.minX, viewport.minX + 54)
        XCTAssertLessThanOrEqual(shortBubble.frame.maxX, viewport.maxX - 12)
        XCTAssertLessThanOrEqual(longBubble.frame.maxX, viewport.maxX - 12)
        XCTAssertEqual(shortBubble.frame.maxX, longBubble.frame.maxX, accuracy: 1)
        XCTAssertTrue(shortBubble.label.contains("Montage works."))
        XCTAssertTrue(longBubble.label.contains("ends on the wide sunset shot."))
    }

    /// KRI-120: chat text is plain SwiftUI `Text`, which can't be selected, so a
    /// long press on a prompt or a Kria reply offers Copy of the exact text.
    func testLongPressCopiesPromptsAndRepliesWithoutBlockingOptionTaps() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat-bubbles"]
        app.launch()

        let prompt = app.descendants(matching: .any)["chat-message-long"].firstMatch
        XCTAssertTrue(prompt.waitForExistence(timeout: 8))
        copy(
            prompt,
            expecting: "Make this a warm, energetic montage that starts with the arrival, keeps the candid reactions, and ends on the wide sunset shot.",
            in: app
        )
        copy(
            app.descendants(matching: .any)["chat-message-pending"].firstMatch,
            expecting: "Keep the laughter at the table.",
            in: app
        )
        let reply = app.staticTexts
            .matching(NSPredicate(format: "label == %@", "Kria: Should the edit end on the sunset or the arrival?")).firstMatch
        copy(reply, expecting: "Should the edit end on the sunset or the arrival?", in: app)

        // Tap the chip's blank trailing side, not its text: after a copy, the
        // whole outlined chip must still select.
        let option = app.buttons["End on the arrival"]
        XCTAssertTrue(option.isHittable)
        option.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)).tap()
        let selected = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == %@", "Selected: End on the arrival"),
            object: app.staticTexts["chat-bubbles-selected-option"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [selected], timeout: 3), .completed)
    }

    func testLaunchEntersChatFirstWorkspace() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        // Exercise creation on every run, even when a prior project restores.
        createFreshChat(in: app)
        let prompt = app.staticTexts["What are we making?"]
        XCTAssertTrue(prompt.waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Attach footage"].exists)
        XCTAssertFalse(app.buttons["Attach footage"].isEnabled)
        XCTAssertTrue(app.staticTexts["Montage"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Narrated"].exists)

        let toggle = app.buttons["workspace-menu-toggle"]
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 3))
        carousel.swipeLeft()
        carousel.swipeRight()
        XCTAssertEqual(toggle.label, "Open projects", "Format-card swipes must not open the drawer")
        let closedMenuX = toggle.frame.minX
        let viewport = app.windows.firstMatch.frame
        XCTAssertEqual(app.staticTexts["workspace-project-title"].frame.midX, viewport.midX, accuracy: 2)
        XCTAssertFalse(app.buttons["header-new-chat"].exists)
        toggle.tap()
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertEqual(toggle.label, "Close projects")
        XCTAssertTrue(toggle.isHittable)
        XCTAssertGreaterThan(toggle.frame.minX, closedMenuX + 200)
        XCTAssertLessThanOrEqual(toggle.frame.maxX, viewport.maxX)
        XCTAssertEqual(app.buttons.matching(identifier: "Close projects").count, 1)
        toggle.tap()
        XCTAssertTrue(eventually { toggle.label == "Open projects" }, "Toggle still reads \(toggle.label)")
        XCTAssertTrue(eventually { abs(toggle.frame.minX - closedMenuX) <= 2 }, "Toggle stayed at x=\(toggle.frame.minX)")
        let swipeStart = app.coordinate(withNormalizedOffset: CGVector(dx: 0.15, dy: 0.55))
        let swipeEnd = app.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.55))
        swipeStart.press(forDuration: 0.05, thenDragTo: swipeEnd)
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 3))
        XCTAssertEqual(toggle.label, "Close projects")
        let drawerScreenshot = XCTAttachment(screenshot: app.screenshot())
        drawerScreenshot.name = "Swipe-open tinted workspace"
        drawerScreenshot.lifetime = .keepAlways
        add(drawerScreenshot)
        swipeEnd.press(forDuration: 0.05, thenDragTo: swipeStart)
        XCTAssertTrue(eventually { toggle.label == "Open projects" }, "Toggle still reads \(toggle.label)")
        XCTAssertTrue(eventually { abs(toggle.frame.minX - closedMenuX) <= 2 }, "Toggle stayed at x=\(toggle.frame.minX)")
        toggle.tap()
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.buttons["drawer-new-chat"].exists)

        let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'" )).firstMatch
        XCTAssertTrue(gallery.exists)
        gallery.tap()
        XCTAssertTrue(app.staticTexts["Your videos and posts"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["All"].exists)
        XCTAssertTrue(app.buttons["Ready"].exists)
    }

    /// KRI-282 regression: every asset picked in the in-app gallery used to fail with "This file couldn't be
    /// read" (a `PhotosPickerItem(itemIdentifier:)` has no item provider). A gallery pick must be read from
    /// Photos and reach the upload step: the offline fixture has no upload reservations, so it surfaces as a
    /// failure NAMED after the file ("kria-outro-paper.mp4: ...") -- never the generic "Selected item".
    func testGalleryPickIsReadFromPhotosAndReachesUpload() {
        let app = XCUIApplication()
        app.resetAuthorizationStatus(for: .photos)
        app.launchArguments = ["-ui-testing-chat", "-ui-testing-seed-photo-video", "-ui-testing-seed-photo-videos"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        createFreshChat(in: app)
        let card = app.buttons["format-montage"]
        if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        card.tap()
        let chooseVideos = app.buttons["choose-videos"]
        XCTAssertTrue(chooseVideos.waitForExistence(timeout: 5))
        chooseVideos.tap()
        func allowFullAccess(_ alert: XCUIElement) -> Bool {
            guard alert.buttons.count == 3 else { return false }
            alert.buttons.element(boundBy: 1).tap()
            return true
        }
        let monitor = addUIInterruptionMonitor(withDescription: "Photos access", handler: allowFullAccess)
        defer { removeUIInterruptionMonitor(monitor) }
        let photos = app.buttons["Choose from Photos"]
        XCTAssertTrue(photos.waitForExistence(timeout: 3))
        photos.tap()
        let permission = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.firstMatch
        if permission.waitForExistence(timeout: 10) { XCTAssertTrue(allowFullAccess(permission)) }

        let tile0 = app.buttons["gallery-tile-0"], tile1 = app.buttons["gallery-tile-1"], tile2 = app.buttons["gallery-tile-2"]
        XCTAssertTrue(tile0.waitForExistence(timeout: 40), app.debugDescription)
        XCTAssertTrue(tile2.exists)
        let count = app.staticTexts["gallery-count"]
        // Apple's picker stays one tap away from the in-app gallery (and back).
        app.buttons["gallery-toggle-picker"].tap()
        XCTAssertTrue(app.scrollViews["photosView_content_scroll_view"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["photos-picker-done"].exists)
        app.buttons["gallery-toggle-picker"].tap()
        XCTAssertTrue(tile0.waitForExistence(timeout: 15), "the toggle returns to the in-app gallery")
        // Slide across three tiles: all selected, numbered in touch order.
        tile0.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(
                forDuration: 0.1,
                thenDragTo: tile2.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)),
                withVelocity: .slow,
                thenHoldForDuration: 0.1
            )
        XCTAssertTrue(eventually { count.label.hasPrefix("3 of") }, count.label)
        XCTAssertEqual(tile1.value as? String, "Selected, 2")
        // A slide that starts on a selected tile deselects.
        tile1.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(
                forDuration: 0.1,
                thenDragTo: tile2.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)),
                withVelocity: .slow,
                thenHoldForDuration: 0.1
            )
        XCTAssertTrue(eventually { count.label.hasPrefix("1 of") }, count.label)
        XCTAssertEqual(tile0.value as? String, "Selected, 1")
        // Tap toggles one tile; toggling it back leaves only the first tile picked.
        tile2.tap()
        XCTAssertTrue(eventually { count.label.hasPrefix("2 of") }, count.label)
        tile2.tap()
        XCTAssertTrue(eventually { count.label.hasPrefix("1 of") }, count.label)
        app.buttons["photos-picker-done"].tap()

        // The offline fixture has no upload reservations, so a pick that was READ from Photos fails later, at the
        // reservation decode, and surfaces that error. A pick the loader could not read never reaches upload,
        // so that text is absent and the generic "This file couldn't be read" line is shown instead.
        let reachedUpload = app.staticTexts["The data couldn\u{2019}t be read because it isn\u{2019}t in the correct format."]
        XCTAssertTrue(reachedUpload.waitForExistence(timeout: 25),
            "The gallery pick must be read from Photos and reach upload\n\(app.debugDescription)")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'This file couldn\u{2019}t be read'")).firstMatch.exists,
            "A loader failure means the Photos asset could not be read")
    }

    /// Full-access Photos returns to the attachment flow on Done. A selected
    /// talking-to-camera clip advances to overlays; Done there returns to chat.
    func testLibraryPickerReturnsToChatByDoneAndBySinglePick() {
        let app = XCUIApplication()
        app.resetAuthorizationStatus(for: .photos)
        app.launchArguments = ["-ui-testing-chat", "-ui-testing-seed-photo-video", "-ui-testing-apple-photo-picker"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        createFreshChat(in: app)
        let card = app.buttons["format-talking_to_camera"]
        if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        card.tap()
        let chooseVideos = app.buttons["choose-videos"]
        XCTAssertTrue(chooseVideos.waitForExistence(timeout: 5))
        chooseVideos.tap()
        // Limit Access… / Allow Full Access / Don't Allow, picked by position: the alert is in the
        // simulator's language, not the app's. The monitor matters: XCTest's default handler would
        // otherwise answer an alert that interrupts the tap with Don't Allow.
        func allowFullAccess(_ alert: XCUIElement) -> Bool {
            guard alert.buttons.count == 3 else { return false }
            alert.buttons.element(boundBy: 1).tap()
            return true
        }
        let monitor = addUIInterruptionMonitor(withDescription: "Photos access", handler: allowFullAccess)
        defer { removeUIInterruptionMonitor(monitor) }
        let photos = app.buttons["Choose from Photos"]
        XCTAssertTrue(photos.waitForExistence(timeout: 3))
        photos.tap()
        let permission = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.firstMatch
        if permission.waitForExistence(timeout: 10) { XCTAssertTrue(allowFullAccess(permission)) }

        let done = app.buttons["photos-picker-done"]
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        done.tap()
        XCTAssertTrue(photos.waitForExistence(timeout: 5), "Done returns to Add media")

        photos.tap()
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        let video = app.images.matching(NSPredicate(format: "label BEGINSWITH 'Video'")).firstMatch
        XCTAssertTrue(video.waitForExistence(timeout: 10), app.debugDescription)
        // PhotosUI can expose its remote image in either screen coordinates
        // or picker-local coordinates depending on the presenting container.
        // Translate only the local case; adding the host origin twice taps below
        // the thumbnail and never exercises the selection callback.
        let picker = app.scrollViews["photosView_content_scroll_view"]
        XCTAssertTrue(picker.exists)
        let videoFrame = video.frame
        let center = CGPoint(x: videoFrame.midX, y: videoFrame.midY)
        let selectionPoint = picker.frame.contains(center) ? center :
            CGPoint(x: picker.frame.minX + center.x, y: picker.frame.minY + center.y)
        XCTAssertTrue(picker.frame.contains(selectionPoint), "The selected video must be inside the visible Photos picker")
        app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: selectionPoint.x, dy: selectionPoint.y)).tap()
        // Filling the picker advances the attachment flow after dismissal.
        // Talking-to-camera has no voiceover step, so it lands on overlays.
        XCTAssertTrue(app.staticTexts["Add overlays"].waitForExistence(timeout: 10))
        XCTAssertFalse(done.exists)
        app.buttons["attachment-done"].tap()
        // This offline chat fixture does not implement upload reservations. The
        // imported asset must still be retained as a named, dismissible failure;
        // closing the picker alone would also pass if the selection were lost.
        XCTAssertTrue(app.staticTexts["footage-upload-failures-message"].waitForExistence(timeout: 10))
        chooseVideos.tap()
        XCTAssertTrue(photos.waitForExistence(timeout: 3))
        let selectedClipFailure = app.staticTexts.matching(
            NSPredicate(format: "label BEGINSWITH 'kria-outro-paper.mp4: '")
        ).firstMatch
        XCTAssertTrue(selectedClipFailure.waitForExistence(timeout: 3),
            "The selected Photos asset remains available as a named upload failure")
        let dismissFailure = app.buttons.matching(NSPredicate(
            format: "label == 'Dismiss' AND identifier != 'footage-upload-failures-dismiss'"
        )).firstMatch
        XCTAssertTrue(dismissFailure.isHittable)
        dismissFailure.tap()
        XCTAssertFalse(selectedClipFailure.exists)
        XCTAssertTrue(photos.isHittable, "The user can choose the clip again after dismissing the failure")
    }

    func testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes() {
        for runtime in ["v1", "v2"] {
            let app = XCUIApplication()
            app.launchArguments = ["-ui-testing-chat"]
            app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = runtime
            app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
            app.launch()
            createFreshChat(in: app)
            app.buttons["format-montage"].tap()
            let next = app.buttons["Send clips"]
            XCTAssertTrue(next.waitForExistence(timeout: 5))
            next.tap()
            let confirm = app.buttons["Create this video"]
            XCTAssertTrue(confirm.waitForExistence(timeout: 10))
            confirm.tap()
            XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
            app.terminate()
        }
    }

    /// KRI-306: launches a creation flow whose fixture server answers a 422 unless the app sent exactly
    /// `expect` ("<orientation>/<fit>", "none" for a key that must be absent), taps `choose` on the picker
    /// (identifiers only), creates, and passes only if the render started.
    private func createWithVideoShape(runtime: String, offered: Bool, expect: String, choose: [String] = [],
                                      file: StaticString = #filePath, line: UInt = #line) {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = runtime
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        if offered { app.launchEnvironment["KRIA_CHAT_RENDER_SHAPE"] = "1" }
        app.launchEnvironment["KRIA_CHAT_RENDER_SHAPE_EXPECT"] = expect
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5), file: file, line: line)
        next.tap()
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "\(runtime) confirm", file: file, line: line)
        let vertical = app.buttons["video-shape-orientation-portrait"]
        if offered {
            XCTAssertTrue(vertical.waitForExistence(timeout: 5), "\(runtime): the picker shows when the server offers a shape", file: file, line: line)
        } else {
            XCTAssertFalse(vertical.exists, "\(runtime): no picker without an offered shape", file: file, line: line)
        }
        if offered {
            let blackBars = app.buttons["video-shape-fit-fit"]
            XCTAssertTrue(vertical.isSelected && blackBars.isSelected, "\(runtime): seeded from the server default (Vertical + Black bars)", file: file, line: line)
            XCTAssertGreaterThanOrEqual(vertical.frame.height, 44, "44pt touch target", file: file, line: line)
            XCTAssertGreaterThanOrEqual(app.buttons["video-shape-orientation-landscape"].frame.height, 44, file: file, line: line)
            XCTAssertEqual(vertical.label, "Video shape: Vertical 9:16", file: file, line: line)
            let capture = XCTAttachment(screenshot: app.screenshot())
            capture.name = "video-shape-confirm-\(runtime)"
            capture.lifetime = .keepAlways
            add(capture)
        }
        for identifier in choose {
            let button = app.buttons[identifier]
            XCTAssertTrue(button.waitForExistence(timeout: 3), identifier, file: file, line: line)
            button.tap()
            XCTAssertTrue(button.isSelected, "\(identifier) selected after tap", file: file, line: line)
        }
        if choose.contains("video-shape-orientation-landscape") {
            XCTAssertFalse(app.buttons["video-shape-fit-fill"].exists, "Landscape always crops, so the fit row disappears", file: file, line: line)
        }
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30), "\(runtime): the app sent \(expect)", file: file, line: line)
        app.terminate()
    }

    /// KRI-306: Landscape on the confirm screen travels with the approval (v2) and the generate action (v1),
    /// and carries no fit because landscape output always crops.
    func testConfirmScreenLandscapeChoiceIsSentWithoutAFit() {
        for runtime in ["v1", "v2"] {
            createWithVideoShape(runtime: runtime, offered: true, expect: "landscape/none", choose: ["video-shape-orientation-landscape"])
        }
    }

    /// KRI-207: after a render the creator sees one chip per requirement, sees which names were
    /// guessed, and can start correcting one with a single tap.
    func testReceiptChipsAndGuessedNamesStartACorrection() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_BRIEF"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))

        // One chip per requirement, named from the brief. The chips wait for the brief before they
        // show, so there is no bare-"Done" phase; still wait on the label rather than assume timing.
        let met = app.buttons["requirement-chip-req-labels"]
        let partial = app.buttons["requirement-chip-req-order"]
        let couldNot = app.buttons["requirement-chip-req-drone"]
        XCTAssertTrue(met.waitForExistence(timeout: 10))
        func waitForLabel(_ element: XCUIElement, _ label: String) {
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", label), object: element)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed, "\(element.identifier) label: \(element.label)")
        }
        waitForLabel(met, "Done: Label each clip with its place")
        waitForLabel(partial, "Partly done: Follow the pier-to-lighthouse route")
        waitForLabel(couldNot, "Couldn’t do: End on a drone shot")

        // A chip explains itself when tapped. The transcript is still settling to the bottom as the
        // ready stage arrives, so let the chip stop moving first or the tap lands on its neighbour.
        let reason = app.staticTexts["requirement-reason-req-order"]
        XCTAssertFalse(reason.exists)
        scrollIntoView(partial, in: app)
        partial.tap()
        XCTAssertTrue(reason.waitForExistence(timeout: 3))
        XCTAssertTrue(reason.label.contains("I kept filming order"))

        // The guess that knows its clip says which; the plain one doesn't invent a number.
        let guessed = app.buttons["guessed-name-0"]
        XCTAssertTrue(guessed.waitForExistence(timeout: 3))
        XCTAssertEqual(guessed.label, "I guessed Harbor Point for clip 4")
        let plain = app.buttons["guessed-name-1"]
        XCTAssertTrue(plain.exists)
        XCTAssertEqual(plain.label, "I guessed Old Lighthouse")

        // One tap: the correction is started, the keyboard is up, and the bare stub alone is not
        // yet sendable (ChatSubmission.isBareCorrectionStub, also unit-tested in RequirementChipsTests).
        scrollIntoView(guessed, in: app)
        guessed.tap()
        let composer = app.textFields["Message Kria"]
        let started = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "Clip 4 isn't Harbor Point, it's "), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [started], timeout: 5), .completed)
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let send = app.buttons["chat-send-message"]
        XCTAssertFalse(send.isEnabled, "A bare correction stub is not a message")

        // Typing the name updates the draft, and a second tap adds to it instead of replacing it.
        composer.typeText("Besiktas")
        // KRI-222: the finished render must have settled the thinking state, so a real message is sendable.
        XCTAssertTrue(send.isEnabled, "Send must be enabled once the render has settled")
        // Dismiss the keyboard before scrolling again: it shrinks the conversation's visible band enough
        // that the next guess can sit entirely underneath it, out of any swipe's reach. The header's tap
        // gesture only clears focus, so the draft (and its appended correction) survives.
        app.staticTexts["workspace-project-title"].tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3))
        scrollIntoView(plain, in: app)
        plain.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let appended = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "Clip 4 isn't Harbor Point, it's Besiktas\nThat isn't Old Lighthouse, it's "), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [appended], timeout: 5), .completed)
    }

    func testClipsOnlySubmissionUsesSuggestAnEdit() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        XCTAssertEqual(send.label, "Send clips")
        send.tap()

        let user = app.staticTexts["You: Suggest an edit."]
        XCTAssertTrue(user.waitForExistence(timeout: 10))
    }

    /// KRI-211: one unreadable file must not hold the rest of the message hostage. The banner
    /// says it will be left out, Send stays enabled, and sending clears it.
    func testUnreadableAttachmentDoesNotBlockSend() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_UPLOAD_FAILURE"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        let banner = app.staticTexts["footage-upload-failures-message"]
        XCTAssertTrue(banner.waitForExistence(timeout: 8))
        XCTAssertEqual(banner.label, "1 file couldn’t be read and won’t be sent")
        XCTAssertTrue(app.buttons["footage-upload-failures-dismiss"].exists)

        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        XCTAssertTrue(send.isEnabled, "A failed attach must not disable Send")
        send.tap()

        XCTAssertTrue(app.staticTexts["You: Suggest an edit."].waitForExistence(timeout: 10))
        XCTAssertTrue(banner.waitForNonExistence(timeout: 5), "Sending clears that project's failures")
    }

    /// KRI-211: an upload that failed on the way (network) keeps its record so it can be retried, but it
    /// is not "in progress": Send stays enabled, and the banner says what happened, not "couldn't be read".
    func testFailedUploadWithRecordDoesNotBlockSendAndOffersRetry() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_UPLOAD_FAILURE"] = "record"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        let banner = app.staticTexts["footage-upload-failures-message"]
        XCTAssertTrue(banner.waitForExistence(timeout: 8))
        XCTAssertEqual(banner.label, "1 file didn’t upload and won’t be sent unless you retry")
        XCTAssertFalse(banner.label.contains("read"))
        XCTAssertTrue(app.buttons["footage-upload-failures-retry"].exists)

        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        XCTAssertTrue(send.isEnabled, "A failed upload that still has its record must not disable Send")
        send.tap()
        XCTAssertTrue(app.staticTexts["You: Suggest an edit."].waitForExistence(timeout: 10))
    }

    func testIncomingResponseDoesNotPullReaderFromScrolledHistory() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_LONG_HISTORY"] = "1"
        app.launchEnvironment["KRIA_CHAT_SLOW_CREATION"] = "1"
        app.launch()
        createFreshChat(in: app)
        let montage = app.buttons["format-montage"]
        // The format heading appears before the independent capabilities
        // request completes. Wait until this card accepts selection so the
        // fixture’s select_format action cannot be dropped while disabled.
        let formatReady = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in montage.isEnabled && montage.isHittable },
            object: montage
        )
        XCTAssertEqual(XCTWaiter.wait(for: [formatReady], timeout: 5), .completed)
        // With long history, the card's accessibility center can sit behind
        // the composer even though its upper portion is hittable. Tap inside
        // the visible carousel, above the composer's 19pt surrounding padding.
        let cardFrame = montage.frame
        let historyFrame = app.scrollViews.firstMatch.frame
        let composerTop = app.buttons["chat-send-message"].frame.minY - 24
        let unobscuredHistory = CGRect(x: historyFrame.minX, y: historyFrame.minY,
            width: historyFrame.width, height: max(0, composerTop - historyFrame.minY))
        let visibleCard = cardFrame.intersection(app.scrollViews["format-carousel"].frame)
            .intersection(unobscuredHistory)
        guard !visibleCard.isNull, visibleCard.width >= 20, visibleCard.height >= 20 else {
            XCTFail("Expected a visible montage card area above the composer")
            return
        }
        montage.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
            .withOffset(CGVector(dx: visibleCard.midX - cardFrame.minX,
                dy: visibleCard.minY + min(20, visibleCard.height / 2) - cardFrame.minY))
            .tap()

        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()

        // Send forces the conversation to the bottom first. Move away while the
        // delayed response is still in flight, then choose a row that is truly
        // visible after that gesture so LazyVStack has materialized it.
        let historyRows = app.staticTexts.matching(
            NSPredicate(format: "label CONTAINS 'Long conversation message number'")
        ).allElementsBoundByIndex
        app.scrollViews.firstMatch.swipeDown()
        XCTAssertTrue(app.buttons["chat-jump-to-latest"].waitForExistence(timeout: 3))
        guard let earlierReply = historyRows.first(where: { $0.isHittable }) else {
            XCTFail("Expected a long-history row to remain visible after scrolling up")
            return
        }
        let yBeforeResponse = earlierReply.frame.minY

        let response = app.staticTexts["Kria: Open on the laugh and keep the pacing quick."]
        // The reply is intentionally outside LazyVStack's visible region.
        // The persistent composer changes when the proposal projection arrives.
        let proposalArrived = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "Tell Kria what you want…"),
            object: app.textFields["Message Kria"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [proposalArrived], timeout: 12), .completed)
        XCTAssertEqual(earlierReply.frame.minY, yBeforeResponse, accuracy: 2)

        app.buttons["chat-jump-to-latest"].tap()
        let latestDismissed = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == false"),
            object: app.buttons["chat-jump-to-latest"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [latestDismissed], timeout: 3), .completed)
        XCTAssertTrue(response.waitForExistence(timeout: 3))
        XCTAssertTrue(response.isHittable)
    }

    func testTypedPromptAppearsBetweenMediaReceiptAndAssistantResponse() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        let receipt = app.descendants(matching: .any)["chat-media-fixture-clip"]
        XCTAssertTrue(receipt.waitForExistence(timeout: 5))
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        XCTAssertEqual(composer.value as? String, "Add instructions (optional)")
        composer.tap()
        composer.typeText("Make it cinematic")
        let send = app.buttons["Send message"]
        XCTAssertTrue(send.waitForExistence(timeout: 3))
        send.tap()

        let user = app.staticTexts["You: Make it cinematic"]
        let assistant = app.staticTexts["Kria: Open on the laugh and keep the pacing quick."]
        XCTAssertTrue(user.waitForExistence(timeout: 10))
        XCTAssertTrue(assistant.waitForExistence(timeout: 10))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label == %@", assistant.label)).count, 1)
        XCTAssertLessThan(receipt.frame.maxY, user.frame.minY)
        XCTAssertLessThan(user.frame.maxY, assistant.frame.minY)
    }

    // MARK: KRI-282 clip picker

    /// Runtime-v2 chat whose reply to "Send clips" is a clip question over four fixture clips
    /// (none has a cached thumbnail, so every tile uses the placeholder + "Clip N" fallback).
    private func launchClipQuestionFixture(capability: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_CLIP_QUESTION"] = capability
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    /// A history shaped like the production Olympics thread (48 phone-proxy clips, the question newest, no
    /// suggestions). `thumbs` seeds cached posters for the first 24 so both tile states show.
    private func launchRealShapeHistory(thumbs: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_CLIP_QUESTION"] = "history"
        if thumbs { app.launchEnvironment["KRIA_CHAT_CLIP_THUMBS"] = "1" }
        app.launch()
        // Same drawer path as `createFreshChat`, for a seeded thread that is already past the format prompt.
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        return app
    }

    private static let realShapeIDs = (1...48).map { String(format: "analysis-proxy-ios-%08X-0000-4000-8000-%012X.mp4", $0, $0) }

    /// Optional review screenshots: `TEST_RUNNER_KRIA_SHOT_DIR=/path xcodebuild test ...`.
    private func shoot(_ app: XCUIApplication, _ name: String) {
        guard let dir = ProcessInfo.processInfo.environment["KRIA_SHOT_DIR"] else { return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? app.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
    }

    private func openRealShapePicker() -> XCUIApplication {
        let app = launchRealShapeHistory(thumbs: true)
        XCTAssertTrue(app.buttons["clip-choose"].waitForExistence(timeout: 20))
        app.buttons["clip-choose"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        return app
    }

    private func tileButton(_ app: XCUIApplication, _ n: Int) -> XCUIElement {
        app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[n])"]
    }

    private func centre(_ element: XCUIElement) -> XCUICoordinate {
        element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
    }

    private func selectedCount(_ app: XCUIApplication) -> String {
        app.descendants(matching: .any)["clip-count"].label
    }

    /// KRI-282 follow-up: press-and-slide selects (or deselects, by the first tile) every tile it crosses, while
    /// tap and press-and-hold-to-preview keep working.
    func testClipPickerSlideAcrossTilesSelectsDeselectsAndKeepsTapAndHold() {
        let app = openRealShapePicker()
        XCTAssertTrue(tileButton(app, 2).waitForExistence(timeout: 5))
        XCTAssertEqual(selectedCount(app), "0 of 48 selected")
        // Slide across the first row from an unselected tile: selects 1-3.
        centre(tileButton(app, 0)).press(forDuration: 0.1, thenDragTo: centre(tileButton(app, 2)))
        XCTAssertTrue(eventually { selectedCount(app) == "3 of 48 selected" }, selectedCount(app))
        XCTAssertEqual(tileButton(app, 1).value as? String, "Selected")
        shoot(app, "4-slide-selected-row")
        // Starting on a SELECTED tile flips the mode: sliding 2 -> 3 deselects both.
        centre(tileButton(app, 1)).press(forDuration: 0.1, thenDragTo: centre(tileButton(app, 2)))
        _ = eventually { selectedCount(app) == "1 of 48 selected" }
        shoot(app, "4b-after-deselect-slide")
        XCTAssertEqual(selectedCount(app), "1 of 48 selected", (0...3).map { "\($0):\(tileButton(app, $0).value as? String ?? "?")" }.joined(separator: " "))
        XCTAssertEqual(tileButton(app, 0).value as? String, "Selected")
        XCTAssertEqual(tileButton(app, 2).value as? String, "Not selected")
        // A slide never leaves the sheet in a half state: tap still toggles a single tile.
        tileButton(app, 4).tap()
        XCTAssertEqual(selectedCount(app), "2 of 48 selected")
        // Hold without moving still opens the preview pager and does not select.
        tileButton(app, 5).press(forDuration: 0.8)
        XCTAssertTrue(app.descendants(matching: .any)["clip-preview-title"].waitForExistence(timeout: 5))
        app.buttons["clip-preview-close"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-count"].waitForExistence(timeout: 5))
        XCTAssertEqual(selectedCount(app), "2 of 48 selected")
        // The structured answer is still the candidate-ordered set.
        XCTAssertTrue(app.buttons["clip-done"].isEnabled)
    }

    /// A vertical drag scrolls (selects nothing); a slide that ends at the bottom edge auto-scrolls the grid and
    /// keeps selecting the tiles that pass under the finger.
    func testClipPickerSlideNearBottomEdgeAutoScrollsAndVerticalDragStillScrolls() {
        let app = openRealShapePicker()
        let grid = app.descendants(matching: .any)["clip-grid"]
        XCTAssertTrue(grid.waitForExistence(timeout: 5))
        XCTAssertTrue(tileButton(app, 0).waitForExistence(timeout: 5))
        // Vertical swipe scrolls and selects nothing.
        let firstBefore = tileButton(app, 0).frame.minY
        let gridFrame = grid.frame
        let top = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: gridFrame.midX + 60, dy: gridFrame.maxY - 80))
        let bottomUp = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: gridFrame.midX + 60, dy: gridFrame.minY + 80))
        top.press(forDuration: 0.05, thenDragTo: bottomUp, withVelocity: .slow, thenHoldForDuration: 0.1)
        XCTAssertEqual(selectedCount(app), "0 of 48 selected", "a vertical drag must scroll, not select")
        shoot(app, "5a-after-vertical")
        XCTAssertTrue(eventually { tileButton(app, 0).frame.minY < firstBefore - 40 }, "grid scrolled \(firstBefore) -> \(tileButton(app, 0).frame.minY)")
        bottomUp.press(forDuration: 0.05, thenDragTo: top, withVelocity: .slow, thenHoldForDuration: 0.1)
        XCTAssertEqual(selectedCount(app), "0 of 48 selected")
        // Slide sideways in the bottom edge zone and hold there: the grid auto-scrolls to the end.
        let frame = grid.frame
        let y = frame.maxY - 24
        let origin = app.coordinate(withNormalizedOffset: .zero)
        let start = origin.withOffset(CGVector(dx: frame.minX + 40, dy: y))
        let finish = origin.withOffset(CGVector(dx: frame.maxX - 30, dy: y))
        start.press(forDuration: 0.1, thenDragTo: finish, withVelocity: .slow, thenHoldForDuration: 6)
        shoot(app, "5-slide-autoscroll")
        XCTAssertTrue(eventually { self.selectedCount(app) != "0 of 48 selected" })
        let count = Int(selectedCount(app).split(separator: " ").first ?? "0") ?? 0
        XCTAssertGreaterThan(count, 8, "auto-scroll kept selecting as rows passed under the finger: \(count)")
    }

    /// KRI-282 root-cause pin: the production-shaped question must always produce the card, never just text.
    func testRealShapeHistoryShowsClipQuestionCardAndOpensTheGridSheet() {
        let app = launchRealShapeHistory(thumbs: true)
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Tap the clips that do")).firstMatch.waitForExistence(timeout: 20))
        XCTAssertTrue(app.descendants(matching: .any)["clip-card"].waitForExistence(timeout: 10), "question text shown but no picker")
        XCTAssertEqual(app.staticTexts["clip-progress-group:dodgeball"].label, "0 of 48 selected")
        shoot(app, "1-chat-card")
        app.buttons["clip-choose"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        let first = app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[0])"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertEqual(first.value as? String, "Not selected")
        first.tap()
        XCTAssertEqual(first.value as? String, "Selected")
        shoot(app, "2a-sheet-open")
        app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[1])"].tap()
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "2 of 48 selected")
        shoot(app, "2-sheet-selected")
        // A tile with a cached poster has no placeholder.
        XCTAssertFalse(app.descendants(matching: .any)["clip-placeholder-\(Self.realShapeIDs[0])"].exists)
        // 1-24 have cached posters; 25+ do not. Scroll the grid until a placeholder tile appears, then back.
        let thumbGrid = app.scrollViews["clip-grid"].firstMatch
        let placeholder = app.descendants(matching: .any)["clip-placeholder-\(Self.realShapeIDs[30])"]
        for _ in 0..<8 where !placeholder.exists { thumbGrid.swipeUp() }
        XCTAssertTrue(placeholder.exists, "a clip with no cached poster is a labelled placeholder")
        XCTAssertEqual(app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[30])"].label, "Clip 31")
        shoot(app, "5-placeholders")
        let tileTwo = app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[2])"]
        for _ in 0..<12 where !tileTwo.isHittable { thumbGrid.swipeDown() }
        // Select all / Clear.
        app.buttons["clip-select-all"].tap()
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "48 of 48 selected")
        app.buttons["clip-clear"].tap()
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "0 of 48 selected")
        XCTAssertFalse(app.buttons["clip-done"].isEnabled, "needs a decision")
        // Press-and-hold opens the pager at that clip; swipe sideways moves through the same list.
        app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[2])"].press(forDuration: 0.8)
        let title = app.descendants(matching: .any)["clip-preview-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 5))
        XCTAssertEqual(title.label, "Clip 3 of 48")
        shoot(app, "3-preview-pager")
        app.descendants(matching: .any)["clip-preview-pager"].swipeLeft()
        XCTAssertTrue(eventually { title.label == "Clip 4 of 48" })
        shoot(app, "3b-preview-next")
        app.buttons["clip-preview-toggle"].tap()
        XCTAssertEqual(app.buttons["clip-preview-toggle"].value as? String, "Selected")
        app.descendants(matching: .any)["clip-preview-pager"].swipeRight()
        XCTAssertTrue(eventually { title.label == "Clip 3 of 48" })
        XCTAssertEqual(app.buttons["clip-preview-toggle"].value as? String, "Not selected", "the toggle follows the clip shown")
        app.buttons["clip-preview-close"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-count"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "1 of 48 selected", "selecting from the preview counts")
        let done = app.buttons["clip-done"]
        XCTAssertTrue(done.isEnabled)
        done.tap()
        // Collapsed: "Dodgeball: 1 clip" (the structured answer went out as the user's message).
        XCTAssertTrue(app.descendants(matching: .any)["clip-card-answered"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["clip-choose"].exists)
        shoot(app, "4-collapsed")
    }

    func testClipPickerSelectsClipsAndSendsStructuredAnswerWithMissingThumbnailFallback() {
        let app = launchClipQuestionFixture(capability: "1")
        let card = app.descendants(matching: .any)["clip-card"]
        XCTAssertTrue(card.waitForExistence(timeout: 15))
        XCTAssertEqual(app.staticTexts["clip-progress-dodgeball"].label, "Dodgeball · 1 of 3 selected", "the suggestion counts")
        app.buttons["clip-choose"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        // Missing thumbnail cache: placeholder tile labelled "Clip N", never a blank or a crash.
        let suggested = app.buttons["clip-thumb-dodgeball-fixture-clip-2"]
        XCTAssertTrue(suggested.waitForExistence(timeout: 3))
        XCTAssertTrue(suggested.label.hasPrefix("Clip 2"))
        XCTAssertEqual(suggested.value as? String, "Selected", "suggested clips start ticked")
        let first = app.buttons["clip-thumb-dodgeball-fixture-clip"]
        first.tap()
        XCTAssertEqual(first.value as? String, "Selected")
        first.tap()
        XCTAssertEqual(first.value as? String, "Not selected", "tapping twice unselects")
        first.tap()
        // Second group via the segmented control.
        app.segmentedControls["clip-category-picker"].buttons.element(boundBy: 1).tap()
        let football = app.buttons["clip-thumb-football-fixture-clip-4"]
        XCTAssertTrue(football.waitForExistence(timeout: 3))
        football.tap()
        XCTAssertTrue(app.buttons["clip-done"].isEnabled)
        app.buttons["clip-done"].tap()
        XCTAssertTrue(app.staticTexts["You: Dodgeball: clips 1, 2. Football: clip 4"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "dodgeball=fixture-clip+fixture-clip-2;football=fixture-clip-4")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "server received the structured clip_selection")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "skipped[false]")).firstMatch.exists)
        // Answered: the card collapses to a read-only summary.
        XCTAssertTrue(app.descendants(matching: .any)["clip-card-answered"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.descendants(matching: .any)["clip-card-answered"].label, "Dodgeball: 2 clips · Football: 1 clip")
        XCTAssertFalse(app.buttons["clip-choose"].exists)
    }

    // MARK: conflict-choice question (KRI-282)

    private func launchChoiceQuestionFixture(capability: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_CHOICE_QUESTION"] = capability
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    func testChoiceQuestionShowsRecommendedOptionAndSendsStructuredAnswerOnce() {
        let app = launchChoiceQuestionFixture(capability: "1")
        XCTAssertTrue(app.descendants(matching: .any)["choice-card"].waitForExistence(timeout: 15))
        let grouped = app.buttons["choice-option-group_first"]
        let chronological = app.buttons["choice-option-chronological"]
        XCTAssertTrue(grouped.waitForExistence(timeout: 3))
        XCTAssertTrue(chronological.exists)
        XCTAssertEqual(grouped.label, "Group by sport, chronological inside each sport (recommended)")
        XCTAssertEqual(chronological.label, "Keep it strictly chronological; sports may interleave")
        XCTAssertGreaterThanOrEqual(grouped.frame.height, 44)
        XCTAssertGreaterThanOrEqual(chronological.frame.height, 44)
        grouped.tap()
        XCTAssertTrue(app.staticTexts["You: Group by sport, chronological inside each sport"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "choice[group_first]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "server received the structured choice_selection")
        // Answered: the card collapses to a read-only summary and the options are gone.
        XCTAssertTrue(app.descendants(matching: .any)["choice-card-answered"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["choice-option-group_first"].exists)
        XCTAssertFalse(app.buttons["choice-option-chronological"].exists)
    }

    func testSlowDirectionAndPreJobFailureNeverReturnToUploading() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_SLOW_CREATION"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        XCTAssertTrue(app.descendants(matching: .any)["chat-thinking"].waitForExistence(timeout: 3))
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        let retry = app.buttons["Retry generation"]
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Continue with 1 clip"].exists)
        XCTAssertTrue(app.staticTexts["Kria couldn’t start the video. Your direction and footage are still saved."].exists)
        retry.tap()
        XCTAssertTrue(app.staticTexts["Preparing your footage and edit"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Continue with 1 clip"].exists)
        XCTAssertFalse(app.buttons["Create this video"].exists)
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
    }

    func testStaleFootageConflictShowsServerReasonAndRefreshesTheDirection() {
        let app = launchConfirmationFixture(["KRIA_CHAT_GENERATE_CONFLICT": "stale_manifest"])
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()

        let reason = app.staticTexts["Footage or capabilities changed; review the plan again."]
        XCTAssertTrue(reason.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["This project changed. Review the latest options and try again."].exists)
        let refresh = app.buttons["Refresh the direction"]
        XCTAssertTrue(refresh.waitForExistence(timeout: 3))
        XCTAssertTrue(confirm.exists, "The create button stays available next to the server's reason")
        let conflictScreenshot = XCTAttachment(screenshot: app.screenshot())
        conflictScreenshot.name = "Confirmation conflict with refresh action"
        conflictScreenshot.lifetime = .keepAlways
        add(conflictScreenshot)

        refresh.tap()
        let refreshMessage = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", "You: Keep the same plan with my current footage")).firstMatch
        XCTAssertTrue(refreshMessage.waitForExistence(timeout: 10))
        let noticeGone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: refresh)
        XCTAssertEqual(XCTWaiter.wait(for: [noticeGone], timeout: 10), .completed)
        XCTAssertFalse(reason.exists)

        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
    }

    func testWaitForRenderConflictKeepsCreateRetryable() {
        let app = launchConfirmationFixture(["KRIA_CHAT_GENERATE_CONFLICT": "wait_for_render"])
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()

        XCTAssertTrue(app.staticTexts["Wait for the current render before confirming."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Refresh the direction"].exists)
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: confirm)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 10), .completed)
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
    }

    /// 2026-09-16 regression: an HTTP 500 from "Create this video" was shown as
    /// "Connection interrupted" with Reconnect while the connection was fine.
    func testCreateServerErrorIsNotShownAsAConnectionProblem() {
        let app = launchConfirmationFixture(["KRIA_CHAT_GENERATE_FAILURE": "server_error"])
        let message = createVideoFailureMessage(in: app, screenshot: "Create this video after HTTP 500")

        XCTAssertEqual(message.label, "That change wasn’t saved. Kria hit a problem on its side. Your chat and footage are safe. Try again in a moment.")
        XCTAssertTrue(app.staticTexts["Something went wrong"].exists)
        XCTAssertFalse(app.staticTexts["Connection interrupted"].exists)
        XCTAssertFalse(app.buttons["Reconnect"].exists)
        retryCreateVideo(in: app, using: app.buttons["Refresh"], dismissing: message)
    }

    func testCreateWithoutAConnectionKeepsConnectionRecovery() {
        let app = launchConfirmationFixture(["KRIA_CHAT_GENERATE_FAILURE": "offline"])
        let message = createVideoFailureMessage(in: app, screenshot: "Create this video without a connection")

        XCTAssertEqual(message.label, "That change wasn’t saved. Check your connection and try again.")
        XCTAssertTrue(app.staticTexts["Connection interrupted"].exists)
        XCTAssertFalse(app.staticTexts["Something went wrong"].exists)
        XCTAssertFalse(app.buttons["Refresh"].exists)
        retryCreateVideo(in: app, using: app.buttons["Reconnect"], dismissing: message)
    }

    func testExpiredApprovalCannotStartGeneration() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_EXPIRED_APPROVAL"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
            let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        XCTAssertTrue(app.staticTexts["This approval expired. Send a message to request an updated direction."].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Create this video"].exists)
    }

    func testUnavailableCapabilitiesShowRetryInsteadOfDeadFormatCards() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launch()
        createFreshChat(in: app)
        // The unconfigured fixture answers every request with HTTP 503: the
        // server was reached, so recovery must not blame the connection.
        XCTAssertTrue(app.staticTexts["Kria couldn’t load creation options. Kria hit a problem on its side. Your chat and footage are safe. Try again in a moment."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["format-montage"].exists)
        XCTAssertTrue(app.staticTexts["Something went wrong"].exists)
        XCTAssertTrue(app.buttons["Refresh"].firstMatch.exists)
        XCTAssertFalse(app.buttons["Reconnect"].exists)
    }

    func testTappingOutsideComposerDismissesKeyboardWithoutBlockingFirstTap() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        createFreshChat(in: app)

        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))

        app.staticTexts["What are we making?"].tap()
        let keyboardGone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.keyboards.firstMatch)
        XCTAssertEqual(XCTWaiter.wait(for: [keyboardGone], timeout: 5), .completed)

        // A tap that dismisses the keyboard must not eat the first tap on a
        // transcript control — the format card should still select immediately.
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let card = app.buttons["format-montage"]
        if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        card.tap()
        XCTAssertTrue(app.buttons["choose-videos"].waitForExistence(timeout: 5))
    }

    /// Long-presses a chat message, taps Copy, and pastes into the bubble
    /// fixture's paste control to check the exact copied text.
    private func copy(_ message: XCUIElement, expecting text: String, in app: XCUIApplication) {
        XCTAssertTrue(message.waitForExistence(timeout: 3))
        message.press(forDuration: 1)
        let copy = app.buttons["Copy"]
        XCTAssertTrue(copy.waitForExistence(timeout: 3))
        copy.tap()
        let menuGone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: copy)
        XCTAssertEqual(XCTWaiter.wait(for: [menuGone], timeout: 3), .completed)

        app.buttons["Paste"].tap()
        let pasted = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == %@", "Pasted: \(text)"),
            object: app.staticTexts["chat-bubbles-pasted"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [pasted], timeout: 3), .completed)
    }

    /// Taps in the blank band above and below a suggestion chip's label, still
    /// inside its 44pt capsule, must apply the suggestion. Plain-style buttons
    /// can leave that band dead, so this guards the chip's explicit capsule
    /// content shape.
    func testReadySuggestionChipsApplyFromTapsNearCapsuleEdges() {
        let app = launchConfirmationFixture([:])
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))

        let composer = app.textFields["Message Kria"]
        for (suggestion, dy) in [("Try a stronger opening", 0.15), ("Make it warmer", 0.85)] {
            let chip = app.buttons["Use suggestion: \(suggestion)"]
            XCTAssertTrue(chip.waitForExistence(timeout: 5))
            XCTAssertTrue(chip.isHittable)
            XCTAssertGreaterThanOrEqual(chip.frame.height, 44)
            chip.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: dy)).tap()
            let applied = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", suggestion), object: composer)
            XCTAssertEqual(XCTWaiter.wait(for: [applied], timeout: 3), .completed, "Edge tap on '\(suggestion)' was ignored")
        }
    }

    /// Taps "Create this video" and waits for the recovery card's message,
    /// attaching a screenshot of the card.
    private func createVideoFailureMessage(in app: XCUIApplication, screenshot name: String) -> XCUIElement {
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        let message = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "That change wasn’t saved.")).firstMatch
        XCTAssertTrue(message.waitForExistence(timeout: 10))
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = name
        screenshot.lifetime = .keepAlways
        add(screenshot)
        return message
    }

    /// The card's button reloads the chat, after which creating again succeeds.
    private func retryCreateVideo(in app: XCUIApplication, using retry: XCUIElement, dismissing message: XCUIElement) {
        XCTAssertTrue(retry.exists)
        retry.tap()
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: message)
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 10), .completed)
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
    }

    /// Runtime-v1 chat with one fixture clip, stopped at the confirmation card.
    private func launchConfirmationFixture(_ environment: [String: String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment.merge(environment) { _, new in new }
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    /// The transcript settles at the bottom (the ready card), which can leave earlier rows under the
    /// header, and the composer's keyboard shrinks the usable viewport further once it is up. Drag the
    /// conversation in whichever direction is actually needed until the element is really tappable, then
    /// let it stop moving.
    private func scrollIntoView(_ element: XCUIElement, in app: XCUIApplication) {
        let conversation = app.descendants(matching: .any)["Conversation history"].firstMatch
        // A row scrolled under the header (or the keyboard) still reports itself hittable but something
        // else takes the tap, so "on screen" means inside the conversation's own current frame.
        func inViewport() -> Bool {
            element.frame.minY >= conversation.frame.minY + 8 && element.frame.maxY <= conversation.frame.maxY - 8
        }
        for _ in 0..<10 where !inViewport() {
            // Above the visible band: bring earlier content down. Below it (including "hidden behind
            // the keyboard"): bring later content up. `swipeDown()`/`swipeUp()` name the drag gesture,
            // which moves the content the opposite way from the reveal it produces.
            if element.frame.minY < conversation.frame.minY {
                conversation.swipeDown(velocity: .slow)
            } else {
                conversation.swipeUp(velocity: .slow)
            }
        }
        var last = element.frame
        var stableSince = Date()
        let deadline = Date().addingTimeInterval(6)
        while Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.15))
            let frame = element.frame
            if frame != last {
                last = frame
                stableSince = Date()
            } else if Date().timeIntervalSince(stableSince) > 0.8 {
                break
            }
        }
        XCTAssertTrue(inViewport() && element.isHittable, "\(element.identifier) must be on screen before it is tapped")
    }

    // MARK: KRI-443 live plan feed

    /// Launches the v2 creation flow with the staged `plan_block` fixture and taps Create, leaving the app
    /// on the live feed.
    private func launchLivePlanFeed(
        reduceMotion: Bool, reduceTransparency: Bool = false, cancel: String? = nil, capability: Bool = true,
        device: Bool = false, followup: Bool = false, contractV2: Bool = false, review: Bool = false
    ) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        if capability { app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS"] = "1" }
        // Live-plan contract v2: structured payloads, 8 sections (post_caption), Review entry points.
        if contractV2 || review { app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS_VERSION"] = "2" }
        // The contract-v2 server model behind the Review sheet: GET /plan, scoped turns, per-section undo.
        if review { app.launchEnvironment["KRIA_CHAT_PLAN_REVIEW"] = "1" }
        if followup { app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS_FOLLOWUP"] = "1" }
        if device {
            app.launchEnvironment["KRIA_CHAT_DEVICE_RENDER"] = "ready"
            app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] = "1"
        }
        if reduceMotion { app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1" }
        if reduceTransparency { app.launchEnvironment["UI_TEST_REDUCE_TRANSPARENCY"] = "1" }
        if let cancel { app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS_CANCEL"] = cancel }
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        return app
    }

    private func planBlockValue(_ app: XCUIApplication, _ section: String) -> String {
        (app.buttons["plan-feed.block.\(section)"].value as? String) ?? ""
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let capture = XCTAttachment(screenshot: app.screenshot())
        capture.name = name
        capture.lifetime = .keepAlways
        add(capture)
    }

    func testLivePlanFeedFullSequence() {
        let app = launchLivePlanFeed(reduceMotion: false)
        let feed = app.otherElements["plan-feed"]
        XCTAssertTrue(feed.waitForExistence(timeout: 30), "the feed replaces the thinking and rendering wait")
        XCTAssertFalse(app.staticTexts["I’m shaping your video"].exists)
        // Waiting sections show as chips under "Still to decide".
        XCTAssertTrue(app.staticTexts["plan-feed.waiting-heading"].waitForExistence(timeout: 10))
        XCTAssertTrue(eventually(timeout: 10) { app.descendants(matching: .any)["plan-feed.chip.look"].exists || self.planBlockValue(app, "look") != "" })
        attach(app, "plan-feed-waiting")

        // Title decides first and is expanded as the newest; clips then takes over and title collapses.
        XCTAssertTrue(eventually(timeout: 30) { self.planBlockValue(app, "title").hasPrefix("decided") })
        XCTAssertTrue(eventually(timeout: 30) { self.planBlockValue(app, "clips").hasPrefix("decided") })
        XCTAssertTrue(eventually(timeout: 10) { !app.descendants(matching: .any)["plan-feed.change.title"].exists },
                      "an older decision collapses to a one-line row")
        attach(app, "plan-feed-midway")
        app.buttons["plan-feed.block.title"].tap()
        XCTAssertTrue(eventually(timeout: 5) { app.descendants(matching: .any)["plan-feed.change.title"].exists }, "tapping a collapsed row expands it")
        XCTAssertFalse(app.descendants(matching: .any)["plan-feed.change.title"].isEnabled && false)
        app.buttons["plan-feed.block.title"].tap()
        XCTAssertTrue(eventually(timeout: 5) { !app.descendants(matching: .any)["plan-feed.change.title"].exists })

        // Everything decided: progress, then the Review plan entry that expands every block.
        let title = app.staticTexts["plan-feed.title"]
        XCTAssertTrue(eventually(timeout: 60) { title.label == "Plan ready · 7 of 7 decided" })
        XCTAssertEqual(app.otherElements["plan-feed.progress"].value as? String ?? app.descendants(matching: .any)["plan-feed.progress"].value as? String, "7 of 7 decided")
        let review = app.buttons["plan-feed.review"]
        XCTAssertTrue(review.waitForExistence(timeout: 5))
        XCTAssertTrue(planBlockValue(app, "overlays").contains("Not used"), "a skipped section counts as decided")
        review.tap()
        for section in ["title", "clips", "captions", "music", "sfx", "look"] {
            XCTAssertTrue(app.descendants(matching: .any)["plan-feed.change.\(section)"].waitForExistence(timeout: 5), "\(section) expanded in review")
        }
        XCTAssertFalse(app.descendants(matching: .any)["plan-feed.change.overlays"].exists, "a section that is not used has nothing to change")
        XCTAssertFalse(app.buttons["plan-feed-review-cta"].exists, "an older server has no Review view, so no Review CTA")
        attach(app, "plan-feed-review")
        // ReadyStage is unchanged once the render finishes.
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 60))
        XCTAssertFalse(feed.exists)
        // Feed only (no contract v2): no Review CTA in the feed and no Review row under the finished video.
        XCTAssertFalse(app.buttons["plan-feed-review-cta"].exists)
        XCTAssertFalse(app.buttons["project-review-entry"].waitForExistence(timeout: 3))
    }

    /// Contract v2: structured payloads render rich cards, a malformed payload falls back to its summary, the 8th
    /// section (post caption) shows, and the finished feed offers the ink + butter "Review your video" CTA.
    func testLivePlanFeedRendersStructuredPayloadsAndOffersReview() {
        let app = launchLivePlanFeed(reduceMotion: true, contractV2: true)
        let feed = app.otherElements["plan-feed"]
        XCTAssertTrue(feed.waitForExistence(timeout: 30))
        XCTAssertTrue(app.staticTexts["Planning your video"].waitForExistence(timeout: 10))
        let stop = app.buttons["plan-feed-stop"]
        XCTAssertTrue(stop.waitForExistence(timeout: 10))
        XCTAssertGreaterThanOrEqual(stop.frame.height, 44)
        XCTAssertFalse(app.buttons["plan-feed-review-cta"].exists, "no Review CTA while sections are still being decided")
        XCTAssertFalse(app.buttons["Create this video"].exists || app.staticTexts["Create this video"].exists, "Create already happened")
        XCTAssertTrue(eventually(timeout: 20) { self.planBlockValue(app, "title") == "deciding" || self.planBlockValue(app, "title").hasPrefix("decided") })
        attach(app, "plan-feed-v2-deciding")
        XCTAssertTrue(eventually(timeout: 30) { self.planBlockValue(app, "clips").hasPrefix("decided") })
        attach(app, "plan-feed-v2-clips")

        let title = app.staticTexts["plan-feed.title"]
        XCTAssertTrue(eventually(timeout: 90) { title.label == "Plan ready · 8 of 8 decided" }, "post_caption is the 8th section")
        XCTAssertEqual(app.descendants(matching: .any)["plan-feed.progress"].value as? String, "8 of 8 decided")
        XCTAssertTrue(app.staticTexts["Your plan is ready"].exists)
        XCTAssertTrue(self.planBlockValue(app, "sfx").contains("4 sound effects"), "a malformed payload falls back to the summary")
        XCTAssertTrue(self.planBlockValue(app, "post_caption").contains("Slow summer days in Bodrum"))

        let review = app.buttons["plan-feed-review-cta"]
        XCTAssertTrue(review.waitForExistence(timeout: 5))
        XCTAssertGreaterThanOrEqual(review.frame.height, 48)
        XCTAssertFalse(app.buttons["plan-feed.review"].exists, "v2 replaces the expand-all toggle with the Review CTA")
        attach(app, "plan-feed-v2-ready")

        // Opening a collapsed row shows its structured content.
        app.buttons["plan-feed.block.captions"].tap()
        let lines = app.descendants(matching: .any)["plan-feed.captions.captions"]
        XCTAssertTrue(lines.waitForExistence(timeout: 5))
        XCTAssertTrue(lines.label.contains("Slow mornings."))
        XCTAssertTrue(app.descendants(matching: .any)["plan-feed.change.captions"].isEnabled, "Change is live on a v2 server")
        attach(app, "plan-feed-v2-captions-open")
        XCTAssertFalse(app.descendants(matching: .any)["plan-feed.change.post_caption"].exists, "a post caption is display only")
    }

    func testReadyVideoFollowupWaitsWithoutReplayingPriorPlan() {
        let app = launchLivePlanFeed(reduceMotion: true, followup: true)
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 100))
        XCTAssertFalse(app.otherElements["plan-feed"].exists)
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Add a new title Lisbon. Animate it")
        app.buttons["Send message"].tap()
        let thinking = app.descendants(matching: .any)["chat-thinking"]
        XCTAssertTrue(thinking.waitForExistence(timeout: 5))
        XCTAssertFalse(app.otherElements["plan-feed"].exists, "the old render plan must not become the edit's waiting row")
        // The fixture delivers an old render event before the current edit's response.
        XCTAssertTrue(eventually(timeout: 5) { app.staticTexts["Kria: Late prior render event"].exists })
        XCTAssertTrue(thinking.exists, "a late event from the prior turn must not end the edit wait")
        XCTAssertFalse(app.otherElements["plan-feed"].exists)
        XCTAssertTrue(app.staticTexts["Kria: Review the Lisbon title edit before saving."].waitForExistence(timeout: 20))
        XCTAssertTrue(eventually(timeout: 5) { !thinking.exists })
        XCTAssertFalse(app.otherElements["plan-feed"].exists)
        attach(app, "ready-video-editor-followup")
    }

    /// iPhone accounts: the server plans in ~1s (all decided) and the phone builds the video. The feed must stay
    /// on screen for the whole device build, advance with the phone's real stages, and Stop must cancel the
    /// device render (the server cancel route 409s for those).
    func testLivePlanFeedStaysLiveAcrossTheDeviceBuild() {
        let app = launchLivePlanFeed(reduceMotion: true, device: true)
        let feed = app.otherElements["plan-feed"]
        XCTAssertTrue(feed.waitForExistence(timeout: 30))
        XCTAssertFalse(app.descendants(matching: .any)["device-render-status"].firstMatch.exists, "the feed hosts the device build")
        let title = app.staticTexts["plan-feed.title"]
        attach(app, "plan-feed-device-start")

        // Work order the device follows. A section may only show decided once every earlier one has.
        let order = ["clips", "music", "title", "captions", "look", "sfx"]
        var sawPartial = false
        let finished = eventually(timeout: 90) {
            XCTAssertTrue(feed.exists, "the feed never leaves during the device build")
            // One query = one consistent snapshot; per-row reads race the advancing build.
            let snapshot = (feed.value as? String ?? "").split(separator: ",").reduce(into: [String: String]()) { result, pair in
                let parts = pair.split(separator: "=")
                if parts.count == 2 { result[String(parts[0])] = String(parts[1]) }
            }
            guard snapshot.count == 7 else { return false }
            let decided = order.map { snapshot[$0] == "decided" }
            if let firstOpen = decided.firstIndex(of: false) {
                XCTAssertFalse(decided[firstOpen...].contains(true), "sections advance in order: \(decided)")
                if firstOpen > 0 { sawPartial = true }
            }
            return title.label == "Plan ready · 7 of 7 decided"
        }
        XCTAssertTrue(finished, "all sections are decided once the device build completes")
        XCTAssertTrue(sawPartial, "sections decided progressively, not all at once")
        XCTAssertTrue(planBlockValue(app, "overlays").contains("Not used"))
        attach(app, "plan-feed-device-done")
    }

    func testLivePlanFeedStopCancelsTheDeviceRender() {
        let app = launchLivePlanFeed(reduceMotion: true, device: true)
        let stop = app.buttons["plan-feed-stop"]
        XCTAssertTrue(stop.waitForExistence(timeout: 30))
        XCTAssertFalse(app.staticTexts["This render can’t be stopped any more."].exists)
        stop.tap()
        // A stopped device render hands over to the status card (retry / find originals), not a server 409 message.
        XCTAssertTrue(app.descendants(matching: .any)["device-render-status"].firstMatch.waitForExistence(timeout: 20))
        XCTAssertFalse(app.otherElements["plan-feed"].exists)
        attach(app, "plan-feed-device-stopped")
    }

    func testLivePlanFeedStopCancelsTheRender() {
        let app = launchLivePlanFeed(reduceMotion: true)
        let stop = app.buttons["plan-feed-stop"]
        XCTAssertTrue(stop.waitForExistence(timeout: 30))
        XCTAssertGreaterThanOrEqual(stop.frame.height, 44)
        XCTAssertTrue(eventually(timeout: 20) { self.planBlockValue(app, "title") != "" })
        stop.tap()
        XCTAssertTrue(eventually(timeout: 20) { !app.otherElements["plan-feed"].exists }, "a stopped render leaves the feed")
        XCTAssertFalse(app.buttons["Open editor"].exists)
        attach(app, "plan-feed-stopped")
    }

    func testLivePlanFeedStopHidesWhenTheRenderCannotBeCancelled() {
        let app = launchLivePlanFeed(reduceMotion: true, cancel: "unavailable")
        let stop = app.buttons["plan-feed-stop"]
        XCTAssertTrue(stop.waitForExistence(timeout: 30))
        stop.tap()
        XCTAssertTrue(app.staticTexts["plan-feed.stop-message"].waitForExistence(timeout: 10))
        XCTAssertTrue(eventually(timeout: 5) { !app.buttons["plan-feed-stop"].exists })
        XCTAssertTrue(app.otherElements["plan-feed"].exists, "the render carries on")
    }

    // MARK: - Live plan & review: the Review your video sheet

    private let reviewSections = ["title", "clips", "captions", "music", "sfx", "overlays", "look", "post_caption"]

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement { app.descendants(matching: .any)[id].firstMatch }

    private func fieldText(_ app: XCUIApplication, _ id: String) -> String { element(app, id).value as? String ?? "" }

    /// Scrolls the sheet until the element sits in the clear middle band (above the floating panel, below the
    /// header), then returns it. The bottom panel floats over the cards, so a bare tap can land on the glass.
    private func reveal(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        let target = element(app, id)
        XCTAssertTrue(target.waitForExistence(timeout: 10), "\(id) exists")
        let window = app.windows.firstMatch
        for _ in 0..<10 {
            let height = window.frame.height
            let y = target.frame.midY
            if y > 200 && y < height * 0.6 { break }
            let from = window.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y >= height * 0.6 ? 0.62 : 0.3))
            let to = window.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: y >= height * 0.6 ? 0.38 : 0.55))
            from.press(forDuration: 0.05, thenDragTo: to)
        }
        return target
    }

    /// The feed finishes, "Review your video" opens the sheet. Returns once every card is on screen.
    private func openReviewFromFeed(_ app: XCUIApplication) {
        let cta = app.buttons["plan-feed-review-cta"]
        XCTAssertTrue(cta.waitForExistence(timeout: 100), "the finished feed offers the Review CTA")
        cta.tap()
        XCTAssertTrue(app.otherElements["review-section-title"].waitForExistence(timeout: 15), "the Review sheet opens")
        XCTAssertTrue(element(app, "review-change-captions").waitForExistence(timeout: 15), "the snapshot makes sections changeable")
    }

    private func sectionValue(_ app: XCUIApplication, _ section: String) -> String {
        app.otherElements["review-section-\(section)"].value as? String ?? ""
    }

    /// Flag Captions and Music, ask for something, press Update video and wait for the Updated state.
    private func updateCaptionsAndMusic(_ app: XCUIApplication, screenshots: Bool = true) {
        reveal(app, "review-change-captions").tap()
        reveal(app, "review-change-music").tap()
        XCTAssertTrue(element(app, "review-chip-captions").waitForExistence(timeout: 5), "a flagged section becomes a chip")
        XCTAssertTrue(element(app, "review-chip-music").exists)
        XCTAssertTrue(eventually(timeout: 5) { self.sectionValue(app, "captions") == "flagged" && self.sectionValue(app, "music") == "flagged" })
        let prompt = element(app, "review-prompt-field")
        prompt.tap()
        prompt.typeText("Shorter captions, and something more upbeat")
        if screenshots { attach(app, "review-flagged") }
        let update = element(app, "review-update-video")
        XCTAssertTrue(update.isEnabled)
        update.tap()
        XCTAssertTrue(element(app, "review-updating").waitForExistence(timeout: 10), "the panel switches to Updating")
        XCTAssertTrue(eventually(timeout: 10) { self.sectionValue(app, "captions") == "updating" }, "flagged cards show as being redone")
        XCTAssertNotEqual(sectionValue(app, "sfx"), "updating", "an unflagged section keeps its card")
        if screenshots { attach(app, "review-updating") }
        XCTAssertTrue(element(app, "review-updated-captions").waitForExistence(timeout: 60), "the update finishes with an Updated pill")
        XCTAssertTrue(element(app, "review-updated-music").exists)
    }

    func testReviewPlanFlagsUpdatesAndUndoesASectionFromTheFeedCTA() {
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
        openReviewFromFeed(app)

        // Every section is there, in order, with the right affordances.
        for section in reviewSections { XCTAssertTrue(app.otherElements["review-section-\(section)"].exists, "\(section) card") }
        XCTAssertEqual(sectionValue(app, "overlays"), "not used")
        XCTAssertEqual(sectionValue(app, "post_caption"), "read only")
        XCTAssertFalse(element(app, "review-change-overlays").exists, "a section that is not used has nothing to change")
        XCTAssertFalse(element(app, "review-change-post_caption").exists, "a post caption is display only")
        XCTAssertTrue(element(app, "review-change-sfx").exists, "a malformed payload still shows its summary and can be changed")
        XCTAssertFalse(element(app, "review-update-video").isEnabled, "nothing flagged yet")
        XCTAssertTrue(element(app, "review-keep-as-is").exists)
        XCTAssertEqual(fieldText(app, "review-edit-caption-c2"), "Salt, sun, sand.")
        attach(app, "review-idle")

        updateCaptionsAndMusic(app)
        XCTAssertFalse(element(app, "review-updated-sfx").exists, "only the flagged sections changed")
        XCTAssertTrue(element(app, "review-summary").label.contains("captions and music"), "the assistant summary is shown")
        XCTAssertTrue(element(app, "review-was-captions").label.hasPrefix("Was 4 lines"))
        XCTAssertEqual(fieldText(app, "review-edit-caption-c2"), "Salt, sun.")
        XCTAssertTrue(element(app, "review-changed-line-c2").exists, "changed lines are marked")
        XCTAssertFalse(element(app, "review-changed-line-c1").exists, "unchanged lines are not")
        XCTAssertTrue(element(app, "review-undo-all").exists)
        XCTAssertTrue(element(app, "review-done").exists)
        attach(app, "review-updated")

        // Undo one section: only captions come back, as another update that can itself be undone.
        reveal(app, "review-undo-captions").tap()
        XCTAssertTrue(eventually(timeout: 60) { self.fieldText(app, "review-edit-caption-c2") == "Salt, sun, sand." }, "the previous captions are restored")
        XCTAssertTrue(element(app, "review-updated-captions").waitForExistence(timeout: 30), "the restored section is marked changed, so Undo toggles")
        XCTAssertFalse(element(app, "review-updated-music").exists, "the music update from the earlier job is not part of this one")
        XCTAssertTrue(element(app, "review-was-captions").label.contains("4 lines · shorter"))
        attach(app, "review-undone")
    }

    func testReviewPlanUndoAllPutsEverythingBack() {
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
        openReviewFromFeed(app)
        updateCaptionsAndMusic(app, screenshots: false)
        XCTAssertEqual(fieldText(app, "review-edit-caption-c2"), "Salt, sun.")
        XCTAssertTrue(app.staticTexts["Golden Hour"].exists, "the update swapped the track")
        element(app, "review-undo-all").tap()
        XCTAssertTrue(eventually(timeout: 60) { self.fieldText(app, "review-edit-caption-c2") == "Salt, sun, sand." }, "undo all restores the captions")
        XCTAssertTrue(app.staticTexts["Sunday Drive"].waitForExistence(timeout: 30), "and the track")
        XCTAssertTrue(element(app, "review-summary").waitForExistence(timeout: 30))
        attach(app, "review-undo-all")
    }

    func testReviewPlanManualEditsOfTitleCaptionsAndMixFlagTheirSections() {
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
        openReviewFromFeed(app)
        XCTAssertFalse(element(app, "review-chip-title").exists)

        // Typing in a field flags its section: no Change tap needed.
        let title = reveal(app, "review-edit-title")
        title.tap()
        title.typeText(" by the sea")
        XCTAssertTrue(element(app, "review-chip-title").waitForExistence(timeout: 5))
        let line = reveal(app, "review-edit-caption-c3")
        line.tap()
        line.typeText("!")
        XCTAssertTrue(element(app, "review-chip-captions").waitForExistence(timeout: 5))
        let slider = reveal(app, "review-mix-music_level")
        XCTAssertTrue(slider.waitForExistence(timeout: 5))
        slider.adjust(toNormalizedSliderPosition: 0.2)
        XCTAssertTrue(element(app, "review-chip-music").waitForExistence(timeout: 5))
        XCTAssertTrue(element(app, "review-update-video").isEnabled, "edits alone are enough to update, no prompt needed")
        attach(app, "review-manual-edits")

        // Removing a chip discards that section's edits.
        element(app, "review-chip-captions").tap()
        XCTAssertTrue(eventually(timeout: 5) { !self.element(app, "review-chip-captions").exists })
        XCTAssertEqual(fieldText(app, "review-edit-caption-c3"), "Golden hour.", "the caption edit is gone with its chip")

        element(app, "review-update-video").tap()
        XCTAssertTrue(element(app, "review-updated-title").waitForExistence(timeout: 60))
        XCTAssertTrue(element(app, "review-updated-music").exists)
        XCTAssertFalse(element(app, "review-updated-captions").exists, "captions were not sent")
        XCTAssertTrue(fieldText(app, "review-edit-title").contains("by the sea"), "the typed title landed: \(fieldText(app, "review-edit-title"))")
        let level = fieldText(app, "review-mix-music_level")
        XCTAssertTrue(level.hasSuffix("percent") && (Int(level.split(separator: " ").first ?? "") ?? 0) <= 30, "the new mix level landed: \(level)")
        attach(app, "review-manual-edits-updated")
    }

    func testReviewPlanChangeOnAFeedCardOpensReviewWithThatSectionFlagged() {
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
        XCTAssertTrue(app.otherElements["plan-feed"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.buttons["plan-feed-review-cta"].waitForExistence(timeout: 100))
        app.buttons["plan-feed.block.captions"].tap()
        let change = element(app, "plan-feed.change.captions")
        XCTAssertTrue(change.waitForExistence(timeout: 5))
        change.tap()
        XCTAssertTrue(element(app, "review-chip-captions").waitForExistence(timeout: 15), "the card's Change flags that section")
        XCTAssertTrue(eventually(timeout: 5) { self.sectionValue(app, "captions") == "flagged" })
        XCTAssertFalse(element(app, "review-chip-music").exists)
        // Un-flagging from the card works the same as from the chip.
        reveal(app, "review-change-captions").tap()
        XCTAssertTrue(eventually(timeout: 5) { !self.element(app, "review-chip-captions").exists })
        XCTAssertFalse(element(app, "review-update-video").isEnabled)
    }

    func testReviewPlanOpensFromTheProjectAndKeepAsIsSendsNothing() {
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 120))
        let entry = app.buttons["project-review-entry"]
        XCTAssertTrue(entry.waitForExistence(timeout: 15), "a finished video offers Review")
        entry.tap()
        XCTAssertTrue(app.otherElements["review-section-captions"].waitForExistence(timeout: 15))
        XCTAssertTrue(element(app, "review-change-captions").waitForExistence(timeout: 15))
        XCTAssertFalse(element(app, "review-undo-all").exists, "nothing has been updated yet")
        reveal(app, "review-change-music").tap()
        element(app, "review-keep-as-is").tap()
        XCTAssertTrue(eventually(timeout: 10) { !app.otherElements["review-section-captions"].exists }, "Keep as is closes the sheet")
        XCTAssertTrue(app.buttons["Open editor"].exists, "nothing was sent: still the same finished video")
        XCTAssertFalse(app.otherElements["plan-feed"].exists)
    }

    private func createFreshChat(in app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
    }

    /// Re-checks `condition` until it holds or `timeout` passes. XCTWaiter polls an
    /// NSPredicate expectation only after 1s and abandons a check still running at the
    /// deadline. On a contended CI simulator one accessibility query took 3.8s, so a 3s
    /// wait for the closed drawer timed out mid-query although the app had closed it
    /// 2s earlier (run 36047011682). Here the first check starts at once and a check
    /// that began before the deadline always counts.
    private func eventually(timeout: TimeInterval = 3, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else { return false }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return true
    }
}
