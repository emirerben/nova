import XCTest

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
        guard triggerPermissionPrompts(in: app, untilEnabled: record) else { return }
        guard waitUntilEnabled(record, timeout: 20) else {
            XCTFail("A physical iPhone with granted camera and microphone permission should enable Record.")
            return
        }

        // All backgrounds render above the genuine preview. Window manipulation must leave
        // the native capture session ready and its Record control exposed.
        setWindowBackground("glass", in: app, fontChangesAllowed: true)
        screenshot(app, named: "Physical glass window over camera")
        setWindowBackground("solid", in: app)
        screenshot(app, named: "Physical solid window over camera")
        setWindowBackground("transparent", in: app)
        moveAndResizeWindow(in: app, avoiding: record)
        XCTAssertTrue(record.isEnabled)
        screenshot(app, named: "Physical transparent window moved and resized")

        // Countdown cancellation must create no take and leave a usable Record action.
        record.tap()
        let cancel = app.buttons["teleprompter-cancel-countdown"]
        XCTAssertTrue(cancel.waitForExistence(timeout: 3))
        cancel.tap()
        XCTAssertTrue(waitUntilEnabled(record, timeout: 5))

        record.tap()
        let stop = app.buttons["teleprompter-stop-recording"]
        XCTAssertTrue(stop.waitForExistence(timeout: 10), "Stop appears only after AVFoundation confirms recording started.")
        let prompter = app.buttons["teleprompter-pause-text"]
        XCTAssertTrue(prompter.isHittable)
        XCTAssertTrue(app.buttons["Stop prompter"].exists)
        prompter.tap()
        XCTAssertTrue(app.buttons["Start prompter"].exists)
        XCTAssertTrue(stop.isEnabled && stop.isHittable,
                      "Pausing the prompter must leave the active recording control available.")
        prompter.tap()
        XCTAssertTrue(app.buttons["Stop prompter"].exists)
        prompter.tap()
        XCTAssertTrue(app.buttons["Start prompter"].exists)
        XCTAssertTrue(stop.isEnabled && stop.isHittable,
                      "Stopping the prompter must not stop the camera capture.")
        // Presenting settings and manipulating the window during capture must not interrupt
        // AVFoundation. This screenshot is compared with the saved movie after the run.
        setWindowBackground("glass", in: app, fontChangesAllowed: false)
        moveAndResizeWindow(in: app, avoiding: stop)
        XCTAssertTrue(stop.isEnabled)
        if app.buttons["Stop prompter"].exists { prompter.tap() }
        XCTAssertTrue(app.buttons["Start prompter"].exists,
                      "The prompter is paused (or has reached its end) before recording stops.")
        XCTAssertTrue(stop.isEnabled && stop.isHittable,
                      "Pausing the prompter before finalizing must keep camera capture active.")
        screenshot(app, named: "Physical glass window adjusted while recording")
        wait(seconds: 5.5)
        stop.tap()
        let retake = app.buttons["teleprompter-retake"]
        guard retake.waitForExistence(timeout: 20) else {
            XCTFail("A stopped real take reaches Review.")
            return
        }
        let useTake = app.buttons["teleprompter-use-take"]
        guard useTake.isEnabled else {
            XCTFail("Review exposes a usable local take before intake.")
            return
        }
        screenshot(app, named: "Physical complete take review")

        retake.tap()
        guard waitUntilEnabled(record, timeout: 12) else {
            XCTFail("Retake restores an enabled Record action.")
            return
        }
        record.tap()
        XCTAssertTrue(stop.waitForExistence(timeout: 10))
        wait(seconds: 1)
        XCUIDevice.shared.press(.home)
        wait(seconds: 1)
        app.activate()
        guard retake.waitForExistence(timeout: 25) else {
            XCTFail("Returning from background reaches interrupted-take review.")
            return
        }
        guard useTake.isEnabled else {
            XCTFail("The interrupted take is playable and recoverable.")
            return
        }
        screenshot(app, named: "Physical interrupted take review")

        app.buttons["teleprompter-close"].tap()
        XCTAssertTrue(app.buttons["teleprompter-open"].waitForExistence(timeout: 10))
        app.buttons["teleprompter-open"].tap()
        XCTAssertTrue(app.buttons["teleprompter-use-take"].waitForExistence(timeout: 12), "Closing and reopening restores the local reviewed take.")
        screenshot(app, named: "Physical take restored after reopen")
        #endif
    }

    /// KRI-1149: a take ends only when the creator presses Stop. A one-line script ends within seconds; the take
    /// must still be recording past the end of the script and past 60 s, then reach Review through Stop.
    func testPhysicalTakeKeepsRecordingPastScriptEndAndSixtySeconds() throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("Teleprompter hardware capture requires a physical iPhone.")
        #else
        guard ProcessInfo.processInfo.environment["KRIA_TELEPROMPTER_HARDWARE_TEST"] == "1" else {
            throw XCTSkip("Set KRIA_TELEPROMPTER_HARDWARE_TEST=1 in the device test runner to enable camera capture.")
        }

        let app = launchTalking()
        let removePermissionMonitor = addUIInterruptionMonitor(withDescription: "Kria camera or microphone permission") { alert in
            let allow = alert.buttons.matching(NSPredicate(format: "label BEGINSWITH %@ OR label == %@", "Allow", "OK")).firstMatch
            guard allow.exists else { return false }
            allow.tap()
            return true
        }
        defer { removeUIInterruptionMonitor(removePermissionMonitor) }

        app.buttons["teleprompter-open"].tap()
        let script = app.textViews["teleprompter-script-editor"]
        XCTAssertTrue(script.waitForExistence(timeout: 8))
        paste("Short script.", into: script, app: app)
        app.buttons["teleprompter-open-camera"].tap()
        let record = app.buttons["teleprompter-record"]
        guard triggerPermissionPrompts(in: app, untilEnabled: record), waitUntilEnabled(record, timeout: 20) else {
            XCTFail("A physical iPhone with granted camera and microphone permission should enable Record.")
            return
        }

        record.tap()
        let stop = app.buttons["teleprompter-stop-recording"]
        XCTAssertTrue(stop.waitForExistence(timeout: 10))
        var elapsed = 0
        for step in [20, 25, 25] {
            wait(seconds: TimeInterval(step))
            elapsed += step
            XCTAssertTrue(stop.exists && stop.isHittable, "Still recording at \(elapsed) s: Stop is the only way out.")
        }
        stop.tap()
        XCTAssertTrue(app.buttons["teleprompter-retake"].waitForExistence(timeout: 20), "Stop reaches Review.")
        XCTAssertTrue(app.buttons["teleprompter-use-take"].isEnabled)
        #endif
    }

    #if !targetEnvironment(simulator)
    private func setWindowBackground(_ appearance: String, in app: XCUIApplication, fontChangesAllowed: Bool? = nil) {
        let grip = app.buttons["teleprompter-window-move"]
        XCTAssertTrue(grip.waitForExistence(timeout: 5))
        grip.tap()
        let choice = app.buttons["teleprompter-background-\(appearance)"]
        XCTAssertTrue(choice.waitForExistence(timeout: 5))
        choice.tap()
        if let fontChangesAllowed {
            let font = app.steppers["teleprompter-text-size"]
            XCTAssertTrue(font.waitForExistence(timeout: 5))
            XCTAssertEqual(font.isEnabled, fontChangesAllowed,
                           "Font changes belong to preparation; recording keeps text layout stable.")
            screenshot(app, named: fontChangesAllowed ? "Kria window settings before capture" : "Kria window settings during capture")
        }
        app.buttons["teleprompter-window-settings-close"].tap()
        let window = app.descendants(matching: .any).matching(identifier: "teleprompter-window").firstMatch
        XCTAssertTrue((window.value as? String)?.localizedCaseInsensitiveContains(appearance) == true)
    }

    private func moveAndResizeWindow(in app: XCUIApplication, avoiding captureButton: XCUIElement) {
        let window = app.descendants(matching: .any).matching(identifier: "teleprompter-window").firstMatch
        let before = window.frame
        let resize = app.buttons["teleprompter-window-resize"]
        XCTAssertTrue(resize.isHittable)
        let resizeStart = resize.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        resizeStart.press(forDuration: 0.15, thenDragTo: resizeStart.withOffset(CGVector(dx: -40, dy: 20)))
        XCTAssertLessThan(window.frame.width, before.width - 10)
        let shrunk = window.frame
        let move = app.buttons["teleprompter-window-move"]
        let moveStart = move.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        moveStart.press(forDuration: 0.15, thenDragTo: moveStart.withOffset(CGVector(dx: 20, dy: 20)))
        XCTAssertGreaterThan(window.frame.minX, shrunk.minX + 5)
        XCTAssertTrue(app.frame.contains(window.frame))
        XCTAssertFalse(window.frame.intersects(captureButton.frame))
        XCTAssertTrue(captureButton.isHittable)
    }

    private func launchTalking() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        // Advertise the supported offline intake capability only. This neither replaces the real
        // camera/review path nor invokes the fixture's placeholder render in this capture test.
        app.launchEnvironment["KRIA_CHAT_DEVICE_RENDER"] = "ready"
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
        field.tap()
        field.typeText(text)
        XCTAssertTrue((field.value as? String)?.contains("recording this on an iPhone") == true,
                      "The real TextEditor must contain the source text before copying it.")

        // A test-runner pasteboard is not necessarily shared with the signed app process. Put
        // the script on the app's own pasteboard through the visible native Select All/Copy menu,
        // then clear and paste it back through that same menu.
        field.press(forDuration: 1)
        tapTextMenuAction("Select All", in: app)
        tapTextMenuAction("Copy", in: app)
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: max(text.utf16.count, 1)))
        XCTAssertEqual((field.value as? String) ?? "", "", "Copy is followed by clearing the real editor before Paste.")

        field.press(forDuration: 1)
        tapTextMenuAction("Paste", in: app)
        XCTAssertTrue((field.value as? String)?.contains("recording this on an iPhone") == true,
                      "The real script entry path restores its copied text with Paste.")
    }

    private func tapTextMenuAction(_ title: String, in app: XCUIApplication) {
        // Current iOS exposes edit-menu actions as buttons; older devices use menu items.
        let button = app.buttons[title]
        if button.waitForExistence(timeout: 3) {
            button.tap()
            return
        }
        let menuItem = app.menuItems[title]
        XCTAssertTrue(menuItem.waitForExistence(timeout: 3), "Expected native \(title) action in the TextEditor menu.")
        menuItem.tap()
    }

    private func triggerPermissionPrompts(in app: XCUIApplication, untilEnabled record: XCUIElement) -> Bool {
        // iOS presents camera and microphone privacy alerts serially. A monitor is invoked only
        // by an interaction, but direct SpringBoard inspection is more reliable on a signed phone
        // than an interruption-query callback. The monitor remains a fallback after this check.
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for attempt in 1...3 where !record.isEnabled {
            let alert = springboard.alerts.firstMatch
            if alert.waitForExistence(timeout: 1) {
                screenshot(springboard, named: "Observed device permission prompt \(attempt)")
                let prompt = "\(alert.label) \(alert.staticTexts.allElementsBoundByIndex.map(\.label).joined(separator: " "))"
                guard isExpectedCapturePermission(prompt) else {
                    XCTFail("Unexpected SpringBoard alert while preparing camera: \(prompt)")
                    return false
                }
                guard let affirmative = alert.buttons.allElementsBoundByIndex.first(where: { isAffirmativePermissionAction($0.label) }) else {
                    XCTFail("Expected an affirmative camera/microphone permission action, found: \(alert.buttons.allElementsBoundByIndex.map(\.label))")
                    return false
                }
                affirmative.tap()
                wait(seconds: 0.5)
                continue
            }
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
            wait(seconds: 0.5)
        }
        return true
    }

    private func isExpectedCapturePermission(_ prompt: String) -> Bool {
        let normalized = prompt.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current).lowercased()
        let requestsCapture = normalized.contains("camera") || normalized.contains("camara")
            || normalized.contains("microphone") || normalized.contains("microfone")
        return normalized.contains("kria") && requestsCapture
    }

    private func isAffirmativePermissionAction(_ label: String) -> Bool {
        let normalized = label.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current).lowercased()
        return normalized.hasPrefix("allow") || normalized == "ok" || normalized.hasPrefix("permitir")
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

