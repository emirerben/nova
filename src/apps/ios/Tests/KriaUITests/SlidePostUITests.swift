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
        // KRI-197: the sheet has no "Kria" title; its content scrolls with soft edges.
        XCTAssertFalse(app.staticTexts["Kria"].exists)
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

    // MARK: Redesigned workspace (KRIA_SLIDE_POST_RICH_TEXT=1)

    /// Walks the fixture creation flow to the redesigned workspace: format, direction, Apply.
    private func openRichWorkspace(dynamicType: String? = nil, chatEdit: Bool = false, many: Bool = false, lateAsset: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        app.launchEnvironment["KRIA_SLIDE_POST_RICH_TEXT"] = "1"
        if chatEdit { app.launchEnvironment["KRIA_SLIDE_POST_CHAT_EDIT"] = "1" }
        if many { app.launchEnvironment["KRIA_SLIDE_POST_MANY"] = "1" }
        if lateAsset { app.launchEnvironment["KRIA_SLIDE_POST_LATE_ASSET"] = "1" }
        if let dynamicType { app.launchEnvironment["UI_TEST_DYNAMIC_TYPE_SIZE"] = dynamicType }
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 12)); app.buttons["Open projects"].tap()
        XCTAssertTrue(app.buttons["drawer-new-chat"].waitForExistence(timeout: 5)); app.buttons["drawer-new-chat"].tap()
        let carousel = app.scrollViews["format-carousel"]
        XCTAssertTrue(carousel.waitForExistence(timeout: 12)); carousel.swipeLeft()
        XCTAssertTrue(app.buttons["format-slides"].waitForExistence(timeout: 5)); app.buttons["format-slides"].tap()
        XCTAssertTrue(app.staticTexts["Start your post"].waitForExistence(timeout: 8))
        let ask = app.buttons["Ask Kria"].firstMatch
        scrollTo(ask, app: app); ask.tap()
        let apply = app.buttons["Apply proposal"].firstMatch
        XCTAssertTrue(apply.waitForExistence(timeout: 8)); scrollTo(apply, app: app); apply.tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 8), "the redesigned tool bar replaces the old buttons")
        return app
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
        let ai = app.buttons["slidepost-openkria"]
        XCTAssertTrue(ai.isHittable)
        attach(app, "AI button on the page")
        ai.tap()
        let input = app.textFields["slidepost-ai-input"]
        XCTAssertTrue(input.waitForExistence(timeout: 5)); input.tap()
        input.typeText("Put them in chronological order and add each photo's location")
        app.buttons["slidepost-ai-send"].tap()
        let reply = app.staticTexts["slidepost-ai-reply"].firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 8), "Kria's reply is shown in the sheet")
        XCTAssertTrue(reply.label.hasPrefix("Done. Your photos now follow the order"), "the reply is server text, verbatim")
        XCTAssertTrue(app.staticTexts["Reordered 3"].exists)
        XCTAssertTrue(app.staticTexts["1 photo has no location"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-ai-note"].firstMatch.exists, "a missing location reads as a note")
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-ai-unsaved"].firstMatch.waitForExistence(timeout: 3))
        attach(app, "AI sheet: staged edit")
        // Undo inside the sheet returns the page to the saved draft; the transcript stays.
        app.buttons["slidepost-ai-undo"].tap()
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-ai-unsaved"].firstMatch.waitForExistence(timeout: 2))
        XCTAssertTrue(app.staticTexts["slidepost-ai-reply"].firstMatch.exists)
        input.tap(); input.typeText("Again please")
        app.buttons["slidepost-ai-send"].tap()
        let save = app.buttons["slidepost-ai-save"]
        expectation(for: NSPredicate(format: "isEnabled == true"), evaluatedWith: save); waitForExpectations(timeout: 10)
        // Leave it staged: the edit is visible on the page behind the sheet.
        app.buttons["slidepost-ai-done"].tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes", "the AI edit is staged on the page")
        app.buttons["slidepost-tile-1"].tap()
        XCTAssertEqual(canvasText(app).label, "Athens", "the staged edit labelled the first slide")
        attach(app, "AI edit staged on the page")
        // Save from the sheet quotes the server's version (a wrong one would 409 and show an error).
        app.buttons["slidepost-openkria"].tap()
        XCTAssertTrue(app.buttons["slidepost-ai-save"].waitForExistence(timeout: 5))
        app.buttons["slidepost-ai-save"].tap()
        app.buttons["slidepost-ai-done"].tap()
        let saved = NSPredicate(format: "label != %@", "Unsaved changes")
        expectation(for: saved, evaluatedWith: app.staticTexts["slidepost-subtitle"]); waitForExpectations(timeout: 8)
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-error"].firstMatch.exists)
    }

    func testAISheetWithoutChatEditKeepsTheProposeFlow() {
        let app = openRichWorkspace()
        app.buttons["slidepost-openkria"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-prompt"].firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["slidepost-ask"].exists)
        XCTAssertFalse(app.textFields["slidepost-ai-input"].exists)
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
        first.press(forDuration: 0.6, thenDragTo: second, withVelocity: .slow, thenHoldForDuration: 0.2)
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
            .press(forDuration: 0.6, thenDragTo: edge, withVelocity: .slow, thenHoldForDuration: 2.5)
        let cover = coverIndex(app, count: 12)
        XCTAssertNotNil(cover)
        XCTAssertGreaterThan(cover ?? 0, 7, "auto-scroll carried the slide past the slides that were visible")
        attach(app, "Reorder: after edge auto-scroll")
    }

    /// The "+" block is the LAST item of the same row, with the tiles' footprint and baseline.
    func testAddBlockIsTheLastStripItemWithTheTileFootprint() {
        let app = openRichWorkspace()
        let tile = app.buttons["slidepost-tile-3"], add = app.buttons["slidepost-add-tile"]
        XCTAssertTrue(add.waitForExistence(timeout: 5))
        print("DBGTREE", app.debugDescription)
        XCTAssertEqual(add.frame.width, tile.frame.width, accuracy: 1)
        XCTAssertEqual(add.frame.height, tile.frame.height, accuracy: 1)
        XCTAssertEqual(add.frame.minY, tile.frame.minY, accuracy: 1, "same baseline as the tiles")
        XCTAssertEqual(add.frame.minX - tile.frame.maxX, 6, accuracy: 1, "the next block in the row, one gap after the last slide")
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
        XCTAssertEqual(pending.frame.width, app.buttons["slidepost-tile-1"].frame.width, accuracy: 1, "same footprint as a tile")
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

    private func scrollTo(_ element: XCUIElement, app: XCUIApplication) {
        for _ in 0..<7 {
            if element.exists && element.isHittable { return }
            app.swipeUp()
        }
    }
}
