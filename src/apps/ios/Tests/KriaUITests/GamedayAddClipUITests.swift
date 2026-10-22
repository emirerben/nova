import XCTest

/// KRI-669: provider/HTTP answers are deterministic, but original resolution,
/// placement, composition, timeline selection, undo and export are production.
@MainActor
final class GamedayAddClipUITests: XCTestCase {
    func testEditedGamedayAddsFifthClipAndReopensCurrentPreview() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-gameday-add", "-ui-testing-editor-gameday-reset"]
        app.launch()
        let probe = app.staticTexts["gameday-fixture-state"]
        waitFor(probe, contains: "clips=4;previewClips=4")
        openQuickAdd(app, option: "native-editor-add-menu-text")
        let input = app.textViews["native-editor-new-text-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap(); input.typeText("Gameday edited title")
        app.buttons["native-editor-text-done"].tap()
        let inspectorDone = app.buttons["native-editor-text-inspector-done"]
        XCTAssertTrue(inspectorDone.waitForExistence(timeout: 5)); inspectorDone.tap()
        waitFor(probe, contains: "Gameday edited title")
        openQuickAdd(app, option: "native-editor-add-menu-video")
        waitFor(probe, contains: "clips=5;previewClips=5")
        XCTAssertTrue(probe.label.contains("Gameday edited title"))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-clip-5"].exists, "The imported clip must be visible in the timeline")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-preview-fallback"].exists)
        XCTAssertNotEqual(app.staticTexts["native-editor-current-time"].value as? String, "0:00.0", "The appended clip is revealed")
        capture(app, "Fifth clip selected with current text")
        app.buttons["native-editor-undo"].tap()
        waitFor(probe, contains: "clips=4;previewClips=4")
        XCTAssertTrue(probe.label.contains("Gameday edited title"), "Undo removes only the imported clip")
        app.buttons["native-editor-redo"].tap()
        waitFor(probe, contains: "clips=5;previewClips=5")
        app.buttons["gameday-fixture-save"].tap()
        // Save persists through the actual editor-commit request; the local
        // fixture keeps its returned canonical snapshot for process relaunch.
        waitFor(probe, contains: "saved=true")
        app.terminate()
        app.launchArguments.removeAll { $0 == "-ui-testing-editor-gameday-reset" }
        app.launch()
        waitFor(probe, contains: "clips=5;previewClips=5")
        XCTAssertTrue(probe.label.contains("Gameday edited title"))
        capture(app, "Reopened fifth clip and edited text")
        app.buttons["native-editor-export"].tap()
        let save = app.buttons["Save to Photos"]
        XCTAssertTrue(save.waitForExistence(timeout: 15)); save.tap()
        let exported = app.descendants(matching: .any)["native-editor-export-state"].firstMatch
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let deadline = Date().addingTimeInterval(150)
        while Date() < deadline {
            let alert = springboard.alerts.firstMatch
            if alert.exists, alert.buttons["Allow"].exists { alert.buttons["Allow"].tap() }
            if exported.exists, exported.label.contains("Saved to Photos") { break }
            RunLoop.current.run(until: Date().addingTimeInterval(0.25))
        }
        XCTAssertTrue(exported.label.contains("Saved to Photos"), "Export must complete from the current composed preview")
        capture(app, "Exported current five-clip edit")
    }

    private func openQuickAdd(_ app: XCUIApplication, option identifier: String) {
        let plus = app.buttons["native-editor-add-clip"]
        waitUntilHittable(plus)
        plus.tap()
        let option = app.descendants(matching: .any)[identifier]
        XCTAssertTrue(option.waitForExistence(timeout: 5), "The menu must open; plus state: \(plus.label)")
        waitUntilHittable(option)
        option.tap()
    }

    private func waitUntilHittable(_ element: XCUIElement) {
        XCTAssertTrue(element.waitForExistence(timeout: 5))
        // After inspector dismissal XCUI can briefly expose the whole window
        // with infinite origin and zero size. isHittable aborts on that snapshot
        // instead of returning false, so wait for usable geometry first within
        // the same deadline. A genuinely unavailable control still fails.
        let hittable = expectation(for: NSPredicate { _, _ in
            guard element.exists else { return false }
            let frame = element.frame
            guard frame.origin.x.isFinite, frame.origin.y.isFinite,
                  frame.width.isFinite, frame.height.isFinite,
                  frame.width > 0, frame.height > 0 else { return false }
            return element.isHittable
        }, evaluatedWith: element)
        XCTAssertEqual(XCTWaiter.wait(for: [hittable], timeout: 5), .completed, "Control must be hittable: \(element.identifier)")
    }

    private func waitFor(_ element: XCUIElement, contains value: String) {
        XCTAssertTrue(element.waitForExistence(timeout: 20))
        let ready = expectation(for: NSPredicate(format: "label CONTAINS %@", value), evaluatedWith: element)
        let result = XCTWaiter.wait(for: [ready], timeout: 25)
        XCTAssertEqual(result, .completed, "Expected \(value); actual fixture state: \(element.label)")
    }

    private func capture(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name; attachment.lifetime = .keepAlways; add(attachment)
    }
}
