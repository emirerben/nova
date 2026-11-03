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
        XCTAssertTrue(app.staticTexts["This voice is AI-generated. Listen before you use it."].exists)
        reveal(submit, in: app)
        submit.tap()
        let play = app.buttons["generated-media-play"]
        XCTAssertTrue(play.waitForExistence(timeout: 15))
        reveal(play, in: app)
        let use = app.buttons["generated-media-use"]
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: use)], timeout: 10), .completed,
                       "The AI voice is not locked behind a listen")
        play.tap()
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

    func testRejectedOverlayKeepsItsPromptAndTryAgainRunsAtOnce() {
        let app = launch(enabled: true, rejectOnce: true)
        openOverlays(in: app)
        describeOverlay(in: app)
        let problem = app.otherElements["generated-media-problem"]
        XCTAssertTrue(problem.waitForExistence(timeout: 10))
        let failed = XCTAttachment(screenshot: app.screenshot())
        failed.name = "Rejected overlay problem card"
        failed.lifetime = .keepAlways
        add(failed)
        XCTAssertTrue(app.staticTexts["I couldn’t make this overlay"].exists)
        app.buttons["Try again"].tap()
        useOverlays(in: app, screenshotName: "Try again ran without a confirm")
    }

    func testTwoOverlaysEachWithTheirOwnPromptAreUsedTogether() {
        let app = launch(enabled: true)
        openOverlays(in: app)
        describeOverlay(in: app)
        let results = XCTAttachment(screenshot: app.screenshot())
        results.name = "Results with one overlay"
        results.lifetime = .keepAlways
        add(results)
        let use = app.buttons["generated-media-use"]
        XCTAssertTrue(use.waitForExistence(timeout: 15))
        app.buttons["overlay-add"].tap()
        let second = app.textViews["overlay-prompt-2"].exists ? app.textViews["overlay-prompt-2"] : app.textFields["overlay-prompt-2"]
        XCTAssertTrue(second.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Copied from overlay 1. Edit freely."].exists)
        second.tap()
        second.typeText(", at golden hour")
        let compose = XCTAttachment(screenshot: app.screenshot())
        compose.name = "Add overlay copies the last prompt"
        compose.lifetime = .keepAlways
        add(compose)
        app.buttons["generated-media-submit"].tap()
        XCTAssertTrue(app.staticTexts["Your 2 overlays"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.buttons["overlay-edit-2"].exists && app.buttons["overlay-redo-2"].exists && app.buttons["overlay-remove-2"].exists)
        useOverlays(in: app, screenshotName: "Two overlays")
        let edits = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "generate-image-edit-"))
        XCTAssertTrue(edits.firstMatch.waitForExistence(timeout: 10))
        XCTAssertEqual(edits.count, 2, "Both overlays reach the visual pool, each as its own image")
    }

    func testImageRecoveryAfterProcessRelaunchNeverPostsAgain() {
        let app = launch(enabled: true, persistent: true)
        openOverlays(in: app)
        describeOverlay(in: app)
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 10))
        app.terminate()
        app.launchEnvironment["KRIA_GENERATED_MEDIA_RECOVERED"] = "1"
        app.launch()
        XCTAssertTrue(app.buttons["voiceover-script-record"].waitForExistence(timeout: 25))
        goToOverlaysStep(in: app)
        XCTAssertTrue(app.buttons["generated-media-resume"].waitForExistence(timeout: 15))
        app.buttons["generated-media-resume"].tap()
        XCTAssertTrue(app.buttons["generated-media-use"].waitForExistence(timeout: 15))
        XCTAssertFalse(app.buttons["generated-media-submit"].exists)
        useOverlays(in: app, screenshotName: "Relaunched overlay recovered with one generation request")
    }

    private func goToOverlaysStep(in app: XCUIApplication) {
        app.buttons["voiceover-script-record"].tap()
        XCTAssertTrue(app.buttons["voiceover-skip"].waitForExistence(timeout: 10))
        app.buttons["voiceover-skip"].tap()
    }

    private func openOverlays(in app: XCUIApplication) {
        goToOverlaysStep(in: app)
        let generate = app.buttons["generate-image"]
        XCTAssertTrue(generate.waitForExistence(timeout: 10))
        generate.tap()
        let choice = XCTAttachment(screenshot: app.screenshot())
        choice.name = "Choose how to make overlays"
        choice.lifetime = .keepAlways
        add(choice)
    }

    /// "I'll describe it", one prompt, Make overlay.
    private func describeOverlay(in app: XCUIApplication) {
        app.buttons["overlay-choice-describe"].tap()
        let prompt = app.textViews["overlay-prompt-1"].exists ? app.textViews["overlay-prompt-1"] : app.textFields["overlay-prompt-1"]
        XCTAssertTrue(prompt.waitForExistence(timeout: 5))
        prompt.tap()
        prompt.typeText("A coastal village in warm sunlight")
        app.buttons["generated-media-submit"].tap()
    }

    private func useOverlays(in app: XCUIApplication, screenshotName: String) {
        let use = app.buttons["generated-media-use"]
        XCTAssertTrue(use.waitForExistence(timeout: 15))
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: use)], timeout: 15), .completed)
        let preview = XCTAttachment(screenshot: app.screenshot())
        preview.name = screenshotName + " before Use"
        preview.lifetime = .keepAlways
        add(preview)
        use.tap()
        let title = app.staticTexts["A coastal village in warm sunlight"]
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
