import XCTest

@MainActor
final class NativeCaptionVisualUITests: XCTestCase {
    func testCaptionSizePositionScopesAndGestureChoice() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-talking-captions", "-ui-testing-editor-live-preview", "-ui-testing-editor-caption-style-scope"]
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-captions"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-captions"].tap()
        let row = app.buttons["native-editor-caption-row-native-caption-0"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()
        app.buttons["native-editor-caption-edit-done"].tap()
        app.buttons["native-editor-captions-tab-Style"].tap()
        let scope = app.segmentedControls["native-editor-caption-style-scope"]
        let scroll = app.scrollViews["native-editor-captions-scroll"]
        for _ in 0..<5 where !scope.isHittable { scroll.swipeUp() }
        XCTAssertTrue(scope.isHittable)
        scope.buttons["Line 1"].tap()
        XCTAssertTrue(scope.buttons["Line 1"].isSelected)
        let size = app.sliders["Caption font size in output pixels"]
        XCTAssertTrue(size.isHittable)
        size.adjust(toNormalizedSliderPosition: 0.2)
        let top = app.buttons["native-editor-caption-position-top"]
        for _ in 0..<3 where !top.isHittable { scroll.swipeUp() }
        XCTAssertTrue(top.isHittable)
        top.tap()
        let lineShot = XCTAttachment(screenshot: app.screenshot())
        lineShot.name = "caption-style-line-size-position"; lineShot.lifetime = .keepAlways; add(lineShot)
        for _ in 0..<3 where !scope.isHittable { scroll.swipeDown() }
        scope.buttons["All captions"].tap()
        size.adjust(toNormalizedSliderPosition: 0.15)
        for _ in 0..<3 where !top.isHittable { scroll.swipeUp() }
        app.buttons["native-editor-caption-position-middle"].tap()
        let allShot = XCTAttachment(screenshot: app.screenshot())
        allShot.name = "caption-style-all-size-position"; allShot.lifetime = .keepAlways; add(allShot)
        app.buttons["native-editor-captions-done"].tap()
        let caption = app.descendants(matching: .any)["native-editor-preview-caption_cue-native-caption-0"].firstMatch
        XCTAssertTrue(caption.waitForExistence(timeout: 10))
        let start = caption.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 20, dy: -30)))
        let choice = app.buttons["All captions in this video"]
        XCTAssertTrue(choice.waitForExistence(timeout: 5))
        let choiceShot = XCTAttachment(screenshot: app.screenshot())
        choiceShot.name = "caption-gesture-scope-choice"; choiceShot.lifetime = .keepAlways; add(choiceShot)
        choice.tap()
        XCTAssertFalse(choice.exists)
    }

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
        // An unfinished card survives closing the panel and switching tools (merged draft-survival check).
        app.buttons["native-editor-visuals-done"].tap()
        app.buttons["native-editor-tool-captions"].tap()
        app.buttons["native-editor-tool-visuals"].tap()
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        XCTAssertEqual(input.value as? String, "My visual card")
        app.scrollViews["native-editor-visuals-scroll"].swipeUp()
        app.buttons["Add card"].tap()
        XCTAssertTrue(app.buttons["native-editor-remove-visual"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Edit text card"].exists)
        XCTAssertFalse(app.buttons["native-editor-import-visual"].exists)
        app.buttons["native-editor-visuals-tab-Animation"].tap()
        XCTAssertFalse(app.buttons["native-editor-text-inspector-done"].exists)
    }

    func testVisualControlsEditAndPlaybackRemainAvailable() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launch()
        let visual = app.descendants(matching: .any)["native-editor-timeline-visual_block-paper-media"].firstMatch
        XCTAssertTrue(visual.waitForExistence(timeout: 20))
        visual.tap()
        let preview = app.descendants(matching: .any)["native-editor-preview"].firstMatch
        XCTAssertTrue(app.descendants(matching: .any)["Video preview"].firstMatch.waitForExistence(timeout: 15))
        for (edge, destination) in [("right", CGVector(dx: 0.9, dy: 0.5)),
                                    ("left", CGVector(dx: 0.06, dy: 0.5)),
                                    ("top", CGVector(dx: 0.5, dy: 0.08)),
                                    ("bottom", CGVector(dx: 0.5, dy: 0.78))] {
            let mediaStart = preview.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            let mediaEnd = preview.coordinate(withNormalizedOffset: destination)
            mediaStart.press(forDuration: 0.1, thenDragTo: mediaEnd, withVelocity: 100, thenHoldForDuration: 0.1)
            XCTAssertTrue((preview.value as? String ?? "").components(separatedBy: "dangerGuideLast:").last?
                .components(separatedBy: ",").contains(edge) == true,
                          "The live media footprint must warn at the \(edge) boundary")
            XCTAssertFalse(app.descendants(matching: .any)["native-editor-danger-guide"].firstMatch.exists)
            mediaEnd.press(forDuration: 0.1, thenDragTo: mediaStart, withVelocity: 100, thenHoldForDuration: 0.1)
            XCTAssertTrue((preview.value as? String ?? "").hasSuffix("dangerGuideLast:"),
                          "Moving the media back inside clears the warning")
        }
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = "media-danger-guide-cleared"; shot.lifetime = .keepAlways; add(shot)
        // KRI-170: only the panel handle grows the panel now (the timeline
        // handle resizes just the preview).
        let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5))
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10))
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
        // The tall panel now covers the transport (KRI-170); collapse it back
        // before checking playback is still available.
        // A small overshoot: a long pull past the bottom closes the panel (KRI-253).
        let raised = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10))
        raised.press(forDuration: 0.1, thenDragTo: raised.withOffset(CGVector(dx: 0, dy: 220)),
                     withVelocity: .slow, thenHoldForDuration: 0.2)
        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.waitForExistence(timeout: 5))
        XCTAssertTrue(play.isHittable)
        let time = app.descendants(matching: .any)["native-editor-current-time"].firstMatch
        let beforeTime = time.value as? String
        play.tap()
        let advanced = NSPredicate { _, _ in (time.value as? String) != beforeTime }
        expectation(for: advanced, evaluatedWith: nil)
        waitForExpectations(timeout: 5)
        XCTAssertTrue(app.buttons["native-editor-visuals-tab-Animation"].exists)
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
        // KRI-294: an analyzing Visual shows a plain status on its tile and the batch summary counts it down.
        let summary = app.descendants(matching: .any)["visual-preparation-summary"].firstMatch
        XCTAssertTrue(summary.waitForExistence(timeout: 5))
        XCTAssertTrue(summary.label.hasPrefix("Getting your videos ready, 23 of 24 ready."), summary.label)
        XCTAssertEqual(first.label, "Gallery video 0, Analyzing…")
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
        // The panel opens short; raise it, then the summary must sit at the top of the library.
        let handle = app.descendants(matching: .any)["native-editor-panel-resize"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 5))
        let grab = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0)).withOffset(CGVector(dx: 0, dy: 10))
        grab.press(forDuration: 0.1, thenDragTo: grab.withOffset(CGVector(dx: 0, dy: -420)))
        XCTAssertTrue(summary.isHittable, "the summary sits at the top of the library")
    }

    func testCaptionSwitchesAndDisplayAtLargestTextSize() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-caption-visuals", "-ui-testing-editor-source-text"]
        app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = "accessibility5"
        app.launch()
        XCTAssertTrue(app.buttons["native-editor-tool-captions"].waitForExistence(timeout: 20))
        func capture(_ name: String) {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.name = name; attachment.lifetime = .keepAlways
            add(attachment)
        }
        app.buttons["native-editor-tool-captions"].tap()
        let visible = app.switches["native-editor-caption-visible"]
        XCTAssertTrue(visible.waitForExistence(timeout: 5))
        XCTAssertTrue(visible.isHittable)
        XCTAssertTrue(app.switches["native-editor-caption-highlight"].isHittable)
        capture("ax5-captions-edit")
        app.buttons["native-editor-captions-tab-Style"].tap()
        XCTAssertTrue(app.buttons["native-editor-caption-display-word"].waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["native-editor-caption-display-sentence"].isHittable)
        XCTAssertTrue(app.buttons["native-editor-caption-display-word"].isHittable)
        capture("ax5-captions-style")
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
        XCTAssertFalse(app.buttons["native-editor-captions-tab-Settings"].exists)
        let visible = app.switches["native-editor-caption-visible"]
        let highlight = app.switches["native-editor-caption-highlight"]
        XCTAssertTrue(visible.waitForExistence(timeout: 5))
        XCTAssertTrue(highlight.exists)
        capture("11-captions-edit")
        if highlight.value as? String == "0" { highlight.tap() }
        XCTAssertEqual(highlight.value as? String, "1")
        XCTAssertTrue(app.descendants(matching: .any)["native-editor-caption-highlight-color"].waitForExistence(timeout: 3),
                      "highlight color shows while highlighting is on")
        capture("11b-captions-edit-highlight-on")
        highlight.tap()
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-caption-highlight-color"].exists)
        visible.tap()
        XCTAssertEqual(visible.value as? String, "0")
        XCTAssertFalse(highlight.isEnabled)
        capture("11c-captions-edit-hidden")
        visible.tap()
        app.buttons["native-editor-captions-tab-Style"].tap()
        let sentence = app.buttons["native-editor-caption-display-sentence"]
        let word = app.buttons["native-editor-caption-display-word"]
        XCTAssertTrue(word.waitForExistence(timeout: 3))
        word.tap()
        XCTAssertTrue(word.isSelected)
        XCTAssertFalse(sentence.isSelected)
        capture("12b-captions-style-word")
        sentence.tap()
        XCTAssertTrue(sentence.isSelected)
        capture("12-captions-style")
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

    // KRI-167: with the capability open (on device), the clip's "Transition" button reconnects the
    // per-clip picker, and the Visuals tab keeps the Transitions category (the deliberate device
    // exception in NativeVisualPanel.availableCategories; every other category is device-hidden).
    // The picker itself (clamping, wire-contract validation) is covered by NativeEditorInspectorTests;
    // this proves the routing. NativeSelectedClipInspector sits in an unidentified Form whose scroll
    // offset this harness can't drive deterministically, so the clip half stops at the sheet opening.
    func testTransitionControlsAreReachableWhenCapabilityOpen() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-device"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        let transitionButton = app.buttons["native-editor-clip-transition"]
        XCTAssertTrue(transitionButton.waitForExistence(timeout: 5))
        XCTAssertTrue(transitionButton.isHittable)
        transitionButton.tap()

        XCTAssertTrue(app.navigationBars["Clip"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-inspector-done"].exists)
        app.buttons["native-editor-inspector-done"].tap()
        XCTAssertFalse(app.navigationBars["Clip"].exists)
        // The rest of the editor stays usable once the sheet closes.
        XCTAssertTrue(app.buttons["native-editor-adjust"].waitForExistence(timeout: 5))

        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 5))
        app.buttons["native-editor-tool-visuals"].tap()
        XCTAssertTrue(app.buttons["Transitions"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Text cards"].exists, "other categories stay device-hidden")
        XCTAssertFalse(app.buttons["Motion"].exists)
        XCTAssertFalse(app.buttons["Camera FX"].exists)

        app.buttons["Transitions"].tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-apply-transition"].waitForExistence(timeout: 5))
        app.buttons["native-editor-visuals-apply-transition"].tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-apply-transition"].exists, "applying doesn't leave the panel")
    }

    // KRI-167: with clips.transitions closed neither the clip button nor the Transitions category
    // may appear (not shown-disabled) -- a render that can't save a transition edit would 422 at Save.
    func testTransitionControlsHiddenWhenCapabilityClosed() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-all-lanes", "-ui-testing-editor-device", "-ui-testing-editor-transitions-closed"]
        app.launch()
        let clip = app.descendants(matching: .any)["native-editor-clip-1"].firstMatch
        XCTAssertTrue(clip.waitForExistence(timeout: 20))
        clip.tap()
        XCTAssertTrue(app.buttons["native-editor-adjust"].waitForExistence(timeout: 5), "the rest of the context strip must still appear")
        XCTAssertFalse(app.buttons["native-editor-clip-transition"].exists)

        XCTAssertTrue(app.buttons["native-editor-tool-visuals"].waitForExistence(timeout: 20))
        app.buttons["native-editor-tool-visuals"].tap()
        XCTAssertTrue(app.buttons["native-editor-import-visual"].waitForExistence(timeout: 5), "media category (the only device category) still renders")
        XCTAssertFalse(app.buttons["Transitions"].exists)
    }
}
