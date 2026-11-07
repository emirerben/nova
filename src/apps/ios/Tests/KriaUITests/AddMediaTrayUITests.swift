import XCTest

/// The composer's + is the one way to add media (KRI-1272). The footage stage no longer carries an "Add more" row, so
/// each state of the tray must land on the place its row names, or say why it cannot.
@MainActor final class AddMediaTrayUITests: XCTestCase {
    func testClipsOnTheStageHaveNoAddMoreRowAndPlusReachesTheCamera() {
        let app = launch(format: "format-montage")
        XCTAssertTrue(app.descendants(matching: .any)["footage-ready-count"].waitForExistence(timeout: 10), "a clip is attached")
        XCTAssertTrue(app.staticTexts["footage-add-more-hint"].exists, "the stage points to the + instead of repeating it")
        XCTAssertFalse(app.buttons["record-footage"].exists, "no Record beside the clips")
        XCTAssertFalse(app.buttons["choose-videos"].exists, "no Upload beside the clips")
        XCTAssertFalse(app.staticTexts["Add more"].exists)
        capture(app, "Stage with a clip, no Add more row")

        app.buttons["chat-attach"].tap()
        XCTAssertTrue(app.buttons["attach-record"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["attach-record"].isEnabled)
        XCTAssertTrue(app.buttons["attach-upload"].isEnabled)
        XCTAssertFalse(app.staticTexts["attach-tray-full"].exists, "there is still room")
        capture(app, "Tray with room")

        app.buttons["attach-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-close"].waitForExistence(timeout: 20), "Record opens the camera")
        app.buttons["teleprompter-close"].tap()
        assertBackInTheChat(app, "closing the camera from the tray lands in the chat, not on the clip list")
    }

    func testClosingTheCameraFromTheAddMediaStepStaysOnAddMedia() {
        let app = launch(format: "format-montage", media: false)
        let record = app.buttons["record-footage"]
        XCTAssertTrue(record.waitForExistence(timeout: 10), "the stage offers Record before any clip is attached")
        record.tap()
        XCTAssertTrue(app.buttons["teleprompter-close"].waitForExistence(timeout: 20))
        app.buttons["teleprompter-close"].tap()
        XCTAssertTrue(app.buttons["attachment-close"].waitForExistence(timeout: 10), "opened from the stage, the camera returns to Add media")
        XCTAssertTrue(app.buttons["source-record"].exists)
        capture(app, "After closing the camera from the Add media step")

        app.buttons["source-record"].tap()
        XCTAssertTrue(app.buttons["teleprompter-close"].waitForExistence(timeout: 20))
        app.buttons["teleprompter-close"].tap()
        XCTAssertTrue(app.buttons["attachment-close"].waitForExistence(timeout: 10), "Record on the Add media step itself returns to Add media")
        XCTAssertTrue(app.buttons["source-upload"].exists)
    }

    func testTrayUploadThenCancellingTheFilesPickerReturnsToTheChat() {
        let app = launch(format: "format-montage")
        XCTAssertTrue(app.descendants(matching: .any)["footage-ready-count"].waitForExistence(timeout: 10))
        app.buttons["chat-attach"].tap()
        app.buttons["attach-upload"].tap()
        let files = app.buttons["upload-source-files"]
        XCTAssertTrue(files.waitForExistence(timeout: 5))
        files.tap()
        let cancel = app.buttons["Cancel"]
        XCTAssertTrue(cancel.waitForExistence(timeout: 15), "the Files picker opens\n\(app.debugDescription)")
        cancel.tap()
        assertBackInTheChat(app, "cancelling the Files picker from the tray lands in the chat")
    }

