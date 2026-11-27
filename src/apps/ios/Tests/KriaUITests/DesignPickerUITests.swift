import XCTest

/// KRI-1582: the informative text design picker, from the question to an edit that keeps the creator's colour.
///
/// The fixture server (`DesignPickerUITestFixture`) answers with the server's own question, generated from the real
/// gate, preview compile and resolver (`Tests/Fixtures/KRI1582DesignPicker.json`, drift-checked by
/// `tests/kria/test_kri1582_design_picker.py`). Each still is drawn on this iPhone by the media engine over the
/// project's own clip. Once the edit is ready, its rows are the resolver's rows for the design tapped, and a Save is
/// served back exactly as the app posted it on the next render.
///
/// Failure modes pinned here: a version 2 question shows text blocks or no card; a still is never drawn, or drawn
/// without her clip; the agent's default is not marked; the tap sends no structured answer, or the wrong key; the
/// edit opens in another design than the one tapped; a heading colour change is not part of the Save, or the edit
/// that comes back after the re-render is not the one she saved.
///
/// CA-4 (`KRIA_CHAT_DESIGN_PICKER_OPTIONS` = `3`, `5` or `rows`): the third tile is cut off the card; a fifth tile
/// does not scroll in; the primary does not name the selected style; the other tiles stay live while the send is out;
/// a refused send shows no red line or no Try again, or Try again sends another style; "Type my own" leaves the tiles
/// or the primary live; the receipt shows no preview of the chosen style; options without previews lose the rows.
@MainActor
final class DesignPickerUITests: XCTestCase {
    private static let designs = ["A1", "A2", "A3", "B1"]
    private static let edited = "#9BCAFF"

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }

    private func waitFor(_ element: XCUIElement, _ format: String, _ args: CVarArg..., timeout: TimeInterval = 15) -> Bool {
        let expectation = XCTNSPredicateExpectation(predicate: NSPredicate(format: format, argumentArray: args), object: element)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }

    private func localized(_ key: String, _ language: String?) -> String {
        guard let language else { return key }
        let catalog = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Kria/Resources/Localizable.xcstrings")
        let strings = (try? JSONSerialization.jsonObject(with: Data(contentsOf: catalog)) as? [String: Any])?["strings"] as? [String: Any]
        let unit = ((strings?[key] as? [String: Any])?["localizations"] as? [String: Any])?[language] as? [String: Any]
        return (unit?["stringUnit"] as? [String: Any])?["value"] as? String ?? key
    }

    private func launch(options: String? = nil, refuseFirst: Bool = false, sendDelay: Int? = nil,
                        language: String? = nil) -> XCUIApplication {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI1582DesignPicker.json").path
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = [
            "KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_FIXTURE_MEDIA": "1", "KRIA_EDITOR_FIXTURE": "1",
            "KRIA_CHAT_DESIGN_PICKER": "1", "KRIA_CHAT_DESIGN_PICKER_FIXTURE": fixture, "UI_TEST_REDUCE_MOTION": "1",
        ]
        if let options { app.launchEnvironment["KRIA_CHAT_DESIGN_PICKER_OPTIONS"] = options }
        if refuseFirst { app.launchEnvironment["KRIA_CHAT_CHOICE_QUESTION"] = "refuse-first" }
        if let sendDelay { app.launchEnvironment["KRIA_CHAT_SEND_DELAY"] = String(sendDelay) }
        if let language { app.launchArguments += ["-AppleLanguages", "(\(language))", "-AppleLocale", "\(language)_\(language.uppercased())"] }
        app.launch()
        let openProjects = app.buttons[localized("Open projects", language)]
        XCTAssertTrue(openProjects.waitForExistence(timeout: 20))
        openProjects.tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor(newChat, "enabled == true", timeout: 20))
        newChat.tap()
        XCTAssertTrue(app.staticTexts[localized("What are we making?", language)].waitForExistence(timeout: 20))
        app.chooseFormat("montage")
        let send = app.buttons[localized("Send clips", language)]
        XCTAssertTrue(send.waitForExistence(timeout: 5))
        send.tap()
        return app
    }

    /// Four designs drawn on this iPhone over her clip, the default marked, the tapped one sent and rendered; then
    /// a heading recoloured in the editor is what the re-rendered edit holds.
    func testFourDesignsRenderOnThePhoneThePickRendersAndHerHeadingColourSticks() {
        // Chat, render, editor, Save and the re-render: about 65 s on a local simulator, so the CI runner gets room.
        executionTimeAllowance = 300
        let app = launch()

        let strip = element(app, "design-preview-strip")
        XCTAssertTrue(strip.waitForExistence(timeout: 20), "a version 2 question shows the strip of stills")
        for key in Self.designs {
            let tile = app.buttons["choice-option-\(key)"]
            XCTAssertTrue(tile.waitForExistence(timeout: 5), "\(key) is offered")
            XCTAssertTrue(waitFor(tile, "value == %@", "Preview drawn over your clip", timeout: 20),
                          "\(key) is drawn on this iPhone over the project's clip; reads \(tile.value ?? "nil")")
        }
        XCTAssertTrue(app.buttons["choice-option-B1"].label.hasSuffix("recommended"), "a subtitle ask defaults to B1")
        XCTAssertFalse(app.buttons["choice-option-A2"].label.hasSuffix("recommended"))
        XCTAssertTrue(app.buttons["choice-option-B1"].isSelected, "the agent's default starts selected")
        XCTAssertFalse(app.buttons["answer-let-kria-decide"].exists, "the primary already names the default")
        attach(app, "Design picker: four designs drawn on the phone")

        app.buttons["choice-option-A2"].tap()
        XCTAssertTrue(app.buttons["choice-option-A2"].isSelected)
        XCTAssertFalse(element(app, "choice-card-answered").exists, "a tile only selects")
        let use = app.buttons["choice-use-style"]
        XCTAssertEqual(use.label, "Use Bold on a soft shade")
        use.tap()
        let echo = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "design[A2] question[design-q-fixture]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 15), "the tap sends the structured answer for A2")
        XCTAssertTrue(element(app, "choice-card-answered").waitForExistence(timeout: 5), "the card closes on her choice")
        XCTAssertTrue(waitFor(element(app, "choice-card-answered"), "value == %@", "Preview drawn over your clip"),
                      "the receipt shows the style she chose")

        let confirm = app.buttons["Create this video"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 15))
        confirm.tap()
        let open = app.buttons["Open editor"]
        XCTAssertTrue(open.waitForExistence(timeout: 30), "the edit renders")
        open.tap()

        let textTool = app.buttons["native-editor-tool-text"]
        XCTAssertTrue(textTool.waitForExistence(timeout: 30), "the editor opens the rendered edit")
        textTool.tap()
        let heading = element(app, "native-editor-text-row-design-heading")
        XCTAssertTrue(heading.waitForExistence(timeout: 15), "the edit carries A2's heading and detail")
        XCTAssertTrue(element(app, "native-editor-text-row-design-detail").exists)
        XCTAssertFalse(element(app, "native-editor-text-row-design-line").exists, "not B1's guide line")
        attach(app, "Editor: the A2 edit")
        heading.tap()
        let style = app.buttons.matching(identifier: "native-editor-text-tabs").matching(NSPredicate(format: "label == %@", "Style")).firstMatch
        XCTAssertTrue(style.waitForExistence(timeout: 5))
        style.tap()
        let blue = app.buttons["Text color \(Self.edited)"]
        XCTAssertTrue(blue.waitForExistence(timeout: 5))
        XCTAssertFalse(blue.isSelected, "the design's heading is not blue")
        blue.tap()
        XCTAssertTrue(waitFor(blue, "selected == true", timeout: 5))
        app.buttons["native-editor-text-inspector-done"].tap()
        let closeText = app.buttons["native-editor-text-done"]
        XCTAssertTrue(closeText.waitForExistence(timeout: 5))
        closeText.tap()

        let save = app.buttons["native-editor-save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertEqual(save.label, "Save changes", "the colour is an unsaved edit")
        save.tap()
        XCTAssertTrue(waitFor(save, "label == %@", "Saved", timeout: 20), "Save reaches the saved state; reads \(save.label)")

        app.buttons["native-editor-back"].tap()
        XCTAssertTrue(open.waitForExistence(timeout: 15))
        open.tap()
        XCTAssertTrue(textTool.waitForExistence(timeout: 30))
        textTool.tap()
        XCTAssertTrue(heading.waitForExistence(timeout: 15), "the re-rendered edit still has its heading")
        heading.tap()
        XCTAssertTrue(style.waitForExistence(timeout: 5))
        style.tap()
        let kept = app.buttons["Text color \(Self.edited)"]
        XCTAssertTrue(kept.waitForExistence(timeout: 5))
        XCTAssertTrue(kept.isSelected, "her heading colour came back from the re-render")
        attach(app, "Editor after the re-render: her heading colour kept")
        app.terminate()
    }

    private func waitForStrip(_ app: XCUIApplication, keys: [String], language: String? = nil) {
        XCTAssertTrue(element(app, "design-preview-strip").waitForExistence(timeout: 20))
        for key in keys {
            let tile = app.buttons["choice-option-\(key)"]
            XCTAssertTrue(tile.waitForExistence(timeout: 5))
            XCTAssertTrue(waitFor(tile, "value == %@", localized("Preview drawn over your clip", language), timeout: 20),
                          "\(key) is drawn over her clip")
        }
    }

    func testThreeStylesFitInsideTheCardAndSelectingNamesThePrimary() {
        let app = launch(options: "3")
        waitForStrip(app, keys: ["A1", "A2", "B1"])
        let card = element(app, "choice-card").frame
        for key in ["A1", "A2", "B1"] {
            let frame = app.buttons["choice-option-\(key)"].frame
            XCTAssertGreaterThanOrEqual(frame.minX, card.minX - 0.5, "\(key) starts inside the card")
            XCTAssertLessThanOrEqual(frame.maxX, card.maxX + 0.5, "\(key) ends inside the card")
            XCTAssertTrue(app.buttons["choice-option-\(key)"].isHittable, "\(key) can be tapped")
        }
        let use = app.buttons["choice-use-style"]
        XCTAssertEqual(use.label, "Use Running guide", "the recommended style starts selected")
        XCTAssertTrue(app.buttons["choice-option-B1"].isSelected)
        app.buttons["choice-option-A1"].tap()
        XCTAssertEqual(use.label, "Use Editorial serif")
        XCTAssertTrue(app.buttons["choice-option-A1"].isSelected)
        XCTAssertFalse(app.buttons["choice-option-B1"].isSelected)
        XCTAssertTrue(app.buttons["answer-type-my-own"].exists)
        attach(app, "ca4-selected")
        app.terminate()
    }

    func testFiveStylesScrollTheStrip() {
        let app = launch(options: "5")
        waitForStrip(app, keys: ["A1", "A2", "A3"])
        let strip = element(app, "design-preview-strip")
        let last = app.buttons["choice-option-B2"]
        XCTAssertTrue(last.waitForExistence(timeout: 5))
        XCTAssertFalse(last.isHittable, "the fifth style starts off the card")
        attach(app, "ca4-five")
        for _ in 0..<3 where !last.isHittable { strip.swipeLeft() }
        XCTAssertTrue(last.isHittable, "swiping the strip brings the fifth style in")
        last.tap()
        XCTAssertEqual(app.buttons["choice-use-style"].label, "Use Large centred title")
        attach(app, "ca4-five-scrolled")
        app.terminate()
    }

    func testSendingShowsApplyingAndTheReceiptShowsTheStyle() {
        let app = launch(options: "3", sendDelay: 6)
        waitForStrip(app, keys: ["A1", "A2", "B1"])
        app.buttons["choice-option-A2"].tap()
        app.buttons["choice-use-style"].tap()
        let use = app.buttons["choice-use-style"]
        XCTAssertTrue(waitFor(use, "label == %@", "Applying Bold on a soft shade", timeout: 3), "the primary names what is going out")
        XCTAssertFalse(app.buttons["choice-option-A1"].isEnabled, "the other styles are out while it sends")
        XCTAssertFalse(app.buttons["choice-option-B1"].isEnabled)
        attach(app, "ca4-submitting")
        let receipt = element(app, "choice-card-answered")
        XCTAssertTrue(receipt.waitForExistence(timeout: 20))
        XCTAssertEqual(receipt.label, "You chose: Bold on a soft shade")
        XCTAssertTrue(waitFor(receipt, "value == %@", "Preview drawn over your clip"), "the receipt draws the chosen style")
        XCTAssertFalse(element(app, "design-preview-strip").exists)
        attach(app, "ca4-receipt")
        app.terminate()
    }

    func testRefusedSendShowsRedLineAndTryAgainSendsTheSameStyle() {
        let app = launch(options: "3", refuseFirst: true)
        waitForStrip(app, keys: ["A1", "A2", "B1"])
        app.buttons["choice-option-A2"].tap()
        app.buttons["choice-use-style"].tap()
        let failed = element(app, "answer-send-failed")
        XCTAssertTrue(failed.waitForExistence(timeout: 10), "a refused send shows the red line")
        let retry = app.buttons["answer-try-again"]
        XCTAssertTrue(retry.exists)
        XCTAssertFalse(app.buttons["choice-use-style"].exists, "Try again replaces the primary")
        XCTAssertTrue(app.buttons["choice-option-A2"].isSelected, "the style she picked is still selected")
        attach(app, "ca4-fail")
        retry.tap()
        let echo = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "design[A2]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 15), "Try again sends the same style")
        XCTAssertTrue(element(app, "choice-card-answered").waitForExistence(timeout: 5))
        XCTAssertFalse(failed.exists)
        app.terminate()
    }

    func testTypeMyOwnLocksTheCardAndTheComposerTakesTheAnswer() {
        let app = launch(options: "3")
        waitForStrip(app, keys: ["A1", "A2", "B1"])
        app.buttons["answer-type-my-own"].tap()
        XCTAssertTrue(waitFor(app.buttons["choice-option-A1"], "enabled == false", timeout: 5), "the tiles lock")
        XCTAssertFalse(app.buttons["choice-use-style"].isEnabled, "so does the primary")
        let composer = app.textFields["Message Kria"].exists ? app.textFields["Message Kria"] : app.textViews["Message Kria"]
        XCTAssertEqual(composer.placeholderValue, "Type your answer")
        attach(app, "ca4-type-own")
        app.terminate()
    }

    func testOptionsWithoutPreviewsKeepThePlainRowsAndTapToSend() {
        let app = launch(options: "rows")
        let first = app.buttons["choice-option-A1"]
        XCTAssertTrue(first.waitForExistence(timeout: 20))
        XCTAssertFalse(element(app, "design-preview-strip").exists, "no previews, no strip")
        XCTAssertFalse(app.buttons["choice-use-style"].exists)
        XCTAssertTrue(app.buttons["answer-let-kria-decide"].exists, "rows keep their own controls")
        attach(app, "ca4-rows")
        app.buttons["choice-option-A2"].tap()
        XCTAssertTrue(element(app, "choice-card-answered").waitForExistence(timeout: 15), "a row still answers on tap")
        XCTAssertNotEqual(element(app, "choice-card-answered").value as? String, "Preview drawn over your clip", "a row's receipt is the capsule")
        app.terminate()
    }

    func testTurkishLabelsFitTheTiles() {
        let app = launch(options: "3tr", language: "tr")
        waitForStrip(app, keys: ["A1", "A2", "B1"], language: "tr")
        let use = app.buttons["choice-use-style"]
        XCTAssertTrue(use.label.hasSuffix("kullan"), "the primary is Turkish; reads \(use.label)")
        let card = element(app, "choice-card").frame
        for key in ["A1", "A2", "B1"] {
            XCTAssertLessThanOrEqual(app.buttons["choice-option-\(key)"].frame.maxX, card.maxX + 0.5)
        }
        attach(app, "ca4-turkish")
        app.terminate()
    }
}
