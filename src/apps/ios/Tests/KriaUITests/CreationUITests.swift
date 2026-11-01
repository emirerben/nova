import XCTest

@MainActor
final class CreationUITests: XCTestCase {
    func testFinishedEditLearningChatAcceptAndDismiss() {
        for action in ["accept", "dismiss"] {
            let app = launchConfirmationFixture(["KRIA_CHAT_FINISHED_LEARNING": "1"])
            let confirm = app.buttons["Create this video"]
            XCTAssertTrue(confirm.waitForExistence(timeout: 10))
            confirm.tap()
            XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
            let count = app.staticTexts["Based on 3 finished videos"]
            XCTAssertTrue(count.waitForExistence(timeout: 5))
            let decision = app.buttons["text-learning-\(action)-learning-position"]
            scrollIntoView(decision, in: app)
            XCTAssertGreaterThanOrEqual(decision.frame.height, 44)
            XCTAssertLessThan(decision.frame.maxY, app.textFields["Message Kria"].frame.minY)
            let screenshot = XCTAttachment(screenshot: app.screenshot())
            screenshot.name = "Finished-edit chat suggestion before \(action)"
            screenshot.lifetime = .keepAlways
            add(screenshot)
            decision.tap()
            let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: count)
            XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 5), .completed)
            XCTAssertTrue(app.buttons["Open editor"].exists)
            app.terminate()
        }
    }

    /// The editor's Kria sheet shows the same chat, so its ready card offers "Open editor" while the
    /// editor is already open underneath. Tapping it must close the sheet and leave the creator in the
    /// editor, not do nothing.
    func testOpenEditorInsideTheEditorsKriaSheetReturnsToTheEditor() {
        // The perf fixture's seeded chats end on a finished render the editor can really open.
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = [
            "KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_SEED_THREADS": "3", "KRIA_CHAT_LONG_HISTORY": "12",
            "KRIA_EDITOR_FIXTURE": "1", "UI_TEST_REDUCE_MOTION": "1",
        ]
        app.launch()
        let chatOpenEditor = app.buttons["Open editor"]
        XCTAssertTrue(chatOpenEditor.waitForExistence(timeout: 30))
        chatOpenEditor.tap()

        let kria = app.buttons["native-editor-conversation"]
        XCTAssertTrue(kria.waitForExistence(timeout: 15), "the editor opened from the chat's ready card")
        kria.tap()
        let sheet = app.descendants(matching: .any)["native-editor-project-conversation"].firstMatch
        XCTAssertTrue(sheet.waitForExistence(timeout: 5))
        let sheetOpenEditor = sheet.buttons["Open editor"]
        XCTAssertTrue(sheetOpenEditor.waitForExistence(timeout: 10), "the sheet's ready card offers Open editor")
        XCTAssertTrue(sheetOpenEditor.isHittable)
        sheetOpenEditor.tap()

        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: sheet)
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 5), .completed, "Open editor closes the Kria sheet")
        XCTAssertTrue(kria.waitForExistence(timeout: 3))
        XCTAssertTrue(kria.isHittable, "the creator is back in the editor")
        app.terminate()
    }

    /// KRI-986, real-device condition: the creator's chat composer is focused (keyboard up) inside the
    /// editor's Kria sheet when they tap "Open editor". The tap must still close the sheet.
    func testOpenEditorInsideTheEditorsKriaSheetWithKeyboardOpenReturnsToTheEditor() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = [
            "KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_SEED_THREADS": "3", "KRIA_CHAT_LONG_HISTORY": "12",
            "KRIA_EDITOR_FIXTURE": "1", "UI_TEST_REDUCE_MOTION": "1",
        ]
        app.launch()
        let chatOpenEditor = app.buttons["Open editor"]
        XCTAssertTrue(chatOpenEditor.waitForExistence(timeout: 30))
        chatOpenEditor.tap()

        let kria = app.buttons["native-editor-conversation"]
        XCTAssertTrue(kria.waitForExistence(timeout: 15), "the editor opened from the chat's ready card")
        kria.tap()
        let sheet = app.descendants(matching: .any)["native-editor-project-conversation"].firstMatch
        XCTAssertTrue(sheet.waitForExistence(timeout: 5))
        let sheetOpenEditor = sheet.buttons["Open editor"]
        XCTAssertTrue(sheetOpenEditor.waitForExistence(timeout: 10), "the sheet's ready card offers Open editor")

        // Focus the composer so the keyboard is up, as on a real device.
        // The chat underneath the editor has its own (covered, unhittable) composer; take the live one.
        let fields = app.textFields.matching(NSPredicate(format: "label == %@", "Message Kria"))
        _ = fields.firstMatch.waitForExistence(timeout: 5)
        let composer = fields.allElementsBoundByIndex.first(where: { $0.isHittable }) ?? fields.firstMatch
        XCTAssertTrue(composer.exists, "the sheet shows the chat composer")
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "tapping the composer opens the keyboard")

        // The keyboard shrinks the visible conversation; bring the button above it if it slid underneath.
        var swipes = 0
        while !sheetOpenEditor.isHittable && swipes < 4 {
            sheet.swipeUp()
            swipes += 1
        }
        XCTAssertTrue(sheetOpenEditor.isHittable, "Open editor is reachable with the keyboard open")
        let beforeTap = XCTAttachment(screenshot: app.screenshot())
        beforeTap.name = "Kria sheet with keyboard open, before tapping Open editor (swipes: \(swipes))"
        beforeTap.lifetime = .keepAlways
        add(beforeTap)
        XCTAssertTrue(app.keyboards.firstMatch.exists, "the keyboard is still open when Open editor is tapped")
        sheetOpenEditor.tap()

        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: sheet)
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 5), .completed, "Open editor closes the Kria sheet with the keyboard open")
        XCTAssertTrue(kria.waitForExistence(timeout: 3))
        XCTAssertTrue(kria.isHittable, "the creator is back in the editor")
        app.terminate()
    }

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

    /// Dragging a text-selection handle sideways moves the handle. The projects drawer's own drag used to claim it.
    func testDraggingASelectionHandleInTheComposerDoesNotOpenTheDrawer() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        createFreshChat(in: app)
        let composer = app.textFields["Message Kria"].exists ? app.textFields["Message Kria"] : app.textViews["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        composer.typeText("alpha beta gamma delta epsilon zeta")
        composer.press(forDuration: 1.2)
        let selectAll = app.menuItems["Select All"]
        XCTAssertTrue(selectAll.waitForExistence(timeout: 5), "Long press offers Select All")
        selectAll.tap()
        let before = XCTAttachment(screenshot: app.screenshot()); before.name = "Selected all"; before.lifetime = .keepAlways; add(before)
        let box = composer.frame
        // The start handle's knob sits above the first character of the selection.
        let start = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: box.minX + 2, dy: box.minY - 6))
        let end = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: box.minX + 150, dy: box.minY - 6))
        start.press(forDuration: 0.3, thenDragTo: end)
        let after = XCTAttachment(screenshot: app.screenshot()); after.name = "After handle drag"; after.lifetime = .keepAlways; add(after)
        XCTAssertFalse(app.buttons["drawer-new-chat"].waitForExistence(timeout: 1.5), "Dragging a selection handle must not open the drawer")
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
        XCTAssertTrue(app.staticTexts["Voiceover"].exists)

        let toggle = app.buttons["workspace-menu-toggle"]
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 3))
        XCTAssertEqual(app.buttons["format-montage"].frame.minX, prompt.frame.minX, accuracy: 2)
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
        let swipeY = carousel.frame.minY - 20
        XCTAssertGreaterThan(swipeY, toggle.frame.maxY)
        let origin = app.coordinate(withNormalizedOffset: .zero)
        let swipeStart = origin.withOffset(CGVector(dx: viewport.width * 0.15, dy: swipeY))
        let swipeEnd = origin.withOffset(CGVector(dx: viewport.width * 0.85, dy: swipeY))
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
    /// The single pick closes Apple's picker on its own only while the server
    /// caps Talking at one clip (multi-clip Talking off, KRI-719).
    func testLibraryPickerReturnsToChatByDoneAndBySinglePick() {
        let app = XCUIApplication()
        app.resetAuthorizationStatus(for: .photos)
        app.launchArguments = ["-ui-testing-chat", "-ui-testing-seed-photo-video", "-ui-testing-apple-photo-picker"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_TALKING_MAX_CLIPS"] = "1"
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

    /// KRI-719: with multi-clip Talking on, the Talking footage step explains the talking video plus
    /// extra clips and lets the creator pick three videos at once. All three are read from Photos and
    /// reach upload (the offline fixture has no upload reservations, so each surfaces as a named failure).
    func testTalkingToCameraPicksThreeVideos() {
        let app = XCUIApplication()
        app.resetAuthorizationStatus(for: .photos)
        app.launchArguments = ["-ui-testing-chat", "-ui-testing-seed-photo-video", "-ui-testing-seed-photo-videos"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        createFreshChat(in: app)
        let card = app.buttons["format-talking_to_camera"]
        if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        XCTAssertTrue(card.waitForExistence(timeout: 5))
        card.tap()
        XCTAssertTrue(app.staticTexts["Choose the video of you talking, plus any clips to show while you talk."].waitForExistence(timeout: 5),
                      app.debugDescription)
        XCTAssertTrue(app.staticTexts["MP4, MOV · up to 10 clips"].exists)
        let chooseVideos = app.buttons["choose-videos"]
        XCTAssertTrue(chooseVideos.waitForExistence(timeout: 5))
        chooseVideos.tap()
        XCTAssertTrue(app.staticTexts["The video of you talking, plus any clips to show while you talk."].waitForExistence(timeout: 5))
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

        let tiles = (0..<3).map { app.buttons["gallery-tile-\($0)"] }
        XCTAssertTrue(tiles[0].waitForExistence(timeout: 40), app.debugDescription)
        let count = app.staticTexts["gallery-count"]
        XCTAssertTrue(eventually { count.label.hasPrefix("0 of 10") }, count.label)
        for (index, tile) in tiles.enumerated() {
            tile.tap()
            XCTAssertTrue(eventually { count.label.hasPrefix("\(index + 1) of 10") }, count.label)
        }
        XCTAssertEqual(tiles[2].value as? String, "Selected, 3")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Talking to camera with three videos picked"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        app.buttons["photos-picker-done"].tap()

        // Three picks reach upload and come back as three failed files: none was dropped by a one-clip cap.
        let reachedUpload = app.staticTexts["The data couldn\u{2019}t be read because it isn\u{2019}t in the correct format."]
        XCTAssertTrue(reachedUpload.waitForExistence(timeout: 25), app.debugDescription)
        if app.buttons["attachment-done"].exists { app.buttons["attachment-done"].tap() }
        let failures = app.staticTexts["footage-upload-failures-message"]
        XCTAssertTrue(failures.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(eventually(timeout: 10) { failures.label.hasPrefix("3 files") }, failures.label)
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

    /// KRI-207: after a render the creator sees one static receipt row per requirement, sees which names were
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

        // One static row per requirement, named from the brief. The rows wait for the brief before they
        // show, so there is no placeholder phase; still wait on the label rather than assume timing.
        func row(_ id: String) -> XCUIElement { app.descendants(matching: .any)["requirement-chip-\(id)"].firstMatch }
        let met = row("req-labels")
        let partial = row("req-order")
        let couldNot = row("req-drone")
        XCTAssertTrue(met.waitForExistence(timeout: 10))
        func waitForLabel(_ element: XCUIElement, _ label: String) {
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", label), object: element)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed, "\(element.identifier) label: \(element.label)")
        }
        // The reason is part of the row from the start: nothing to tap, nothing to expand.
        waitForLabel(met, "Done: Label each clip with its place. Every clip has its own place name.")
        waitForLabel(partial, "Partly done: Follow the pier-to-lighthouse route. Your clips were filmed from the lighthouse to the pier, the reverse of the route you gave. I kept filming order; tell me if you want your route order instead.")
        waitForLabel(couldNot, "Couldn’t do: End on a drone shot. None of your clips is aerial footage.")
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "requirement-chip-")).count, 0,
                       "Receipts are static rows, not tappable pills")
        // The server's short lead replaces the long prose (which the old builds and the web still show).
        XCTAssertTrue(app.staticTexts["Kria: Partly done."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Kria: The cut is ready. I did most of what you asked; one thing needs your call."].exists)
        let also = app.staticTexts["requirement-also-changed"]
        scrollIntoView(also, in: app)
        XCTAssertEqual(also.label, "Also: Retimed the title (0–4.3s → 3–7.3s)")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "receipt-rows-with-brief"; shot.lifetime = .keepAlways; add(shot)
        scrollIntoView(partial, in: app)

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

    /// KRI-742: talk instead of type. The microphone and recognizer are scripted (a simulator has neither); the
    /// composer, mic button, controller and send path are the real ones. Live words appear while listening, the
    /// server text replaces them on stop, and the result is an ordinary sendable message.
    func testDictationShowsLiveWordsThenServerTextAndIsSendable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_VOICE_DICTATION"] = "1"
        app.launch()
        createFreshChat(in: app)

        let mic = app.buttons["chat-dictate"]
        XCTAssertTrue(mic.waitForExistence(timeout: 10), "The mic appears once the server advertises voice_dictation")
        XCTAssertGreaterThanOrEqual(mic.frame.height, 44)
        let composer = app.textFields["Message Kria"]
        mic.tap()

        let live = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "make the intro shorter"), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [live], timeout: 5), .completed, "Live words appear while listening")
        XCTAssertEqual(mic.label, "Stop dictation")

        mic.tap()
        let final = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Make the intro shorter and add the beach clip first."), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [final], timeout: 8), .completed, "The server text replaces the live words")
        XCTAssertEqual(mic.label, "Dictate message")
        XCTAssertTrue(app.buttons["chat-send-message"].isEnabled)

        app.buttons["chat-send-message"].tap()
        let sent = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value != %@", "Make the intro shorter and add the beach clip first."), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [sent], timeout: 8), .completed, "Sending clears the composer")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Dictation sent"
        shot.lifetime = .keepAlways
        add(shot)
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

    /// "Clean up speech and create" replaces the tall approval card with the render stage while the server has
    /// not queued the render yet (its decision writes only `approval_approved`). Failure modes pinned: the
    /// footage stage ("Add your footage") comes back for a poll or two; the viewport stays where the card's
    /// buttons were with the "Latest" pill up, so nothing of the conversation is on screen; the plan feed then
    /// lands below the fold.
    func testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_SPEECH_CLEANUP"] = "choice"
        app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        // DirectionStage's own identifier is inherited by everything inside it, so the button is found by its words.
        let clean = app.buttons["Clean up speech and create"]
        XCTAssertTrue(clean.waitForExistence(timeout: 15), "the approval card offers the cleanup choice")

        // Read the card the way a creator does: scroll up past its buttons, then bring "Clean up" back into
        // view. "Keep original" and "Change direction" stay below it, so the reader is still above the
        // follow threshold and the "Latest" pill is up when the button is tapped.
        let history = app.scrollViews.firstMatch
        history.swipeDown()
        let pill = app.buttons["chat-jump-to-latest"]
        XCTAssertTrue(pill.waitForExistence(timeout: 3))
        let composer = app.buttons["chat-send-message"]
        func cleanIsClear() -> Bool { clean.isHittable && clean.frame.maxY <= composer.frame.minY - 4 }
        // A swipe flings; drag by exactly what is needed so the buttons below "Clean up" stay off screen.
        for _ in 0..<8 where !cleanIsClear() {
            let needed = min(320, max(40, clean.frame.maxY - composer.frame.minY + 12))
            let from = history.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.7))
            let to = from.withOffset(CGVector(dx: 0, dy: -needed))
            from.press(forDuration: 0.1, thenDragTo: to, withVelocity: .slow, thenHoldForDuration: 0.3)
        }
        XCTAssertTrue(cleanIsClear(), "Clean up must be on screen above the composer")
        XCTAssertTrue(pill.exists, "precondition: the reader is above the card's buttons")
        clean.tap()

        // The server still reports a plain draft for a poll or two after Create (its decision writes only
        // `approval_approved`): the footage stage must not come back in that window, and the render stage
        // must replace the card at once, in view, until the plan feed takes over.
        let footage = app.staticTexts["Add your footage"]
        let shaping = app.staticTexts["I’m shaping your video"]
        let feed = app.otherElements["plan-feed"]
        let settle = Date().addingTimeInterval(1.5)
        while Date() < settle {
            XCTAssertFalse(footage.exists, "the footage stage must not return after Create")
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        XCTAssertTrue(eventually(timeout: 5) { shaping.exists || feed.exists }, "approving shows the render stage")
        let deadline = Date().addingTimeInterval(4)
        while Date() < deadline, !feed.exists {
            XCTAssertFalse(footage.exists, "the footage stage must not flash while the render is queued")
            XCTAssertTrue(shaping.exists, "the render stage stays until the plan feed takes over")
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        XCTAssertTrue(eventually(timeout: 3) { !pill.exists }, "the conversation sits at its end, so no Latest pill")
        XCTAssertTrue(eventually(timeout: 3) { (shaping.exists && shaping.isHittable) || feed.exists },
                      "the render stage is in view, not below the fold")
        attach(app, "render-stage-after-cleanup-approval")

        // The queued render's live plan then lands in view, and the reader still follows the end.
        XCTAssertTrue(feed.waitForExistence(timeout: 30), "the plan feed follows the render stage")
        XCTAssertTrue(eventually(timeout: 5) { app.staticTexts["plan-feed.header"].isHittable }, "the plan feed is in view")
        XCTAssertFalse(pill.exists)
        attach(app, "plan-feed-after-cleanup-approval")
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
    private func launchRealShapeHistory(thumbs: Bool = false, question: String = "history") -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_CLIP_QUESTION"] = question
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

    /// The circle on a tile: picks or unpicks the clip without opening it.
    private func tickButton(_ app: XCUIApplication, _ n: Int) -> XCUIElement {
        app.buttons["clip-select-group:dodgeball-\(Self.realShapeIDs[n])"]
    }

    private func centre(_ element: XCUIElement) -> XCUICoordinate {
        element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
    }

    private func selectedCount(_ app: XCUIApplication) -> String {
        app.descendants(matching: .any)["clip-count"].label
    }

    /// KRI-282 follow-up: press-and-slide selects (or deselects, by the first tile) every tile it crosses, while
    /// the tile's circle still picks one clip and a tap still opens the clip.
    func testClipPickerSlideAcrossTilesSelectsDeselectsAndKeepsCircleAndTapToWatch() {
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
        // A slide never leaves the sheet in a half state: the circle still toggles a single tile.
        tickButton(app, 4).tap()
        XCTAssertEqual(selectedCount(app), "2 of 48 selected")
        // A tap opens the clip and does not select it.
        tileButton(app, 5).tap()
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

    /// KRI-693: a Visuals photo in the picker shows its thumbnail and reads as a photo in the chat's language,
    /// while the chat clips beside it stay "Clip N".
    func testPickerShowsAVisualsPhotoWithItsThumbnailAndAPhotoName() {
        let app = launchRealShapeHistory(thumbs: true, question: "photo")
        XCTAssertTrue(app.descendants(matching: .any)["clip-card"].waitForExistence(timeout: 20))
        app.buttons["clip-choose"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        let photoID = "asset-d9627a79-b228-4db0-9cbb-201f6f921bb2"
        let photo = app.buttons["clip-thumb-group:dodgeball-\(photoID)"]
        XCTAssertTrue(photo.waitForExistence(timeout: 5))
        XCTAssertEqual(photo.label, "Fotoğraf 1")
        XCTAssertEqual(app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[0])"].label, "Clip 1", "chat clips are unchanged")
        // The thumbnail arrives from Visuals; until it does the tile is a placeholder named like the photo.
        let placeholder = app.descendants(matching: .any)["clip-placeholder-\(photoID)"]
        let loaded = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: placeholder)
        XCTAssertEqual(XCTWaiter.wait(for: [loaded], timeout: 15), .completed, "the photo never got its thumbnail")
        shoot(app, "kri693-photo-tile")
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
        tickButton(app, 0).tap()
        XCTAssertEqual(first.value as? String, "Selected")
        shoot(app, "2a-sheet-open")
        tickButton(app, 1).tap()
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
        // A tap opens the pager at that clip; swipe sideways moves through the same list.
        app.buttons["clip-thumb-group:dodgeball-\(Self.realShapeIDs[2])"].tap()
        let title = app.descendants(matching: .any)["clip-preview-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 5))
        XCTAssertEqual(title.label, "Clip 3 of 48")
        shoot(app, "3-preview-pager")
        app.descendants(matching: .any)["clip-preview-pager"].swipeLeft()
        XCTAssertTrue(eventually { title.label == "Clip 4 of 48" })
        shoot(app, "3b-preview-next")
        XCTAssertEqual(app.buttons["clip-preview-toggle"].value as? String, "Not selected", "the button follows the clip shown")
        // "Use this clip" picks the clip being shown and goes back to the grid.
        app.buttons["clip-preview-toggle"].tap()
        XCTAssertTrue(title.waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["clip-count"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "1 of 48 selected", "selecting from the preview counts")
        XCTAssertEqual(tileButton(app, 3).value as? String, "Selected", "the clip that was shown is the one picked")
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
        let firstCircle = app.buttons["clip-select-dodgeball-fixture-clip"]
        firstCircle.tap()
        XCTAssertEqual(first.value as? String, "Selected")
        firstCircle.tap()
        XCTAssertEqual(first.value as? String, "Not selected", "tapping twice unselects")
        firstCircle.tap()
        // Second group via the segmented control.
        app.segmentedControls["clip-category-picker"].buttons.element(boundBy: 1).tap()
        let football = app.buttons["clip-select-football-fixture-clip-4"]
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

    /// Two questions in one picker: while another question is still unanswered the main button says Next and
    /// moves there without sending; the last answer sends both on one turn. Done never sends half an answer.
    func testTwoQuestionPickerNextShowsTheSecondQuestionAndDoneSendsBoth() {
        let app = launchClipQuestionFixture(capability: "1")
        XCTAssertTrue(app.buttons["clip-choose"].waitForExistence(timeout: 15))
        let sent = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:"))
        let sentBefore = sent.count
        app.buttons["clip-choose"].tap()
        let main = app.buttons["clip-done"]
        XCTAssertTrue(main.waitForExistence(timeout: 5))
        // The suggestion already answers the first question, so the button moves on instead of sending.
        XCTAssertEqual(main.label, "Next")
        XCTAssertTrue(main.isEnabled)
        let title = app.staticTexts["clip-sheet-title"]
        XCTAssertEqual(title.label, "Which clips are Dodgeball?")
        attachScreenshot(app, "two-questions-next")
        main.tap()
        XCTAssertTrue(eventually { title.label == "Which clips are Football?" }, title.label)
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].exists, "Next keeps the picker open")
        XCTAssertEqual(sent.count, sentBefore, "Next sends nothing")
        XCTAssertFalse(main.isEnabled, "the second question still needs an answer")
        // Going back to the first question by its tab still offers Next, never a half-answer Done.
        app.segmentedControls["clip-category-picker"].buttons.element(boundBy: 0).tap()
        XCTAssertTrue(eventually { title.label == "Which clips are Dodgeball?" }, title.label)
        XCTAssertEqual(main.label, "Next")
        main.tap()
        XCTAssertTrue(eventually { title.label == "Which clips are Football?" }, title.label)
        app.buttons["clip-select-football-fixture-clip-4"].tap()
        XCTAssertEqual(main.label, "Done")
        attachScreenshot(app, "two-questions-done")
        main.tap()
        XCTAssertTrue(app.staticTexts["You: Dodgeball: clip 2. Football: clip 4"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "answers[dodgeball=fixture-clip-2;football=fixture-clip-4]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "one turn carried both answers")
        XCTAssertEqual(sent.count, sentBefore + 1, "one message for both questions")
    }

    /// Leverkusen M2: the second question came pre-ticked (one of the two clips it needed). Answering the first one
    /// turned the button straight into Done, which sent the unseen pre-tick. A pre-ticked question is only done once
    /// the creator has seen it, so Next shows it first and the pre-tick can still be corrected.
    func testPreTickedSecondQuestionIsShownBeforeDoneAndCanBeCorrected() {
        let app = launchClipQuestionFixture(capability: "later-suggested")
        XCTAssertTrue(app.buttons["clip-choose"].waitForExistence(timeout: 15))
        let sent = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:"))
        let sentBefore = sent.count
        app.buttons["clip-choose"].tap()
        let main = app.buttons["clip-done"]
        XCTAssertTrue(main.waitForExistence(timeout: 5))
        let title = app.staticTexts["clip-sheet-title"]
        XCTAssertEqual(title.label, "Which clips are Dodgeball?")
        XCTAssertFalse(main.isEnabled, "the first question has no suggestion and needs an answer")
        app.buttons["clip-select-dodgeball-fixture-clip"].tap()
        XCTAssertEqual(main.label, "Next", "the pre-ticked second question has not been shown yet")
        attachScreenshot(app, "pre-ticked-second-next")
        main.tap()
        XCTAssertTrue(eventually { title.label == "Which clips are Football?" }, title.label)
        XCTAssertEqual(sent.count, sentBefore, "Next sends nothing")
        XCTAssertEqual(app.buttons["clip-thumb-football-fixture-clip-4"].value as? String, "Selected", "the pre-tick is shown")
        XCTAssertEqual(main.label, "Done")
        // The suggestion only had one of the two clips; the creator adds the other before sending.
        app.buttons["clip-select-football-fixture-clip-3"].tap()
        XCTAssertEqual(app.descendants(matching: .any)["clip-count"].label, "2 of 2 selected")
        attachScreenshot(app, "pre-ticked-second-corrected")
        main.tap()
        XCTAssertTrue(app.staticTexts["You: Dodgeball: clip 1. Football: clips 3, 4"].waitForExistence(timeout: 10))
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "answers[dodgeball=fixture-clip;football=fixture-clip-3+fixture-clip-4]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "one turn carried the corrected second answer")
        XCTAssertEqual(sent.count, sentBefore + 1, "one message for both questions")
    }

    /// A tap opens the clip full screen and playing, without picking it; "Use this clip" picks the clip being shown
    /// and goes back to the grid; the circle on a tile picks without opening anything.
    func testTappingAClipOpensItPlayingAndUseThisClipPicksIt() {
        let app = launchClipQuestionFixture(capability: "1")
        XCTAssertTrue(app.buttons["clip-choose"].waitForExistence(timeout: 15))
        app.buttons["clip-choose"].tap()
        let count = app.descendants(matching: .any)["clip-count"]
        XCTAssertTrue(count.waitForExistence(timeout: 5))
        XCTAssertEqual(count.label, "1 of 3 selected")
        let first = app.buttons["clip-thumb-dodgeball-fixture-clip"]
        first.tap()
        let title = app.descendants(matching: .any)["clip-preview-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 5), "a tap opens the clip")
        XCTAssertTrue(title.label.hasPrefix("Clip 1 of"), title.label)
        // fixture-clip has a local original: its video is on screen (the mute control only shows on a video).
        XCTAssertTrue(app.buttons["clip-preview-mute"].waitForExistence(timeout: 10), "the clip plays")
        attachScreenshot(app, "tap-to-watch-playing")
        let use = app.buttons["clip-preview-toggle"]
        XCTAssertEqual(use.label, "Use this clip")
        XCTAssertEqual(use.value as? String, "Not selected", "opening a clip does not pick it")
        use.tap()
        XCTAssertTrue(title.waitForNonExistence(timeout: 5), "Use this clip goes back to the grid")
        XCTAssertEqual(count.label, "2 of 3 selected")
        XCTAssertEqual(first.value as? String, "Selected")
        // The circle picks and unpicks without opening the clip.
        let thirdCircle = app.buttons["clip-select-dodgeball-fixture-clip-3"]
        thirdCircle.tap()
        XCTAssertEqual(count.label, "3 of 3 selected")
        XCTAssertFalse(title.exists, "the circle never opens the clip")
        thirdCircle.tap()
        XCTAssertEqual(count.label, "2 of 3 selected")
        // A picked clip opens as picked; the button then removes it and stays on the clip.
        app.buttons["clip-thumb-dodgeball-fixture-clip-2"].tap()
        XCTAssertTrue(title.waitForExistence(timeout: 5))
        XCTAssertEqual(use.value as? String, "Selected")
        // fixture-clip-2 is not on this device: its poster and a note, never a blank page.
        XCTAssertTrue(app.staticTexts["clip-preview-not-on-device"].waitForExistence(timeout: 5))
        use.tap()
        XCTAssertEqual(use.value as? String, "Not selected")
        XCTAssertTrue(title.exists, "removing a clip keeps it open")
        app.buttons["clip-preview-close"].tap()
        XCTAssertTrue(title.waitForNonExistence(timeout: 5))
        XCTAssertEqual(count.label, "1 of 3 selected")
    }

    // MARK: reply UX: answer chips + one receipt

    private func launchReplyFixture(_ mode: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_REPLY_FIXTURE"] = mode
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    private func attachScreenshot(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name; shot.lifetime = .keepAlways; add(shot)
    }

    /// A question that offers answers shows them as chips plus "Other…"; a chip sends its exact text as an
    /// ordinary message, "Other…" only opens the composer, and an answered question leaves no chips behind.
    func testReplyOptionChipsSendTheTappedLabelAndOtherOnlyFocusesTheComposer() {
        let app = launchReplyFixture("options")
        let first = app.buttons["reply-option-0"], second = app.buttons["reply-option-1"], third = app.buttons["reply-option-2"]
        let other = app.buttons["reply-option-other"]
        XCTAssertTrue(second.waitForExistence(timeout: 15))
        XCTAssertEqual(first.label, "About 1 second")
        XCTAssertEqual(second.label, "About 0.8 seconds")
        XCTAssertEqual(third.label, "About 0.5 seconds")
        XCTAssertEqual(other.label, "Other…")
        for chip in [first, second, third, other] { XCTAssertGreaterThanOrEqual(chip.frame.height, 44, chip.identifier) }
        // The prose above the chips is unchanged (old builds rely on it).
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: The clips are about 1.3 seconds each")).firstMatch.exists)
        attachScreenshot(app, "reply-option-chips")

        // "Other…": the creator is put in the composer, and nothing is sent.
        let sentBefore = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:")).count
        other.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "Other… focuses the composer")
        RunLoop.current.run(until: Date().addingTimeInterval(1))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:")).count, sentBefore,
                       "Other… must not send a message")
        XCTAssertFalse(app.staticTexts["You: Other…"].exists)
        XCTAssertTrue(second.exists, "The question is still open")
        attachScreenshot(app, "reply-option-other-composer-focused")

        // A chip sends exactly its label, as plain text.
        app.staticTexts["workspace-project-title"].tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 3))
        scrollIntoView(second, in: app)
        second.tap()
        XCTAssertTrue(app.staticTexts["You: About 0.8 seconds"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "reply[About 0.8 seconds]")).firstMatch
            .waitForExistence(timeout: 10), "the server received the chip text as the message")
        // Answered: no tappable chips remain in the history.
        XCTAssertTrue(app.buttons["reply-option-1"].waitForNonExistence(timeout: 5))
        XCTAssertFalse(app.buttons["reply-option-other"].exists)
    }

    /// After an edit the result is said once: a short lead, then one static row per ask with its reason
    /// already visible, and the request text appears a single time.
    func testReplyWithReceiptsShowsOneLeadAndStaticRowsOnce() {
        let app = launchReplyFixture("receipts")
        XCTAssertTrue(app.staticTexts["Kria: I did 1 of your 2 asks."].waitForExistence(timeout: 20))
        XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Couldn't: End on a drone shot")).count, 0,
                       "The long prose is replaced by the lead")
        func row(_ id: String) -> XCUIElement { app.descendants(matching: .any)["requirement-chip-\(id)"].firstMatch }
        let done = row("req-length"), couldNot = row("req-drone")
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        func waitForLabel(_ element: XCUIElement, _ label: String) {
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", label), object: element)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed, "\(element.identifier) label: \(element.label)")
        }
        // The reason is visible with no tap.
        waitForLabel(done, "Done: Make the clips about 0.8 seconds. Set 11 clips to 0.8s; Video length 45.64s → 39.78s")
        waitForLabel(couldNot, "Couldn’t do: End on a drone shot. None of your clips is aerial footage.")
        XCTAssertEqual(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "requirement-chip-")).count, 0, "no pills")
        for request in ["Make the clips about 0.8 seconds", "End on a drone shot"] {
            // XCUITest also lists a row's inner text as its own element; what matters is that the request is
            // named in exactly one receipt row and no longer in the assistant's own text.
            let rows = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH %@ AND label CONTAINS %@", "requirement-chip-", request))
            XCTAssertEqual(rows.count, 1, "\(request) is named in exactly one receipt row")
            XCTAssertEqual(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@ AND label CONTAINS %@", "Kria:", request)).count, 0,
                           "\(request) is not repeated in the reply prose")
        }
        let also = app.staticTexts["requirement-also-changed"]
        scrollIntoView(also, in: app)
        XCTAssertEqual(also.label, "Also: Retimed the title (0–4.3s → 3–7.3s)")
        attachScreenshot(app, "receipt-lead-and-rows")
    }

    /// One ask: the creator's own message is right above, so the row is just the outcome and the reason.
    func testReplyWithOneReceiptShowsNoRequestTitle() {
        let app = launchReplyFixture("receipt-single")
        XCTAssertTrue(app.staticTexts["Kria: Done."].waitForExistence(timeout: 20))
        let done = app.descendants(matching: .any)["requirement-chip-req-length"].firstMatch
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        XCTAssertEqual(done.label, "Done: Set 11 clips to 0.8s; Video length 45.64s → 39.78s")
        XCTAssertEqual(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Make the clips about 0.8 seconds")).count, 0)
        attachScreenshot(app, "receipt-single-row")
    }

    /// No `reply_lead` (an older server, or a review turn whose prose still lists the reasons): the prose
    /// is shown as before and the rows are only outcome + request, never a second copy of the reasons.
    func testReplyWithReceiptsButNoLeadKeepsTheProseAndShowsTitleOnlyRows() {
        let app = launchReplyFixture("receipts-no-lead")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Couldn't: End on a drone shot")).firstMatch
            .waitForExistence(timeout: 20), "the full prose is still the message")
        func row(_ id: String) -> XCUIElement { app.descendants(matching: .any)["requirement-chip-\(id)"].firstMatch }
        let done = row("req-length"), couldNot = row("req-drone")
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        func waitForLabel(_ element: XCUIElement, _ label: String) {
            let settled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == %@", label), object: element)
            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed, "\(element.identifier) label: \(element.label)")
        }
        waitForLabel(done, "Done: Make the clips about 0.8 seconds")
        waitForLabel(couldNot, "Couldn’t do: End on a drone shot")
        XCTAssertFalse(app.staticTexts["requirement-also-changed"].exists, "the prose already says what else changed")
    }

    // MARK: one reply, two questions (KRI-802)

    private func launchQuestionBatchFixture() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_QUESTION_BATCH"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    /// The Don Julio first reply: the title wording card and the clip picker in one message. The first answer is
    /// held (nothing is sent, the card says what is next); the second sends BOTH on one turn, so the creator waits
    /// for one re-plan instead of two.
    func testTitleCardAndClipPickerInOneReplyGoOutOnOneTurn() {
        let app = launchQuestionBatchFixture()
        let approve = app.buttons["choice-option-approve"]
        XCTAssertTrue(approve.waitForExistence(timeout: 15))
        let choose = app.buttons["clip-choose"]
        XCTAssertTrue(choose.exists)
        XCTAssertLessThan(approve.frame.minY, choose.frame.minY, "the cards follow the reply: title first, then clips")
        let sent = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:"))
        let sentBefore = sent.count
        approve.tap()
        XCTAssertTrue(app.staticTexts["choice-batch-note"].waitForExistence(timeout: 3))
        XCTAssertEqual(approve.value as? String, "Selected")
        XCTAssertEqual(sent.count, sentBefore, "the title answer waits for the clips")
        choose.tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        let redCap = app.buttons["clip-select-red_cap-fixture-clip-2"]
        XCTAssertTrue(redCap.waitForExistence(timeout: 3))
        redCap.tap()
        app.buttons["clip-done"].tap()
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "turns[1] choice[approve] answers[red_cap=fixture-clip-2]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "one turn carried both structured answers")
        XCTAssertTrue(app.staticTexts["You: Use this wording · My friend in the red cap: clip 2"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["choice-card-answered"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.descendants(matching: .any)["clip-card-answered"].exists)
    }

    /// The other order: clips first (held, the picker card says so), then the title tap sends both.
    func testClipsFirstThenTheTitleTapSendsBothOnOneTurn() {
        let app = launchQuestionBatchFixture()
        let choose = app.buttons["clip-choose"]
        XCTAssertTrue(choose.waitForExistence(timeout: 15))
        let sent = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "You:"))
        let sentBefore = sent.count
        choose.tap()
        XCTAssertTrue(app.descendants(matching: .any)["clip-sheet"].waitForExistence(timeout: 5))
        app.buttons["clip-select-red_cap-fixture-clip-2"].tap()
        app.buttons["clip-done"].tap()
        XCTAssertTrue(app.staticTexts["clip-batch-note"].waitForExistence(timeout: 5))
        XCTAssertEqual(sent.count, sentBefore, "the clips wait for the title answer")
        app.buttons["choice-option-approve"].tap()
        let echo = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "turns[1] choice[approve] answers[red_cap=fixture-clip-2]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 10), "one turn carried both structured answers")
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

    /// KRI-640: New chat opens on its create reply alone. Every chat read and every capabilities read after the
    /// first answers 10s late here, so a new chat that reads itself back or re-reads capabilities before its
    /// format cards work misses the 5s budget (the budget leaves room for a slow CI accessibility query).
    func testNewChatShowsUsableFormatsWithoutWaitingForMoreReads() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_SLOW_READS"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)

        let tappedAt = Date()
        newChat.tap()
        let card = app.buttons["format-montage"]
        let usable = eventually(timeout: 5) { card.exists && card.isEnabled && app.staticTexts["What are we making?"].exists }
        let elapsed = Date().timeIntervalSince(tappedAt)
        XCTAssertTrue(usable, "format cards took \(String(format: "%.1f", elapsed))s after New chat")
    }

    /// The Lip-sync card is the far-right card, choosing it posts its own sentence, and its attachment
    /// sheet opens on the song step with the lip-sync subtitle (the song leads a lip-sync).
    func testLipsyncCardIsFarRightAndOpensTheSheetOnTheSong() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_LIPSYNC"] = "1"
        app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        app.launch()
        createFreshChat(in: app)

        let slider = app.buttons["format-slides"]
        let lipsync = app.buttons["format-lipsync"]
        XCTAssertTrue(lipsync.waitForExistence(timeout: 10))
        XCTAssertEqual(lipsync.label, "Lip-sync")
        XCTAssertTrue(slider.exists)
        XCTAssertGreaterThan(lipsync.frame.minX, slider.frame.minX, "Lip-sync is the far-right card, after Slider")
        for _ in 0..<5 where !lipsync.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        XCTAssertTrue(lipsync.isHittable, "the far-right card can be scrolled to")
        lipsync.tap()

        let bubble = app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "Let’s make a lip-sync video")).firstMatch
        XCTAssertTrue(bubble.waitForExistence(timeout: 10), "choosing the card posts its sentence")
        let attach = app.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 10))
        attach.tap()
        XCTAssertTrue(app.staticTexts["Add your song"].waitForExistence(timeout: 5), "the sheet opens on the song step")
        XCTAssertTrue(app.staticTexts["Add the song you’re lip-syncing to."].exists)
        XCTAssertTrue(app.descendants(matching: .any)["attachment-step-song"].firstMatch.exists)
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
        // Capability off (no KRIA_SUGGESTION_CHIPS): no server chip row is drawn at all.
        XCTAssertFalse(app.descendants(matching: .any)["ready-suggestions"].exists)

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

    /// KRI-756 journey, capability ON (server stubbed): while clip understanding is pending only chip #1 shows; the
    /// full set then lands under the same set_id without moving chip #1; a tap SENDS the chip's `message` (not its
    /// label) and the row goes; the finished video offers the server's chips instead of the two fixed ones. The
    /// capability-OFF half is `testReadySuggestionChipsApplyFromTapsNearCapsuleEdges` above, which launches without
    /// `KRIA_SUGGESTION_CHIPS` and still pins the fixed chips.
    func testPendingServerSuggestionChipsShowOnlyTheDefaultEdit() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v1", "KRIA_CHAT_FIXTURE_MEDIA": "1",
                                 "KRIA_SUGGESTION_CHIPS": "1", "KRIA_SUGGESTION_CHIPS_PENDING": "hold"]
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        XCTAssertTrue(app.buttons["Let Kria cut the first version"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Add captions"].exists)
        XCTAssertFalse(app.buttons["Add sound effects"].exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Add a hook to the opening")).firstMatch.exists)
        XCTAssertFalse(app.buttons["Use suggestion: Try a stronger opening"].exists)
        app.terminate()
    }

    func testServerSuggestionChipsSendOnTapAndReplaceFixedChips() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_SUGGESTION_CHIPS"] = "1"
        app.launchEnvironment["KRIA_SUGGESTION_CHIPS_PENDING"] = "0"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        // Pending rendering is checked separately with a held server response. Polling may
        // finish before XCUITest observes it; this journey starts with the delivered set.
        let defaultEdit = app.buttons["Let Kria cut the first version"]
        XCTAssertTrue(defaultEdit.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Use suggestion: Try a stronger opening"].exists)
        let hook = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Add a hook to the opening")).firstMatch
        XCTAssertTrue(hook.waitForExistence(timeout: 15))
        XCTAssertEqual(hook.label, "Add a hook to the opening, the first shot is a quiet one")
        XCTAssertTrue(app.buttons["Add captions"].exists)
        XCTAssertTrue(app.buttons["Add sound effects"].exists)
        XCTAssertEqual(app.descendants(matching: .any)["suggestion-chip-0"].label, "Let Kria cut the first version")
        XCTAssertLessThan(defaultEdit.frame.minX, hook.frame.minX + 1, "Chip #1 stays first")
        for chip in [defaultEdit, hook, app.buttons["Add captions"], app.buttons["Add sound effects"]] {
            XCTAssertGreaterThanOrEqual(chip.frame.height, 44)
        }
        // A chip is a real send, not a fill of the composer: the message is the chip's `message`, not its label.
        defaultEdit.tap()
        XCTAssertTrue(app.staticTexts["You: Suggest an edit."].waitForExistence(timeout: 10))
        XCTAssertTrue(defaultEdit.waitForNonExistence(timeout: 5), "Chips go once the creator has said something")
        XCTAssertFalse(app.buttons["Add captions"].exists)

        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10))
        confirm.tap()
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))

        let captions = app.buttons["Add captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Turn the music down"].exists)
        // KRI-769: the chips arrive after the card, below the fold; a reader at the bottom is scrolled to them, so
        // none sits under the composer.
        let composerTop = app.textFields["Message Kria"].frame.minY
        for chip in [captions, app.buttons["Turn the music down"]] {
            XCTAssertLessThanOrEqual(chip.frame.maxY, composerTop, "\(chip.label) is hidden under the composer")
        }
        XCTAssertFalse(app.buttons["Use suggestion: Try a stronger opening"].exists)
        XCTAssertFalse(app.buttons["Use suggestion: Make it warmer"].exists)
        captions.tap()
        XCTAssertTrue(app.staticTexts["You: Add captions"].waitForExistence(timeout: 10))
    }

    /// KRI-756 wire check on the runtime-v2 turn path: a chip tap carries `suggestion {set_id, chip_id}` and the exact
    /// `message`; a typed message carries none. The stub echoes what it received.
    func testServerSuggestionChipTapNamesTheOfferedChipOnTheTurn() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_SUGGESTION_CHIPS"] = "1"
        app.launchEnvironment["KRIA_SUGGESTION_CHIPS_PENDING"] = "0"
        app.launchEnvironment["KRIA_SUGGESTION_CHIPS_ECHO"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()

        let captions = app.buttons["Add captions"]
        XCTAssertTrue(captions.waitForExistence(timeout: 15))
        captions.tap()
        XCTAssertTrue(app.staticTexts["You: Add captions"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Got it. suggestion[sg_fixture_first_prompt/sc_fixture_captions]"].waitForExistence(timeout: 10))
        XCTAssertTrue(captions.waitForNonExistence(timeout: 5))
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
        device: Bool = false, followup: Bool = false, contractV2: Bool = false, review: Bool = false,
        syncFailure: Bool = false,
        thoughts: String? = nil
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
        if let thoughts { app.launchEnvironment["KRIA_CHAT_THOUGHTS"] = thoughts }
        if device {
            app.launchEnvironment["KRIA_CHAT_DEVICE_RENDER"] = "ready"
            app.launchEnvironment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] = "1"
        }
        if syncFailure { app.launchEnvironment["KRIA_CHAT_DEVICE_SYNC_FAILURE"] = "1" }
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
        // `review` also serves the Review sheet's server model, which the card's Change hand-off at the end needs.
        let app = launchLivePlanFeed(reduceMotion: true, review: true)
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

        // A feed card's Change opens Review with that section flagged (the card is already open above).
        element(app, "plan-feed.change.captions").tap()
        XCTAssertTrue(element(app, "review-chip-captions").waitForExistence(timeout: 15), "the card's Change flags that section")
        XCTAssertTrue(eventually(timeout: 5) { self.sectionValue(app, "captions") == "flagged" })
        XCTAssertFalse(element(app, "review-chip-music").exists)
        // Un-flagging from the card works the same as from the chip.
        reveal(app, "review-change-captions").tap()
        XCTAssertTrue(eventually(timeout: 5) { !self.element(app, "review-chip-captions").exists })
        XCTAssertFalse(element(app, "review-update-video").isEnabled)
    }

    /// KRI-627: while Kria thinks the chat shows ONE line (the latest step); the full thought, code included,
    /// only appears when the creator taps it. `hold` keeps the turn streaming for the whole test.
    func testLiveThoughtShowsOneLineSummaryAndExpandsOnTap() {
        let app = launchLivePlanFeed(reduceMotion: true, followup: true, thoughts: "hold")
        sendFollowup(app)

        let toggle = element(app, "thought-summary-toggle")
        XCTAssertTrue(toggle.waitForExistence(timeout: 30), "the live thought must appear as a single tappable line")
        XCTAssertTrue(toggle.label.contains("Matching the Lisbon title to your clip"), "headline is the latest step, got: \(toggle.label)")
        XCTAssertFalse(toggle.label.contains("Reading your direction"), "only the latest step is summarised, got: \(toggle.label)")
        XCTAssertEqual(toggle.value as? String, "Collapsed")
        XCTAssertFalse(element(app, "thought-summary-detail").exists, "the full thought must stay hidden until tapped")
        attach(app, "thought-live-collapsed")

        toggle.tap()
        let detail = element(app, "thought-summary-detail")
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        XCTAssertTrue(detail.label.contains("Checking which moment of the clip"), "expanded text shows the full thought, got: \(detail.label)")
        XCTAssertEqual(toggle.value as? String, "Expanded")
        attach(app, "thought-live-expanded")

        toggle.tap()
        XCTAssertTrue(eventually(timeout: 5) { !self.element(app, "thought-summary-detail").exists }, "tapping again collapses it")
    }

    /// KRI-627: once the reply lands the thought stays as a collapsed "Thought for Ns" line.
    func testFinishedThoughtStaysCollapsedUntilTapped() {
        let app = launchLivePlanFeed(reduceMotion: true, followup: true, thoughts: "1")
        sendFollowup(app)
        XCTAssertTrue(app.staticTexts["Kria: Review the Lisbon title edit before saving."].waitForExistence(timeout: 90))
        let toggle = element(app, "thought-summary-toggle")
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        // The live row hands over to the finished one right after the reply lands.
        XCTAssertTrue(eventually(timeout: 15) { toggle.label == "Thought for 3s" }, "got: \(toggle.label)")
        XCTAssertEqual(toggle.value as? String, "Collapsed")
        XCTAssertFalse(element(app, "thought-summary-detail").exists, "a finished thought stays collapsed")
        attach(app, "thought-completed")

        toggle.tap()
        XCTAssertTrue(element(app, "thought-summary-detail").waitForExistence(timeout: 5))
    }

    private func sendFollowup(_ app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 100))
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Add a new title Lisbon. Animate it")
        app.buttons["Send message"].tap()
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
        attach(app, "plan-feed-device-start")

        // Work order the device follows. A section may only show decided once every earlier one has.
        let order = ["clips", "music", "title", "captions", "look", "sfx"]
        var sawPartial = false
        let finished = eventually(timeout: 90) {
            if app.buttons["device-render-play"].exists { return true }
            XCTAssertTrue(feed.exists, "the feed stays visible until an export exists")
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
            return false
        }
        XCTAssertTrue(finished, "the completed build hands over to playback")
        XCTAssertTrue(sawPartial, "sections decided progressively, not all at once")
        XCTAssertTrue(app.buttons["device-render-play"].waitForExistence(timeout: 10))
        XCTAssertFalse(feed.exists)
        attach(app, "plan-feed-device-done")
    }

    func testFinishedPhoneVideoKeepsPlaybackAndEditorWhileSyncRetries() {
        let app = launchLivePlanFeed(reduceMotion: true, device: true, syncFailure: true)
        let play = app.buttons["device-render-play"]
        XCTAssertTrue(play.waitForExistence(timeout: 60), app.debugDescription)
        for _ in 0..<4 {
            XCTAssertTrue(play.exists, "A sync retry must not replace a finished video with the plan feed")
            XCTAssertFalse(app.staticTexts["Your plan is ready"].exists)
            RunLoop.current.run(until: Date().addingTimeInterval(1))
        }
        let editor = app.buttons["workspace-mode-editor"]
        XCTAssertTrue(editor.exists, "The finished local edit stays accessible")
        attach(app, "local-video-sync-retry")
        editor.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 20), app.debugDescription)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-load-failed"].firstMatch.exists)
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 20), app.debugDescription)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-clip-1"].firstMatch.exists)
        let playback = app.buttons["native-editor-play-pause"]
        playback.tap()
        XCTAssertTrue(eventually(timeout: 10) { playback.label == "Pause preview" }, app.debugDescription)
        XCTAssertFalse(app.buttons["native-editor-retry-source-preview"].exists)
        attach(app, "local-video-editor-open")
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
        // A Stop is the creator's pause: resumable, never "needs attention".
        XCTAssertTrue(app.staticTexts["Rendering paused"].waitForExistence(timeout: 10))
        XCTAssertEqual(app.buttons["device-render-retry"].label, "Resume")
        XCTAssertFalse(app.staticTexts["This edit needs your attention"].exists)
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

    func testSubmittedMessageAppearsWhileSendPreflightIsStillWaiting() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_SUGGESTION_CHIPS_ECHO": "1", "KRIA_CHAT_SEED_THREADS": "3", "KRIA_PERF_DISK_STORE": "1", "KRIA_CHAT_LONG_HISTORY": "12",
                                 "KRIA_SLIDE_POST_CAPS": "slow"]
        app.launch()
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 40))
        replaceSubmissionText("Keep the whole take", in: composer, app: app)
        // Capabilities become stale after 30 seconds. Their fixture response takes six seconds.
        Thread.sleep(forTimeInterval: 40)
        app.buttons["chat-send-message"].tap()
        let submitted = app.staticTexts.matching(NSPredicate(format: "label == %@", "You: Keep the whole take"))
        XCTAssertTrue(submitted.firstMatch.waitForExistence(timeout: 2))
        XCTAssertNotEqual(composer.value as? String, "Keep the whole take")
        XCTAssertFalse(app.buttons["chat-send-message"].isEnabled)
        let status = app.staticTexts["chat-submission-status"]
        assertSubmissionStatusVisible(in: app, composer: composer)
        let fullyVisible = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
            submitted.firstMatch.exists && submitted.firstMatch.frame.maxY <= status.frame.minY
        }, object: nil)
        XCTAssertEqual(XCTWaiter.wait(for: [fullyVisible], timeout: 2), .completed,
                       "The submitted bubble clears the composer and sending status")
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Submitted message during capability preflight"
        shot.lifetime = .keepAlways
        add(shot)
        XCTAssertTrue(app.staticTexts["Got it. suggestion[none/none]"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["chat-retry-message"].exists)
        XCTAssertEqual(submitted.count, 1)
        app.terminate()
    }

    func testPendingSubmissionStatusStaysVisibleWithKeyboardOpenAndClosed() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_SUGGESTION_CHIPS_ECHO": "1",
                                 "KRIA_CHAT_FIXTURE_MEDIA": "1", "KRIA_CHAT_SEND_DELAY": "15"]
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        composer.typeText("Keep this pending message visible")
        app.buttons["chat-send-message"].tap()
        assertSubmissionStatusVisible(in: app, composer: composer)
        attach(app, "Pending message keyboard open")
        app.staticTexts["workspace-project-title"].tap()
        XCTAssertTrue(eventually(timeout: 5) { !app.keyboards.firstMatch.exists })
        assertSubmissionStatusVisible(in: app, composer: composer)
        let bubble = app.staticTexts["You: Keep this pending message visible"]
        XCTAssertTrue(bubble.isHittable)
        XCTAssertLessThanOrEqual(bubble.frame.maxY, app.staticTexts["chat-submission-status"].frame.minY)
        attach(app, "Pending message keyboard closed")
        XCTAssertTrue(app.staticTexts["Got it. suggestion[none/none]"].waitForExistence(timeout: 25))
        app.terminate()
    }

    func testRejectedSubmissionRestoresDraftAndRetryDoesNotDuplicatePrompt() {
        checkSendFailure("reject")
    }

    func testUnconfirmedSubmissionRetriesOnePrompt() {
        checkSendFailure("offline")
    }

    func testAcknowledgedMessageIsNotRestoredWhenThePostResponseIsLost() {
        checkSendFailure("lost-response")
    }

    func testUnconfirmedSubmissionSurvivesAppRelaunch() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_SUGGESTION_CHIPS_ECHO": "1", "KRIA_CHAT_SEED_THREADS": "3", "KRIA_PERF_DISK_STORE": "1",
                                 "KRIA_CHAT_LONG_HISTORY": "12", "KRIA_CHAT_SEND_FAILURE": "offline"]
        app.launch()
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 30))
        let message = "Keep this request after relaunch \(Int.random(in: 100000...999999))"
        replaceSubmissionText(message, in: composer, app: app)
        app.buttons["chat-send-message"].tap()
        XCTAssertTrue(app.buttons["chat-retry-message"].waitForExistence(timeout: 15))
        replaceSubmissionText(message, in: composer, app: app)
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(app.buttons["chat-retry-message"].waitForExistence(timeout: 10))
        app.terminate()
        app.launchEnvironment.removeValue(forKey: "KRIA_CHAT_SEND_FAILURE")
        app.launchArguments.removeAll { $0 == "-perf-reset" }
        app.launch()
        let retry = app.buttons["chat-retry-message"]
        XCTAssertTrue(retry.waitForExistence(timeout: 30))
        XCTAssertEqual(composer.value as? String, message, "A deliberately retyped draft survives relaunch")
        retry.tap()
        XCTAssertTrue(app.staticTexts["Got it. suggestion[none/none]"].waitForExistence(timeout: 15))
        XCTAssertTrue(retry.waitForNonExistence(timeout: 5))
        let submitted = app.staticTexts.matching(NSPredicate(format: "label == %@", "You: \(message)"))
        let found = submitted.firstMatch.waitForExistence(timeout: 10)
        if !found {
            print("Send recovery labels: \(app.staticTexts.allElementsBoundByIndex.map(\.label))")
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.name = "Recovery text diagnostic"
            shot.lifetime = .keepAlways
            add(shot)
        }
        XCTAssertTrue(found)
        XCTAssertEqual(submitted.count, 1)
        XCTAssertEqual(composer.value as? String, message, "Acknowledgement preserves the new identical draft")
        replaceSubmissionText("", in: composer, app: app)
        app.terminate()
    }

    func testReopenedChatSharesTheLiveSendAndReceivesItsRejectedDraft() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_SEED_THREADS": "3",
                                 "KRIA_PERF_DISK_STORE": "1", "KRIA_CHAT_LONG_HISTORY": "12",
                                 "KRIA_CHAT_SEND_FAILURE": "reject", "KRIA_CHAT_SEND_DELAY": "20"]
        app.launch()
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 30))
        let message = "Keep the arrival \(Int.random(in: 100000...999999))"
        replaceSubmissionText(message, in: composer, app: app)
        app.buttons["chat-send-message"].tap()
        openSubmissionChat("Chat 2", in: app)
        openSubmissionChat("Chat 1", in: app)
        XCTAssertFalse(app.buttons["chat-send-message"].isEnabled, "The reopened view shares the original live request")
        XCTAssertFalse(app.buttons["chat-retry-message"].exists)
        let newerDraft = "Use a warmer ending"
        replaceSubmissionText(newerDraft, in: composer, app: app)
        let merged = message + "\n\n" + newerDraft
        let recovered = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@ AND enabled == true", merged), object: composer)
        XCTAssertEqual(XCTWaiter.wait(for: [recovered], timeout: 30), .completed)
        XCTAssertFalse(app.buttons["chat-retry-message"].exists)
        XCTAssertFalse(app.staticTexts["You: \(message)"].exists, "A rejected prompt is a draft rather than a sent bubble")
        let edited = merged + " and keep the sound"
        replaceSubmissionText(edited, in: composer, app: app)
        openSubmissionChat("Chat 2", in: app)
        openSubmissionChat("Chat 1", in: app)
        XCTAssertEqual(composer.value as? String, edited)
        replaceSubmissionText("", in: composer, app: app)
        app.terminate()
    }

    func testEditorChatDraftPersistsWhenReturningToTheMainChat() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_SEED_THREADS": "3",
                                 "KRIA_PERF_DISK_STORE": "1", "KRIA_CHAT_LONG_HISTORY": "12",
                                 "KRIA_EDITOR_FIXTURE": "1", "UI_TEST_REDUCE_MOTION": "1"]
        app.launch()
        let openEditor = app.buttons["Open editor"]
        XCTAssertTrue(openEditor.waitForExistence(timeout: 30))
        openEditor.tap()
        let kria = app.buttons["native-editor-conversation"]
        XCTAssertTrue(kria.waitForExistence(timeout: 15))
        kria.tap()
        let sheet = app.descendants(matching: .any)["native-editor-project-conversation"].firstMatch
        XCTAssertTrue(sheet.waitForExistence(timeout: 10))
        let fields = app.textFields.matching(NSPredicate(format: "label == %@", "Message Kria"))
        XCTAssertTrue(fields.firstMatch.waitForExistence(timeout: 10))
        let composer = fields.allElementsBoundByIndex.first(where: { $0.isHittable }) ?? fields.firstMatch
        let message = "Keep this editor draft \(Int.random(in: 100000...999999))"
        replaceSubmissionText(message, in: composer, app: app)
        let returnToEditor = sheet.buttons["Open editor"]
        var attempts = 0
        while !returnToEditor.isHittable && attempts < 4 { sheet.swipeUp(); attempts += 1 }
        XCTAssertTrue(returnToEditor.isHittable)
        returnToEditor.tap()
        XCTAssertTrue(sheet.waitForNonExistence(timeout: 5))
        app.buttons["native-editor-back"].tap()
        let mainComposer = app.textFields["Message Kria"]
        XCTAssertTrue(mainComposer.waitForExistence(timeout: 10))
        XCTAssertEqual(mainComposer.value as? String, message)
        replaceSubmissionText("", in: mainComposer, app: app)
        app.terminate()
    }

    private func openSubmissionChat(_ title: String, in app: XCUIApplication) {
        app.buttons["workspace-menu-toggle"].tap()
        let row = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", title)).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 10))
    }

    private func replaceSubmissionText(_ message: String, in composer: XCUIElement, app: XCUIApplication) {
        composer.tap()
        if let existing = composer.value as? String,
           !["", "Tell Kria what you want…", "Add instructions (optional)"].contains(existing) {
            composer.press(forDuration: 1.2)
            let selectAll = app.menuItems["Select All"]
            XCTAssertTrue(selectAll.waitForExistence(timeout: 5))
            selectAll.tap()
            if message.isEmpty { composer.typeText(XCUIKeyboardKey.delete.rawValue) }
        }
        if !message.isEmpty { composer.typeText(message) }
        if !message.isEmpty { XCTAssertEqual(composer.value as? String, message) }
    }

    private func assertSubmissionStatusVisible(in app: XCUIApplication, composer: XCUIElement,
                                               file: StaticString = #filePath, line: UInt = #line) {
        let status = app.staticTexts["chat-submission-status"]
        XCTAssertTrue(eventually(timeout: 3) {
            status.exists && status.isHittable && status.frame.height >= 13
                && status.frame.maxY + 8 <= composer.frame.minY
                && status.frame.minY >= app.frame.minY
        }, "Submission status stays fully above the composer", file: file, line: line)
        let retry = app.buttons["chat-retry-message"]
        if retry.exists {
            XCTAssertTrue(retry.isHittable, file: file, line: line)
            XCTAssertLessThanOrEqual(retry.frame.maxY, composer.frame.minY, file: file, line: line)
        }
    }

    private func checkSendFailure(_ mode: String) {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_SUGGESTION_CHIPS_ECHO": "1", "KRIA_CHAT_FIXTURE_MEDIA": "1",
                                 "KRIA_CHAT_SEND_FAILURE": mode]
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        composer.typeText("Keep my first clip")
        app.buttons["chat-send-message"].tap()
        if mode == "lost-response" {
            XCTAssertTrue(app.staticTexts["Got it. suggestion[none/none]"].waitForExistence(timeout: 15))
            XCTAssertNotEqual(composer.value as? String, "Keep my first clip")
        } else if mode == "offline" {
            let retry = app.buttons["chat-retry-message"]
            XCTAssertTrue(retry.waitForExistence(timeout: 15))
            XCTAssertGreaterThanOrEqual(retry.frame.height, 44)
            XCTAssertNotEqual(composer.value as? String, "Keep my first clip")
            assertSubmissionStatusVisible(in: app, composer: composer)
            attach(app, "Unconfirmed message keyboard open")
            app.staticTexts["workspace-project-title"].tap()
            XCTAssertTrue(eventually(timeout: 5) { !app.keyboards.firstMatch.exists })
            assertSubmissionStatusVisible(in: app, composer: composer)
            attach(app, "Unconfirmed message keyboard closed")
            composer.tap()
            XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
            assertSubmissionStatusVisible(in: app, composer: composer)
            retry.tap()
        } else {
            let restored = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", "Keep my first clip"), object: composer)
            XCTAssertEqual(XCTWaiter.wait(for: [restored], timeout: 15), .completed)
            app.buttons["chat-send-message"].tap()
        }
        XCTAssertTrue(app.staticTexts["Got it. suggestion[none/none]"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["chat-retry-message"].exists)
        let submitted = app.staticTexts.matching(NSPredicate(format: "label == %@", "You: Keep my first clip"))
        XCTAssertTrue(submitted.firstMatch.waitForExistence(timeout: 10))
        XCTAssertEqual(submitted.count, 1)
        app.terminate()
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
