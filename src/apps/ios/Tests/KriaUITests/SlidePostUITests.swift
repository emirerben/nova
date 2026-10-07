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
        XCTAssertTrue(save.isEnabled, "a fresh draft is unsaved"); save.tap()
        expectation(for: NSPredicate(format: "isEnabled == false"), evaluatedWith: save); waitForExpectations(timeout: 10)
        // There is no Create step: exporting a saved-but-unrendered post renders it first.
        let export = app.buttons["slidepost-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 5)); XCTAssertTrue(export.isEnabled); export.tap()
        XCTAssertTrue(app.buttons["slidepost-share-files"].waitForExistence(timeout: 3))
        app.buttons["slidepost-save-photos"].tap()
        let banner = app.descendants(matching: .any)["slidepost-export-state"]
        expectation(for: NSPredicate(format: "label CONTAINS %@", "Saved 3 slides"), evaluatedWith: banner); waitForExpectations(timeout: 25)
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

    /// Back from a slide post returns to the chats drawer (where the user came from), NOT the format
    /// chooser, and the project is still the slide editor when re-entered.
    func testBackReturnsToTheDrawerNotTheFormatChooser() {
        let app = openRichWorkspace(dynamicType: "accessibility3")
        XCTAssertTrue(app.buttons["Back to creation"].waitForExistence(timeout: 8))
        app.buttons["Back to creation"].tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 5), "Back opens the chats drawer")
        XCTAssertFalse(app.staticTexts["What are we making?"].exists, "never the format chooser")
        attach(app, "Back lands on the drawer")
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
    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = name; shot.lifetime = .keepAlways; add(shot)
    }

    func testSlideStripSwipeDoesNotOpenDrawer() {
        let app = openRichWorkspace()
        let tile = app.buttons["slidepost-tile-1"]
        XCTAssertTrue(tile.waitForExistence(timeout: 5))
        // The drawer opens with a rightward swipe; over the strip it must belong to the strip.
        tile.swipeRight()
        tile.swipeRight()
        XCTAssertFalse(app.buttons["drawer-new-chat"].isHittable, "swiping the strip must not open the projects drawer")
        XCTAssertTrue(app.buttons["slidepost-tool-text"].isHittable)
        attach(app, "Strip swipe leaves the drawer closed")
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
        field.tap(); field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        field.typeText("Athens")
        attach(app, "Text mode: edit tab")
        app.buttons["Style"].tap()
        let preset = app.buttons["native-editor-text-preset"]
        XCTAssertTrue(preset.waitForExistence(timeout: 5))
        preset.tap(); app.buttons["Highlight"].tap()
        XCTAssertTrue(preset.label.contains("Highlight"))
        let rotation = app.buttons["native-editor-text-rotation-Increment"]
        revealInTextPanel(rotation, app: app)
        rotation.tap()
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
        let target = app.buttons["native-editor-text-rotation-Increment"]
        revealInTextPanel(target, app: app)
        XCTAssertEqual(target.value as? String, "5 degrees")
        app.buttons["Edit text"].tap()
        XCTAssertEqual(app.textViews["slidepost-text-field"].firstMatch.value as? String, "Your text", "words are never copied")
        attach(app, "Style applied to all slides")
    }

    /// The slide Text tab IS the native Text panel: same Edit text / Style tabs and controls, no timing, no Animation tab.
    func testSlideTextTabIsTheNativeTextPanel() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertFalse(app.textFields["native-editor-text-time-start"].exists, "slides have no timeline")
        XCTAssertFalse(app.buttons["Animation"].exists)
        field.tap(); field.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        field.typeText("Parity")
        attach(app, "Slide text tab: Edit text")
        app.buttons["Style"].tap()
        let preset = app.buttons["native-editor-text-preset"]
        XCTAssertTrue(preset.waitForExistence(timeout: 5))
        preset.tap(); app.buttons["Bold"].tap()
        XCTAssertTrue(preset.label.contains("Bold"))
        attach(app, "Slide text tab: Style top")
        let font = app.buttons["native-editor-text-font"]
        XCTAssertTrue(font.exists)
        let size = app.textFields["native-editor-text-size"]
        revealInTextPanel(size, app: app)
        let before = size.value as? String
        app.buttons["Increase text size"].tap()
        XCTAssertNotEqual(size.value as? String, before)
        let rotation = app.buttons["native-editor-text-rotation-Increment"]
        revealInTextPanel(rotation, app: app)
        rotation.tap(); rotation.tap()
        XCTAssertEqual(rotation.value as? String, "10 degrees")
        let outline = app.sliders["Outline"]
        revealInTextPanel(outline, app: app)
        outline.adjust(toNormalizedSliderPosition: 0.5)
        attach(app, "Slide text tab: Style")
        app.buttons["slidepost-done"].tap()
        // Reopening shows the saved style.
        app.buttons["slidepost-tool-text"].tap()
        app.buttons["Style"].tap()
        XCTAssertTrue(app.buttons["native-editor-text-preset"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["native-editor-text-preset"].label.contains("Bold"))
        let again = app.buttons["native-editor-text-rotation-Increment"]
        revealInTextPanel(again, app: app)
        XCTAssertEqual(again.value as? String, "10 degrees")
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

    /// Integration: every Style control is reachable above the pinned row, and canvas gestures show the same numbers in the panel.
    func testStyleTabScrollsToEveryControlAndCanvasAgreesWithPanel() {
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
        text.tap()
        app.buttons["Style"].tap()
        preview.pinch(withScale: 1.4, velocity: 1)
        preview.rotate(CGFloat.pi / 4, withVelocity: 1)
        let value = text.value as? String ?? ""
        let canvasSize = number(value, after: "size"), canvasRotation = number(value, after: "rotation")
        attach(app, "Integration: canvas resized + rotated")
        // Panel shows the same numbers.
        let rotation = app.buttons["native-editor-text-rotation-Increment"]
        revealInTextPanel(rotation, app: app)
        XCTAssertEqual(rotation.value as? String, "\(Int(canvasRotation)) degrees")
        let size = app.textFields["native-editor-text-size"]
        revealInTextPanel(size, app: app)
        XCTAssertEqual(Double(size.value as? String ?? ""), canvasSize)
        // Bottom of the Style tab: every control above the pinned row.
        let scroll = app.scrollViews["native-editor-text-inspector-scroll"]
        for _ in 0..<8 { scroll.swipeUp() }
        let apply = app.buttons["slidepost-apply-all"]
        XCTAssertTrue(apply.isHittable)
        XCTAssertLessThanOrEqual(rotation.frame.maxY, apply.frame.minY, "the last control clears the pinned row")
        XCTAssertTrue(rotation.isHittable)
        attach(app, "Integration: style bottom")
        // Panel edit reaches the canvas, undo/redo round trips.
        rotation.tap()
        XCTAssertNotEqual(number(text.value as? String ?? "", after: "rotation"), canvasRotation)
        app.buttons["slidepost-undo"].tap()
        XCTAssertEqual(number(text.value as? String ?? "", after: "rotation"), canvasRotation)
        app.buttons["slidepost-redo"].tap()
        XCTAssertNotEqual(number(text.value as? String ?? "", after: "rotation"), canvasRotation)
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

    func testAISheetWithoutChatEditKeepsTheProposeFlow() {
        let app = openRichWorkspace()
        app.buttons["slidepost-openkria"].tap()
        XCTAssertTrue(app.textFields["Message Kria"].waitForExistence(timeout: 5), "the same composer as the video editor's Kria sheet")
        XCTAssertTrue(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", "propose an arrangement")).firstMatch.exists, "propose-flow intro")
        attach(app, "AI sheet: propose flow")
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

    func testPlainTapStillSelectsAndAScrollDoesNotReorder() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tile-2"].tap()
        XCTAssertTrue(app.buttons["slidepost-tile-2"].isSelected)
        app.buttons["slidepost-tile-1"].swipeLeft()
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "a plain swipe scrolls, it never reorders")
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
    }

    func testStripScrollsWithSlowFingerDragAndSwipes() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-3"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY, w = app.windows.firstMatch.frame.width
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: w - 40, toX: 40, y: y, hold: 0.02, velocity: .slow) }
        for _ in 0..<8 { fingerDrag(app, fromX: 30, toX: w - 30, y: y, velocity: .fast) }
        XCTAssertTrue(app.buttons["slidepost-tile-1"].isHittable, "a swipe right scrolls back to the start")
        // The stock XCUITest swipe, from a tile that is on screen at rest.
        app.buttons["slidepost-tile-3"].swipeLeft()
        XCTAssertLessThan(app.buttons["slidepost-tile-1"].frame.midX, 0, "swipeLeft scrolls the strip")
    }

    func testStripFlickScrollsFromATileTheGapAndTheAddBlock() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-2"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY
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
    }

    func testStripScrollsWithASelectedSlideAndShowsAnOverflowTile() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-2"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        tile.tap(); XCTAssertTrue(tile.isSelected)
        let window = app.windows.firstMatch.frame
        let cut = (1...12).filter { app.buttons["slidepost-tile-\($0)"].frame.maxX > window.maxX }
        XCTAssertFalse(cut.isEmpty, "a tile is cut by the right edge: the strip visibly overflows")
        let y = tile.frame.midY
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: window.width - 40, toX: 40, y: y) }
        app.buttons["slidepost-tile-12"].tap()
        XCTAssertTrue(app.buttons["slidepost-tile-12"].isSelected)
    }

    func testSelectingAHeldTextStillLetsTheStripScroll() {
        let app = openRichWorkspace(many: true)
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.waitForExistence(timeout: 8))
        // Hold-select the text on the preview (browse mode keeps the strip), then scroll the strip.
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.8)
        let tile = app.buttons["slidepost-tile-3"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY, w = app.windows.firstMatch.frame.width
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: w - 40, toX: 40, y: y) }
    }

    func testStripSwipeDoesNotOpenDrawerButAPreviewSwipeStillDoes() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-3"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY, w = app.windows.firstMatch.frame.width
        for _ in 0..<3 { fingerDrag(app, fromX: 30, toX: w - 30, y: y) }
        XCTAssertFalse(app.buttons["drawer-new-chat"].isHittable, "swiping the strip must not open the drawer")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let py = preview.frame.minY + 30
        windowPoint(app, x: 4, y: py).press(forDuration: 0.05, thenDragTo: windowPoint(app, x: w * 0.85, y: py), withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 3), "a swipe that starts above the strip still opens the drawer")
        attach(app, "Drawer opens from outside the strip")
    }

    func testLongPressReorderStillWorksAfterScrollingTheStrip() {
        let app = openRichWorkspace(many: true)
        let tile = app.buttons["slidepost-tile-3"]; XCTAssertTrue(tile.waitForExistence(timeout: 8))
        let y = tile.frame.midY, w = app.windows.firstMatch.frame.width
        assertStripReachesTheEnd(app) { fingerDrag(app, fromX: w - 40, toX: 40, y: y) }
        let last = app.buttons["slidepost-tile-12"], prior = app.buttons["slidepost-tile-11"]
        XCTAssertEqual(coverIndex(app, count: 12), 1)
        last.press(forDuration: 0.9, thenDragTo: prior, withVelocity: .slow, thenHoldForDuration: 0.2)
        attach(app, "Strip: after reorder at the end")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "long press then drag reorders")
    }

    /// The "+" block is the LAST item of the same row, with the tiles' footprint and baseline.
    func testAddBlockIsTheLastStripItemWithTheTileFootprint() {
        let app = openRichWorkspace()
        let tile = app.buttons["slidepost-tile-3"], add = app.buttons["slidepost-add-tile"]
        XCTAssertTrue(add.waitForExistence(timeout: 5))
        // XCUI reports an aspect-filled photo's overflow as the tile's width, so compare height, baseline and
        // CENTRES: the + block is exactly one 66pt pitch (60pt block + 6pt gap) after the last slide.
        XCTAssertEqual(add.frame.height, tile.frame.height, accuracy: 1)
        XCTAssertEqual(add.frame.height, 72, accuracy: 1)
        XCTAssertEqual(add.frame.width, 56, accuracy: 1)
        XCTAssertEqual(add.frame.minY, tile.frame.minY, accuracy: 1, "same baseline as the tiles")
        XCTAssertEqual(add.frame.midX - tile.frame.midX, 66, accuracy: 1, "the next block in the row, one pitch after the last slide")
        for index in 1...3 { XCTAssertLessThan(app.buttons["slidepost-tile-\(index)"].frame.minX, add.frame.minX) }
        attach(app, "Strip: add block last")
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

    func testTappingATextInBrowseOpensTheEditTabWithTheKeyboard() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        attach(app, "Browse: text on the canvas")
        text.tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5), "the Text panel opened on Edit text")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "the keyboard is up so the user can type")
        XCTAssertFalse(app.buttons["native-editor-text-preset"].exists, "Edit tab, not Style")
        field.typeText("!")
        expectation(for: NSPredicate(format: "label == %@", "Athens!"), evaluatedWith: canvasText(app)); waitForExpectations(timeout: 5)
        attach(app, "Tap text: Edit tab with keyboard")
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
        attach(app, "Ready post: tap text opens Edit text with keyboard")
    }

    func testHoldAndDragTextOnACreatedPostMovesItDirectly() {
        let app = openRichWorkspace(extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        openReadyPostWithText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
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
    }

    func testHoldSelectThenCornerHandleResizesWithoutPanel() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        text.press(forDuration: 0.9)
        let handle = app.descendants(matching: .any)["slidepost-text-handle"].firstMatch
        XCTAssertTrue(handle.waitForExistence(timeout: 3), "hold selects and shows the handle")
        panelAndKeyboardAbsent(app, "after hold")
        attach(app, "Hold-select: frame and handle, no panel")
        let sizeBefore = number(text.value as? String ?? "", after: "size")
        handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.9, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.62)))
        XCTAssertNotEqual(sizeBefore, number(text.value as? String ?? "", after: "size"), "corner drag changes the size")
        panelAndKeyboardAbsent(app, "after corner drag")
        attach(app, "Hold-select: after corner resize")
    }

    func testTapOnHoldSelectedTextOpensEditTextWithKeyboard() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        text.press(forDuration: 0.9)
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-text-handle"].firstMatch.waitForExistence(timeout: 3))
        panelAndKeyboardAbsent(app, "after hold")
        Thread.sleep(forTimeInterval: 0.9)
        text.tap()
        XCTAssertTrue(app.textViews["slidepost-text-field"].firstMatch.waitForExistence(timeout: 5), "tap opens Edit text")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), "keyboard is up")
        attach(app, "Tap after hold-select: Edit text with keyboard")
    }

    func testHoldDragIsExactlyOneUndoStep() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        let text = canvasText(app)
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        let before = text.value as? String ?? ""
        text.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.9, thenDragTo: preview.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.25)))
        XCTAssertNotEqual(before, text.value as? String ?? "")
        app.buttons["slidepost-undo"].tap()
        XCTAssertEqual(before.replacingOccurrences(of: ", selected", with: ""), (text.value as? String ?? "").replacingOccurrences(of: ", selected", with: ""), "one undo returns the text to where it started")
        XCTAssertEqual(text.label, "Athens", "undo did not remove the text")
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

    func testTappingEmptyCanvasInBrowseDoesNothingAndInTextModeDeselects() {
        let app = openRichWorkspace()
        addText(app, "Athens")
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        preview.coordinate(withNormalizedOffset: CGVector(dx: 0.92, dy: 0.06)).tap()
        XCTAssertFalse(app.textViews["slidepost-text-field"].firstMatch.exists, "browse: tapping empty canvas opens nothing")
        XCTAssertTrue(app.buttons["slidepost-tool-text"].isHittable)
    }

    func testWorkspaceKeepsToolbarReachableAtLargeText() {
        let app = openRichWorkspace(dynamicType: "accessibility3")
        XCTAssertTrue(app.buttons["slidepost-tile-1"].isHittable)
        let tools = ["slidepost-tool-text", "slidepost-tool-cover", "slidepost-tool-look", "slidepost-tool-more"]
        let bar = app.buttons[tools[0]]
        for id in tools {
            let tool = app.buttons[id]
            XCTAssertTrue(tool.waitForExistence(timeout: 5), id)
            var tries = 0
            while !tool.isHittable && tries < 4 { (tries < 2 ? bar : app.buttons[tools[3]]).swipeLeft(); tries += 1 }
            XCTAssertTrue(tool.isHittable, "\(id) must be reachable at accessibility3 (scrolling the bar if needed)")
        }
        attach(app, "Workspace at large text")
    }

    func testTextPanelKeepsEditFieldVisibleWithKeyboardUp() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tool-text"].tap()
        let field = app.textViews["slidepost-text-field"].firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(field.isHittable, "the Edit field stays visible above the keyboard")
        let keyboardTop = app.keyboards.firstMatch.frame.minY
        XCTAssertLessThan(field.frame.maxY, keyboardTop)
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertTrue(preview.exists, "the preview stays on screen too")
        XCTAssertLessThan(preview.frame.maxY, field.frame.minY + 1, "the stage shrinks above the field")
        attach(app, "Text panel with keyboard up")
        app.buttons["Style"].tap()
        attach(app, "Text panel style chips")
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

    func testRichLayoutForANewChatWhenCapabilitiesAreSlow() {
        let app = openRichWorkspace(capabilities: "slow", save: false)
        assertRichLayout(app, "new chat, slow capabilities")
        attach(app, "Entry: new chat (slow caps)")
    }

    func testRichLayoutForANewChatWhenCapabilitiesLoad() {
        let app = openRichWorkspace(save: false)
        assertRichLayout(app, "new chat, capabilities load")
        attach(app, "Entry: new chat (caps true)")
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

    func testRichLayoutFromTheDrawerWhenCapabilitiesAreSlow() {
        let app = launchRich(capabilities: "slow", readyThread: true)
        openWeekendTripFromDrawer(app)
        assertRichLayout(app, "drawer, slow capabilities")
        attach(app, "Entry: drawer (slow caps)")
    }

    func testRichLayoutFromTheDrawerWhenCapabilitiesLoad() {
        let app = launchRich(readyThread: true)
        openWeekendTripFromDrawer(app)
        assertRichLayout(app, "drawer, capabilities load")
        attach(app, "Entry: drawer (caps true)")
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

    /// KRI-305: export is available from any state. An unsaved, unrendered post saves, renders and then
    /// reports the Photos save in the banner (the fixture writer stands in for Photos).
    func testExportMenuSavesAnUnsavedPostToPhotosAndShowsTheBanner() {
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_FIXTURE_PHOTOS": "1"])
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        let export = app.buttons["slidepost-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 5)); export.tap()
        XCTAssertTrue(app.buttons["slidepost-share-files"].waitForExistence(timeout: 3), "Share files sits beside Save to Photos")
        app.buttons["slidepost-save-photos"].tap()
        let banner = app.descendants(matching: .any)["slidepost-export-state"]
        XCTAssertTrue(banner.waitForExistence(timeout: 10), "export reports progress in the banner")
        expectation(for: NSPredicate(format: "label CONTAINS %@", "Saved 3 slides"), evaluatedWith: banner); waitForExpectations(timeout: 25)
        attach(app, "Export banner: saved to Photos")
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "exporting saved the draft first")
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

    /// Prod 2026-10-05: the first AI message on a never-saved post sent a version-0 draft and the server 422'd it.
    /// The stub enforces the same rule, so this fails if the wire draft is ever invalid again.
    func testFirstAIMessageOnABrandNewPostSucceedsWithoutSavingFirst() {
        let app = openRichWorkspace(chatEdit: true, save: false)
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "the post is brand new: nothing saved")
        app.buttons["slidepost-openkria"].tap()
        let input = app.textFields["Message Kria"]
        XCTAssertTrue(input.waitForExistence(timeout: 5)); input.tap(); input.typeText("Order them by time")
        app.buttons["chat-send-message"].tap()
        let reply = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Kria: Done.")).firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 8), "a new post's first AI request succeeds")
        XCTAssertFalse(app.buttons["slidepost-ai-retry"].exists)
        attach(app, "New post: AI request succeeded")
    }

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

    /// The add-media sheet for a slide post never says "overlays" or "visuals".
    func testAddMediaSheetForSlidePostsSaysPhotosAndVideos() {
        let app = openRichWorkspace(save: false)
        let add = app.buttons["slidepost-add-tile"].firstMatch
        XCTAssertTrue(add.waitForExistence(timeout: 8)); add.tap()
        XCTAssertTrue(app.staticTexts["Add photos & videos"].waitForExistence(timeout: 8))
        XCTAssertFalse(app.staticTexts["Add overlays"].exists)
        XCTAssertFalse(app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS[c] 'overlay' OR label CONTAINS[c] 'visuals'")).firstMatch.exists)
        attach(app, "Add photos & videos sheet")
    }

    /// Photos are served by a slow fixture "CDN" with a fresh signature on every response. Once a slide has
    /// loaded, selecting it again (or a prefetched neighbour) never shows a loading state.
    func testSwitchingSlidesNeverShowsALoadingStateForLoadedImages() {
        let app = openRichWorkspace(save: false, extraEnv: ["KRIA_SLIDE_POST_REMOTE_MEDIA": "1", "KRIA_SLIDE_POST_MEDIA_DELAY_MS": "600"])
        let loading = app.descendants(matching: .any)["slidepost-preview-loading"]
        let image = app.descendants(matching: .any)["slidepost-preview-image"]
        let first = Date()
        XCTAssertTrue(image.waitForExistence(timeout: 10), "the first photo appears")
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: loading); waitForExpectations(timeout: 10)
        let firstVisible = Date().timeIntervalSince(first)
        sleep(3) // neighbours are prefetched in the background
        attach(app, "Slide 1 loaded")
        var switches: [TimeInterval] = []
        for tile in ["slidepost-tile-3", "slidepost-tile-1", "slidepost-tile-3", "slidepost-tile-1"] {
            let started = Date()
            app.buttons[tile].tap()
            XCTAssertFalse(loading.exists, "no loading indicator right after selecting \(tile)")
            XCTAssertFalse(app.descendants(matching: .any)["slidepost-preview-blurred"].exists, "no blur state right after selecting \(tile)")
            XCTAssertTrue(image.exists, "the photo is on screen immediately")
            switches.append(Date().timeIntervalSince(started))
        }
        attach(app, "After switching slides")
        print("KRIA_SLIDE_TIMING first-visible=\(String(format: "%.3f", firstVisible))s switch-taps=\(switches.map { String(format: "%.3f", $0) })")
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
