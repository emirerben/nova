import XCTest

@MainActor
final class CreationUITests: XCTestCase {
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

    func testLaunchEntersChatFirstWorkspace() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        // Exercise creation on every run, even when a prior project restores.
        createFreshChat(in: app)
        let prompt = app.staticTexts["What kind of video are we making?"]
        XCTAssertTrue(prompt.waitForExistence(timeout: 20))
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Attach footage"].exists)
        XCTAssertFalse(app.buttons["Attach footage"].isEnabled)
        XCTAssertTrue(app.staticTexts["Montage"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Narrated"].exists)

        let toggle = app.buttons["workspace-menu-toggle"]
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 3))
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
        let closed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == 'Open projects'"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [closed], timeout: 3), .completed)
        XCTAssertEqual(toggle.frame.minX, closedMenuX, accuracy: 2)
        let swipeStart = app.coordinate(withNormalizedOffset: CGVector(dx: 0.15, dy: 0.55))
        let swipeEnd = app.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.55))
        swipeStart.press(forDuration: 0.05, thenDragTo: swipeEnd)
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 3))
        XCTAssertEqual(toggle.label, "Close projects")
        let drawerScreenshot = XCTAttachment(screenshot: app.screenshot())
        drawerScreenshot.name = "Swipe-open tinted workspace"
        drawerScreenshot.lifetime = .keepAlways
        add(drawerScreenshot)
        swipeEnd.press(forDuration: 0.05, thenDragTo: swipeStart)
        let swipeClosed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label == 'Open projects'"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [swipeClosed], timeout: 3), .completed)
        XCTAssertEqual(toggle.frame.minX, closedMenuX, accuracy: 2)
        toggle.tap()
        XCTAssertTrue(app.staticTexts["Recent chats"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.buttons["drawer-new-chat"].exists)

        let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'" )).firstMatch
        XCTAssertTrue(gallery.exists)
        gallery.tap()
        XCTAssertTrue(app.staticTexts["Your finished videos"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["All"].exists)
        XCTAssertTrue(app.buttons["Ready"].exists)
    }

    func testEveryCreationFormatOpensTheAttachmentFlow() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launch()
        for format in ["montage", "narrated", "talking_to_camera"] {
            createFreshChat(in: app)
            let card = app.buttons["format-\(format)"]
            if !card.isHittable { app.scrollViews["format-carousel"].swipeLeft() }
            XCTAssertTrue(card.waitForExistence(timeout: 5))
            card.tap()
            XCTAssertTrue(app.buttons["choose-videos"].waitForExistence(timeout: 5))
            app.buttons["choose-videos"].tap()
            XCTAssertTrue(app.buttons["Choose from Photos"].waitForExistence(timeout: 3))
            XCTAssertTrue(app.buttons["Choose from Files or iCloud"].exists)
            app.buttons["Done"].tap()
        }
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
            let next = app.buttons["Continue with 1 clip"]
            XCTAssertTrue(next.waitForExistence(timeout: 5))
            next.tap()
            let confirm = app.buttons["Create this video"]
            XCTAssertTrue(confirm.waitForExistence(timeout: 10))
            confirm.tap()
            XCTAssertTrue(app.buttons["Open editor"].waitForExistence(timeout: 30))
            app.terminate()
        }
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
        let next = app.buttons["Continue with 1 clip"]
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

    func testExpiredApprovalCannotStartGeneration() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_EXPIRED_APPROVAL"] = "1"
        app.launch()
        createFreshChat(in: app)
        app.buttons["format-montage"].tap()
        let next = app.buttons["Continue with 1 clip"]
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
        XCTAssertTrue(app.staticTexts["Kria couldn’t load creation options. Check your connection and retry."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["format-montage"].exists)
        XCTAssertTrue(app.buttons["Reconnect"].firstMatch.exists)
    }

    private func createFreshChat(in app: XCUIApplication) {
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 3))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What kind of video are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
    }
}
