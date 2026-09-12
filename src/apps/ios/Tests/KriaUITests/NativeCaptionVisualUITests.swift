import XCTest

@MainActor
final class NativeCaptionVisualUITests: XCTestCase {
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
        let item = app.buttons["native-editor-visual-item-visualBlock-paper-media"]
        // The in-edit list follows the category controls and can scroll on compact phones.
        if !item.isHittable { app.swipeUp() }
        let media = app.buttons.matching(NSPredicate(format: "identifier CONTAINS %@", "native-editor-visual-item-")).firstMatch
        XCTAssertTrue(media.exists)
        media.tap()
        capture("15-placement")
        app.buttons["native-editor-visuals-tab-Animation"].tap()
        capture("19-animation")
        app.buttons["In visual animation Pop"].tap()
        XCTAssertTrue(app.buttons["native-editor-visuals-done"].exists)
    }
}
