import XCTest

/// Uses a captured, server-compiled draft. This proves native display, not live provider behavior.
@MainActor final class ResearchSourcesUITests: XCTestCase {
    func testPreparedResearchSourcesAndPreview() throws {
        guard ProcessInfo.processInfo.environment["KRIA_RESEARCH_DEVICE_UI"] == "1" else {
            throw XCTSkip("Set KRIA_RESEARCH_DEVICE_UI=1 after copying a researched draft to the QA app")
        }
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-captured-creation"]
        app.launchEnvironment["KRIA_UI_FIXTURE_DRAFT"] = "Documents/researchqa/editor-draft.json"
        app.launchEnvironment["KRIA_UI_FIXTURE_LOCAL_SOURCES"] = "1"
        app.launch()
        let export = app.buttons["native-editor-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 30))
        addScreenshot(app, "Research editor on device")
        export.tap()
        let sources = app.buttons["native-editor-sources-button"]
        XCTAssertTrue(sources.waitForExistence(timeout: 5))
        sources.tap()
        XCTAssertTrue(app.navigationBars["Sources"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.links.firstMatch.exists, "Source links must be available in the native app")
        addScreenshot(app, "Research sources on device")
        app.buttons["Done"].tap()
        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.waitForExistence(timeout: 5))
        play.tap()
        XCTAssertEqual(play.label, "Pause preview")
        sleep(4)
        addScreenshot(app, "Car second in native preview")
        play.tap()
        app.terminate()
        app.launch()
        XCTAssertTrue(export.waitForExistence(timeout: 20))
        export.tap()
        XCTAssertTrue(sources.waitForExistence(timeout: 5))
    }

    private func addScreenshot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
