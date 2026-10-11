import XCTest
import UIKit

/// Physical-device failure scenarios covered here before implementation:
/// - A real permission prompt must be limited to camera/microphone and never be bypassed by a fixture.
/// - Cancelling the countdown must leave the creator able to start a genuine take.
/// - A normal take must reach Review only after a visible Stop action and at least five recorded seconds.
/// - Backgrounding an in-progress retake must finalize a playable interrupted candidate rather than lose it.
/// - Closing and reopening must restore the reviewed local take without relying on a fake review file.
///
/// This is deliberately opt-in: it drives the paired iPhone camera and microphone, so simulator and
/// ordinary CI runs skip it. The device runner separately copies and inspects the resulting movies.
@MainActor final class TeleprompterHardwareUITests: XCTestCase {
    func testPhysicalCameraTakeInterruptionAndRecovery() throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("Teleprompter hardware capture requires a physical iPhone.")
        #else
        guard ProcessInfo.processInfo.environment["KRIA_TELEPROMPTER_HARDWARE_TEST"] == "1" else {
            throw XCTSkip("Set KRIA_TELEPROMPTER_HARDWARE_TEST=1 in the device test runner to enable camera capture.")
        }

        let app = launchTalking()
        let removePermissionMonitor = addUIInterruptionMonitor(withDescription: "Kria camera or microphone permission") { alert in
            let prompt = "\(alert.label) \(alert.staticTexts.allElementsBoundByIndex.map(\.label).joined(separator: " "))"
            guard prompt.localizedCaseInsensitiveContains("camera") || prompt.localizedCaseInsensitiveContains("microphone") else {
                return false
            }
            let allow = alert.buttons.matching(NSPredicate(format: "label BEGINSWITH %@ OR label == %@", "Allow", "OK")).firstMatch
            guard allow.exists else { return false }
            allow.tap()
            return true
        }
        defer { removeUIInterruptionMonitor(removePermissionMonitor) }

        app.buttons["teleprompter-open"].tap()
        let script = app.textViews["teleprompter-script-editor"]
        XCTAssertTrue(script.waitForExistence(timeout: 8))
        paste("I am recording this on an iPhone. I can pause, retake, and keep an interrupted take.", into: script, app: app)

        app.buttons["teleprompter-open-camera"].tap()
        let record = app.buttons["teleprompter-record"]
        triggerPermissionPrompts(in: app, untilEnabled: record)
        XCTAssertTrue(waitUntilEnabled(record, timeout: 20), "A physical iPhone with granted camera and microphone permission should enable Record.")

        // Countdown cancellation must create no take and leave a usable Record action.
        record.tap()
        let cancel = app.buttons["teleprompter-cancel-countdown"]
        XCTAssertTrue(cancel.waitForExistence(timeout: 3))
        cancel.tap()
        XCTAssertTrue(waitUntilEnabled(record, timeout: 5))

        record.tap()
        let stop = app.buttons["teleprompter-stop-recording"]
        XCTAssertTrue(stop.waitForExistence(timeout: 10), "Stop appears only after AVFoundation confirms recording started.")
        wait(seconds: 5.5)
        stop.tap()
        XCTAssertTrue(app.buttons["teleprompter-retake"].waitForExistence(timeout: 20), "A stopped real take reaches Review.")
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled, "Review exposes a usable local take before intake.")
        screenshot(app, named: "Physical complete take review")

        app.buttons["teleprompter-retake"].tap()
        XCTAssertTrue(waitUntilEnabled(record, timeout: 12))
        record.tap()
        XCTAssertTrue(stop.waitForExistence(timeout: 10))
        wait(seconds: 1)
        XCUIDevice.shared.press(.home)
        wait(seconds: 1)
        app.activate()
        XCTAssertTrue(app.buttons["teleprompter-retake"].waitForExistence(timeout: 25), "Returning from background reaches interrupted-take review.")
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled, "The interrupted take is playable and recoverable.")
        screenshot(app, named: "Physical interrupted take review")

        app.buttons["teleprompter-close"].tap()
        XCTAssertTrue(app.buttons["teleprompter-open"].waitForExistence(timeout: 10))
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 12), "Closing and reopening restores the local reviewed take.")
        screenshot(app, named: "Physical take restored after reopen")
        #endif
    }

    #if !targetEnvironment(simulator)
    private func launchTalking() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        // Do not set KRIA_TELEPROMPTER_CAMERA_UNAVAILABLE or the review fixture here: this
        // test exercises the device's real camera/microphone path.
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 8))
        XCTAssertTrue(waitUntilEnabled(newChat, timeout: 20))
        newChat.tap()
        let talking = app.buttons["format-talking_to_camera"]
        XCTAssertTrue(talking.waitForExistence(timeout: 10))
        if !talking.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
        talking.tap()
        let attach = app.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 8))
        attach.tap()
        XCTAssertTrue(app.buttons["teleprompter-open"].waitForExistence(timeout: 8))
        return app
    }

    private func paste(_ text: String, into field: XCUIElement, app: XCUIApplication) {
        UIPasteboard.general.string = text
        field.tap()
        field.press(forDuration: 1)
        // Existing native UI coverage observes this as a button on current iOS. Retain the
        // menu-item fallback for devices that expose the older text-selection menu hierarchy.
        let pasteButton = app.buttons["Paste"]
        if pasteButton.waitForExistence(timeout: 3) {
            pasteButton.tap()
        } else {
            let pasteMenuItem = app.menuItems["Paste"]
            XCTAssertTrue(pasteMenuItem.waitForExistence(timeout: 3), "The real script entry path pastes creator text.")
            pasteMenuItem.tap()
        }
        XCTAssertTrue((field.value as? String)?.contains("recording this on an iPhone") == true)
    }

    private func triggerPermissionPrompts(in app: XCUIApplication, untilEnabled record: XCUIElement) {
        // iOS presents camera and microphone privacy alerts serially. A monitor is invoked only
        // by an interaction, so make a small bounded number of harmless taps before passively
        // waiting for capture preparation; the monitor itself accepts no unrelated prompt.
        for _ in 0..<3 where !record.isEnabled {
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            wait(seconds: 0.5)
        }
    }

    private func waitUntilEnabled(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        let expectation = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == true AND enabled == true"), object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func wait(seconds: TimeInterval) {
        let expectation = expectation(description: "Wait \(seconds) seconds")
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { expectation.fulfill() }
        wait(for: [expectation], timeout: seconds + 2)
    }

    private func screenshot(_ app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
    #endif
}
