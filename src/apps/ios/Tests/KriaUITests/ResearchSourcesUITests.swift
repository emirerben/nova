import XCTest

/// Exercises native display with an offline CI fixture or an explicitly prepared device draft.
/// Neither mode proves live provider behavior.
@MainActor final class ResearchSourcesUITests: XCTestCase {
    func testPreparedResearchSourcesAndPreview() throws {
        let app = XCUIApplication()
        let capturedDeviceDraft = ProcessInfo.processInfo.environment["KRIA_RESEARCH_DEVICE_UI"] == "1"
        if capturedDeviceDraft {
            app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-captured-creation"]
            app.launchEnvironment["KRIA_UI_FIXTURE_DRAFT"] = "Documents/researchqa/editor-draft.json"
            app.launchEnvironment["KRIA_UI_FIXTURE_LOCAL_SOURCES"] = "1"
        } else {
            app.launchArguments = ["-ui-testing-editor", "-ui-testing-editor-research-sources"]
        }
        app.launch()
        let export = app.buttons["native-editor-export"]
        XCTAssertTrue(export.waitForExistence(timeout: 30))
        addScreenshot(app, "Research editor on device")
        export.tap()
        let sources = app.buttons["native-editor-sources-button"]
        XCTAssertTrue(sources.waitForExistence(timeout: 5))
        sources.tap()
        XCTAssertTrue(app.navigationBars["Sources"].waitForExistence(timeout: 5))
        if capturedDeviceDraft {
            XCTAssertTrue(app.links.firstMatch.waitForExistence(timeout: 5), "Source links must be available in the native app")
        } else {
            // SwiftUI Link is exposed as a button on some iOS versions.
            XCTAssertTrue(app.descendants(matching: .any)["Museum fixture source"].firstMatch.waitForExistence(timeout: 5))
        }
        addScreenshot(app, "Research sources on device")
        app.buttons["Done"].tap()
        let play = app.buttons["native-editor-play-pause"]
        XCTAssertTrue(play.waitForExistence(timeout: 5))
        play.tap()
        expectation(for: NSPredicate(format: "label == %@", "Pause preview"), evaluatedWith: play)
        waitForExpectations(timeout: 5)
        sleep(4)
        addScreenshot(app, "Researched draft playback")
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
