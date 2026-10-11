import XCTest
@testable import Kria

/// KRI-575: the fixed-playhead song timeline. These pin the ways its numbers can go wrong: the playhead running
/// off either end of the song, "next" getting stuck on the clip it is already on, two clips sharing a start giving
/// two stops, a bad number poisoning the clock, and a lifted clip drifting away from the finger while the timeline
/// auto-scrolls under it.
@MainActor final class SongTimelineMathTests: XCTestCase {
    private func segment(_ id: String, _ start: Double, _ end: Double, hidden: Bool = false) -> SongTimelineState.Segment {
        .init(mediaID: id, start: start, end: end, hidden: hidden)
    }

    // MARK: clock

    func testSeekNeverLeavesTheSong() {
        let clock = SongTimelineClock(duration: 30)
        clock.seek(to: -4); XCTAssertEqual(clock.currentTime, 0)
        clock.seek(to: 999); XCTAssertEqual(clock.currentTime, 30)
        clock.seek(to: .nan); XCTAssertEqual(clock.currentTime, 0, "not a number is the start, never a poisoned clock")
        clock.seek(to: 12.5); XCTAssertEqual(clock.currentTime, 12.5)
    }

    func testShorteningTheSongPullsThePlayheadBackInside() {
        let clock = SongTimelineClock(duration: 90, time: 80)
        clock.setDuration(60)
        XCTAssertEqual(clock.currentTime, 60)
        XCTAssertEqual(SongTimelineClock(duration: 20, time: 50).currentTime, 20, "the starting time is clamped too")
    }

    func testZoomStaysInRangeAndYieldsToTheCreatorOnceChosen() {
        let clock = SongTimelineClock(duration: 60)
        clock.fit(viewportWidth: 360)
        XCTAssertEqual(clock.pixelsPerSecond, 30, accuracy: 0.001, "12 s across the viewport")
        for _ in 0..<20 { clock.zoom(by: 1.4) }
        XCTAssertEqual(clock.pixelsPerSecond, SongTimelineMath.maxPixelsPerSecond)
        for _ in 0..<40 { clock.zoom(by: 1 / 1.4) }
        XCTAssertEqual(clock.pixelsPerSecond, SongTimelineMath.minPixelsPerSecond)
        clock.fit(viewportWidth: 600)
        XCTAssertEqual(clock.pixelsPerSecond, SongTimelineMath.minPixelsPerSecond, "a rotation or relayout must not undo the creator's zoom")
        clock.setPixelsPerSecond(.nan)
        XCTAssertEqual(clock.pixelsPerSecond, SongTimelineMath.minPixelsPerSecond)
    }

    func testDefaultZoomFitsAboutTwelveSecondsAndNeverLeavesTheRange() {
        XCTAssertEqual(SongTimelineMath.defaultPixelsPerSecond(viewportWidth: 360), 30, accuracy: 0.001)
        XCTAssertEqual(SongTimelineMath.defaultPixelsPerSecond(viewportWidth: 20), SongTimelineMath.minPixelsPerSecond)
        XCTAssertEqual(SongTimelineMath.defaultPixelsPerSecond(viewportWidth: 5000), SongTimelineMath.maxPixelsPerSecond)
        XCTAssertTrue(SongTimelineMath.defaultPixelsPerSecond(viewportWidth: 0).isFinite, "an unmeasured viewport still gives a usable zoom")
    }

    // MARK: stops

    func testNextAndPreviousStopAtTheEndsOfTheSongInsteadOfWrapping() {
        let stops = SongTimelineMath.stops(segments: [segment("a", 4, 12), segment("b", 22, 30)], gaps: [.init(index: 0, start: 12, end: 22)])
        XCTAssertEqual(stops.map(\.time), [4, 12, 22])
        XCTAssertNil(SongTimelineMath.previousStop(before: 4, in: stops), "nothing before the first stop")
        XCTAssertNil(SongTimelineMath.nextStop(after: 22, in: stops), "nothing after the last stop")
        XCTAssertNil(SongTimelineMath.nextStop(after: 40, in: []))
    }

