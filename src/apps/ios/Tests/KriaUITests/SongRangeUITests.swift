import XCTest

/// KRI-580: "Where in the song did you film?" on the attached song, driven through the fixture chat transport.
/// KRIA_CHAT_SONG_RANGE=1 attaches a 90 s song and advertises song_filming_range; "off" attaches it without the flag.
@MainActor
final class SongRangeUITests: XCTestCase {
    private func launch(songRange: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ui-testing-chat"]
        app.launchEnvironment["KRIA_CHAT_CREATION_FLOW"] = "v2"
        app.launchEnvironment["KRIA_CHAT_FIXTURE_MEDIA"] = "1"
        app.launchEnvironment["KRIA_CHAT_SONG_RANGE"] = songRange
        app.launch()
        XCTAssertTrue(app.buttons["Open projects"].waitForExistence(timeout: 20))
        app.buttons["Open projects"].tap()
        let newChat = app.buttons["drawer-new-chat"]
        XCTAssertTrue(newChat.waitForExistence(timeout: 5))
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == true"), object: newChat)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 20), .completed)
        newChat.tap()
        XCTAssertTrue(app.staticTexts["What are we making?"].waitForExistence(timeout: 20))
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: app.buttons["drawer-new-chat"])
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
        app.buttons["format-montage"].tap()
        return app
    }

    /// Attachments sheet, on the song step where the attached song is listed.
    private func openSongStep(_ app: XCUIApplication) {
        let attach = app.buttons["Attach footage"]
        XCTAssertTrue(attach.waitForExistence(timeout: 10))
        attach.tap()
        XCTAssertTrue(app.buttons["attachment-next"].waitForExistence(timeout: 5))
        app.buttons["attachment-next"].tap()
        XCTAssertTrue(app.staticTexts["Add your song"].waitForExistence(timeout: 5))
    }

    private func wheel(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        let picker = app.descendants(matching: .any)[id].firstMatch
        XCTAssertTrue(picker.waitForExistence(timeout: 5), "\(id) must exist")
        let inner = picker.pickerWheels.firstMatch
        return inner.exists ? inner : app.pickerWheels[id]
    }

    private func choose(_ app: XCUIApplication, _ id: String, _ time: String) {
        wheel(app, id).adjust(toPickerWheelValue: time)
    }

    func testSettingARangeShowsItOnTheRowReopensWithItsValuesAndClearsBackToTheWholeSong() {
        let app = launch(songRange: "1")
        openSongStep(app)
        let row = app.buttons["song-range-row"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertEqual(row.label, "Where in the song did you film?")
        XCTAssertGreaterThanOrEqual(row.frame.height, 44)
        row.tap()

        XCTAssertTrue(app.staticTexts["Where in the song did you film?"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["song-range-clear"].exists, "nothing to clear yet")
        choose(app, "song-range-from", "0:40")
        choose(app, "song-range-to", "1:20")
        XCTAssertFalse(app.descendants(matching: .any)["song-range-error"].exists)
        let save = app.buttons["song-range-save"]
        XCTAssertTrue(save.isEnabled)
        save.tap()

        XCTAssertTrue(app.buttons["song-range-save"].waitForNonExistence(timeout: 10), "the sheet closes after a save")
        XCTAssertTrue(app.buttons["song-range-row"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["song-range-row"].label, "Filmed from 0:40 to 1:20")

        app.buttons["song-range-row"].tap()
        XCTAssertTrue(app.buttons["song-range-clear"].waitForExistence(timeout: 5))
        XCTAssertEqual(wheel(app, "song-range-from").value as? String, "0:40")
        XCTAssertEqual(wheel(app, "song-range-to").value as? String, "1:20")
        app.buttons["song-range-clear"].tap()

        XCTAssertTrue(app.buttons["song-range-clear"].waitForNonExistence(timeout: 10))
        XCTAssertEqual(app.buttons["song-range-row"].label, "Where in the song did you film?")
    }

    func testSaveIsOffUnderFiveSecondsAndSaysWhy() {
        let app = launch(songRange: "1")
        openSongStep(app)
        app.buttons["song-range-row"].tap()
        XCTAssertTrue(app.buttons["song-range-save"].waitForExistence(timeout: 5))
        choose(app, "song-range-from", "0:40")
        choose(app, "song-range-to", "0:43")
        XCTAssertFalse(app.buttons["song-range-save"].isEnabled)
        let reason = app.descendants(matching: .any)["song-range-error"]
        XCTAssertTrue(reason.waitForExistence(timeout: 3))
        XCTAssertEqual(reason.label, "Pick at least five seconds of your song.")
        choose(app, "song-range-to", "0:50")
        XCTAssertTrue(app.buttons["song-range-save"].isEnabled)
        XCTAssertFalse(reason.exists)
        app.buttons["song-range-cancel"].tap()
        XCTAssertTrue(app.buttons["song-range-row"].waitForExistence(timeout: 5))
        XCTAssertEqual(app.buttons["song-range-row"].label, "Where in the song did you film?", "cancel saves nothing")
    }

    func testMovingFromPastToPushesToOut() {
        let app = launch(songRange: "1")
        openSongStep(app)
        app.buttons["song-range-row"].tap()
        XCTAssertTrue(app.buttons["song-range-save"].waitForExistence(timeout: 5))
        choose(app, "song-range-to", "0:30")
        choose(app, "song-range-from", "0:50")
        XCTAssertEqual(wheel(app, "song-range-to").value as? String, "0:55")
        XCTAssertTrue(app.buttons["song-range-save"].isEnabled)
    }

    func testThereIsNoRowWhenTheServerDoesNotAdvertiseTheRange() {
        let app = launch(songRange: "off")
        openSongStep(app)
        XCTAssertTrue(app.staticTexts["fixture-song.mp3"].waitForExistence(timeout: 5), "the song is attached")
        XCTAssertFalse(app.buttons["song-range-row"].exists)
        XCTAssertFalse(app.descendants(matching: .any)["song-range-row"].exists)
    }
}
