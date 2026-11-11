import XCTest

@MainActor
final class AppleTextAccessibilityUITests: XCTestCase {
    func testTextInspectorKeepsEditingAndStyleControlsReachableAtAccessibilityTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        app.launchEnvironment["UI_TEST_EDITOR_WIDTH"] = "320"
        app.launch()

        let timelineText = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(timelineText.waitForExistence(timeout: 8))
        timelineText.tap()
        timelineText.tap()

        let panel = app.descendants(matching: .any)["native-editor-text-panel"].firstMatch
        XCTAssertTrue(panel.waitForExistence(timeout: 3))
        let window = app.windows.firstMatch.frame
        let viewport = CGRect(x: (window.width - 320) / 2, y: window.minY, width: 320, height: window.height)
        XCTAssertGreaterThanOrEqual(panel.frame.minX, viewport.minX)
        XCTAssertLessThanOrEqual(panel.frame.maxX, viewport.maxX)

        // KRI-508: an existing text opens on Edit text with the keyboard up, and the one-line box
        // must stay reachable (and inside the viewport) at this text size.
        let content = app.descendants(matching: .any)["native-editor-text-content"].firstMatch
        XCTAssertTrue(content.waitForExistence(timeout: 3))
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(content.isHittable, "the text box should remain reachable at accessibility text size")
        XCTAssertGreaterThanOrEqual(content.frame.minX, viewport.minX)
        XCTAssertLessThanOrEqual(content.frame.maxX, viewport.maxX)
        XCTAssertLessThanOrEqual(content.frame.maxY, app.keyboards.firstMatch.frame.minY)
        content.typeText(" accessible")
        XCTAssertTrue((content.value as? String ?? "").contains("accessible"))

        app.buttons["Style"].tap()
        let font = app.buttons["native-editor-text-font"]
        let alignRight = app.buttons["Align text right"]
        let size = app.textFields["native-editor-text-size"]
        let inspector = app.scrollViews["native-editor-text-inspector-scroll"]
        // First and last controls bound the scroll range; alignRight is revealed and tapped below.
        for control in [font, size] {
            reveal(control, in: inspector)
            XCTAssertTrue(control.waitForExistence(timeout: 3))
            XCTAssertTrue(control.isHittable, "\(control.identifier) should remain reachable at accessibility text size")
            XCTAssertGreaterThanOrEqual(control.frame.minX, viewport.minX)
            XCTAssertLessThanOrEqual(control.frame.maxX, viewport.maxX)
        }
        reveal(alignRight, in: inspector)
        alignRight.tap()

