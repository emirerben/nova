import XCTest

/// KRI-815: a script Kria wrote from the clips is shown as a card whose Record button opens the voiceover step
/// with that script on screen; without the capability the same script is plain reply text.
final class VoiceoverScriptCardUITests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    private func launch(capability: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_VOICEOVER_SCRIPT"] = capability
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Send clips"]
        XCTAssertTrue(next.waitForExistence(timeout: 5))
        next.tap()
        return app
    }

    func testRecordWithThisScriptOpensTheVoiceoverStepWithTheScript() {
        let app = launch(capability: "1")
        XCTAssertTrue(app.descendants(matching: .any)["voiceover-script-card"].waitForExistence(timeout: 15))
        let words = app.descendants(matching: .any)["voiceover-script-text"]
        XCTAssertTrue(words.waitForExistence(timeout: 3))
        XCTAssertTrue(words.label.contains("This hall stopped me in my tracks."))
        let checks = app.descendants(matching: .any)["voiceover-script-checks"]
        XCTAssertTrue(checks.exists)
        XCTAssertTrue(checks.label.contains("1922"))
        // The reply keeps its lead sentence; the script itself is shown once, in the card.
        let reply = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: Here's a script")).firstMatch
        XCTAssertTrue(reply.exists)
        XCTAssertFalse(reply.label.contains("This hall stopped me in my tracks."))

        let record = app.buttons["voiceover-script-record"]
        XCTAssertTrue(record.exists)
        XCTAssertGreaterThanOrEqual(record.frame.height, 44)
        XCTAssertTrue(app.buttons["voiceover-script-import"].exists)
        record.tap()

        // The sheet opens on the voiceover step (a montage project has none otherwise) with the script to read.
        let readAlong = app.descendants(matching: .any)["voiceover-script-readalong"]
        XCTAssertTrue(readAlong.waitForExistence(timeout: 10))
        XCTAssertTrue(readAlong.label.contains("And the gold mask, found in 1922, glowing under the spotlight."))
        XCTAssertTrue(app.buttons["voiceover-record"].exists, "the record control sits under the script")
        XCTAssertTrue(app.staticTexts["Add voiceover"].exists, "the voiceover step, where importing also lives")
    }

    func testWithoutTheCapabilityTheScriptIsReplyText() {
        let app = launch(capability: "legacy")
        let reply = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "This hall stopped me in my tracks.")).firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 15))
        XCTAssertFalse(app.descendants(matching: .any)["voiceover-script-card"].exists)
        XCTAssertFalse(app.buttons["voiceover-script-record"].exists)
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
}
