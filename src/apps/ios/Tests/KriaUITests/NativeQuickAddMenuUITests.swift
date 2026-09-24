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

    func testVideoOptionOpensAddClipSheet() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        app.buttons["native-editor-add-clip"].tap()
        let video = app.descendants(matching: .any)["native-editor-add-menu-video"]
        XCTAssertTrue(video.waitForExistence(timeout: 3))
        video.tap()

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-add-clip-photos"].waitForExistence(timeout: 3))
        // The menu itself is gone once a sheet is up.
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-add-menu-text"].exists)
    }

    func testVisualOptionOpensTheSameVisualsPanelTheToolRailOpens() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor"]
        app.launch()

        app.buttons["native-editor-add-clip"].tap()
        let visual = app.descendants(matching: .any)["native-editor-add-menu-visual"]
        XCTAssertTrue(visual.waitForExistence(timeout: 3))
        visual.tap()

        XCTAssertTrue(app.staticTexts["Add visual"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.staticTexts["Your added photos and videos"].waitForExistence(timeout: 3))
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

    func testOutroPlaceholderStillAppearsAfterTheLastClip() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-source-text"]
        app.launch()

        let play = app.buttons["native-editor-play-pause"]
        let clock = app.staticTexts["native-editor-current-time"]
        let duration = app.staticTexts["native-editor-duration"]
        XCTAssertTrue(play.waitForExistence(timeout: 8))
        expectation(for: NSPredicate(format: "value == %@", "0:05.6"), evaluatedWith: duration)
        waitForExpectations(timeout: 15)

        play.tap()
        expectation(for: NSPredicate(format: "value == %@", "0:05.6"), evaluatedWith: clock)
        waitForExpectations(timeout: 12)

        XCTAssertTrue(app.descendants(matching: .any)["native-editor-outro-placeholder"].waitForExistence(timeout: 3))
    }
}
