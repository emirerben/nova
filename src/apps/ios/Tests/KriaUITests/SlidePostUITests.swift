import UniformTypeIdentifiers
import XCTest

@MainActor final class SlidePostUITests: XCTestCase {
    /// A READY slide post whose drawer row has no format signal must land in the slide workspace; the video
    /// editor (`native-editor-*`) and its Chat|Editor entry must never appear.
    func testReadySlidePostOpenedFromDrawerNeverReachesVideoEditor() {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        app.launchEnvironment["KRIA_SLIDE_POST_READY_THREAD"] = "1"
        app.launch()
        // The only project is selected on launch (same `selectProject` state the drawer sets); when the
        // chat chrome is showing, also go through the drawer row like a user would.
        let menu = app.buttons["Open projects"]
        if menu.waitForExistence(timeout: 4) {
            menu.tap()
            let row = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Weekend trip'")).firstMatch
            if row.waitForExistence(timeout: 4) { row.tap() }
        }
        let slide = app.buttons["slidepost-add-tile"].firstMatch
        _ = slide.waitForExistence(timeout: 15)
        XCTAssertTrue(slide.exists, "slide workspace appears")
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-preview"].exists)
        XCTAssertFalse(app.buttons["Editor"].exists, "no Chat|Editor switch for a slide post")
        XCTAssertFalse(app.buttons["open-current-cut"].exists)
    }

