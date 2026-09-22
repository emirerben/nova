import XCTest

@MainActor final class SlidePostUITests: XCTestCase {
    func testSlidePostCreationUsesExplicitProposalAndCreateThenContextualKria() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        app.launch()
        let menu = app.buttons["Open projects"]
        XCTAssertTrue(menu.waitForExistence(timeout: 12)); menu.tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5)); newChat.tap()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 12))
        carousel.swipeLeft()
        let format = app.buttons["format-slides"]
        XCTAssertTrue(format.waitForExistence(timeout: 5))
        format.tap()
        XCTAssertTrue(app.staticTexts["Start your post"].waitForExistence(timeout: 8))
        let ask = app.buttons["Ask Kria"].firstMatch
        scrollTo(ask, app: app); ask.tap()
        let apply = app.buttons["Apply proposal"].firstMatch
        XCTAssertTrue(apply.waitForExistence(timeout: 8))
        XCTAssertFalse(app.buttons["slidepost-save-photos"].exists)
        scrollTo(apply, app: app); apply.tap()
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        let viewport = app.windows.firstMatch.frame
        XCTAssertLessThanOrEqual(preview.frame.width, viewport.width)
        XCTAssertLessThanOrEqual(preview.frame.height, viewport.height)
        XCTAssertEqual(preview.frame.width / preview.frame.height, 4.0 / 5.0, accuracy: 0.06)
        XCTAssertTrue(app.buttons["slidepost-openkria"].isHittable)
        let draftPreview = XCTAttachment(screenshot: app.screenshot()); draftPreview.name = "Native slide draft preview"; draftPreview.lifetime = .keepAlways; add(draftPreview)
        let create = app.buttons["slidepost-create"]
        scrollTo(create, app: app)
        XCTAssertTrue(create.waitForExistence(timeout: 6)); create.tap()
        let save = app.buttons["slidepost-save-photos"]
        scrollTo(save, app: app)
        XCTAssertTrue(save.waitForExistence(timeout: 8)); XCTAssertTrue(save.isEnabled)
        let ready = XCTAttachment(screenshot: app.screenshot()); ready.name = "Native slide post ready"; ready.lifetime = .keepAlways; add(ready)
        for _ in 0..<5 where !app.buttons["slidepost-openkria"].isHittable { app.swipeDown() }
        app.buttons["slidepost-openkria"].tap()
        let prompt = app.descendants(matching: .any)["slidepost-prompt"].firstMatch
        XCTAssertTrue(prompt.waitForExistence(timeout: 5))
        prompt.tap(); prompt.typeText(" End on the view.")
        app.buttons["slidepost-ask"].tap()
        let applyChange = app.buttons["slidepost-apply"]
        XCTAssertTrue(applyChange.waitForExistence(timeout: 8)); applyChange.tap()
        let applied = XCTAttachment(screenshot: app.screenshot()); applied.name = "Native Kria applied proposal"; applied.lifetime = .keepAlways; add(applied)
    }

    func testSlideFormatCoverAndBackRemainReachableAtLargeText() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility3"
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 12))
        app.buttons["Open projects"].tap()
        app.buttons["drawer-new-chat"].tap()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 12)); carousel.swipeLeft()
        let format = app.buttons["format-slides"]
        XCTAssertTrue(format.waitForExistence(timeout: 5)); format.tap()
        XCTAssertTrue(app.buttons["Back to creation"].waitForExistence(timeout: 8))
        app.buttons["Back to creation"].tap()
        XCTAssertTrue(app.staticTexts["What are we making?"].waitForExistence(timeout: 5))
        let screenshot = XCTAttachment(screenshot: app.screenshot()); screenshot.name = "Slide format at large text"; screenshot.lifetime = .keepAlways; add(screenshot)
    }

    private func scrollTo(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<7 {
            if element.exists && element.isHittable { return }
            app.swipeUp()
        }
    }
}