/// Real HTTP journey, explicitly opt-in; no URLProtocol or recipe injection.
/// Failure cases: wrong/absent originals, missing proposed copy/reasoning, selection
/// rendering without approval, a changed copy being ignored, and a result that
/// cannot reopen as an editable project. API/worker/storage use an isolated account copy.
@MainActor final class CameraRollHardwareFlowTests: XCTestCase {
    func testSelectedFootageIdeaCorrectionApprovalRenderAndReopen() throws {
        continueAfterFailure = false
        #if targetEnvironment(simulator)
        throw XCTSkip("Requires the owner's paired iPhone and full-quality originals.")
        #else
        guard ProcessInfo.processInfo.environment["KRIA_CAMERA_ROLL_LIVE_TEST"] == "1",
              let raw = ProcessInfo.processInfo.environment["KRIA_CAMERA_ROLL_ASSET_IDS"] else {
            throw XCTSkip("Enable the isolated own-account hardware journey explicitly.")
        }
        let identifiers = raw.split(separator: ",").map(String.init).sorted(by: >)
        XCTAssertTrue((2...12).contains(identifiers.count))
        let app = XCUIApplication()
        let preflight = ProcessInfo.processInfo.environment["KRIA_CAMERA_ROLL_PREFLIGHT_ONLY"] == "1"
        app.launchArguments = preflight ? ["-camera-roll-original-link"] : ["-camera-roll-live-qa"]
        app.launch()
        if preflight {
            let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = "Original matching hardware preflight"; shot.lifetime = .keepAlways; add(shot)
            let linked = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Linked ")).firstMatch
            XCTAssertTrue(linked.waitForExistence(timeout: 120), app.debugDescription)
            return
        }
        addUIInterruptionMonitor(withDescription: "Local device QA network") { alert in
            let text = alert.staticTexts.allElementsBoundByIndex.map(\.label).joined(separator: " ")
            guard text.localizedCaseInsensitiveContains("local network") else { return false }
            if alert.buttons["Allow"].exists { alert.buttons["Allow"].tap(); return true }
            return false
        }
        let menu = app.buttons["workspace-menu-toggle"]
        XCTAssertTrue(menu.waitForExistence(timeout: 30), app.debugDescription)
        menu.tap()
        let collections = app.buttons["camera-roll-collection-open"]
        XCTAssertTrue(collections.waitForExistence(timeout: 15), app.debugDescription)
        collections.tap()
        XCTAssertTrue(app.buttons["collection-choose"].waitForExistence(timeout: 10))
        app.buttons["collection-choose"].tap()
        app.buttons["Saved uploads"].tap()
        for id in identifiers {
            let row = app.buttons["collection-saved-\(id.uppercased())"]
            for _ in 0..<8 where !row.isHittable { app.swipeUp() }
            XCTAssertTrue(row.waitForExistence(timeout: 10), app.debugDescription)
            row.tap()
        }
        app.buttons["collection-saved-add"].tap()
        let collectionShot = XCTAttachment(screenshot: app.screenshot()); collectionShot.name = "Owned footage selected with linked originals"; collectionShot.lifetime = .keepAlways; add(collectionShot)
        app.buttons["collection-options"].tap()
        app.buttons["Analysis options"].tap()
        let cached = app.switches["Use cached analysis only"]
        if cached.value as? String == "1" { cached.switches.firstMatch.tap() }
        app.navigationBars["Analysis options"].buttons["Done"].tap()
        let direction = app.textFields["collection-story-query"]
        direction.tap(); direction.typeText("Suggest TikTok ideas worth making from this outing. Give a specific angle, why it could work, and short proposed on-screen wording.")
        app.buttons["collection-find-stories"].tap()
        XCTAssertTrue(app.staticTexts["Why it could work"].waitForExistence(timeout: 60), app.debugDescription)
        XCTAssertTrue(app.staticTexts["Proposed on-screen text"].exists)
        let ideaShot = XCTAttachment(screenshot: app.screenshot()); ideaShot.name = "Real generated TikTok idea and reasoning"; ideaShot.lifetime = .keepAlways; add(ideaShot)
        app.buttons["story-edit-wording"].firstMatch.tap()
        let opening = app.descendants(matching: .any)["story-opening-text"].firstMatch
        XCTAssertTrue(opening.waitForExistence(timeout: 10))
        opening.tap(); opening.press(forDuration: 1.2)
        let selectAll = app.menuItems["Select All"].firstMatch
        let selectAllButton = app.buttons["Select All"].firstMatch
        if selectAll.waitForExistence(timeout: 2) { selectAll.tap() }
        else { XCTAssertTrue(selectAllButton.waitForExistence(timeout: 3), app.debugDescription); selectAllButton.tap() }
        opening.typeText("Would you spend an afternoon here?")
        XCTAssertEqual(opening.value as? String, "Would you spend an afternoon here?")
        app.buttons["story-save-wording"].tap()
        XCTAssertTrue(app.staticTexts["“Would you spend an afternoon here?”"].waitForExistence(timeout: 10))
        let build = app.buttons["Build this project"].firstMatch
        for _ in 0..<12 where !build.isHittable { app.swipeUp() }
        XCTAssertTrue(build.exists); build.tap()
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 90), app.debugDescription)
        XCTAssertFalse(app.buttons["Open editor"].exists, "Choosing an idea must not render it")
        let approvalShot = XCTAttachment(screenshot: app.screenshot()); approvalShot.name = "Separate approval of reviewed wording and footage"; approvalShot.lifetime = .keepAlways; add(approvalShot)
        confirm.tap()
        let editor = app.buttons["Open editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 180), app.debugDescription)
        editor.tap()
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 30), app.debugDescription)
        let resultShot = XCTAttachment(screenshot: app.screenshot()); resultShot.name = "Full-quality rendered idea reopens as an editable project"; resultShot.lifetime = .keepAlways; add(resultShot)
        app.terminate()
        app.launch()
        XCTAssertTrue(app.buttons["workspace-menu-toggle"].waitForExistence(timeout: 30), app.debugDescription)
        let reopenedEditor = app.buttons["Open editor"]
        XCTAssertTrue(reopenedEditor.waitForExistence(timeout: 30), app.debugDescription)
        reopenedEditor.tap()
        XCTAssertTrue(preview.waitForExistence(timeout: 30), app.debugDescription)
        assertEditableOriginalsAndWording(in: app)
        let reopenedShot = XCTAttachment(screenshot: app.screenshot()); reopenedShot.name = "Rendered project survives app relaunch"; reopenedShot.lifetime = .keepAlways; add(reopenedShot)
        #endif
    }

    func testPublishedProjectReopensWithEditableOriginalsAndText() throws {
        continueAfterFailure = false
        guard ProcessInfo.processInfo.environment["KRIA_CAMERA_ROLL_REOPEN_TEST"] == "1",
              let title = ProcessInfo.processInfo.environment["KRIA_CAMERA_ROLL_PROJECT_TITLE"] else {
            throw XCTSkip("Enable the owner's already published project check explicitly.")
        }
        let app = XCUIApplication()
        app.launchArguments = ["-camera-roll-live-qa"]
        app.terminate(); app.launch()
        XCTAssertTrue(app.buttons["workspace-menu-toggle"].waitForExistence(timeout: 30), app.debugDescription)
        XCTAssertEqual(app.staticTexts["workspace-project-title"].label, title)
        let editor = app.buttons["Open editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 30), app.debugDescription)
        editor.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-preview"].firstMatch.waitForExistence(timeout: 30), app.debugDescription)
        assertEditableOriginalsAndWording(in: app)
        let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = "Published camera-roll project reopened after relaunch"; shot.lifetime = .keepAlways; add(shot)
    }

    private func assertEditableOriginalsAndWording(in app: XCUIApplication) {
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-timeline-content"].firstMatch.exists)
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 15), "Reopening must retain editable source clips: \(app.debugDescription)")
        XCTAssertTrue(clip.isEnabled)
        let textTool = app.buttons["native-editor-tool-text"]
        XCTAssertTrue(textTool.isEnabled)
        textTool.tap()
        let wordingRow = app.descendants(matching: .any).matching(NSPredicate(
            format: "identifier BEGINSWITH %@ AND label CONTAINS %@",
            "native-editor-text-row-", "Would you spend an afternoon here?"
        )).firstMatch
        XCTAssertTrue(wordingRow.waitForExistence(timeout: 10), app.debugDescription)
        wordingRow.tap()
        let editText = app.buttons["Edit text"]
        if editText.exists { editText.tap() }
        let editableWording = app.descendants(matching: .any)["native-editor-text-content"].firstMatch
        XCTAssertTrue(editableWording.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(editableWording.isEnabled)
        XCTAssertEqual(editableWording.value as? String, "Would you spend an afternoon here?")
        let editableShot = XCTAttachment(screenshot: app.screenshot()); editableShot.name = "Original clips and reviewed wording remain editable after relaunch"; editableShot.lifetime = .keepAlways; add(editableShot)
        app.buttons["native-editor-text-inspector-done"].tap()
        let fallback = app.descendants(matching: .any)["native-editor-preview-fallback"].firstMatch
        XCTAssertFalse(fallback.exists, "The editable source preview must replace the finished-video fallback")
        let play = app.buttons["native-editor-play-pause"]
        let clock = app.staticTexts["native-editor-current-time"]
        let before = clock.value as? String
        play.tap()
        let advanced = expectation(for: NSPredicate(format: "value != %@", before ?? "0:00.0"), evaluatedWith: clock)
        wait(for: [advanced], timeout: 8)
        play.tap()
        XCTAssertFalse(fallback.exists)
    }
}
