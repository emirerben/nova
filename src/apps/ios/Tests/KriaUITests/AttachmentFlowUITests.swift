import XCTest

@MainActor
final class AttachmentFlowUITests: XCTestCase {
    func testNarratedAttachmentsSwipeAndRequireExplicitAudioChoice() {
        let app = launchAttachments()
        XCTAssertTrue(app.staticTexts["Add footage"].waitForExistence(timeout: 5))
        app.scrollViews["attachment-step-footage"].swipeLeft()
        XCTAssertTrue(app.buttons["voiceover-record"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Upload an audio file"].exists)
        XCTAssertFalse(app.buttons["attachment-next"].exists, "Voiceover needs a deliberate choice")
        capture(app, "Voiceover start")
        app.scrollViews["attachment-step-voiceover"].swipeRight()
        XCTAssertTrue(app.buttons["Choose from Photos"].waitForExistence(timeout: 5))
        app.buttons["attachment-next"].tap()
        XCTAssertTrue(app.buttons["voiceover-skip"].waitForExistence(timeout: 5))
        app.buttons["voiceover-skip"].tap()
        XCTAssertTrue(app.staticTexts["Add overlays"].waitForExistence(timeout: 5))
        capture(app, "Overlays after footage audio choice")
        app.buttons["attachment-done"].tap()
        XCTAssertTrue(app.buttons["Attach footage"].waitForExistence(timeout: 5))
    }

    func testImportedVoiceoverReviewsTimingAndPlaybackBeforeUpload() {
        let app = launchAttachments(review: "1", duration: "3")
        app.buttons["attachment-next"].tap()
        XCTAssertTrue(app.buttons["voiceover-play"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Review voiceover"].exists)
        let warning = app.descendants(matching: .any)["voiceover-duration-warning"].firstMatch
        XCTAssertTrue(warning.waitForExistence(timeout: 5), "Six-second narration must warn against three-second footage")
        XCTAssertTrue(app.buttons["voiceover-use"].isEnabled, "Long voiceovers remain a creator choice")
        XCTAssertTrue(app.buttons["voiceover-choose-another"].isHittable, "Review controls fit the expanded attachment sheet")
        app.buttons["voiceover-play"].tap()
        XCTAssertTrue(app.buttons["voiceover-play"].label.localizedCaseInsensitiveContains("pause"))
        let progressed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "NOT (label BEGINSWITH %@)", "0:00 /"), object: app.staticTexts["voiceover-playback-time"])
        XCTAssertEqual(XCTWaiter.wait(for: [progressed], timeout: 4), .completed)
        app.buttons["voiceover-play"].tap()
        capture(app, "Imported voiceover longer than footage")
        // Opening and listening must not enqueue the file. The offline transport
        // rejects reservations, so any premature enqueue would show an error.
        XCTAssertFalse(app.staticTexts["voiceover-upload-error"].exists)
        app.buttons["attachment-close"].tap()
        XCTAssertTrue(app.buttons["Discard recording"].waitForExistence(timeout: 3))
        // Compact confirmation popovers omit their cancel button on newer
        // iOS versions; tapping outside is the native keep-recording action.
        if app.buttons["Keep recording"].exists { app.buttons["Keep recording"].tap() }
        else if app.buttons["Cancel"].exists { app.buttons["Cancel"].tap() }
        else { app.staticTexts["Review voiceover"].tap() }
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["Discard recording"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 3), .completed)
        XCTAssertTrue(app.buttons["voiceover-use"].waitForExistence(timeout: 3))
        app.buttons["voiceover-use"].tap()
        XCTAssertTrue(app.staticTexts["voiceover-upload-error"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["voiceover-play"].exists, "Failed upload preserves the local take")
        XCTAssertTrue(app.buttons["voiceover-use"].isEnabled, "The same take can be retried")
    }

    func testUnknownFootageDurationDoesNotInventComparison() {
        let app = launchAttachments(review: "1")
        app.buttons["attachment-next"].tap()
        XCTAssertTrue(app.buttons["voiceover-use"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.descendants(matching: .any)["voiceover-duration-warning"].firstMatch.exists)
        XCTAssertTrue(app.buttons["voiceover-use"].isEnabled)
        capture(app, "Voiceover with unknown footage duration")
    }

    func testExistingVoiceoverKeepsItsCapacityAndContinueChoice() {
        for state in ["attached", "preparing"] {
            let app = launchAttachments(existingVoiceover: state)
            app.buttons["attachment-next"].tap()
            let record = app.buttons["voiceover-record"]
            XCTAssertTrue(record.waitForExistence(timeout: 5))
            XCTAssertFalse(record.isEnabled, "An attached or preparing voiceover occupies the single audio slot")
            if state == "attached" {
                app.buttons["Remove existing-voiceover.m4a"].tap()
                XCTAssertTrue(app.staticTexts["attachment-removal-error"].waitForExistence(timeout: 5))
                XCTAssertFalse(record.isEnabled, "A failed removal must keep the audio slot occupied")
            }
            XCTAssertFalse(app.buttons["voiceover-skip"].exists, "Do not claim to use footage audio while keeping a voiceover")
            let next = app.buttons["Continue with voiceover"]
            XCTAssertTrue(next.isHittable)
            next.tap()
            XCTAssertTrue(app.staticTexts["Add overlays"].waitForExistence(timeout: 5))
            app.terminate()
        }
    }

    private func launchAttachments(review: String? = nil, duration: String? = nil, existingVoiceover: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        if existingVoiceover == "attached" {
            app.launchEnvironment["KRIA_CHAT_FIXTURE_VOICEOVER"] = "1"
            app.launchEnvironment["KRIA_CHAT_REMOVE_MEDIA_FAILURE"] = "1"
        }
        if existingVoiceover == "preparing" { app.launchEnvironment["KRIA_VOICEOVER_PREPARING_FIXTURE"] = "1" }
        app.launchEnvironment["KRIA_VOICEOVER_REVIEW_FIXTURE"] = review
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA_DURATION"] = duration
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        let narrated = app.buttons["format-narrated"]
        XCTAssertTrue(narrated.waitForExistence(timeout: 10))
        if !narrated.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        narrated.tap()
        let attach = app.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 5))
        attach.tap()
        XCTAssertTrue(app.buttons["attachment-next"].waitForExistence(timeout: 5))
        return app
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
