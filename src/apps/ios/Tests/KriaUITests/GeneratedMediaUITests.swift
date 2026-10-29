import XCTest

@MainActor final class GeneratedMediaUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    private func launch(enabled: Bool, persistent: Bool = false, rejectOnce: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment = ["KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_FIXTURE_MEDIA": "1",
                                 "KRIA_CHAT_VOICEOVER_SCRIPT": "1", "KRIA_GENERATED_MEDIA": enabled ? "1" : "off",
                                 "UI_TEST_REDUCE_MOTION": "1"]
        if rejectOnce { app.launchEnvironment["KRIA_GENERATED_MEDIA_REJECT_ONCE"] = "1" }
        if persistent { app.launchEnvironment["KRIA_GENERATED_MEDIA_PERSIST"] = UUID().uuidString }
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.buttons["format-montage"].waitForExistence(timeout: 20))
        app.buttons["format-montage"].tap()
        XCTAssertTrue(app.buttons["Send clips"].waitForExistence(timeout: 10))
        app.buttons["Send clips"].tap()
        XCTAssertTrue(app.buttons["voiceover-script-record"].waitForExistence(timeout: 20))
        return app
    }

    func testApprovedNarrationPreviewThenUseAttachesAudioWithoutImageAsset() {
        let app = launch(enabled: true)
        let options = app.buttons["voiceover-script-ai-voice"]
        XCTAssertTrue(options.waitForExistence(timeout: 5))
        reveal(options, in: app)
        options.tap()
        let generate = app.buttons["generate-narration"]
        XCTAssertTrue(generate.waitForExistence(timeout: 10))
        reveal(generate, in: app)
        generate.tap()
        let submit = app.buttons["generated-media-submit"]
        XCTAssertTrue(submit.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["This voice is AI-generated. Review the script and listen before using it."].exists)
        reveal(submit, in: app)
        submit.tap()
        let play = app.buttons["generated-media-play"]
        XCTAssertTrue(play.waitForExistence(timeout: 15))
        reveal(play, in: app)
        let use = app.buttons["generated-media-use"]
        XCTAssertFalse(use.isEnabled)
        play.tap()
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: use)], timeout: 10), .completed)
        let preview = XCTAttachment(screenshot: app.screenshot())
        preview.name = "AI voice preview before explicit Use"
        preview.lifetime = .keepAlways
        add(preview)
        reveal(use, in: app)
        use.tap()
        XCTAssertTrue(app.staticTexts["Generated narration.wav"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
        let attached = XCTAttachment(screenshot: app.screenshot())
        attached.name = "Generated narration attached through voiceover flow"
        attached.lifetime = .keepAlways
        add(attached)
    }

    func testDisabledCapabilitiesPreserveRecordImportAndHideGeneration() {
        let app = launch(enabled: false)
        XCTAssertFalse(app.buttons["voiceover-script-ai-voice"].exists)
        XCTAssertTrue(app.buttons["voiceover-script-import"].exists)
        app.buttons["voiceover-script-record"].tap()
        XCTAssertTrue(app.buttons["voiceover-record"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["generate-narration"].exists)
        app.buttons["voiceover-skip"].tap()
        XCTAssertTrue(app.buttons["attachment-done"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["generate-image"].exists)
        XCTAssertFalse(app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "generate-image-edit-")).firstMatch.exists)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Capabilities off keeps generation hidden"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }

    func testValidationRejectionRequiresEditAndExplicitNewSubmission() {
        let app = launch(enabled: true, rejectOnce: true)
        openImages(in: app)
        submitImage(in: app)
        let edit = app.buttons["generated-media-edit-rejected"]
        XCTAssertTrue(edit.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
        XCTAssertFalse(app.buttons["generated-media-use"].exists)
        edit.tap()
        let submit = app.buttons["generated-media-submit"]
        XCTAssertTrue(submit.waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["generated-media-title"].isEnabled)
        submit.tap()
        useImage(in: app, screenshotName: "Explicit generation after a rejected request")
    }

    func testGeneratedImagePreviewUseRefreshesVisualPool() {
        let app = launch(enabled: true)
        openImages(in: app)
        submitImage(in: app)
        useImage(in: app, screenshotName: "Generated image preview and visual pool")
    }

    func testImageRecoveryAfterProcessRelaunchNeverPostsAgain() {
        let app = launch(enabled: true, persistent: true)
        openImages(in: app)
        submitImage(in: app)
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 10))
        app.terminate()
        app.launchEnvironment["KRIA_GENERATED_MEDIA_RECOVERED"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["voiceover-script-record"].waitForExistence(timeout: 25))
        openImages(in: app)
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
        useImage(in: app, screenshotName: "Relaunched image recovered with one generation request")
    }

    private func openImages(in app: XCUIApplication) {
        app.buttons["voiceover-script-record"].tap()
        XCTAssertTrue(app.buttons["voiceover-skip"].waitForExistence(timeout: 10))
        app.buttons["voiceover-skip"].tap()
        let generate = app.buttons["generate-image"]
        XCTAssertTrue(generate.waitForExistence(timeout: 10))
        generate.tap()
    }

    private func submitImage(in app: XCUIApplication) {
        let title = app.textFields["generated-media-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 10))
        title.tap()
        title.typeText("Saved cover title")
        let prompt = app.textFields["generated-media-prompt"].exists
            ? app.textFields["generated-media-prompt"] : app.textViews["generated-media-prompt"]
        XCTAssertTrue(prompt.waitForExistence(timeout: 3))
        prompt.tap()
        prompt.typeText("A coastal village in warm sunlight")
        app.buttons["generated-media-submit"].tap()
    }

    private func useImage(in app: XCUIApplication, screenshotName: String) {
        let use = app.buttons["generated-media-use"]
        XCTAssertTrue(use.waitForExistence(timeout: 15))
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: use)], timeout: 15), .completed)
        let preview = XCTAttachment(screenshot: app.screenshot())
        preview.name = screenshotName + " before Use"
        preview.lifetime = .keepAlways
        add(preview)
        reveal(use, in: app)
        use.tap()
        let title = app.staticTexts["Saved cover title"]
        XCTAssertTrue(title.waitForExistence(timeout: 15))
        reveal(title, in: app)
        let pool = XCTAttachment(screenshot: app.screenshot())
        pool.name = screenshotName + " after Use"
        pool.lifetime = .keepAlways
        add(pool)
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
    }

    private func reveal(_ element: XCUIElement, in app: XCUIApplication) {
        for _ in 0..<5 where !element.isHittable { app.swipeUp() }
        XCTAssertTrue(element.isHittable)
    }
}
