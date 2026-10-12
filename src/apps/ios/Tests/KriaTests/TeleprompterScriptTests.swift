import XCTest
@testable import Kria
import UIKit

@MainActor
final class TeleprompterScriptTests: XCTestCase {
    func testPaceChangeContinuesFromCurrentPositionAndPauseFreezes() {
        var now: TimeInterval = 0
        let model = TeleprompterScript(text: "one two three four", wordsPerMinute: 120, uptime: { now })
        model.setLayout(lines: [line(0..<3, 0), line(4..<7, 100)], travelDistance: 100)
        model.resume()
        now = 0.5; model.tick()
        let beforeChange = model.scrollPosition
        XCTAssertGreaterThan(beforeChange, 0)

        model.setWordsPerMinute(240)
        XCTAssertEqual(model.scrollPosition, beforeChange, accuracy: 0.001)
        now = 0.75; model.tick()
        XCTAssertGreaterThan(model.scrollPosition, beforeChange)

        model.pause()
        let paused = model.scrollPosition
        now = 8; model.tick()
        XCTAssertEqual(model.scrollPosition, paused, accuracy: 0.001)
        XCTAssertFalse(model.isPlaying)
    }

    func testFinalLineEndsTextPlaybackAtLastAnchor() {
        var now: TimeInterval = 0
        let model = TeleprompterScript(text: "one two", wordsPerMinute: 240, uptime: { now })
        model.setLayout(lines: [line(0..<3, 0), line(4..<7, 100)], travelDistance: 100)
        model.resume()
        now = 10; model.tick()
        XCTAssertTrue(model.hasEnded)
        XCTAssertFalse(model.isPlaying)
        XCTAssertEqual(model.scrollPosition, 100, accuracy: 0.001)
        XCTAssertEqual(model.anchorUTF16, 4)
    }

    func testReflowKeepsCharacterAnchorAndManualLineMovementPauses() {
        let model = TeleprompterScript(text: "one two three", uptime: { 0 })
        model.setLayout(lines: [line(0..<3, 0), line(4..<7, 40), line(8..<13, 80)], travelDistance: 80)
        model.resume()
        model.nextLine()
        XCTAssertEqual(model.anchorUTF16, 4)
        XCTAssertFalse(model.isPlaying)

        model.setLayout(lines: [line(0..<3, 0), line(4..<13, 60)], travelDistance: 60)
        XCTAssertEqual(model.anchorUTF16, 4)
        XCTAssertEqual(model.scrollPosition, 60, accuracy: 0.001)
        model.backLine()
        XCTAssertEqual(model.anchorUTF16, 0)
    }

    func testUnicodeWhitespaceDefinesReadingUnits() {
        XCTAssertEqual(TeleprompterScript.readingUnitCount(in: "Merhaba\tworld\ncan’t-stop  12:30"), 4)
    }

    func testDynamicTypeLayoutUsesTheSameScaledInterFontForAnchors() {
        let regular = TeleprompterTextLayout.scaledInterFont(size: 22, dynamicTypeSize: .large)
        let accessible = TeleprompterTextLayout.scaledInterFont(size: 22, dynamicTypeSize: .accessibility3)
        XCTAssertGreaterThan(accessible.pointSize, regular.pointSize)

        let lines = TeleprompterTextLayout.lines(text: "one two three four five", width: 80, font: accessible)
        XCTAssertGreaterThan(lines.count, 1)
        XCTAssertEqual(lines[1].anchor, accessible.lineHeight, accuracy: 0.001)
    }

    func testSharedUTF16BoundaryBelongsToFollowingVisualLine() {
        let model = TeleprompterScript(text: "one two three", uptime: { 0 })
        model.setLayout(lines: [line(0..<3, 0), line(3..<7, 30), line(7..<13, 60)], travelDistance: 60)
        model.draft = .init(text: "one two three", revision: model.revision, anchorUTF16: 3)
        model.setLayout(lines: [line(0..<3, 0), line(3..<7, 30), line(7..<13, 60)], travelDistance: 60)

        model.nextLine()
        XCTAssertEqual(model.anchorUTF16, 7, "next must advance from the line beginning at the shared boundary")
        model.backLine()
        XCTAssertEqual(model.anchorUTF16, 3)
    }

    func testSingleLineCanResumeAfterItsPlannedDuration() {
        var now: TimeInterval = 0
        let model = TeleprompterScript(text: "one", wordsPerMinute: 240, uptime: { now })
        model.setLayout(lines: [line(0..<3, 0)], travelDistance: 0)
        model.resume()
        now = 1; model.tick()
        XCTAssertTrue(model.hasEnded)

        model.resume()
        now = 1.1; model.tick()
        XCTAssertTrue(model.isPlaying)
        now = 2; model.tick()
        XCTAssertTrue(model.hasEnded)
    }

    func testPauseAtSingleLineEndStillAllowsAFullReplay() {
        var now: TimeInterval = 0
        let model = TeleprompterScript(text: "one", wordsPerMinute: 240, uptime: { now })
        model.setLayout(lines: [line(0..<3, 0)], travelDistance: 0)
        model.resume()
        now = 1; model.tick()
        model.pause() // Countdown pauses rehearsal before the capture acknowledgment.
        model.resume()
        now = 1.1; model.tick()
        XCTAssertTrue(model.isPlaying)
        now = 2; model.tick()
        XCTAssertTrue(model.hasEnded)
    }

    private func line(_ range: Range<Int>, _ anchor: CGFloat) -> TeleprompterVisualLine {
        .init(range: range, anchor: anchor, text: "line")
    }
}