        let color = app.buttons["Text color #E7DDF5"]
        reveal(color, in: inspector)
        XCTAssertTrue(color.waitForExistence(timeout: 3))
        XCTAssertTrue(color.isHittable)
        XCTAssertGreaterThanOrEqual(color.frame.minX, viewport.minX)
        XCTAssertLessThanOrEqual(color.frame.maxX, viewport.maxX)
    }

    func testTextFontPickerListsWebFontsAndAppliesTheChoice() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launchEnvironment["UI_TEST_EDITOR_WIDTH"] = "390"
        app.launch()

        let timelineText = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(timelineText.waitForExistence(timeout: 8))
        timelineText.tap()
        timelineText.tap()

        // KRI-508: the panel opens on Edit text; the font lives on Style.
        let styleTab = app.buttons["Style"]
        XCTAssertTrue(styleTab.waitForExistence(timeout: 3))
        styleTab.tap()
        let font = app.buttons["native-editor-text-font"]
        reveal(font, in: app.scrollViews["native-editor-text-inspector-scroll"])
        XCTAssertTrue(font.waitForExistence(timeout: 3))
        font.tap()

        // Fonts beyond the old four-font shortlist are offered.
        let bebas = app.buttons["native-editor-font-option-bebas-neue"]
        XCTAssertTrue(bebas.waitForExistence(timeout: 3))
        attachScreenshot(app, name: "Font picker sheet")
        bebas.tap()

        XCTAssertTrue(font.waitForExistence(timeout: 3))
        XCTAssertEqual(font.value as? String, "Bebas Neue")
    }

    /// KRI-1312: the font sheet groups fonts by type under tabs that scroll sideways.
    func testFontPickerGroupsFontsByTypeUnderSidewaysTabs() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-two-text"]
        app.launchEnvironment["UI_TEST_EDITOR_WIDTH"] = "390"
        app.launch()

        let timelineText = app.buttons["native-editor-timeline-text-00000000-0000-4000-8000-000000000100"].firstMatch
        XCTAssertTrue(timelineText.waitForExistence(timeout: 8))
        timelineText.tap()
        timelineText.tap()
        let styleTab = app.buttons["Style"]
        XCTAssertTrue(styleTab.waitForExistence(timeout: 3))
        styleTab.tap()
        let font = app.buttons["native-editor-text-font"]
        reveal(font, in: app.scrollViews["native-editor-text-inspector-scroll"])
        XCTAssertTrue(font.waitForExistence(timeout: 3))
        font.tap()

        let all = app.buttons["native-editor-font-tab-all"]
        let script = app.buttons["native-editor-font-tab-script"]
        XCTAssertTrue(all.waitForExistence(timeout: 3))
        XCTAssertTrue(all.isSelected)
        XCTAssertTrue(app.buttons["native-editor-font-option-bebas-neue"].waitForExistence(timeout: 3))
        attachScreenshot(app, name: "Font picker, All tab")

        // A swipe on the pages moves to the next type and the chip follows.
        app.scrollViews.element(boundBy: 1).swipeLeft()
        let editorial = app.buttons["native-editor-font-tab-editorial"]
        XCTAssertTrue(editorial.waitForExistence(timeout: 3))
        XCTAssertTrue(editorial.isSelected)
        // The page lists its fonts alphabetically in a lazy list, so a font far down it is not on screen yet.
        let playfair = app.buttons["native-editor-font-option-editorial-playfair-display"]
        let editorialPages = app.scrollViews.containing(NSPredicate(format: "identifier BEGINSWITH %@", "native-editor-font-option-editorial-"))
        XCTAssertTrue(editorialPages.firstMatch.waitForExistence(timeout: 3))
        for _ in 0..<8 where !playfair.isHittable {
            // The innermost match is the page's own vertical list; the outer one is the sideways pager.
            editorialPages.element(boundBy: editorialPages.count - 1).swipeUp()
        }
        XCTAssertTrue(playfair.waitForExistence(timeout: 3))
        XCTAssertTrue(playfair.isHittable, "Playfair Display is listed on the Editorial page")

        // Tapping a chip jumps straight to that type, which lists only its fonts.
        XCTAssertTrue(script.waitForExistence(timeout: 3))
        // The strip has scrolled to centre Editorial, so Script is past its edge: drag the strip sideways to reach it.
        let origin = app.coordinate(withNormalizedOffset: .zero)
        let window = app.windows.firstMatch.frame
        for _ in 0..<3 where !window.contains(script.frame) {
            let row = editorial.frame.midY
            origin.withOffset(CGVector(dx: window.width * 0.85, dy: row))
                .press(forDuration: 0.05, thenDragTo: origin.withOffset(CGVector(dx: window.width * 0.15, dy: row)))
        }
        script.tap()
        let pacifico = app.buttons["native-editor-font-option-script-pacifico"]
        XCTAssertTrue(pacifico.waitForExistence(timeout: 3))
        XCTAssertFalse(app.buttons["native-editor-font-option-script-bebas-neue"].exists)
        attachScreenshot(app, name: "Font picker, Script tab")
        pacifico.tap()

        XCTAssertTrue(font.waitForExistence(timeout: 3))
        XCTAssertEqual(font.value as? String, "Pacifico")
    }

    private func reveal(_ control: XCUIElement, in inspector: XCUIElement) {
        for _ in 0..<24 where !control.isHittable {
            let moveUp = !control.exists || control.frame.midY > inspector.frame.midY
            let start = inspector.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.7 : 0.4))
            let end = inspector.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: moveUp ? 0.4 : 0.7))
            start.press(forDuration: 0.05, thenDragTo: end, withVelocity: 20, thenHoldForDuration: 0.3)
        }
    }

    private func attachScreenshot(_ app: XCUIApplication, name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
