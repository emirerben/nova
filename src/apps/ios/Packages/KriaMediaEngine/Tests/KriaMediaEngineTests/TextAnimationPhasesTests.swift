import XCTest
@testable import KriaMediaEngine

final class TextAnimationPhasesTests: XCTestCase {
    func testIndependentEdgesAndSpeedKeepTheItemWindow() throws {
        let phases = TextAnimationPhases(entrance: .fade, exit: .slide, loop: .pulse, speed: 2)
        let start = try phases.sample(time: 0.1, duration: 2)
        let end = try phases.sample(time: 1.9, duration: 2)
        XCTAssertEqual(start.alpha, 0.875, accuracy: 0.000001)
        XCTAssertEqual(start.xTranslate, 0)
        XCTAssertEqual(end.xTranslate, 5, accuracy: 0.000001)
        XCTAssertEqual(end.alpha, 0.875, accuracy: 0.000001)
        XCTAssertEqual(try phases.sample(time: 2, duration: 2).alpha, 0)
        XCTAssertEqual(try phases.sample(time: 1, duration: 2).alpha, 1)
    }

    func testBackwardSeekAndGraphemeReveal() throws {
        let phases = TextAnimationPhases(entrance: .typewriter, exit: .typewriter)
        let sample = try phases.sample(time: 0.05, duration: 0.2)
        _ = try phases.sample(time: 0.18, duration: 0.2)
        XCTAssertEqual(try phases.sample(time: 0.05, duration: 0.2), sample)
        XCTAssertEqual(sample.revealProgress, 0.5, accuracy: 0.000001)
        XCTAssertEqual(TextAnimationPhases.visiblePrefix("A👨‍👩‍👧‍👦BC", progress: 0.5), "A👨‍👩‍👧‍👦")
        XCTAssertEqual(try phases.sample(time: 0.1, duration: 0.2).revealProgress, 1)
    }
}
