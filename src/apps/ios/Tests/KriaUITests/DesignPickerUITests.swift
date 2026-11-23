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

    private func launch() -> XCUIApplication {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI1582DesignPicker.json").path
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat", "-perf-reset"]
        app.launchEnvironment = [
            "KRIA_CHAT_CREATION_FLOW": "v2", "KRIA_CHAT_FIXTURE_MEDIA": "1", "KRIA_EDITOR_FIXTURE": "1",
            "KRIA_CHAT_DESIGN_PICKER": "1", "KRIA_CHAT_DESIGN_PICKER_FIXTURE": fixture, "UI_TEST_REDUCE_MOTION": "1",
        ]
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor(newChat, "enabled == true", timeout: 20))
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What are we making?"].waitForExistence(timeout: 20))
        app.buttons["format-montage"].tap()
        let send = app.buttons["Send clips"]
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
        XCTAssertTrue(app.buttons["answer-let-kria-decide"].exists)
        attach(app, "Design picker: four designs drawn on the phone")

        app.buttons["choice-option-A2"].tap()
        let echo = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "design[A2] question[design-q-fixture]")).firstMatch
        XCTAssertTrue(echo.waitForExistence(timeout: 15), "the tap sends the structured answer for A2")
        XCTAssertTrue(element(app, "choice-card-answered").waitForExistence(timeout: 5), "the card closes on her choice")

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
}