    /// New chat -> Slider lands straight in the one slide layout (no "Start your post"
    /// screen): preview, strip, AI button and Save/Create, then Create and the contextual Kria sheet.
    func testSlidePostCreationIsTheRichLayoutThenCreateThenKriaPropose() {
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        let viewport = app.windows.firstMatch.frame
        XCTAssertLessThanOrEqual(preview.frame.width, viewport.width)
        XCTAssertLessThanOrEqual(preview.frame.height, viewport.height)
        XCTAssertEqual(preview.frame.width / preview.frame.height, 4.0 / 5.0, accuracy: 0.06)
        XCTAssertTrue(app.buttons["slidepost-openkria"].isHittable)
        attach(app, "Native slide draft preview")
        let save = app.buttons["slidepost-save"]
        XCTAssertTrue(save.isEnabled, "a fresh draft is unsaved")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        // There is no Create step, and export is available from any state: exporting an unsaved, unrendered post
        // saves it, renders it, then reports the Photos save in the banner (the fixture writer stands in for Photos).
        let export = app.buttons["slidepost-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 5)); XCTAssertTrue(export.isEnabled); export.tap()
        XCTAssertTrue(app.buttons["slidepost-share-files"].waitForExistence(timeout: 3), "Share files sits beside Save to Photos")
        app.buttons["slidepost-save-photos"].tap()
        let banner = app.descendants(matching: .any)["slidepost-export-state"]
        XCTAssertTrue(banner.waitForExistence(timeout: 10), "export reports progress in the banner")
        expectation(for: NSPredicate(format: "label CONTAINS %@", "Saved 3 slides"), evaluatedWith: banner); waitForExpectations(timeout: 25)
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "exporting saved the draft first")
        attach(app, "Native slide post exported")
        app.buttons["slidepost-openkria"].tap()
        let input = app.textFields["Message Kria"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Kria"].exists)
        input.tap(); input.typeText("End on the view.")
        app.buttons["chat-send-message"].tap()
        let applyChange = app.buttons["slidepost-apply"]
        XCTAssertTrue(applyChange.waitForExistence(timeout: 8)); applyChange.tap()
        attach(app, "Native Kria applied proposal")
    }

    /// Slide posts have no header actions menu, so the drawer row is their only way to rename. The alert
    /// used to hang off that lazy row and vanished when the keyboard came up.
    func testRenamingASlidePostFromTheDrawerKeepsTheAlertOpen() {
        let app = openRichWorkspace()
        app.buttons["slidepost-back"].tap()
        let menu = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Project actions for'")).firstMatch
        XCTAssertTrue(menu.waitForExistence(timeout: 5)); menu.tap()
        app.buttons["Rename project"].tap()
        let panel = app.alerts["Rename project"]
        XCTAssertTrue(panel.waitForExistence(timeout: 3))
        let field = panel.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 3))
        field.tap(); field.typeText(" renamed")
        // Outlast the keyboard layout pass and the workspace's background refresh behind the alert.
        _ = eventually(timeout: 5) { !panel.exists }
        XCTAssertTrue(panel.exists, "the rename alert must stay open while the user types")
        XCTAssertTrue((field.value as? String ?? "").hasSuffix(" renamed"))
        panel.buttons["Cancel"].tap()
    }

    /// The Slider tile is the fourth card in the format picker; its picture must be exactly as tall as the others'.
    func testSliderFormatTileMatchesTheOtherTilesPictureSize() {
        let app = launchRich()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 12)); app.buttons["Open projects"].tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 5)); app.buttons["drawer-new-chat"].tap()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 12))
        expectation(for: NSPredicate(format: "isHittable == true"), evaluatedWith: carousel); waitForExpectations(timeout: 20)
        carousel.swipeLeft()
        let slides = app.buttons["format-slides"]
        XCTAssertTrue(slides.waitForExistence(timeout: 5))
        XCTAssertEqual(slides.frame.height, 228, accuracy: 0.5, "same 156x228 picture as the other format tiles")
        XCTAssertEqual(slides.frame.width, 156, accuracy: 0.5)
        // Let it swipe once so the screenshot shows a settled frame, not half of two.
        _ = eventually(timeout: 3.6) { false }
        attach(app, "Slider format tile after one swipe")
    }

    // MARK: Redesigned workspace (KRIA_SLIDE_POST_RICH_TEXT=1)

    func testLooksAppearOnThePhotoBeforeSavingWhenDeviceCapabilityIsEnabled() {
        let app = openRichWorkspace(save: false, extraEnv: [
            "KRIA_SLIDE_POST_REMOTE_MEDIA": "1", "KRIA_SLIDE_POST_EXTENDED_DEVICE_EXPORT": "1",
        ])
        let image = app.descendants(matching: .any)["slidepost-preview-image"]
        XCTAssertTrue(image.waitForExistence(timeout: 10))
        app.buttons["slidepost-tool-look"].tap()
        XCTAssertFalse(app.staticTexts["Save to apply this look to the preview."].exists)
        for look in ["golden_hour", "smoky_split_tone", "faded_analog"] {
            app.buttons["slidepost-look-\(look)"].tap()
            expectation(for: NSPredicate(format: "value ENDSWITH %@", "|look:\(look)"), evaluatedWith: image)
            waitForExpectations(timeout: 15)
            XCTAssertFalse(app.staticTexts["Look preview unavailable"].exists)
            attach(app, "Live \(look) before save")
        }
        app.buttons["slidepost-look-none"].tap()
        expectation(for: NSPredicate(format: "value ENDSWITH '|full'"), evaluatedWith: image)
        waitForExpectations(timeout: 10)
    }

    /// Walks the fixture creation flow to the redesigned workspace: format, direction, Apply.
    private func openRichWorkspace(dynamicType: String? = nil, chatEdit: Bool = false, many: Bool = false, lateAsset: Bool = false, capabilities: String? = nil, save: Bool = true, extraEnv: [String: String] = [:]) -> XCUIApplication {
        let app = launchRich(dynamicType: dynamicType, chatEdit: chatEdit, many: many, lateAsset: lateAsset, capabilities: capabilities, extraEnv: extraEnv)
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 12)); app.buttons["Open projects"].tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 5)); app.buttons["drawer-new-chat"].tap()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 12))
        expectation(for: NSPredicate(format: "isHittable == true"), evaluatedWith: carousel); waitForExpectations(timeout: 20)
        carousel.swipeLeft()
        XCTAssertTrue(app.buttons["format-slides"].waitForExistence(timeout: 5)); app.buttons["format-slides"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 12), "the one slide layout appears with its tool dock")
        if save { saveDraft(app) }
        return app
    }
    private func launchRich(dynamicType: String? = nil, chatEdit: Bool = false, many: Bool = false, lateAsset: Bool = false, capabilities: String? = nil, readyThread: Bool = false, extraEnv: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        for (key, value) in extraEnv { app.launchEnvironment[key] = value }
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = readyThread ? "v2" : "v1"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        // Capability fixtures: nil = loads true (rich + maybe chat edit), "slow" = answers after ~6s, "fail" = never answers.
        if let capabilities { app.launchEnvironment["KRIA_SLIDE_POST_CAPS"] = capabilities } else { app.launchEnvironment["KRIA_SLIDE_POST_RICH_TEXT"] = "1" }
        if chatEdit { app.launchEnvironment["KRIA_SLIDE_POST_CHAT_EDIT"] = "1" }
        if many { app.launchEnvironment["KRIA_SLIDE_POST_MANY"] = "1" }
        if lateAsset { app.launchEnvironment["KRIA_SLIDE_POST_LATE_ASSET"] = "1" }
        if readyThread { app.launchEnvironment["KRIA_SLIDE_POST_READY_THREAD"] = "1" }
        if let dynamicType { app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = dynamicType }
        app.launch()
        return app
    }
    private func saveDraft(_ app: XCUIApplication) {
        let save = app.buttons["slidepost-save"]
        if save.waitForExistence(timeout: 5), save.isEnabled { save.tap() }
        expectation(for: NSPredicate(format: "label != %@", "Unsaved changes"), evaluatedWith: app.staticTexts["slidepost-subtitle"])
        waitForExpectations(timeout: 10)
    }
    /// Polls a condition on the main run loop (a CGRect key path is not usable in an NSPredicate).
    private func eventually(timeout: TimeInterval = 6, _ condition: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end { if condition() { return true }; RunLoop.current.run(until: Date().addingTimeInterval(0.2)) }
        return condition()
    }
    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = name; shot.lifetime = .keepAlways; add(shot)
    }

    func testSlidePreviewLeavesToolbarAndStripVisible() {
        let app = openRichWorkspace()
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        let window = app.windows.firstMatch.frame
        XCTAssertLessThanOrEqual(preview.frame.height, window.height * 0.56, "the preview is capped near 53.5% of the screen")
        XCTAssertEqual(preview.frame.width / preview.frame.height, 4.0 / 5.0, accuracy: 0.05, "aspect is preserved")
        for id in ["slidepost-tile-1", "slidepost-add-tile", "slidepost-openkria", "slidepost-tool-text", "slidepost-tool-cover", "slidepost-tool-look", "slidepost-tool-more"] {
            let element = app.descendants(matching: .any)[id].firstMatch
            XCTAssertTrue(element.exists && element.isHittable, "\(id) must stay reachable without scrolling")
        }
        XCTAssertLessThanOrEqual(preview.frame.maxY, app.buttons["slidepost-tile-1"].frame.minY + 1, "the preview never overlaps the strip")
        XCTAssertFalse(app.buttons["slidepost-tool-arrange"].exists, "there is no Arrange tool: reorder by dragging")
        XCTAssertFalse(app.textFields["slidepost-composer"].exists, "the page has no chat composer")
        XCTAssertFalse(app.buttons["slidepost-chat-open"].exists)
        attach(app, "Workspace 4:5")
    }

    /// KRI-644: selecting the fixture's video slide (slide 2) plays it at once with no native player chrome over the
    /// slide, only a small pause/play control. Photo -> video -> photo -> video, pause/resume, and a trip to the
    /// background all leave exactly the selected video playing.
    func testSelectedVideoSlidePlaysByItselfWithoutPlayerChrome() {
        let app = openRichWorkspace(save: false)
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-preview"].firstMatch.waitForExistence(timeout: 8))
        let toggle = app.buttons["slidepost-video-toggle"]
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertFalse(toggle.exists, "a photo slide has no playback control")
        app.buttons["slidepost-tile-2"].tap()
        XCTAssertTrue(toggle.waitForExistence(timeout: 8), "the video slide shows its pause control")
        waitFor(toggle, value: "Playing", "selecting the video starts it without a play tap")
        XCTAssertEqual(toggle.label, "Pause video")
        for chrome in ["Play", "Pause", "Skip Forward", "Skip Back", "Full Screen"] {
            XCTAssertFalse(app.buttons[chrome].exists, "no native \(chrome) control over the slide")
        }
        XCTAssertEqual(app.sliders.count, 0, "no native scrubber over the slide")
        attach(app, "Video slide playing")

        toggle.tap()
        waitFor(toggle, value: "Paused", "the control pauses it")
        XCTAssertEqual(toggle.label, "Play video")
        toggle.tap()
        waitFor(toggle, value: "Playing", "and plays it again")

        app.buttons["slidepost-tile-3"].tap()
        XCTAssertTrue(eventually { !toggle.exists }, "another slide stops the video and drops its control")
        attach(app, "Photo slide after the video")
        app.buttons["slidepost-tile-2"].tap()
        XCTAssertTrue(toggle.waitForExistence(timeout: 8))
        waitFor(toggle, value: "Playing", "coming back to the video starts it again")

        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(toggle.waitForExistence(timeout: 8))
        waitFor(toggle, value: "Playing", "returning to the app resumes the selected video")
    }

    /// KRI-644: a video that cannot play says so over its poster and offers Try again, instead of a dead player.
    func testUnplayableVideoSlideExplainsAndOffersRetry() {
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_BROKEN_VIDEO": "1"])
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-preview"].firstMatch.waitForExistence(timeout: 8))
        app.buttons["slidepost-tile-2"].tap()
        XCTAssertTrue(app.staticTexts["slidepost-video-failed"].waitForExistence(timeout: 10), "the failure is explained")
        XCTAssertEqual(app.staticTexts["slidepost-video-failed"].label, "Couldn’t play this video")
        let retry = app.buttons["slidepost-video-retry"]
        XCTAssertTrue(retry.isHittable)
        XCTAssertFalse(app.buttons["slidepost-video-toggle"].exists, "no pause control for a video that is not playing")
        attach(app, "Unplayable video slide")
        retry.tap()
        XCTAssertTrue(app.staticTexts["slidepost-video-failed"].waitForExistence(timeout: 10), "Try again loads it again")
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertTrue(eventually { !app.staticTexts["slidepost-video-failed"].exists }, "the notice belongs to the video slide only")
    }

    private func waitFor(_ element: XCUIElement, value: String, _ message: String, timeout: TimeInterval = 10) {
        XCTAssertTrue(eventually(timeout: timeout) { element.exists && element.value as? String == value },
                      "\(message) (value: \(String(describing: element.value)))")
    }

    /// KRI-606: the Size tool resizes the whole post at once, "Fit all" shows each photo whole with bars, and
    /// both survive a save (the fixture server echoes exactly what the app sent, `aspect` and `fit` included).
    func testSizeSheetSquareFitAllReshapesThePreviewAndSurvivesSave() {
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1", "KRIA_SLIDE_POST_SIZES": "1"])
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertEqual(preview.frame.width / preview.frame.height, 4.0 / 5.0, accuracy: 0.06, "starts at the Instagram default")
        let size = app.buttons["slidepost-tool-size"]
        XCTAssertTrue(size.waitForExistence(timeout: 8), "the Size tool is offered when the server advertises slide_post_sizes")
        size.tap()
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-size-sheet"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["slidepost-size-chip-1x1"].exists)
        XCTAssertFalse(app.buttons["slidepost-size-chip-9x16"].exists, "no full-screen size on Instagram")
        attach(app, "Size sheet (Instagram)")
        app.buttons["slidepost-size-chip-1x1"].tap()
        app.buttons["slidepost-size-fit-fit"].tap()
        XCTAssertTrue(app.buttons["slidepost-size-bars-blur"].waitForExistence(timeout: 3), "Fit offers bar styles")
        app.buttons["slidepost-size-bars-white"].tap()
        XCTAssertTrue(app.buttons["slidepost-size-bars-white"].isSelected)
        attach(app, "Size sheet (square, white bars)")
        app.buttons["slidepost-size-done"].tap()
        XCTAssertTrue(waitForSquare(preview), "the stage reshapes to 1:1 once the sheet closes")
        attach(app, "Square post, photos fit with white bars")
        saveDraft(app)
        // With sizes on, the status line is a button that reopens the sheet, not a plain text.
        let subtitle = app.descendants(matching: .any)["slidepost-subtitle"].firstMatch
        expectation(for: NSPredicate(format: "label CONTAINS %@", "1:1"), evaluatedWith: subtitle)
        waitForExpectations(timeout: 10)
        XCTAssertTrue(waitForSquare(preview), "the saved draft keeps the size")
        subtitle.tap()
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-size-sheet"].waitForExistence(timeout: 5), "the status line reopens the Size sheet")
        XCTAssertTrue(app.buttons["slidepost-size-fit-fit"].isSelected, "Fit survived the save")
    }

    /// KRI-646: the Bend slider (curved text) is offered only when the server advertises `text_bend`; dragging it
    /// curves the slide's text, and "Apply to all slides" carries the bend to the other slides.
    func testBendSliderCurvesTheTextAndApplyToAllCopiesIt() {
        func openTextStyle(_ app: XCUIApplication) {
            app.buttons["slidepost-tool-text"].tap()
            let field = app.textViews["slidepost-text-field"].firstMatch
            XCTAssertTrue(field.waitForExistence(timeout: 5))
            field.tap(); field.press(forDuration: 1.0)
            if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
            field.typeText("Athens")
            app.buttons["Style"].tap()
            XCTAssertTrue(app.buttons["native-editor-text-preset"].waitForExistence(timeout: 5))
        }
        let off = openRichWorkspace(save: false)
        openTextStyle(off)
        revealInTextPanel(off.sliders["native-editor-text-rotation"], app: off)
        XCTAssertTrue(off.sliders["native-editor-text-rotation"].exists)
        XCTAssertFalse(off.sliders["native-editor-text-bend"].exists, "no Bend slider without the capability")
        off.terminate()

        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_TEXT_BEND": "1"])
        openTextStyle(app)
        let bend = app.sliders["native-editor-text-bend"]
        revealInTextPanel(bend, app: app)
        XCTAssertTrue(bend.exists, "the Bend slider is offered when the server advertises text_bend")
        XCTAssertEqual(bend.value as? String, "0", "text starts straight")
        bend.adjust(toNormalizedSliderPosition: 0.85)
        let applied = bend.value as? String
        XCTAssertNotNil(applied)
        XCTAssertNotEqual(applied, "0", "the slider moved the bend")
        attach(app, "Bend slider on a slide text")
        app.buttons["slidepost-done"].tap()
        // Slide 2 gets the same look from "Apply to all slides", bend included.
        app.buttons["slidepost-tile-2"].tap()
        app.buttons["slidepost-tool-text"].tap()
        XCTAssertTrue(app.textViews["slidepost-text-field"].firstMatch.waitForExistence(timeout: 5))
        app.buttons["slidepost-done"].tap()
        app.buttons["slidepost-tile-1"].tap()
        app.buttons["slidepost-tool-text"].tap()
        app.buttons["Style"].tap()
        let applyAll = app.buttons["slidepost-apply-all"]
        XCTAssertTrue(applyAll.waitForExistence(timeout: 5)); applyAll.tap()
        XCTAssertTrue(app.staticTexts["slidepost-apply-all-result"].waitForExistence(timeout: 3))
        app.buttons["slidepost-done"].tap()
        app.buttons["slidepost-tile-2"].tap()
        app.buttons["slidepost-tool-text"].tap()
        app.buttons["Style"].tap()
        let target = app.sliders["native-editor-text-bend"]
        revealInTextPanel(target, app: app)
        XCTAssertEqual(target.value as? String, applied, "apply-to-all copies the bend")
        attach(app, "Bend applied to all slides")
    }

    /// KRI-589: the Draw tool is offered only when the server advertises `slide_post_drawing`; a finger stroke + Done
    /// leaves static ink on the slide, Cancel leaves nothing, "Draw more" appends, and a save (the fixture server echoes
    /// exactly what the app sent) keeps the ink on that slide only.
    func testDrawOnASlideLeavesInkThatSurvivesSaveAndSlideSwitching() {
        let off = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        XCTAssertTrue(off.buttons["slidepost-tool-text"].exists)
        XCTAssertFalse(off.buttons["slidepost-tool-draw"].exists, "no Draw tool without the capability")
        off.terminate()

        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1", "KRIA_SLIDE_POST_DRAWING": "1"])
        let ink = app.descendants(matching: .any)["slidepost-drawing-ink"].firstMatch
        XCTAssertFalse(ink.exists, "a fresh slide has no ink")
        let draw = app.buttons["slidepost-tool-draw"]
        XCTAssertTrue(draw.waitForExistence(timeout: 8), "the Draw tool is offered when the server advertises slide_post_drawing")

        func stroke(_ canvas: XCUIElement, _ a: CGVector, _ b: CGVector) {
            canvas.coordinate(withNormalizedOffset: a).press(forDuration: 0.05, thenDragTo: canvas.coordinate(withNormalizedOffset: b))
        }
        // Cancel leaves no residue.
        draw.tap()
        let canvas = app.descendants(matching: .any)["native-editor-draw-canvas"].firstMatch
        XCTAssertTrue(canvas.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-draw-done"].isEnabled, "nothing drawn yet")
        stroke(canvas, CGVector(dx: 0.2, dy: 0.3), CGVector(dx: 0.8, dy: 0.4))
        attach(app, "Slide draw mode with one stroke")
        app.buttons["native-editor-draw-cancel"].tap()
        XCTAssertFalse(ink.waitForExistence(timeout: 1.5), "Cancel leaves no ink")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "the fresh draft is unsaved anyway")

        // Done keeps the stroke as static ink.
        draw.tap()
        XCTAssertTrue(canvas.waitForExistence(timeout: 5))
        app.buttons["native-editor-draw-swatch-32ADE6"].tap()
        stroke(canvas, CGVector(dx: 0.2, dy: 0.3), CGVector(dx: 0.8, dy: 0.4))
        XCTAssertTrue(app.buttons["native-editor-draw-done"].isEnabled)
        app.buttons["native-editor-draw-done"].tap()
        XCTAssertTrue(ink.waitForExistence(timeout: 5), "Done leaves the ink on the slide")
        XCTAssertEqual(ink.value as? String, "1 stroke")
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 3), "back to the browse dock")
        attach(app, "Slide with a saved stroke")

        // Draw more appends to what is already there.
        draw.tap()
        XCTAssertTrue(canvas.waitForExistence(timeout: 5))
        stroke(canvas, CGVector(dx: 0.3, dy: 0.7), CGVector(dx: 0.7, dy: 0.6))
        app.buttons["native-editor-draw-done"].tap()
        expectation(for: NSPredicate(format: "value == %@", "2 strokes"), evaluatedWith: ink); waitForExpectations(timeout: 5)

        // The save keeps it, on this slide only.
        saveDraft(app)
        app.buttons["slidepost-tile-2"].tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: ink); waitForExpectations(timeout: 5)
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertTrue(ink.waitForExistence(timeout: 5), "the saved ink is back on slide 1")
        XCTAssertEqual(ink.value as? String, "2 strokes")
        attach(app, "Saved ink after switching slides")

        // Remove drawing (More menu) removes it, and undo brings it back.
        app.buttons["slidepost-tool-more"].tap()
        let clear = app.buttons["slidepost-clear-drawing"]
        XCTAssertTrue(clear.waitForExistence(timeout: 3)); clear.tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: ink); waitForExpectations(timeout: 5)
        app.buttons["slidepost-undo"].tap()
        XCTAssertTrue(ink.waitForExistence(timeout: 5), "undo restores the cleared drawing")
    }

    /// KRI-1138: the Tilt tool is offered only when the server advertises `slide_post_tilt`; the dial turns the photo live,
    /// a background can be picked, Done keeps it, a save (the fixture server echoes what the app sent) keeps it on that
    /// slide only, and Reset puts the photo back.
    func testTiltTurnsThePhotoLiveAndSurvivesSave() throws {
        let off = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        XCTAssertTrue(off.buttons["slidepost-tool-text"].exists)
        XCTAssertFalse(off.buttons["slidepost-tool-tilt"].exists, "no Tilt tool without the capability")
        off.terminate()

        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1", "KRIA_SLIDE_POST_TILT": "1"])
        let tilt = app.buttons["slidepost-tool-tilt"]
        XCTAssertTrue(tilt.waitForExistence(timeout: 8), "the Tilt tool is offered when the server advertises slide_post_tilt")
        let preview = app.descendants(matching: .any)["slidepost-preview-image"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        XCTAssertFalse((preview.value as? String ?? "").contains("|tilt:"), "a fresh photo is upright")

        tilt.tap()
        let dial = app.sliders["slidepost-tilt-dial"]
        XCTAssertTrue(dial.waitForExistence(timeout: 5))
        let readout = app.staticTexts["slidepost-tilt-readout"]
        XCTAssertEqual(readout.label, "Upright")
        app.buttons["slidepost-tilt-bg-blur"].tap()
        dial.adjust(toNormalizedSliderPosition: 0.75)
        expectation(for: NSPredicate(format: "label == %@", "90\u{00B0} right"), evaluatedWith: readout); waitForExpectations(timeout: 5)
        expectation(for: NSPredicate(format: "value CONTAINS %@", "|tilt:90"), evaluatedWith: preview); waitForExpectations(timeout: 5)
        attach(app, "Slide tilted 90 degrees with a blurred background")

        app.buttons["slidepost-tilt-minus"].tap()
        expectation(for: NSPredicate(format: "label == %@", "89\u{00B0} right"), evaluatedWith: readout); waitForExpectations(timeout: 5)
        app.buttons["slidepost-done"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 3), "back to the browse dock")

        // The save keeps it, on this slide only.
        saveDraft(app)
        app.buttons["slidepost-tile-2"].tap()
        expectation(for: NSPredicate(format: "NOT (value CONTAINS %@)", "|tilt:"), evaluatedWith: preview); waitForExpectations(timeout: 5)
        app.buttons["slidepost-tile-1"].tap()
        expectation(for: NSPredicate(format: "value CONTAINS %@", "|tilt:89"), evaluatedWith: preview); waitForExpectations(timeout: 5)

        // Reset puts the photo back, and undo brings the turn back.
        tilt.tap()
        XCTAssertTrue(app.buttons["slidepost-tilt-reset"].waitForExistence(timeout: 3))
        app.buttons["slidepost-tilt-reset"].tap()
        expectation(for: NSPredicate(format: "NOT (value CONTAINS %@)", "|tilt:"), evaluatedWith: preview); waitForExpectations(timeout: 5)
        app.buttons["slidepost-done"].tap()
        app.buttons["slidepost-undo"].tap()
        expectation(for: NSPredicate(format: "value CONTAINS %@", "|tilt:89"), evaluatedWith: preview); waitForExpectations(timeout: 5)
    }

    /// True once the preview element is square (polls; `frame` is not predicate-addressable).
    private func waitForSquare(_ element: XCUIElement, timeout: TimeInterval = 8) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let frame = element.frame
            if frame.height > 0, abs(frame.width / frame.height - 1) < 0.06 { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.2))
        }
        return false
    }

    private func revealInTextPanel(_ element: XCUIElement, app: XCUIApplication) {
        let scroll = app.scrollViews["native-editor-text-inspector-scroll"]
        for _ in 0..<6 where !(element.exists && element.isHittable) { scroll.swipeUp() }
    }

    func testAddStyleAndApplyTextToAllSlides() {
        let app = openRichWorkspace()
        // Slide 1: add text (the real native Text panel opens on Edit with the box focused).
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertFalse(app.textFields["native-editor-text-time-start"].exists, "slides have no timeline")
        XCTAssertFalse(app.buttons["Animation"].exists, "slides have no Animation tab")
        field.tap(); field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        field.typeText("Athens")
        attach(app, "Text mode: edit tab")
        app.buttons["Style"].tap()
        let preset = app.buttons["native-editor-text-preset"]
        XCTAssertTrue(preset.waitForExistence(timeout: 5))
        preset.tap(); app.buttons["Highlight"].tap()
        XCTAssertTrue(preset.label.contains("Highlight"))
        // KRI-646: rotation is a slider. Drag it to 3/4 of the range (about 90 degrees).
        let rotation = app.sliders["native-editor-text-rotation"]
        revealInTextPanel(rotation, app: app)
        rotation.adjust(toNormalizedSliderPosition: 0.75)
        let appliedRotation = rotation.value as? String
        XCTAssertNotNil(appliedRotation)
        XCTAssertNotEqual(appliedRotation, "0 degrees", "the slider moved the rotation")
        attach(app, "Text mode: style tab")
        app.buttons["slidepost-done"].tap()
        // Slide 2: add text with the default look.
        app.buttons["slidepost-tile-2"].tap()
        app.buttons["slidepost-tool-text"].tap()
        XCTAssertTrue(app.textViews["slidepost-text-field"].firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-text-preset"].exists, "the edit tab does not show style controls")
        app.buttons["slidepost-done"].tap()
        // Back on slide 1: apply its whole look to every slide.
        app.buttons["slidepost-tile-1"].tap()
        app.buttons["slidepost-tool-text"].tap()
        app.buttons["Style"].tap()
        let applyAll = app.buttons["slidepost-apply-all"]
        XCTAssertTrue(applyAll.waitForExistence(timeout: 5)); applyAll.tap()
        XCTAssertTrue(app.staticTexts["slidepost-apply-all-result"].waitForExistence(timeout: 3))
        app.buttons["slidepost-done"].tap()
        // Slide 2 now carries slide 1's preset and rotation, but its own words.
        app.buttons["slidepost-tile-2"].tap()
        app.buttons["slidepost-tool-text"].tap()
        app.buttons["Style"].tap()
        XCTAssertTrue(app.buttons["native-editor-text-preset"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-text-preset"].label.contains("Highlight"))
        let target = app.sliders["native-editor-text-rotation"]
        revealInTextPanel(target, app: app)
        XCTAssertEqual(target.value as? String, appliedRotation, "apply-to-all copies the rotation")
        app.buttons["Edit text"].tap()
        XCTAssertEqual(app.textViews["slidepost-text-field"].firstMatch.value as? String, "Your text", "words are never copied")
        attach(app, "Style applied to all slides")
    }

    // MARK: Canvas text manipulation (KRI-298 Lane G: same gestures as the native video preview)

    private func canvasText(_ app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-text-'")).firstMatch
    }
    private func number(_ value: String, after key: String) -> Double {
        guard let range = value.range(of: key + " ") else { return .nan }
        let tail = value[range.upperBound...].prefix { "-0123456789.".contains($0) }
        return Double(tail) ?? .nan
    }

    func testSlideTextSelectDragPinchRotateAndDeselect() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap(); field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        field.typeText("Athens")
        app.buttons["Style"].tap()
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        attach(app, "Slide canvas: text before manipulation")

        // Select by tapping the text itself (opens the Edit tab with the keyboard); transforms happen on Style.
        text.tap()
        app.buttons["Style"].tap()
        let handle = app.descendants(matching: .any)["slidepost-text-handle"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 3), "selecting shows the real resize/rotate handle")
        XCTAssertTrue(preview.frame.insetBy(dx: -1, dy: -1).contains(CGPoint(x: handle.frame.midX, y: handle.frame.midY)), "the handle is fully inside the stage")
        XCTAssertTrue(handle.frame.minX >= preview.frame.minX - 1 && handle.frame.maxX <= preview.frame.maxX + 1 && handle.frame.maxY <= preview.frame.maxY + 1, "the handle is never clipped")
        attach(app, "Slide canvas: selected with handle")

        // Drag moves the text.
        let beforeMove = text.value as? String ?? ""
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.1, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.25)))
        let afterMove = text.value as? String ?? ""
        XCTAssertNotEqual(beforeMove, afterMove, "dragging changes the position")
        XCTAssertLessThan(number(afterMove, after: "position"), 45)
        attach(app, "Slide canvas: moved")

        // Pinch resizes.
        let sizeBefore = number(text.value as? String ?? "", after: "size")
        preview.pinch(withScale: 1.5, velocity: 1)
        let sizeAfter = number(text.value as? String ?? "", after: "size")
        XCTAssertGreaterThan(sizeAfter, sizeBefore, "pinching out grows the text")
        attach(app, "Slide canvas: resized")

        // Two-finger rotate.
        let rotationBefore = number(text.value as? String ?? "", after: "rotation")
        preview.rotate(CGFloat.pi / 3, withVelocity: 1)
        let rotation = number(text.value as? String ?? "", after: "rotation")
        XCTAssertGreaterThan(abs(rotation - rotationBefore), 20, "twisting rotates the text")
        attach(app, "Slide canvas: rotated")

        // Tapping empty canvas deselects.
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.06)).tap()
        XCTAssertFalse(handle.waitForExistence(timeout: 2), "tapping empty canvas deselects")
        attach(app, "Slide canvas: deselected")
    }

    func testMoreMenuRemoveAndCover() {
        let app = openRichWorkspace()
        XCTAssertTrue(app.buttons["slidepost-tile-3"].exists)
        app.buttons["slidepost-tile-2"].tap()
        app.buttons["slidepost-tool-cover"].tap()
        XCTAssertEqual(app.buttons["slidepost-tile-2"].value as? String, "Cover")
        XCTAssertNotEqual(app.buttons["slidepost-tile-1"].value as? String, "Cover")
        app.buttons["slidepost-tool-more"].tap()
        let remove = app.buttons["Remove slide"]
        XCTAssertTrue(remove.waitForExistence(timeout: 3)); remove.tap()
        XCTAssertFalse(app.buttons["slidepost-tile-3"].exists, "the selected slide is gone")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertTrue(app.buttons["slidepost-undo"].isEnabled)
        app.buttons["slidepost-undo"].tap()
        XCTAssertTrue(app.buttons["slidepost-tile-3"].waitForExistence(timeout: 3), "undo restores the removed slide")
        attach(app, "More menu then undo")
    }

    /// KRI-647: the system Paste control in the tool dock turns copied text into a text on the selected slide.
    func testPasteAddsCopiedTextToTheSelectedSlide() {
        UIPasteboard.general.string = "5.2 km in 28:14"
        let app = openRichWorkspace()
        XCTAssertFalse(canvasText(app).exists, "a fresh slide has no text")
        let paste = app.descendants(matching: .any)["slidepost-paste"].firstMatch
        XCTAssertTrue(paste.waitForExistence(timeout: 5))
        paste.tap()
        XCTAssertTrue(canvasText(app).waitForExistence(timeout: 5), "the pasted text appears on the canvas")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertTrue(app.buttons["slidepost-undo"].isEnabled)
        attach(app, "Pasted text on a slide")
    }

    /// KRI-460: copying a transparent PNG (Strava's share image) and tapping Paste puts it on the selected slide as a sticker:
    /// it is NOT a new slide, it moves with a finger, it survives a save and a slide switch, it is removable (and undoable),
    /// and the export (which renders on the phone from the original bytes) still saves every slide.
    /// A transparent PNG: an orange disc with an opaque white bar, and fully clear corners (the photo must show there).
    private func putTransparentPictureOnThePasteboard() throws {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = false
        let png = try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 480, height: 300), format: format).image { _ in
            UIColor(red: 0.99, green: 0.32, blue: 0.0, alpha: 1).setFill()
            UIBezierPath(ovalIn: CGRect(x: 90, y: 0, width: 300, height: 300)).fill()
            UIColor.white.setFill(); UIRectFill(CGRect(x: 160, y: 130, width: 160, height: 40))
        }.pngData())
        UIPasteboard.general.setData(png, forPasteboardType: UTType.png.identifier)
    }

    /// KRI-668 / KRI-644: on a VIDEO slide a pasted picture behaves exactly as on a photo (its centre is the picture's,
    /// with no dead zone), and the video's pause control stays tappable even under a picture, because it is drawn above them.
    func testStickerOnAVideoSlideLeavesThePauseControlTappable() throws {
        try putTransparentPictureOnThePasteboard()
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1", "KRIA_SLIDE_POST_STICKERS": "1"])
        XCTAssertTrue(app.buttons["slidepost-tile-2"].waitForExistence(timeout: 8)); app.buttons["slidepost-tile-2"].tap()
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        let toggle = app.buttons["slidepost-video-toggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 8), "slide 2 is the fixture video")
        let paste = app.descendants(matching: .any)["slidepost-paste"].firstMatch
        XCTAssertTrue(paste.waitForExistence(timeout: 8))
        // The simulator shares the Mac's clipboard; put the picture back right before pasting so a copy made meanwhile can't win.
        try putTransparentPictureOnThePasteboard()
        paste.tap()
        let sticker = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-sticker-'")).firstMatch
        XCTAssertTrue(sticker.waitForExistence(timeout: 20), "the pasted picture appears on the video slide")
        XCTAssertTrue(app.buttons["slidepost-sticker-delete"].exists, "a new sticker is selected")

        // The centre is the picture's, as on a photo: touching the selected picture there lets go of it.
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        XCTAssertTrue(eventually { !app.buttons["slidepost-sticker-delete"].exists }, "a centre touch is a touch on the picture")

        // Drag the picture from its centre over the pause control, then use the control through it.
        let grab = sticker.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        grab.press(forDuration: 0.1, thenDragTo: toggle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)))
        XCTAssertTrue(eventually { sticker.frame.contains(CGPoint(x: toggle.frame.midX, y: toggle.frame.midY)) }, "the picture now covers the control")
        let covered = sticker.frame
        waitFor(toggle, value: "Playing", "the video plays under the picture")
        toggle.tap()
        waitFor(toggle, value: "Paused", "the control still pauses it")
        XCTAssertEqual(sticker.frame.midX, covered.midX, accuracy: 2, "the tap went to the control, not the picture")
        XCTAssertEqual(sticker.frame.midY, covered.midY, accuracy: 2)
        attach(app, "Pause control above a picture on a video slide")
    }

    func testPastedPictureBecomesAStickerOnTheSlideNotANewSlide() throws {
        try putTransparentPictureOnThePasteboard()

        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1", "KRIA_SLIDE_POST_STICKERS": "1"])
        let sticker = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-sticker-'")).firstMatch
        XCTAssertFalse(sticker.exists, "a fresh slide has no picture")
        XCTAssertTrue(app.buttons["slidepost-tile-3"].exists)
        let paste = app.descendants(matching: .any)["slidepost-paste"].firstMatch
        XCTAssertTrue(paste.waitForExistence(timeout: 8), "Paste is offered when the clipboard holds a picture")
        try putTransparentPictureOnThePasteboard()
        paste.tap()

        XCTAssertTrue(sticker.waitForExistence(timeout: 20), "the pasted picture appears on the canvas")
        // The picture reached the pool as a ready asset; give the workspace time to (wrongly) turn it into a slide.
        Thread.sleep(forTimeInterval: 4)
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists, "a pasted picture is a sticker, never a new slide")
        XCTAssertTrue(app.staticTexts["slidepost-subtitle"].label == "Unsaved changes")
        XCTAssertTrue(app.buttons["slidepost-sticker-delete"].exists, "the new sticker is selected")
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-paste-notice"].exists, "no refusal notice")
        attach(app, "Pasted picture on slide 1 (selected)")

        // A finger drag moves it.
        let before = sticker.frame
        sticker.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(
            forDuration: 0.1, thenDragTo: sticker.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).withOffset(CGVector(dx: -70, dy: -110)))
        XCTAssertTrue(eventually { sticker.frame.minY < before.minY - 60 }, "dragging moved it up")
        XCTAssertLessThan(sticker.frame.minX, before.minX - 40, "dragging moved it left")
        XCTAssertEqual(sticker.frame.width, before.width, accuracy: 3, "moving never resizes")
        attach(app, "Picture dragged up and left")
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists)

        // The corner handle's drag resizes it.
        let widthBefore = sticker.frame.width
        let handle = app.descendants(matching: .any)["slidepost-sticker-handle"].firstMatch
        XCTAssertTrue(handle.exists)
        handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(
            forDuration: 0.1, thenDragTo: handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).withOffset(CGVector(dx: 60, dy: 60)))
        XCTAssertTrue(eventually { sticker.frame.width > widthBefore + 10 }, "the handle's drag made it bigger")
        attach(app, "Picture enlarged with the corner handle")

        // It stays on its own slide, through a save.
        saveDraft(app)
        XCTAssertTrue(sticker.exists, "the save keeps it")
        app.buttons["slidepost-tile-2"].tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: sticker); waitForExpectations(timeout: 5)
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertTrue(sticker.waitForExistence(timeout: 5), "the picture is back on slide 1")
        attach(app, "Picture still on slide 1 after a save and a slide switch")

        // Export renders on the phone from the original PNG; every slide is saved and nothing fails.
        app.buttons["slidepost-export"].tap()
        app.buttons["slidepost-save-photos"].tap()
        let banner = app.descendants(matching: .any)["slidepost-export-state"]
        XCTAssertTrue(banner.waitForExistence(timeout: 10))
        expectation(for: NSPredicate(format: "label CONTAINS %@", "Saved 3 slides"), evaluatedWith: banner); waitForExpectations(timeout: 30)
        attach(app, "Exported with the picture")

        // Remove (X) takes it off, and Undo brings it back.
        if !app.buttons["slidepost-sticker-delete"].exists { sticker.tap() }
        XCTAssertTrue(app.buttons["slidepost-sticker-delete"].waitForExistence(timeout: 3))
        app.buttons["slidepost-sticker-delete"].tap()
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: sticker); waitForExpectations(timeout: 5)
        app.buttons["slidepost-undo"].tap()
        XCTAssertTrue(sticker.waitForExistence(timeout: 5), "undo brings the picture back")
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists)
    }

    /// Without `slide_post_stickers` a picture paste says so and uploads nothing.
    func testPastingAPictureWithoutTheCapabilityExplainsItself() throws {
        let format = UIGraphicsImageRendererFormat(); format.scale = 1
        UIPasteboard.general.setData(try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 40, height: 40), format: format).image { _ in
            UIColor.red.setFill(); UIRectFill(CGRect(x: 0, y: 0, width: 40, height: 40))
        }.pngData()), forPasteboardType: UTType.png.identifier)
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        let paste = app.descendants(matching: .any)["slidepost-paste"].firstMatch
        XCTAssertTrue(paste.waitForExistence(timeout: 8)); paste.tap()
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-paste-notice"].waitForExistence(timeout: 5), "the refusal is visible, not silent")
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-sticker-'")).firstMatch.exists)
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists)
    }

    /// The page has ONE AI entry (the editor's sparkles button). It opens a sheet that drives the chat
    /// edit: the server's edit is STAGED on the page behind (unsaved, undoable) and Save quotes the
    /// server's base version (the stub 409s otherwise).
    func testAIButtonOpensSheetAndChatEditStagesOnThePage() {
        let app = openRichWorkspace(chatEdit: true)
        XCTAssertTrue(app.buttons["slidepost-tile-1"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.textFields["slidepost-composer"].exists, "no chat composer on the page")
        XCTAssertFalse(app.textFields["Message Kria"].exists, "the conversation lives only in the AI sheet")
        let ai = app.buttons["slidepost-openkria"]
        XCTAssertTrue(ai.isHittable)
        attach(app, "AI button on the page")
        ai.tap()
        let input = app.textFields["Message Kria"]
        XCTAssertTrue(input.waitForExistence(timeout: 5)); input.tap()
        input.typeText("Put them in chronological order and add each photo's location")
        app.buttons["chat-send-message"].tap()
        let reply = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: Done.")).firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 8), "Kria's reply is shown in the sheet")
        XCTAssertTrue(reply.label.hasPrefix("Kria: Done. Your photos now follow the order"), "the reply is server text, verbatim")
        XCTAssertTrue(app.staticTexts["Reordered 3"].exists)
        XCTAssertTrue(app.staticTexts["1 photo has no location"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-ai-note"].firstMatch.exists, "a missing location reads as a note")
        attach(app, "AI sheet: staged edit")
        // The sheet has no Undo/Save of its own: the page behind carries the staged edit.
        XCTAssertFalse(app.buttons["slidepost-ai-undo"].exists)
        XCTAssertFalse(app.buttons["slidepost-ai-save"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-ai-unsaved"].exists)
        app.swipeDown(velocity: .fast) // same dismissal as the video editor's Kria sheet
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "the AI edit is staged on the page")
        XCTAssertTrue(app.buttons["slidepost-save"].isEnabled, "the header Save is armed by the staged edit")
        XCTAssertTrue(app.buttons["slidepost-undo"].isEnabled, "the transport Undo can revert it")
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertEqual(canvasText(app).label, "Athens", "the staged edit labelled the first slide")
        attach(app, "AI edit staged on the page")
        // Undo on the page returns to the saved draft; the transcript stays in the sheet.
        app.buttons["slidepost-undo"].tap()
        let clean = NSPredicate(format: "label != %@", "Unsaved changes")
        expectation(for: clean, evaluatedWith: app.staticTexts["slidepost-subtitle"]); waitForExpectations(timeout: 5)
        app.buttons["slidepost-openkria"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: Done.")).firstMatch.waitForExistence(timeout: 5))
        let again = app.textFields["Message Kria"]
        XCTAssertTrue(again.waitForExistence(timeout: 5)); again.tap(); again.typeText("Again please")
        app.buttons["chat-send-message"].tap()
        let restaged = app.staticTexts["Reordered 3"]
        XCTAssertTrue(restaged.waitForExistence(timeout: 8))
        app.swipeDown(velocity: .fast)
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))
        // Save from the header quotes the server's version (a wrong one would 409 and show an error).
        let save = app.buttons["slidepost-save"]
        expectation(for: NSPredicate(format: "isEnabled == true"), evaluatedWith: save); waitForExpectations(timeout: 10)
        save.tap()
        expectation(for: clean, evaluatedWith: app.staticTexts["slidepost-subtitle"]); waitForExpectations(timeout: 8)
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-error"].firstMatch.exists)
    }

    private func coverIndex(_ app: XCUIApplication, count: Int) -> Int? {
        (1...count).first { app.buttons["slidepost-tile-\($0)"].value as? String == "Cover" }
    }

    /// Long-press a block and drag: no Arrange tool, no mode. The cover follows its slide.
    func testLongPressDragReordersAndKeepsTheCover() {
        let app = openRichWorkspace()
        let first = app.buttons["slidepost-tile-1"], second = app.buttons["slidepost-tile-2"]
        XCTAssertEqual(first.value as? String, "Cover")
        first.press(forDuration: 0.9, thenDragTo: second, withVelocity: .slow, thenHoldForDuration: 0.2)
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertEqual(app.buttons["slidepost-tile-2"].value as? String, "Cover", "the cover moved with its slide")
        XCTAssertTrue(app.buttons["slidepost-undo"].isEnabled)
        app.buttons["slidepost-undo"].tap()
        XCTAssertEqual(app.buttons["slidepost-tile-1"].value as? String, "Cover", "one undo step puts it back")
        attach(app, "Reorder: after drag and undo")
    }

    /// Dragging a block to the strip's edge scrolls it, so a slide can travel past what is on screen.
    func testDraggingToTheEdgeAutoScrollsTheStrip() {
        let app = openRichWorkspace(many: true)
        XCTAssertTrue(app.buttons["slidepost-tile-12"].waitForExistence(timeout: 8))
        let first = app.buttons["slidepost-tile-1"]
        let window = app.windows.firstMatch.frame
        let edge = app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: window.maxX - 8, dy: first.frame.midY))
        first.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.9, thenDragTo: edge, withVelocity: .slow, thenHoldForDuration: 2.5)
        let cover = coverIndex(app, count: 12)
        XCTAssertNotNil(cover)
        XCTAssertGreaterThan(cover ?? 0, 7, "auto-scroll carried the slide past the slides that were visible")
        attach(app, "Reorder: after edge auto-scroll")
    }

    // MARK: Strip scrolling with real-finger gestures (the strip must scroll on a plain swipe)

    private func windowPoint(_ app: XCUIApplication, x: CGFloat, y: CGFloat) -> XCUICoordinate {
        app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: y))
    }
    /// A finger drag from `fromX` to `toX` at the strip's height with a SHORT press (a real swipe never holds 0.3s).
    private func fingerDrag(_ app: XCUIApplication, fromX: CGFloat, toX: CGFloat, y: CGFloat, hold: TimeInterval = 0.05, velocity: XCUIGestureVelocity = .default) {
        windowPoint(app, x: fromX, y: y).press(forDuration: hold, thenDragTo: windowPoint(app, x: toX, y: y), withVelocity: velocity, thenHoldForDuration: 0)
    }
    private func assertStripReachesTheEnd(_ app: XCUIApplication, _ gesture: () -> Void, file: StaticString = #filePath, line: UInt = #line) {
        let first = app.buttons["slidepost-tile-1"], add = app.buttons["slidepost-add-tile"]
        XCTAssertTrue(first.waitForExistence(timeout: 8), file: file, line: line)
        let window = app.windows.firstMatch.frame
        let startX = first.frame.midX
        XCTAssertFalse(add.frame.maxX <= window.maxX, "12 slides: the + Add block starts off screen", file: file, line: line)
        for _ in 0..<8 where !(add.frame.maxX <= window.maxX && add.frame.minX >= 0) { gesture() }
        XCTAssertLessThan(first.frame.midX, startX - 20, "the strip moved: the first tile scrolled left", file: file, line: line)
        XCTAssertLessThan(first.frame.midX, window.minX, "the first tile scrolled off the left edge", file: file, line: line)
        XCTAssertTrue(add.exists && add.frame.midX < window.maxX, "+ Add is reachable at the far right", file: file, line: line)
        XCTAssertTrue(app.buttons["slidepost-tile-12"].frame.midX < window.maxX, "the last slide is reachable", file: file, line: line)
    }

    func testStripScrollsWithAShortPressFingerDrag() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-3"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY, w = app.windows.firstMatch.frame.width
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: w - 40, toX: 40, y: y) }
        attach(app, "Strip: scrolled to the end")
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "scrolling never reorders")
        XCTAssertFalse(app.buttons["drawer-new-chat"].isHittable, "scrolling the strip does not open the drawer")
        for _ in 0..<8 { fingerDrag(app, fromX: 40, toX: w - 40, y: y) }
        XCTAssertTrue(app.buttons["slidepost-tile-1"].isHittable, "scrolled back to the start")
        attach(app, "Strip: scrolled back")
        // A rightward swipe along the strip from the left edge, at the start position, must not open the drawer.
        for _ in 0..<3 { fingerDrag(app, fromX: 30, toX: w - 30, y: y) }
        XCTAssertFalse(app.buttons["drawer-new-chat"].isHittable, "swiping the strip must not open the drawer")
        // A swipe that starts above the strip still opens the drawer.
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let py = preview.frame.minY + 30
        windowPoint(app, x: 4, y: py).press(forDuration: 0.05, thenDragTo: windowPoint(app, x: w * 0.85, y: py), withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 3), "a swipe that starts above the strip still opens the drawer")
        attach(app, "Drawer opens from outside the strip")
    }

    func testStripFlickScrollsFromATileTheGapAndTheAddBlock() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-2"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY
        // A selected slide does not stop the strip scrolling, and 12 slides visibly overflow the right edge.
        tile.tap(); XCTAssertTrue(tile.isSelected)
        let overflowWindow = app.windows.firstMatch.frame
        XCTAssertFalse((1...12).filter { app.buttons["slidepost-tile-\($0)"].frame.maxX > overflowWindow.maxX }.isEmpty, "a tile is cut by the right edge: the strip visibly overflows")
        let tiles = [app.buttons["slidepost-tile-1"], app.buttons["slidepost-tile-2"]]
        let gapX = (tiles[0].frame.maxX + tiles[1].frame.minX) / 2
        let w = app.windows.firstMatch.frame.width
        for (name, x) in [("tile", tile.frame.midX), ("gap", gapX)] {
            let before = app.buttons["slidepost-tile-1"].frame.midX
            windowPoint(app, x: x, y: y).press(forDuration: 0.03, thenDragTo: windowPoint(app, x: max(10, x - 220), y: y), withVelocity: .fast, thenHoldForDuration: 0)
            XCTAssertLessThan(app.buttons["slidepost-tile-1"].frame.midX, before - 20, "a flick starting on the \(name) scrolls the strip")
            for _ in 0..<4 { fingerDrag(app, fromX: 30, toX: w - 30, y: y, velocity: .fast) }
        }
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: w - 40, toX: 40, y: y, velocity: .fast) }
        let add = app.buttons["slidepost-add-tile"]
        let beforeAdd = app.buttons["slidepost-tile-12"].frame.minX
        windowPoint(app, x: add.frame.midX, y: y).press(forDuration: 0.03, thenDragTo: windowPoint(app, x: 20, y: y), withVelocity: .default, thenHoldForDuration: 0)
        XCTAssertLessThanOrEqual(app.buttons["slidepost-tile-12"].frame.minX, beforeAdd, "a drag that starts on + Add never moves the strip the wrong way")
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "no flick reorders")
        // The far end of the strip is selectable, and a long press + drag still reorders after scrolling.
        app.buttons["slidepost-tile-12"].tap()
        XCTAssertTrue(app.buttons["slidepost-tile-12"].isSelected)
        let last = app.buttons["slidepost-tile-12"], prior = app.buttons["slidepost-tile-11"]
        XCTAssertEqual(coverIndex(app, count: 12), 1)
        last.press(forDuration: 0.9, thenDragTo: prior, withVelocity: .slow, thenHoldForDuration: 0.2)
        attach(app, "Strip: after reorder at the end")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "long press then drag reorders")
    }

    /// Media that finishes importing joins the post by itself: a placeholder while it is processing,
    /// then a slide, with no second tap.
    func testNewlyReadyAssetBecomesASlideWithoutTapping() {
        let app = openRichWorkspace(lateAsset: true)
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists)
        let pending = app.descendants(matching: .any)["slidepost-pending-1"]
        XCTAssertTrue(pending.waitForExistence(timeout: 10), "an uploading/processing block shows in the strip")
        XCTAssertEqual(pending.frame.height, 72, accuracy: 1, "same footprint as a tile")
        XCTAssertEqual(pending.frame.minY, app.buttons["slidepost-tile-2"].frame.minY, accuracy: 1, "same baseline")
        attach(app, "Strip: processing placeholder")
        let added = app.buttons["slidepost-tile-4"]
        XCTAssertTrue(added.waitForExistence(timeout: 20), "the ready photo was added as slide 4")
        XCTAssertFalse(pending.exists, "the placeholder is replaced by the slide")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertTrue(app.buttons["slidepost-undo"].isEnabled)
        app.buttons["slidepost-undo"].tap()
        XCTAssertFalse(app.buttons["slidepost-tile-4"].exists, "one undo removes it")
        attach(app, "Strip: auto-added then undone")
    }

    // MARK: Tap a text on the preview to edit it

    private func addText(_ app: XCUIApplication, _ words: String) {
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap(); field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        field.typeText(words)
        app.buttons["slidepost-done"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))
    }

    // MARK: A created (rendered, exportable) post keeps editable text

    /// Real-device bug: opening an already created post showed the burned render with no live text, so tapping
    /// the text did nothing. The editor must always show source media + live editable text.
    private func openReadyPostWithText(_ app: XCUIApplication, _ words: String) {
        addText(app, words)
        let save = app.buttons["slidepost-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5)); if save.isEnabled { save.tap() }
        // There is no Create step: exporting renders the post first (KRI-305). Needs KRIA_SLIDE_POST_FIXTURE_PHOTOS=1.
        let export = app.buttons["slidepost-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 10)); export.tap()
        app.buttons["slidepost-save-photos"].tap()
        let banner = app.descendants(matching: .any)["slidepost-export-state"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "Saved"), evaluatedWith: banner); waitForExpectations(timeout: 25)
        // The "Saved" banner auto-dismisses; wait it out so it can't shift or cover the preview under the next tap.
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: banner); waitForExpectations(timeout: 10)
    }

    func testTappingTextOnACreatedPostOpensEditTextWithKeyboard() {
        let app = openRichWorkspace(extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        openReadyPostWithText(app, "Athens")
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5), "a created post still shows its live, editable text")
        XCTAssertEqual(app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-text-'")).count, 1, "one live text, not burned + live")
        attach(app, "Ready post: live text on the preview")
        text.tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5), "tap on a created post's text opens Edit text")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["native-editor-text-preset"].exists, "Edit tab, not Style")
        field.typeText("!")
        expectation(for: NSPredicate(format: "label == %@", "Athens!"), evaluatedWith: canvasText(app)); waitForExpectations(timeout: 5)
        attach(app, "Ready post: tap text opens Edit text with keyboard")
        // Hold + drag on a created post moves the text directly (no panel) and makes it a draft again.
        app.buttons["slidepost-done"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let before = text.value as? String ?? ""
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.9, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.25)))
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-text-handle"].firstMatch.waitForExistence(timeout: 3))
        XCTAssertNotEqual(before, text.value as? String ?? "", "hold + drag moved the text of a created post")
        panelAndKeyboardAbsent(app, "after hold-drag on a created post")
        XCTAssertTrue(app.buttons["slidepost-save"].waitForExistence(timeout: 5), "the edit made the post a draft again (Save re-renders)")
    }

    // MARK: Hold to transform (tap edits, hold + drag moves/resizes directly, no panel)

    private func panelAndKeyboardAbsent(_ app: XCUIApplication, _ note: String) {
        XCTAssertFalse(app.textViews["slidepost-text-field"].firstMatch.exists, "no Text panel: \(note)")
        XCTAssertFalse(app.keyboards.firstMatch.exists, "no keyboard: \(note)")
        XCTAssertFalse(app.buttons["slidepost-done"].exists, "no panel Done: \(note)")
    }

    func testHoldThenDragMovesTextWithoutOpeningThePanel() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        let before = text.value as? String ?? ""
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.9, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.25)))
        let handle = app.descendants(matching: .any)["slidepost-text-handle"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 3), "text stays selected with its handle")
        XCTAssertNotEqual(before, text.value as? String ?? "", "hold + drag moved the text")
        panelAndKeyboardAbsent(app, "after hold-drag")
        attach(app, "Hold-drag: moved, handles, no panel")
        // Further body drags keep moving it directly.
        let mid = text.value as? String ?? ""
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.1, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.6, dy: 0.6)))
        XCTAssertNotEqual(mid, text.value as? String ?? "")
        panelAndKeyboardAbsent(app, "after second drag")
        // Tapping empty canvas deselects.
        Thread.sleep(forTimeInterval: 0.9)
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.06)).tap()
        XCTAssertFalse(handle.waitForExistence(timeout: 2), "tap on empty canvas deselects")
        panelAndKeyboardAbsent(app, "after deselect")
        // Each drag is exactly one undo step: two undos return the text to where it started, and never remove it.
        func unselected(_ value: String) -> String { value.replacingOccurrences(of: ", selected", with: "") }
        app.buttons["slidepost-undo"].tap()
        XCTAssertEqual(unselected(mid), unselected(text.value as? String ?? ""), "one undo reverts only the last drag")
        app.buttons["slidepost-undo"].tap()
        XCTAssertEqual(unselected(before), unselected(text.value as? String ?? ""), "one more undo returns the text to where it started")
        XCTAssertEqual(text.label, "Athens", "undo did not remove the text")

        // Hold-select alone shows the frame and handle with no panel, and the corner handle resizes in place.
        // Runs after the undo checks above, which count drags: a resize here would add an undo step to them.
        text.press(forDuration: 0.9)
        XCTAssertTrue(handle.waitForExistence(timeout: 3), "hold selects and shows the handle")
        panelAndKeyboardAbsent(app, "after hold")
        attach(app, "Hold-select: frame and handle, no panel")
        let sizeBefore = number(text.value as? String ?? "", after: "size")
        handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.9, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.62)))
        XCTAssertNotEqual(sizeBefore, number(text.value as? String ?? "", after: "size"), "corner drag changes the size")
        panelAndKeyboardAbsent(app, "after corner drag")
        attach(app, "Hold-select: after corner resize")
    }

    /// KRI-564: a line typed on one line stays on one line on the slide, and resizing only scales it. The edit
    /// box scrolls sideways so the caret stays visible past the box edge and comes back when it moves back.
    func testTypedLineStaysOnOneLineAndResizeOnlyScalesIt() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        let selection = app.descendants(matching: .any)["slidepost-text-selection"].firstMatch
        text.press(forDuration: 0.9)
        XCTAssertTrue(selection.waitForExistence(timeout: 3))
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-text-handle"].firstMatch.waitForExistence(timeout: 3))
        panelAndKeyboardAbsent(app, "after hold")
        let oneLineHeight = selection.frame.height
        XCTAssertGreaterThan(oneLineHeight, 0)

        // Re-open the text and type a line far wider than the canvas (it used to wrap onto four or more lines).
        Thread.sleep(forTimeInterval: 0.9)
        text.tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5), "tap after hold-select opens Edit text")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "keyboard is up")
        field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        let longLine = "One long line the creator typed without a single line break"
        field.typeText(longLine)
        attach(app, "Edit box: caret at the end of a long line")
        // Deleting the tail pulls the box back to the start of the line; retyping it scrolls forward again.
        let tail = " the creator typed without a single line break"
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: tail.count))
        Thread.sleep(forTimeInterval: 0.5)
        attach(app, "Edit box: scrolled back after deleting the tail")
        field.typeText(tail)
        app.buttons["slidepost-done"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))

        XCTAssertEqual(canvasText(app).label, longLine)
        canvasText(app).press(forDuration: 0.9)
        XCTAssertTrue(selection.waitForExistence(timeout: 3))
        let longHeight = selection.frame.height
        XCTAssertLessThan(longHeight, oneLineHeight * 1.5, "the typed line stays on one line, not wrapped")
        attach(app, "Long typed line: one line on the canvas")

        // Shrinking changes the size, never the number of lines.
        Thread.sleep(forTimeInterval: 0.9)
        canvasText(app).tap()
        app.buttons["Style"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-text-handle"].firstMatch.waitForExistence(timeout: 3))
        let sizeBefore = number(canvasText(app).value as? String ?? "", after: "size")
        preview.pinch(withScale: 0.5, velocity: -1)
        let shrunk = NSPredicate { _, _ in self.number(self.canvasText(app).value as? String ?? "", after: "size") < sizeBefore }
        expectation(for: shrunk, evaluatedWith: nil); waitForExpectations(timeout: 5)
        XCTAssertLessThan(selection.frame.height, oneLineHeight * 1.5, "still one line after resizing")
        attach(app, "Long typed line: one line after shrinking")
    }

    func testTappingAnotherTextWhilePanelIsOpenSwitchesToItsEditField() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        app.buttons["slidepost-tool-text"].tap()
        XCTAssertTrue(app.buttons["slidepost-add-text"].waitForExistence(timeout: 5))
        app.buttons["slidepost-add-text"].tap()
        app.buttons["Style"].tap()
        let texts = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-text-'"))
        XCTAssertTrue(texts.element(boundBy: 1).waitForExistence(timeout: 5))
        let athens = texts.matching(NSPredicate(format: "label == 'Athens'")).firstMatch
        XCTAssertTrue(athens.exists)
        // Both texts start at the centre; move the new one up so each can be tapped on its own.
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        texts.matching(NSPredicate(format: "label == 'Your text'")).firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            .press(forDuration: 0.1, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)))
        Thread.sleep(forTimeInterval: 0.8)   // a tap right after a drag is (deliberately) ignored
        athens.tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertEqual(field.value as? String, "Athens", "the field now edits the tapped text")
        attach(app, "Tap another text while the panel is open")
    }

    /// KRI-305: on a small phone the keyboard used to hide the Add text / Apply row (and the stage kept
    /// growing past the KRI-185 120pt rule). Run on an iPhone SE class simulator for the real constraint.
    func testAddTextStaysHittableWithKeyboardUpAndStagePinnedTo120() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        let keyboard = app.keyboards.firstMatch
        XCTAssertTrue(keyboard.waitForExistence(timeout: 5))
        sleep(1) // layout animation
        let add = app.buttons["slidepost-add-text"]
        XCTAssertTrue(add.waitForExistence(timeout: 3), "Add text is on screen with the keyboard up")
        XCTAssertTrue(add.isHittable, "Add text is hittable with the keyboard up")
        XCTAssertLessThanOrEqual(add.frame.maxY, keyboard.frame.minY + 1, "Add text sits above the keyboard")
        XCTAssertTrue(field.isHittable, "the edit field stays usable too")
        XCTAssertLessThanOrEqual(field.frame.maxY, keyboard.frame.minY + 1, "the whole edit field sits above the keyboard")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.exists)
        XCTAssertEqual(preview.frame.height, 120, accuracy: 2, "typing pins the stage to 120pt (shrink, never cover)")
        XCTAssertLessThan(preview.frame.maxY, field.frame.minY + 1, "the stage shrinks above the field")
        attach(app, "Small phone: keyboard up")
        let canvasTexts = app.descendants(matching: .any).matching(NSPredicate(format: "identifier BEGINSWITH 'slidepost-canvas-text-'"))
        let before = canvasTexts.count
        add.tap()
        expectation(for: NSPredicate(format: "count > %d", before), evaluatedWith: canvasTexts); waitForExpectations(timeout: 5)
    }

    /// KRI-305: one pass over the slide screens at an accessibility Dynamic Type size (only accessibility5 is
    /// honoured by the app-level UI-test override, so that is the size used).
    func testSlideScreensAtAccessibilityTypeSizeStayReachable() {
        let app = openRichWorkspace(dynamicType: "accessibility5")
        XCTAssertTrue(app.buttons["slidepost-tile-1"].isHittable, "strip tile")
        XCTAssertTrue(app.buttons["slidepost-tool-text"].isHittable, "tool dock")
        attach(app, "A11y type: browse")
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["slidepost-done"].waitForExistence(timeout: 3) || app.buttons["slidepost-add-text"].exists)
        attach(app, "A11y type: text panel, no keyboard")
        field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        sleep(1)
        let add = app.buttons["slidepost-add-text"]
        XCTAssertTrue(add.exists, "Add text exists at large type")
        XCTAssertTrue(add.isHittable, "Add text is hittable at large type with the keyboard up")
        XCTAssertLessThanOrEqual(add.frame.maxY, app.keyboards.firstMatch.frame.minY + 1)
        attach(app, "A11y type: text panel, keyboard up")
    }

    // MARK: One layout in every entry path and capability state

    /// The slide editor, whatever loaded or didn't: tool dock, "+ Add" LAST in the strip with the tiles'
    /// footprint, floating AI button, and none of the old layout's chat box / buttons / video editor.
    private func assertRichLayout(_ app: XCUIApplication, _ context: String, lastTile: Int = 3, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(app.buttons["slidepost-tool-text"].firstMatch.waitForExistence(timeout: 20), "\(context): tool dock", file: file, line: line)
        // From the gallery the post is also mounted (hidden) under the cover: use the visible one.
        func visible(_ id: String) -> XCUIElement {
            app.buttons.matching(identifier: id).allElementsBoundByIndex.first { $0.isHittable } ?? app.buttons.matching(identifier: id).firstMatch
        }
        XCTAssertTrue(app.buttons["slidepost-add-tile"].firstMatch.waitForExistence(timeout: 10), "\(context): + Add block", file: file, line: line)
        XCTAssertTrue(app.buttons["slidepost-tile-\(lastTile)"].firstMatch.waitForExistence(timeout: 10), "\(context): slide \(lastTile)", file: file, line: line)
        // Some pair (the visible post; the gallery also keeps the post mounted beneath) must have + Add one
        // pitch (66pt) after the last tile, on its baseline. A selected tile's 2pt ring adds 4pt to its frame.
        let adds = app.buttons.matching(identifier: "slidepost-add-tile").allElementsBoundByIndex
        let tiles = app.buttons.matching(identifier: "slidepost-tile-\(lastTile)").allElementsBoundByIndex
        let paired = adds.contains { add in
            tiles.contains { tile in
                abs(add.frame.midX - tile.frame.midX - 66) <= 1 && abs(add.frame.height - tile.frame.height) <= 5 && abs(add.frame.minY - tile.frame.minY) <= 3
            }
        }
        XCTAssertTrue(paired, "\(context): + Add is the LAST block with the tiles' footprint and baseline", file: file, line: line)
        XCTAssertTrue(visible("slidepost-openkria").isHittable, "\(context): floating AI button", file: file, line: line)
        XCTAssertFalse(app.textFields["Message Kria"].exists, "\(context): no chat box on the page", file: file, line: line)
        XCTAssertFalse(app.buttons["Add photos & videos"].exists, "\(context): no legacy add button", file: file, line: line)
        XCTAssertFalse(app.buttons["Ask Kria"].exists, "\(context): no legacy Direction composer", file: file, line: line)
        XCTAssertFalse(app.staticTexts["Start your post"].exists, file: file, line: line)
        XCTAssertFalse(app.descendants(matching: .any)["native-editor-preview"].exists, "\(context): never the video editor", file: file, line: line)
        XCTAssertFalse(app.buttons["Editor"].exists, file: file, line: line)
        XCTAssertFalse(app.buttons["open-current-cut"].exists, file: file, line: line)
    }

    private func openWeekendTripFromDrawer(_ app: XCUIApplication) {
        let menu = app.buttons["Open projects"]
        if menu.waitForExistence(timeout: 6) {
            menu.tap()
            let row = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Weekend trip'")).firstMatch
            if row.waitForExistence(timeout: 6) { row.tap() }
        }
    }

    func testRichLayoutFromTheDrawerWhenCapabilitiesFailToLoad() {
        let app = launchRich(capabilities: "fail", readyThread: true)
        openWeekendTripFromDrawer(app)
        assertRichLayout(app, "drawer, capabilities fail")
        attach(app, "Entry: drawer (caps fail)")
        // Optional AI staging is off without capabilities: the sheet is the propose flow, the layout is unchanged.
        app.buttons["slidepost-openkria"].tap()
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.textFields["Message Kria"].placeholderValue, "Describe the post…", "no capabilities => propose flow, not chat-edit staging")
        attach(app, "AI sheet (caps fail)")
    }

    func testRichLayoutFromTheGalleryForEveryCapabilityState() {
        for caps in [nil, "slow", "fail"] as [String?] {
            let app = launchRich(capabilities: caps, readyThread: true)
            // The slide editor has no menu button: Back is the way to the drawer.
            XCTAssertTrue(app.buttons["Back to creation"].waitForExistence(timeout: 15)); app.buttons["Back to creation"].tap()
            let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'")).firstMatch
            XCTAssertTrue(gallery.waitForExistence(timeout: 5)); gallery.tap()
            let card = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Open post'")).firstMatch
            XCTAssertTrue(card.waitForExistence(timeout: 10), "gallery lists the slide post"); card.tap()
            assertRichLayout(app, "gallery, caps \(caps ?? "true")")
            attach(app, "Entry: gallery (caps \(caps ?? "true"))")
            app.terminate()
        }
    }

    /// KRI-305: a post opened from the gallery shows exactly one back control (the workspace's own circle,
    /// never the system bar's second one) and a post that already has a saved server draft is not
    /// "Unsaved changes" just because it was opened.
    func testGalleryOpenedPostHasOneBackButtonAndNoUnsavedChanges() {
        let app = launchRich(readyThread: true, extraEnv: ["KRIA_SLIDE_POST_READY_DRAFT": "1"])
        XCTAssertTrue(app.buttons["Back to creation"].waitForExistence(timeout: 15)); app.buttons["Back to creation"].tap()
        let gallery = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Gallery'")).firstMatch
        XCTAssertTrue(gallery.waitForExistence(timeout: 5)); gallery.tap()
        let card = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Open post'")).firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 10), "gallery lists the slide post"); card.tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].firstMatch.waitForExistence(timeout: 20))
        let subtitles = app.staticTexts.matching(identifier: "slidepost-subtitle")
        let visible = NSPredicate(format: "isHittable == true")
        expectation(for: visible, evaluatedWith: subtitles.firstMatch); waitForExpectations(timeout: 10)
        // Let the first refresh land before judging the status line.
        sleep(2)
        for subtitle in subtitles.allElementsBoundByIndex {
            XCTAssertNotEqual(subtitle.label, "Unsaved changes", "a saved post must not read as unsaved on open")
        }
        let backs = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Back'")).allElementsBoundByIndex.filter { $0.isHittable }
        XCTAssertEqual(backs.count, 1, "exactly one visible back button: \(backs.map(\.label))")
        XCTAssertTrue(app.navigationBars.buttons.allElementsBoundByIndex.filter { $0.isHittable }.isEmpty, "no system navigation-bar buttons")
        attach(app, "Gallery-opened post")
    }

    /// Back -> drawer -> tap the project again (and via a second project switch): always the slide editor.
    func testReEnteringAfterBackAlwaysLandsInTheSlideEditor() {
        let app = launchRich(readyThread: true)
        openWeekendTripFromDrawer(app)
        assertRichLayout(app, "first entry")
        app.buttons["Back to creation"].tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 5), "Back opens the drawer")
        XCTAssertFalse(app.staticTexts["What are we making?"].exists, "not the format chooser")
        attach(app, "Back: drawer")
        // Re-enter through another project and back, like a user hopping chats.
        app.buttons["drawer-new-chat"].tap()
        XCTAssertTrue(app.scrollViews["format-carousel"].waitForExistence(timeout: 12), "a new chat shows the chooser, as it should")
        openWeekendTripFromDrawer(app)
        assertRichLayout(app, "re-entry after hopping chats")
        attach(app, "Re-entry")
    }

    // MARK: Brand-new post: AI + add-media copy + seamless slide switching

    /// A failed AI request says what happened, keeps the user's message, and offers Try again.
    func testFailedAIRequestShowsAClearMessageAndRetryKeepsTheMessage() {
        let app = openRichWorkspace(chatEdit: true, save: false, extraEnv: ["KRIA_SLIDE_POST_CHAT_EDIT_FAIL_ONCE": "1"])
        app.buttons["slidepost-openkria"].tap()
        let input = app.textFields["Message Kria"]
        XCTAssertTrue(input.waitForExistence(timeout: 5)); input.tap(); input.typeText("Order them by time")
        app.buttons["chat-send-message"].tap()
        let retry = app.buttons["slidepost-ai-retry"]
        XCTAssertTrue(retry.waitForExistence(timeout: 8), "failure offers Try again")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "problem on its side")).firstMatch.exists, "a clear reason, not a bare failure")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Order them by time")).firstMatch.exists, "the user's message is still shown")
        attach(app, "AI failure: clear message and Try again")
        retry.tap()
        let reply = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: Done.")).firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 8), "retrying resends the same message")
    }

    /// KRI-305: after adding text, the next slide used to flash its blurred thumbnail because the NSCache had
    /// dropped the previews under text-mode memory pressure. `EVICT_ON_TEXT` reproduces that eviction
    /// deterministically; the media delay makes any async fallback (network or disk) visible.
    func testSwitchingSlidesAfterAddingTextNeverShowsTheBlur() {
        let app = openRichWorkspace(save: false, extraEnv: [
            "KRIA_SLIDE_POST_REMOTE_MEDIA": "1", "KRIA_SLIDE_POST_MEDIA_DELAY_MS": "900", "KRIA_SLIDE_POST_EVICT_ON_TEXT": "1",
        ])
        let image = app.descendants(matching: .any)["slidepost-preview-image"]
        let blurred = app.descendants(matching: .any)["slidepost-preview-blurred"]
        XCTAssertTrue(image.waitForExistence(timeout: 10))
        expectation(for: NSPredicate(format: "value ENDSWITH '|full'"), evaluatedWith: image); waitForExpectations(timeout: 15)
        sleep(4) // neighbours + disk copies are written in the background
        addText(app, "Athens")
        for tile in ["slidepost-tile-3", "slidepost-tile-1", "slidepost-tile-3", "slidepost-tile-1"] {
            app.buttons[tile].tap()
            let value = image.value as? String ?? "<none>"
            XCTAssertTrue(value.hasSuffix("|full"), "right after \(tile) the full preview is showing, not the blur (\(value))")
            XCTAssertFalse(blurred.exists, "no blurred thumbnail right after \(tile)")
        }
        attach(app, "After add text then switching slides")
    }

    private func scrollTo(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<7 {
            if element.exists && element.isHittable { return }
            app.swipeUp()
        }
    }
}
