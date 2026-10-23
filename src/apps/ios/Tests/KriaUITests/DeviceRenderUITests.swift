import XCTest

/// KRI-141: the phone-render journey end to end through the chat. `KRIA_CHAT_DEVICE_RENDER`
/// gives the offline fixture account iPhone rendering, hands the confirmed edit's variant to
/// this device, and serves `/me/jobs/{id}/device-render`. The app's own `DeviceRenderSessions`
/// reconciles it; only the AVFoundation export and the upload are fixture stand-ins
/// (`DeviceRenderUITestFixture` in ChatUITestTransport.swift).
@MainActor
final class DeviceRenderUITests: XCTestCase {
    /// Format → attached footage → confirm → the status card renders on the iPhone (Stop is
    /// offered while it works) and finishes with Play / Save / Share and nothing to retry.
    func testPhoneRenderReachesReadyWithPlaySaveAndShare() {
        let app = launch(scenario: "ready")
        let card = startPhoneEdit(in: app)

        let stop = card.buttons["device-render-stop"]
        XCTAssertTrue(stop.waitForExistence(timeout: 10), "Stop is offered while the iPhone renders")

        let play = card.buttons["device-render-play"]
        XCTAssertTrue(play.waitForExistence(timeout: 30), app.debugDescription)
        XCTAssertTrue(card.buttons["device-render-save-to-photos"].exists)
        XCTAssertTrue(card.descendants(matching: .any)["device-render-share"].firstMatch.exists)
        // Play appears at `localReady` (which briefly offers "Retry sync"); the fixture upload then syncs it.
        XCTAssertTrue(card.staticTexts["Your video is synced"].waitForExistence(timeout: 15))
        XCTAssertFalse(stop.exists, "Stop disappears once the render finished")
        XCTAssertTrue(eventually { !card.buttons["device-render-retry"].exists }, "A finished, synced render has nothing to retry")

        // Play opens the local file in a player sheet.
        play.tap()
        let done = app.buttons["Done"]
        XCTAssertTrue(done.waitForExistence(timeout: 10))
        done.tap()
        XCTAssertTrue(play.waitForExistence(timeout: 10))
    }

    /// The server refused the recipe itself (`unsupported_recipe`): trying again would fail the
    /// same way, so the card says to start a new edit and offers no retry.
    func testUnsupportedRecipeAsksForANewEditWithoutRetry() {
        let app = launch(scenario: "unsupported_recipe")
        let card = startPhoneEdit(in: app)

        XCTAssertTrue(card.staticTexts["This edit needs your attention"].waitForExistence(timeout: 20), app.debugDescription)
        let guidance = card.staticTexts.containing(NSPredicate(format: "label CONTAINS[c] %@", "Start a new edit")).firstMatch
        XCTAssertTrue(guidance.waitForExistence(timeout: 5))
        XCTAssertFalse(card.buttons["device-render-retry"].exists)
        XCTAssertFalse(card.buttons["device-render-stop"].exists)
        XCTAssertFalse(card.buttons["device-render-play"].exists)
        // Later polls must not bring the retry back.
        RunLoop.current.run(until: Date().addingTimeInterval(3))
        XCTAssertFalse(card.buttons["device-render-retry"].exists)
    }

