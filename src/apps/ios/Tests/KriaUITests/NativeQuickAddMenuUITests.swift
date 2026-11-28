import XCTest

/// KRI-166: a single, always-visible "+" in the transport row opens a small
/// chooser (Video/Visual/Text) that morphs out of the button and routes to
/// the same tool the bottom rail already opens for Visual/Text. Replaces an
/// earlier per-lane "+" design the user found confusing.
@MainActor
final class NativeQuickAddMenuUITests: XCTestCase {
    func testPlusOpensMenuWithThreeOptions() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let plus = app.buttons["native-editor-add-clip"]
        XCTAssertTrue(plus.waitForExistence(timeout: 8))
        plus.tap()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-add-menu-video"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-add-menu-visual"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-add-menu-text"].exists)
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Quick-add menu open"
        shot.lifetime = .keepAlways
        add(shot)
    }

    func testTappingPlusAgainClosesTheMenu() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let plus = app.buttons["native-editor-add-clip"]
        plus.tap()
        let menuItem = app.descendants(matching: .any)["native-editor-add-menu-text"]
        XCTAssertTrue(menuItem.waitForExistence(timeout: 3))

        plus.tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: menuItem)
        waitForExpectations(timeout: 3)
    }

    func testTappingOutsideTheMenuDismissesIt() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        app.buttons["native-editor-add-clip"].tap()
        let menuItem = app.descendants(matching: .any)["native-editor-add-menu-text"]
        XCTAssertTrue(menuItem.waitForExistence(timeout: 3))

        // Tap inside the timeline area but well clear of the popover (which
        // grows leftward from the "+" near the trailing side): the far-left
        // lane-label column, below the transport row.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.06, dy: 0.80)).tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: menuItem)
        waitForExpectations(timeout: 3)
    }

    func testTextOptionStartsTextCreation() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        app.buttons["native-editor-add-clip"].tap()
        let text = app.descendants(matching: .any)["native-editor-add-menu-text"]
        XCTAssertTrue(text.waitForExistence(timeout: 3))
        text.tap()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-new-text-input"].waitForExistence(timeout: 3))
    }

    /// KRI-647: the system Paste control in the "+" menu turns copied text into a selected text element.
    func testPasteOptionAddsCopiedTextAsTextElement() {
        UIPasteboard.general.string = "5.2 km in 28:14"
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        app.buttons["native-editor-add-clip"].tap()
        let paste = app.descendants(matching: .any)["native-editor-add-menu-paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 3))
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "Quick-add menu with Paste"
        shot.lifetime = .keepAlways
        add(shot)
        paste.tap()

        // The new text is selected, which opens the Text actions strip.
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-text-context"].waitForExistence(timeout: 5))
        let after = XCTAttachment(screenshot: app.screenshot())
        after.name = "Pasted text selected"
        after.lifetime = .keepAlways
        add(after)
    }

    /// The editor's real entry point opens the shared Photos picker. Selecting two items doesn't start
    /// either import until confirmation; cancel clears the picks and returns to the source sheet.
    func testTimelinePhotosPickerConfirmsTwoSelectionsOnce() {
        let app = XCUIApplication()
        app.resetAuthorizationStatus(for: .photos)
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()
        app.buttons["native-editor-add-clip"].tap()
        app.descendants(matching: .any)["native-editor-add-menu-video"].tap()
        app.buttons["native-editor-add-clip-upload"].tap()
        let photos = app.buttons["native-editor-add-clip-photos"]
        XCTAssertTrue(photos.waitForExistence(timeout: 5))
        photos.tap()
        let done = app.buttons["photos-picker-done"]
        let prompt = XCUIApplication(bundleIdentifier: "com.apple.springboard").alerts.firstMatch
        let deadline = Date().addingTimeInterval(30)
        while !done.exists && Date() < deadline {
            if prompt.exists && prompt.buttons.count == 3 { prompt.buttons.element(boundBy: 1).tap() }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        XCTAssertTrue(done.exists, app.debugDescription)
        let first = app.buttons["gallery-tile-0"], second = app.buttons["gallery-tile-1"]
        XCTAssertTrue(first.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(second.exists)
        XCTAssertFalse(done.isEnabled)
        first.tap()
        XCTAssertTrue(done.label.hasPrefix("Add 1 "), done.label)
        app.buttons["gallery-cancel"].tap()
        XCTAssertTrue(photos.waitForExistence(timeout: 5), "Cancel does not dismiss the add sheet")
        photos.tap()
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertFalse(done.isEnabled, "Cancel does not leave an item selected")
        second.tap()
        first.tap()
        XCTAssertEqual(done.label, "Add 2 items")
        XCTAssertEqual(second.value as? String, "Selected, 1")
        XCTAssertEqual(first.value as? String, "Selected, 2")
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Ordered timeline Photos selection"
        screenshot.lifetime = .keepAlways
        self.add(screenshot)
        done.tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: photos)
        waitForExpectations(timeout: 8)
    }

    /// Video routes to the add-clip sheet; Visual opens the same panel the tool rail opens.
    func testVideoAndVisualOptionsRouteToTheirSurfaces() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        let plus = app.buttons["native-editor-add-clip"]
        XCTAssertTrue(plus.waitForExistence(timeout: 8))
        plus.tap()
        let video = app.descendants(matching: .any)["native-editor-add-menu-video"]
        XCTAssertTrue(video.waitForExistence(timeout: 3))
        video.tap()

        // The sheet opens on Record | Upload; the library sources sit behind Upload.
        let upload = app.buttons["native-editor-add-clip-upload"]
        XCTAssertTrue(upload.waitForExistence(timeout: 3))
        // The menu itself is gone once a sheet is up.
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-add-menu-text"].exists)

        upload.tap()
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-add-clip-photos"].waitForExistence(timeout: 3))
        let close = app.buttons["native-editor-add-clip-close"]
        XCTAssertTrue(close.waitForExistence(timeout: 3))
        close.tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: app.descendants(matching: .any)["native-editor-add-clip-photos"])
        waitForExpectations(timeout: 3)

        plus.tap()
        let visual = app.descendants(matching: .any)["native-editor-add-menu-visual"]
        XCTAssertTrue(visual.waitForExistence(timeout: 3))
        visual.tap()

        XCTAssertTrue(app.staticTexts["Add visual"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Your added photos and videos"].waitForExistence(timeout: 3))
    }
}