    func testRepeatedNextAlwaysProgressesEvenAtAStopOrJustShortOfIt() {
        let stops = SongTimelineMath.stops(segments: [segment("a", 4, 12), segment("b", 22, 30)], gaps: [.init(index: 0, start: 12, end: 22)])
        var time = 0.0
        var visited: [Double] = []
        while let next = SongTimelineMath.nextStop(after: time, in: stops) { visited.append(next.time); time = next.time }
        XCTAssertEqual(visited, [4, 12, 22])
        XCTAssertEqual(SongTimelineMath.nextStop(after: 11.97, in: stops)?.time, 22, "a playhead within 0.05 s of a stop is already on it")
        XCTAssertEqual(SongTimelineMath.previousStop(before: 12.03, in: stops)?.time, 4)
        XCTAssertEqual(SongTimelineMath.previousStop(before: 12.5, in: stops)?.time, 12)
    }

    func testClipsSharingAStartGiveOneStopAndTheVisibleOneWins() {
        let stops = SongTimelineMath.stops(
            segments: [segment("hidden", 5, 9, hidden: true), segment("shown", 5, 15), segment("later", 20, 25)], gaps: [])
        XCTAssertEqual(stops.map(\.time), [5, 20])
        XCTAssertEqual(stops.first?.kind, .block("shown"))
    }

    func testAnEmptySpotThatStartsWhereAClipDoesDoesNotDoubleUp() {
        let stops = SongTimelineMath.stops(segments: [segment("a", 0, 10)], gaps: [.init(index: 0, start: 0, end: 5)])
        XCTAssertEqual(stops.count, 1)
        XCTAssertEqual(stops.first?.kind, .block("a"))
    }

    func testPlayheadStartsOnTheFirstVisibleClip() {
        XCTAssertEqual(SongTimelineMath.initialTime(segments: [segment("h", 3, 6, hidden: true), segment("a", 7, 12)]), 7)
        XCTAssertEqual(SongTimelineMath.initialTime(segments: [segment("h", 3, 6, hidden: true)]), 3, "only hidden clips: still start on content")
        XCTAssertEqual(SongTimelineMath.initialTime(segments: []), 0, "nothing placed: the top of the song")
    }

    // MARK: lift

    func testALiftedClipStaysUnderTheFingerWhileTheTimelineScrolls() {
        // Picked up at 10 s, finger moved 60 pt at 30 pt/s (+2 s), timeline auto-scrolled 3 s: 15 s.
        XCTAssertEqual(SongTimelineMath.liftedDelta(startDelta: 10, fingerTravel: 60, pixelsPerSecond: 30, clockShift: 3), 15, accuracy: 1e-9)
        XCTAssertEqual(SongTimelineMath.liftedDelta(startDelta: 10, fingerTravel: 0, pixelsPerSecond: 30, clockShift: 0), 10)
        XCTAssertEqual(SongTimelineMath.liftedDelta(startDelta: 10, fingerTravel: -300, pixelsPerSecond: 30, clockShift: 0), 0, accuracy: 1e-9)
    }

    func testALiftSurvivesBadInput() {
        XCTAssertEqual(SongTimelineMath.liftedDelta(startDelta: 10, fingerTravel: .nan, pixelsPerSecond: 30, clockShift: 2), 12)
        XCTAssertEqual(SongTimelineMath.liftedDelta(startDelta: 10, fingerTravel: 50, pixelsPerSecond: 0, clockShift: 0), 10, "no division by a zero zoom")
    }

    // MARK: readout and ruler

    func testTimecodeRoundsToTenthsWithoutShowingSixtySeconds() {
        XCTAssertEqual(SongTimelineMath.timecode(0), "0:00.0")
        XCTAssertEqual(SongTimelineMath.timecode(4.5), "0:04.5")
        XCTAssertEqual(SongTimelineMath.timecode(59.96), "1:00.0")
        XCTAssertEqual(SongTimelineMath.timecode(125.04), "2:05.0")
        XCTAssertEqual(SongTimelineMath.timecode(-3), "0:00.0")
        XCTAssertEqual(SongTimelineMath.timecode(.infinity), "0:00.0")
    }

    func testRulerLabelsNeverCrowdAtAnyZoom() {
        for pps in stride(from: SongTimelineMath.minPixelsPerSecond, through: SongTimelineMath.maxPixelsPerSecond, by: 3) {
            let step = SongTimelineMath.tickStep(pixelsPerSecond: pps)
            XCTAssertGreaterThanOrEqual(step * Double(pps), 49.9, "labels at \(pps) pt/s are \(step * Double(pps)) pt apart")
        }
        XCTAssertEqual(SongTimelineMath.tickStep(pixelsPerSecond: 60), 1)
        XCTAssertEqual(SongTimelineMath.tickStep(pixelsPerSecond: 6), 10)
    }
}