    /// A transient failure (the server's reaper `timed_out`) keeps Try again; retrying mints a
    /// fresh identity server-side and this iPhone renders it to the ready state.
    func testTimedOutRenderRetriesToReady() {
        let app = launch(scenario: "timed_out")
        let card = startPhoneEdit(in: app)

        let copy = card.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "stopped waiting after a day")).firstMatch
        XCTAssertTrue(copy.waitForExistence(timeout: 20), app.debugDescription)
        let retry = card.buttons["device-render-retry"]
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        retry.tap()

        XCTAssertTrue(card.buttons["device-render-play"].waitForExistence(timeout: 30), app.debugDescription)
        XCTAssertTrue(card.staticTexts["Your video is synced"].waitForExistence(timeout: 15))
        XCTAssertTrue(eventually { !retry.exists }, "The retried render finished; nothing is left to retry")
    }

    /// KRI-823: a device delivery failure never moves the job off `awaiting_device`, so the drawer used to keep
    /// saying "Rendering" while the card said the edit needed attention. Launch 1 caches the project as Rendering on
    /// disk; launch 2 finds the server already gave up (`timed_out`). Drawer and detail must both say so after the
    /// relaunch, stay that way across a drawer refresh with the detail open, then follow Retry to Rendering and the
    /// published export to Ready.
    func testDrawerFollowsDeviceDeliveryFailureThroughRelaunchRetryAndReady() {
        let first = launchSeeded(scenario: "ready", reset: true)
        XCTAssertTrue(drawerRow(in: first, says: "Rendering"), first.debugDescription)
        first.terminate()

        let app = launchSeeded(scenario: "timed_out", reset: false, publishReady: true)
        XCTAssertTrue(drawerRow(in: app, says: "Needs attention"), "A cached Rendering row must not survive the server's failure. \(app.debugDescription)")
        seededRow(in: app).tap()

        let card = app.descendants(matching: .any)["device-render-status"].firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20), app.debugDescription)
        let copy = card.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "stopped waiting after a day")).firstMatch
        XCTAssertTrue(copy.waitForExistence(timeout: 20), app.debugDescription)

        // Reopening the drawer reloads the list while the detail is open; neither may flip back to Rendering.
        XCTAssertTrue(drawerRow(in: app, says: "Needs attention"))
        closeDrawer(in: app)
        XCTAssertTrue(copy.waitForExistence(timeout: 5), "The open detail keeps the needs-attention card")

        let retry = card.buttons["device-render-retry"]
        XCTAssertTrue(retry.waitForExistence(timeout: 5))
        retry.tap()
        XCTAssertTrue(drawerRow(in: app, says: "Rendering", timeout: 10), app.debugDescription)
        closeDrawer(in: app)
        XCTAssertTrue(drawerRow(in: app, says: "Ready", timeout: 40), app.debugDescription)
    }

    /// KRI-823 record-only case: a refused phone EDIT (or an expired repair permission) marks only the device
    /// record; the variant keeps `render_status: awaiting_device` and its last accepted cut. Only `delivery_phase`
    /// says the delivery needs the creator, so the drawer and detail must follow it, never the generic
    /// "wasn't made" failure.
    func testRecordOnlyEditFailureShowsNeedsAttentionWithoutTheGenericFailure() {
        let app = launchSeeded(scenario: "timed_out", reset: true, recordOnly: true)
        XCTAssertTrue(drawerRow(in: app, says: "Needs attention"), "Only delivery_phase carries the failure. \(app.debugDescription)")
        seededRow(in: app).tap()

        let card = app.descendants(matching: .any)["device-render-status"].firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20), app.debugDescription)
        XCTAssertTrue(card.buttons["device-render-retry"].waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertFalse(app.staticTexts["Your video wasn’t made"].exists, "The last accepted cut still exists")
    }

    // MARK: - Helpers

    private func launchSeeded(scenario: String, reset: Bool, publishReady: Bool = false, recordOnly: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"] + (reset ? ["-perf-reset"] : [])
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_DEVICE_RENDER"] = scenario
        app.launchEnvironment["KRIA_CHAT_DEVICE_SEED_THREAD"] = "1"
        app.launchEnvironment["KRIA_PERF_DISK_STORE"] = "1"
        if recordOnly { app.launchEnvironment["KRIA_CHAT_DEVICE_RECORD_ONLY"] = "1" }
        if publishReady {
            app.launchEnvironment["KRIA_CHAT_DEVICE_PUBLISH_READY"] = "1"
            // The first launch's local export is gone (an interrupted device); only the server's record remains.
            app.launchEnvironment["KRIA_CHAT_DEVICE_LOST_LOCAL_EXPORT"] = "1"
        }
        app.launch()
        return app
    }

    private func seededRow(in app: XCUIApplication) -> XCUIElement {
        app.buttons.containing(NSPredicate(format: "label BEGINSWITH %@", "Delivery check")).firstMatch
    }

    /// Opens the drawer (re-reading the project list) until the seeded project's row says `status`.
    private func drawerRow(in app: XCUIApplication, says status: String, timeout: TimeInterval = 20) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if !app.buttons["drawer-new-chat"].exists {
                let open = app.buttons["Open projects"]
                if open.waitForExistence(timeout: 5) { open.tap() }
            }
            let row = seededRow(in: app)
            if row.waitForExistence(timeout: 2), row.label.contains(status) { return true }
            closeDrawer(in: app)
            RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        } while Date() < deadline
        return false
    }

    private func closeDrawer(in app: XCUIApplication) {
        let close = app.buttons["Close projects"]
        if close.exists { close.tap() }
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        _ = XCTWaiter.wait(for: [dismissed], timeout: 5)
    }

    private func launch(scenario: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_DEVICE_RENDER"] = scenario
        app.launch()
        return app
    }

    /// New chat → Montage (the fixture attaches one phone-proxy clip) → send → confirm. Returns
    /// the device render status card that replaces the cloud rendering stage.
    private func startPhoneEdit(in app: XCUIApplication) -> XCUIElement {
        createFreshChat(in: app)
        let montage = app.buttons["format-montage"]
        XCTAssertTrue(montage.waitForExistence(timeout: 10))
        montage.tap()
        let send = app.buttons["Send clips"]
        XCTAssertTrue(send.waitForExistence(timeout: 10))
        send.tap()
        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 15))
        confirm.tap()
        let card = app.descendants(matching: .any)["device-render-status"].firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 20), app.debugDescription)
        return card
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

    /// Re-checks `condition` until it holds or `timeout` passes (see CreationUITests.eventually).
    private func eventually(timeout: TimeInterval = 5, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else { return false }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return true
    }
}
