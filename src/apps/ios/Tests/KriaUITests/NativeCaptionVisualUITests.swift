import XCTest

@MainActor
final class NativeCaptionVisualUITests: XCTestCase {
    func testLegacyServerAllowsOpeningVisualImporterAndAddingCard() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-legacy-visuals", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        let visuals = app.buttons["native-editor-tool-visuals"]
        XCTAssertTrue(visuals.waitForExistence(timeout: 20))
        visuals.tap()
        let add = app.buttons["native-editor-import-visual"]
        XCTAssertTrue(add.waitForExistence(timeout: 5))
        XCTAssertTrue(add.isEnabled)
        add.tap()
        XCTAssertTrue(app.buttons["Choose from Photos"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Choose from Files or iCloud"].isEnabled)
        app.navigationBars["Add photo or video"].buttons["Done"].tap()
        app.buttons["Text cards"].tap()
        app.buttons["native-editor-card-preset-simple"].tap()
        let text = app.textFields["native-editor-new-card-text"]
        let multiline = app.textViews["native-editor-new-card-text"]
        let input = text.exists ? text : multiline
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        app.scrollViews["native-editor-visuals-scroll"].swipeUp()
        XCTAssertTrue(input.isHittable)
        input.tap()
        input.typeText("My visual card")
        app.buttons["Add card"].tap()
        XCTAssertTrue(app.buttons["native-editor-remove-visual"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Edit text card"].exists)
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
        app.buttons["native-editor-visuals-tab-Animation"].tap()
        XCTAssertFalse(app.buttons["native-editor-text-inspector-done"].exists)
    }

    func testExistingVisualOpensEditorAndAddActionOpensLibrary() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        let visual = app.descendants(matching: .any)["native-editor-timeline-visual_block-paper-media"].firstMatch
        XCTAssertTrue(visual.waitForExistence(timeout: 20))
        visual.tap()
        XCTAssertTrue(app.staticTexts["Edit video"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].exists)
        XCTAssertTrue(app.buttons["native-editor-remove-visual"].exists)
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
        app.buttons["native-editor-add-another-visual"].tap()
        XCTAssertTrue(app.staticTexts["Add visual"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-import-visual"].exists)
        XCTAssertFalse(app.buttons["native-editor-remove-visual"].exists)
        XCTAssertFalse(app.buttons["native-editor-visuals-tab-Animation"].exists)
        app.buttons["native-editor-visuals-done"].tap()
        XCTAssertTrue(visual.waitForExistence(timeout: 5))
        visual.tap()
        XCTAssertTrue(app.staticTexts["Edit video"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
        app.buttons["native-editor-visuals-done"].tap()
        let previewVisual = app.descendants(matching: .any)["native-editor-preview-visual_block-paper-media"].firstMatch
        XCTAssertTrue(previewVisual.waitForExistence(timeout: 5))
        previewVisual.tap()
        XCTAssertTrue(app.staticTexts["Edit video"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
    }

    func testAddingCameraEffectOpensItsSpecificEditor() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        app.buttons["Camera FX"].tap()
        app.buttons.containing(.staticText, identifier: "Zoom pulse").firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Edit zoom pulse"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Intensity"].exists)
        XCTAssertFalse(app.buttons["native-editor-visuals-tab-Animation"].exists)
        XCTAssertFalse(app.buttons["Camera FX"].exists)
        XCTAssertTrue(app.buttons["native-editor-remove-visual"].exists)
    }

    func testVisualControlsEditAndPlaybackRemainAvailable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        let visual = app.descendants(matching: .any)["native-editor-timeline-visual_block-paper-media"].firstMatch
        XCTAssertTrue(visual.waitForExistence(timeout: 20))
        visual.tap()
        let handle = app.descendants(matching: .any)["native-editor-timeline-resize"].firstMatch
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -180)))
        for name in ["Zoom", "Rotation"] {
            let slider = app.sliders[name]
            XCTAssertTrue(slider.waitForExistence(timeout: 5))
            XCTAssertTrue(slider.isEnabled)
            let before = slider.value as? String
            slider.adjust(toNormalizedSliderPosition: 0.65)
            XCTAssertNotEqual(slider.value as? String, before, name)
        }
        app.buttons["native-editor-visuals-tab-Animation"].tap()
        app.buttons["In visual animation Fade"].tap()
        let speed = app.sliders["Animation speed"]
        XCTAssertTrue(speed.isEnabled)
        let previousSpeed = speed.value as? String
        speed.adjust(toNormalizedSliderPosition: 0.8)
        XCTAssertNotEqual(speed.value as? String, previousSpeed)
        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.isHittable)
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let beforeTime = time.value as? String
        play.tap()
        let advanced = NSPredicate { _, _ in (time.value as? String) != beforeTime }
        expectation(for: advanced, evaluatedWith: nil)
        waitForExpectations(timeout: 5)
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].exists)
    }

    func testAnalyzingGalleryCanScrollInBothDirections() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text", "-ui-testing-editor-analyzing-gallery"]
        app.launch()
        let visuals = app.buttons["native-editor-tool-visuals"]
        XCTAssertTrue(visuals.waitForExistence(timeout: 20))
        visuals.tap()
        let gallery = app.scrollViews["native-editor-visuals-scroll"]
        XCTAssertTrue(gallery.waitForExistence(timeout: 5))
        let first = app.buttons["native-editor-add-visual-analyzing-0"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        XCTAssertFalse(first.isEnabled)
        let last = app.buttons["native-editor-add-visual-analyzing-23"]
        XCTAssertTrue(last.exists)
        let bottomBeforeThumbnails = last.frame.minY
        // Cover a poll while ready thumbnails decode beside the pending video.
        _ = XCTWaiter.wait(for: [XCTestExpectation(description: "Gallery refresh window")], timeout: 3.5)
        XCTAssertEqual(last.frame.minY, bottomBeforeThumbnails, accuracy: 1,
                       "Thumbnail loading and analysis polling must not change the scroll extent")
        let initialY = first.frame.minY
        let start = gallery.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.85))
        let end = gallery.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2))
        start.press(forDuration: 0.05, thenDragTo: end)
        XCTAssertTrue(!first.exists || first.frame.minY < initialY - 40)
        end.press(forDuration: 0.05, thenDragTo: start)
        gallery.swipeDown()
        XCTAssertTrue(app.buttons["native-editor-import-visual"].isHittable)
        XCTAssertFalse(first.isEnabled, "Scrolling must work before analysis finishes")
    }

    func testCaptionAndVisualPaperScreens() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-captions"].waitForExistence(timeout: 20))
        func capture(_ name: String) {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = name; attachment.lifetime = .keepAlways
            add(attachment)
        }
        app.buttons["native-editor-tool-captions"].tap()
        XCTAssertTrue(app.buttons["native-editor-captions-tab-Style"].waitForExistence(timeout: 5))
        capture("11-captions-edit")
        app.buttons["native-editor-captions-tab-Style"].tap()
        capture("12-captions-style")
        app.buttons["native-editor-captions-tab-Settings"].tap()
        XCTAssertFalse(app.staticTexts["Language"].exists)
        capture("13-captions-settings")
        app.buttons["native-editor-captions-done"].tap()
        app.buttons["native-editor-tool-visuals"].tap()
        capture("14-visuals-media")
        app.buttons["Text cards"].tap()
        capture("16-text-cards")
        app.buttons["Motion"].tap()
        capture("17-motion")
        app.buttons["Camera FX"].tap()
        capture("18-camera-fx")
        app.buttons["native-editor-visuals-done"].tap()
        let media = app.descendants(matching: .any)["native-editor-timeline-visual_block-paper-media"].firstMatch
        XCTAssertTrue(media.waitForExistence(timeout: 5))
        media.tap()
        XCTAssertTrue(app.staticTexts["Edit video"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
        capture("15-placement")
        app.buttons["native-editor-visuals-tab-Animation"].tap()
        capture("19-animation")
        app.buttons["In visual animation Pop"].tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-done"].exists)
    }
}
