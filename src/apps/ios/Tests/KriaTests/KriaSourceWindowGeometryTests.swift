import XCTest
@testable import Kria

/// The Range picker's pure math (KRI-717): whatever a finger does, the window stays inside the source, never gets
/// shorter than the floor, and satisfies the server's bound (`in + length * rate <= source + 0.05`).
final class KriaSourceWindowGeometryTests: XCTestCase {
    private typealias Geometry = KriaSourceWindowGeometry
    private typealias Window = KriaSourceWindowGeometry.Window
    private let accuracy = 1e-9

    func testSlideKeepsLengthAndClampsToBothEnds() {
        let window = Window(start: 3, length: 2)
        XCTAssertEqual(Geometry.slid(window, by: 1.5, rate: 1, sourceDuration: 10), Window(start: 4.5, length: 2))
        XCTAssertEqual(Geometry.slid(window, by: -50, rate: 1, sourceDuration: 10), Window(start: 0, length: 2))
        XCTAssertEqual(Geometry.slid(window, by: 50, rate: 1, sourceDuration: 10), Window(start: 8, length: 2))
    }

    func testSlideAtRateTwoLeavesRoomForTheWholeSourceSpan() {
        // 2s of output at 2x plays 4s of source, so the latest start in a 10s file is 6.
        let slid = Geometry.slid(Window(start: 0, length: 2), by: 50, rate: 2, sourceDuration: 10)
        XCTAssertEqual(slid.start, 6, accuracy: accuracy)
        XCTAssertEqual(slid.length, 2, accuracy: accuracy)
    }

    func testSlideOfASlotLongerThanItsFootageStaysAtTheStart() {
        XCTAssertEqual(Geometry.slid(Window(start: 0, length: 12), by: 3, rate: 1, sourceDuration: 10).start, 0)
    }

    func testLeadingHandleKeepsTheSourceEnd() {
        let moved = Geometry.resizedLeading(Window(start: 3, length: 2), by: -1, rate: 1)
        XCTAssertEqual(moved.start, 2, accuracy: accuracy)
        XCTAssertEqual(Geometry.sourceEnd(moved, rate: 1), 5, accuracy: accuracy)
        XCTAssertEqual(moved.length, 3, accuracy: accuracy)
    }

    func testLeadingHandleStopsAtTheSourceStartAndTheMinimum() {
        let early = Geometry.resizedLeading(Window(start: 3, length: 2), by: -50, rate: 1)
        XCTAssertEqual(early.start, 0, accuracy: accuracy)
        XCTAssertEqual(Geometry.sourceEnd(early, rate: 1), 5, accuracy: accuracy)
        let late = Geometry.resizedLeading(Window(start: 3, length: 2), by: 50, rate: 1)
        XCTAssertEqual(late.length, Geometry.minimumLength, accuracy: accuracy)
        XCTAssertEqual(Geometry.sourceEnd(late, rate: 1), 5, accuracy: accuracy)
    }

    func testTrailingHandleKeepsTheInPointAndStopsAtTheSourceEnd() {
        let longer = Geometry.resizedTrailing(Window(start: 3, length: 2), by: 1, rate: 1, sourceDuration: 10)
        XCTAssertEqual(longer, Window(start: 3, length: 3))
        let capped = Geometry.resizedTrailing(Window(start: 3, length: 2), by: 50, rate: 1, sourceDuration: 10)
        XCTAssertEqual(capped.start, 3, accuracy: accuracy)
        XCTAssertEqual(Geometry.sourceEnd(capped, rate: 1), 10, accuracy: accuracy)
        let shortest = Geometry.resizedTrailing(Window(start: 3, length: 2), by: -50, rate: 1, sourceDuration: 10)
        XCTAssertEqual(shortest.length, Geometry.minimumLength, accuracy: accuracy)
    }

    func testTrailingHandleLetsAnOverlongSlotShrinkSmoothly() {
        // 12s of output over a 10s file already runs past the end; dragging inward must still work.
        let shrunk = Geometry.resizedTrailing(Window(start: 0, length: 12), by: -1, rate: 1, sourceDuration: 10)
        XCTAssertEqual(shrunk.length, 11, accuracy: accuracy)
    }

    func testHandleDragsAtRateTwoConvertSourceSecondsToOutputSeconds() {
        // 2s of output at 2x = source [1, 5]. Moving the end 2 source-seconds later adds 1 output-second.
        let longer = Geometry.resizedTrailing(Window(start: 1, length: 2), by: 2, rate: 2, sourceDuration: 20)
        XCTAssertEqual(longer.length, 3, accuracy: accuracy)
        XCTAssertEqual(Geometry.sourceEnd(longer, rate: 2), 7, accuracy: accuracy)
    }

    func testEveryDragKeepsTheServersBound() {
        let source = 10.0
        for rate in [0.5, 1, 2] {
            for start in stride(from: 0.0, through: 9.0, by: 1.5) {
                for length in [0.1, 1, 3] where start + length * rate <= source {
                    let base = Window(start: start, length: length)
                    for delta in [-20.0, -3, -0.07, 0, 0.07, 3, 20] {
                        for next in [
                            Geometry.slid(base, by: delta, rate: rate, sourceDuration: source),
                            Geometry.resizedLeading(base, by: delta, rate: rate),
                            Geometry.resizedTrailing(base, by: delta, rate: rate, sourceDuration: source),
                        ] {
                            XCTAssertTrue(Geometry.isWithinBounds(next, rate: rate, sourceDuration: source),
                                          "\(base) rate \(rate) delta \(delta) -> \(next)")
                        }
                    }
                }
            }
        }
    }

    func testBoundsMatchTheServerRuleIncludingItsSlack() {
        XCTAssertTrue(Geometry.isWithinBounds(Window(start: 8, length: 2.04), rate: 1, sourceDuration: 10))
        XCTAssertFalse(Geometry.isWithinBounds(Window(start: 8, length: 2.2), rate: 1, sourceDuration: 10))
        XCTAssertFalse(Geometry.isWithinBounds(Window(start: -0.1, length: 1), rate: 1, sourceDuration: 10))
        XCTAssertFalse(Geometry.isWithinBounds(Window(start: 0, length: 0.05), rate: 1, sourceDuration: 10))
    }

    func testStripMappingAndTileBuckets() {
        let span = Geometry.stripSpan(Window(start: 2.5, length: 5), rate: 1, sourceDuration: 10, width: 400)
        XCTAssertEqual(span.x, 100, accuracy: accuracy)
        XCTAssertEqual(span.width, 200, accuracy: accuracy)
        XCTAssertEqual(Geometry.tileCount(width: 336), 6)
        XCTAssertEqual(Geometry.tileCount(width: 10), 1)
        XCTAssertEqual(Geometry.tileCount(width: 10_000), 24)
        XCTAssertEqual(Geometry.sampleTimes(sourceDuration: 10, count: 4), [1.25, 3.75, 6.25, 8.75])
        XCTAssertTrue(Geometry.sampleTimes(sourceDuration: 0, count: 4).isEmpty)
    }

    func testLabel() {
        XCTAssertEqual(Geometry.label(Window(start: 3, length: 2), rate: 1), "3.0s – 5.0s · 2.0s")
    }
}
