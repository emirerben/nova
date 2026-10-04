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
    private func openRichWorkspace(dynamicType: String? = nil, chatEdit: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v1"
        app.launchEnvironment["KRIA_SLIDE_POST_FIXTURE"] = "1"
        app.launchEnvironment["KRIA_SLIDE_POST_RICH_TEXT"] = "1"
        if chatEdit { app.launchEnvironment["KRIA_SLIDE_POST_CHAT_EDIT"] = "1" }
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
        for id in ["slidepost-tile-1", "slidepost-tool-text", "slidepost-tool-arrange", "slidepost-tool-cover", "slidepost-tool-look", "slidepost-tool-more", "slidepost-composer"] {
            let element = app.descendants(matching: .any)[id].firstMatch
            XCTAssertTrue(element.exists && element.isHittable, "\(id) must stay reachable without scrolling")
        }
        XCTAssertLessThanOrEqual(preview.frame.maxY, app.buttons["slidepost-tile-1"].frame.minY + 1, "the preview never overlaps the strip")
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

    /// KRI-298 Lane E: a chat message stages the server's edit (unsaved, undoable) in the compact
    /// thread layout, and Save PUTs it quoting the server's base version (the stub 409s otherwise).
    func testSlideChatEditStagesAndSaves() {
        let app = openRichWorkspace(chatEdit: true)
        XCTAssertTrue(app.buttons["slidepost-tile-1"].waitForExistence(timeout: 5))
        let composer = app.textFields["slidepost-composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5)); composer.tap()
        composer.typeText("Put them in chronological order and add each photo's location")
        app.buttons["slidepost-composer-send"].tap()
        let reply = app.staticTexts["slidepost-chat-reply"].firstMatch
        XCTAssertTrue(reply.waitForExistence(timeout: 8), "Kria's reply is shown")
        XCTAssertTrue(reply.label.hasPrefix("Done. Your photos now follow the order"), "the reply is server text, verbatim")
        XCTAssertTrue(app.staticTexts["Reordered 3"].exists)
        XCTAssertTrue(app.staticTexts["1 photo has no location"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-chat-note"].firstMatch.exists, "a missing location reads as a note")
        XCTAssertFalse(app.buttons["slidepost-tool-text"].exists, "the tool bar is hidden while the thread shows")
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertTrue(app.descendants(matching: .any)["slidepost-chat-unsaved"].firstMatch.exists)
        let preview = app.descendants(matching: .any)["slidepost-preview"].firstMatch
        XCTAssertLessThan(preview.frame.height, 260, "the preview is compact while chatting")
        attach(app, "AI turn: staged edit")
        // Undo returns the editor to the saved draft; the thread stays.
        app.buttons["slidepost-chat-undo"].tap()
        XCTAssertNotEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertTrue(app.staticTexts["slidepost-chat-reply"].firstMatch.exists)
        composer.tap(); composer.typeText("Again please")
        app.buttons["slidepost-composer-send"].tap()
        XCTAssertTrue(app.buttons["slidepost-chat-save"].waitForExistence(timeout: 8))
        // Save stays disabled while Kria is still editing; wait for the staged result.
        expectation(for: NSPredicate(format: "isEnabled == true"), evaluatedWith: app.buttons["slidepost-chat-save"]); waitForExpectations(timeout: 10)
        app.buttons["slidepost-chat-save"].tap()
        let back = app.buttons["slidepost-chat-close"]
        XCTAssertTrue(back.waitForExistence(timeout: 8))
        let saved = NSPredicate(format: "label != %@", "Unsaved changes")
        expectation(for: saved, evaluatedWith: app.staticTexts["slidepost-subtitle"]); waitForExpectations(timeout: 8)
        XCTAssertFalse(app.descendants(matching: .any)["slidepost-error"].firstMatch.exists, "a PUT that quoted the wrong version would 409 and show an error")
        attach(app, "AI turn: saved")
        back.tap()
        XCTAssertTrue(app.buttons["slidepost-tool-text"].waitForExistence(timeout: 5), "closing the thread returns to the normal workspace")
    }

    func testArrangeDragReordersAndKeepsTheCover() {
        let app = openRichWorkspace()
        app.buttons["slidepost-tool-arrange"].tap()
        XCTAssertTrue(app.staticTexts["slidepost-arrange-hint"].waitForExistence(timeout: 3))
        let first = app.buttons["slidepost-tile-1"], second = app.buttons["slidepost-tile-2"]
        first.press(forDuration: 0.5, thenDragTo: second, withVelocity: .slow, thenHoldForDuration: 0.2)
        XCTAssertEqual(app.staticTexts["slidepost-subtitle"].label, "Unsaved changes")
        XCTAssertEqual(app.buttons["slidepost-tile-2"].value as? String, "Cover", "the cover moved with its slide")
        attach(app, "Arrange: after drag")
    }

    func testWorkspaceKeepsToolbarReachableAtLargeText() {
        let app = openRichWorkspace(dynamicType: "accessibility3")
        XCTAssertTrue(app.buttons["slidepost-tile-1"].isHittable)
        let tools = ["slidepost-tool-text", "slidepost-tool-arrange", "slidepost-tool-cover", "slidepost-tool-look", "slidepost-tool-more"]
        let bar = app.buttons[tools[0]]
        for id in tools {
            let tool = app.buttons[id]
            XCTAssertTrue(tool.waitForExistence(timeout: 5), id)
            var tries = 0
            while !tool.isHittable && tries < 4 { (tries < 2 ? bar : app.buttons[tools[4]]).swipeLeft(); tries += 1 }
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