    private func assertBackInTheChat(_ app: XCUIApplication, _ message: String) {
        let left = app.buttons["attachment-close"].waitForNonExistence(timeout: 10)
        capture(app, message)
        XCTAssertTrue(left, message)
        let composer = app.buttons["chat-attach"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        XCTAssertTrue(composer.isHittable, "the composer is in front")
        XCTAssertFalse(app.buttons["source-upload"].exists)
    }

    func testPlusAtTheClipLimitExplainsAndOffersManageClips() {
        let app = launch(format: "format-talking_to_camera", talkingMaxClips: "1")
        XCTAssertTrue(app.buttons["manage-footage"].waitForExistence(timeout: 10), "the stage explains the limit")
        XCTAssertFalse(app.staticTexts["footage-add-more-hint"].exists)

        app.buttons["chat-attach"].tap()
        XCTAssertTrue(app.staticTexts["attach-tray-full"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["attach-tray-full"].label.contains("1 clip"))
        XCTAssertFalse(app.buttons["attach-record"].isEnabled, "no camera at the most clips")
        XCTAssertFalse(app.buttons["attach-upload"].isEnabled, "no picker at the most clips")
        XCTAssertFalse(app.buttons["attach-script"].isEnabled)
        capture(app, "Tray at the clip limit")
        app.buttons["attach-upload"].tap()
        XCTAssertFalse(app.buttons["upload-source-photos"].exists, "a full edit never opens a picker")

        app.buttons["attach-manage"].tap()
        XCTAssertTrue(app.buttons["attachment-close"].waitForExistence(timeout: 10), "Manage clips opens Add media")
        XCTAssertTrue(app.buttons["Remove sample.mov"].waitForExistence(timeout: 5), "where a clip can be removed")
        XCTAssertFalse(app.buttons["upload-source-photos"].exists)
        capture(app, "Manage clips from the tray")
    }

    func testTrayAtTheClipLimitReachesManageClipsAtAccessibilitySize() {
        let app = launch(format: "format-talking_to_camera", talkingMaxClips: "1", largeText: true)
        app.buttons["chat-attach"].tap()
        let manage = app.buttons["attach-manage"]
        XCTAssertTrue(manage.waitForExistence(timeout: 10))
        let tray = app.scrollViews.containing(.button, identifier: "attach-manage").firstMatch
        for _ in 0..<4 where !manage.isHittable { tray.swipeUp() }
        XCTAssertTrue(manage.isHittable, "Manage clips stays reachable when the rows outgrow the tray")
        capture(app, "Tray at the clip limit, accessibility5")
        manage.tap()
        XCTAssertTrue(app.buttons["attachment-close"].waitForExistence(timeout: 10))
    }

    func testPlusClosesTheKeyboardSoTheTrayHasRoom() {
        let app = launch(format: "format-montage")
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        app.buttons["chat-attach"].tap()
        XCTAssertTrue(app.buttons["attach-upload"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 5), "the tray replaces the keyboard")
        XCTAssertTrue(app.buttons["attach-record"].isHittable)
        capture(app, "Tray after the keyboard closed")
    }

    func testPlusWhileKriaIsReplyingSaysWhyInsteadOfPromptingForAFormat() {
        let app = launch(format: "format-montage", sendDelay: "20")
        let composer = app.textFields["Message Kria"]
        XCTAssertTrue(composer.waitForExistence(timeout: 10))
        composer.tap()
        composer.typeText("Make it punchy")
        app.buttons["chat-send-message"].tap()
        let plus = app.buttons["chat-attach"]
        XCTAssertTrue(plus.waitForExistence(timeout: 5))
        plus.tap()
        let reason = app.staticTexts["attach-tray-reason"]
        XCTAssertTrue(reason.waitForExistence(timeout: 5))
        XCTAssertTrue(reason.label.contains("replying"), "the reason matches the cause: \(reason.label)")
        XCTAssertFalse(reason.label.contains("format"))
        XCTAssertFalse(app.buttons["attach-record"].isEnabled)
        XCTAssertFalse(app.buttons["attach-upload"].isEnabled)
        capture(app, "Tray while Kria is replying")
    }

    private func launch(format: String, media: Bool = true, talkingMaxClips: String? = nil, largeText: Bool = false, sendDelay: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        if media { app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1" }
        app.launchEnvironment["KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE"] = "1"
        if let talkingMaxClips { app.launchEnvironment["KRIA_CHAT_TALKING_MAX_CLIPS"] = talkingMaxClips }
        if let sendDelay { app.launchEnvironment["KRIA_CHAT_SEND_DELAY"] = sendDelay }
        if largeText {
            app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
            app.launchEnvironment["UI_TEST_REDUCE_MOTION"] = "1"
        }
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        let card = app.buttons[format]
        XCTAssertTrue(card.waitForExistence(timeout: 10))
        if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        card.tap()
        return app
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
