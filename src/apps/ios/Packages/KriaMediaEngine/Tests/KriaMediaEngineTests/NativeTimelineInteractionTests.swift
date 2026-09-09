import Foundation
import XCTest
@testable import KriaMediaEngine

final class NativeTimelineInteractionTests: XCTestCase {
    func testPixelAndTimeMappingIsOneToOneAndClamped() {
        XCTAssertEqual(TimelineMath.time(forPixelX: 0, contentWidth: 800, duration: 20), 0)
        XCTAssertEqual(TimelineMath.time(forPixelX: 400, contentWidth: 800, duration: 20), 10)
        XCTAssertEqual(TimelineMath.time(forPixelX: -30, contentWidth: 800, duration: 20), 0)
        XCTAssertEqual(TimelineMath.time(forPixelX: 900, contentWidth: 800, duration: 20), 20)
        XCTAssertEqual(TimelineMath.pixelX(forTime: 10, contentWidth: 800, duration: 20), 400)
        XCTAssertEqual(TimelineMath.pixelX(forTime: -1, contentWidth: 800, duration: 20), 0)
        XCTAssertEqual(TimelineMath.pixelX(forTime: 99, contentWidth: 800, duration: 20), 800)
    }

    func testMappingHandlesEmptyOrInvalidGeometrySafely() {
        XCTAssertEqual(TimelineMath.time(forPixelX: 10, contentWidth: 0, duration: 20), 0)
        XCTAssertEqual(TimelineMath.time(forPixelX: 10, contentWidth: 800, duration: 0), 0)
        XCTAssertEqual(TimelineMath.time(forPixelX: .infinity, contentWidth: 800, duration: 20), 0)
        XCTAssertEqual(TimelineMath.pixelX(forTime: 10, contentWidth: 0, duration: 20), 0)
        XCTAssertEqual(TimelineMath.pixelX(forTime: 10, contentWidth: 800, duration: 0), 0)
    }

    func testClipLookupPrefersLaterClipAtSharedBoundary() {
        let clips = [
            TimelineClip(id: "a", sourceAssetID: "asset-a", sourceDuration: 4, timelineStart: 0),
            TimelineClip(id: "b", sourceAssetID: "asset-b", sourceDuration: 6, timelineStart: 4)
        ]
        XCTAssertEqual(TimelineMath.clip(atTimelineTime: 2, in: clips)?.id, "a")
        XCTAssertEqual(TimelineMath.clip(atTimelineTime: 4, in: clips)?.id, "b")
        XCTAssertEqual(TimelineMath.clip(atTimelineTime: 10, in: clips)?.id, "b")
        XCTAssertNil(TimelineMath.clip(atTimelineTime: 10.01, in: clips))
        XCTAssertNil(TimelineMath.clip(atTimelineTime: .nan, in: clips))
    }

    func testTrimEdgeMappingPreservesMinimumDurationAndClamps() {
        XCTAssertEqual(
            TimelineMath.clampedTrimTime(2, edge: .leading, clipStart: 4, clipEnd: 10),
            4
        )
        XCTAssertEqual(
            TimelineMath.clampedTrimTime(99, edge: .leading, clipStart: 4, clipEnd: 10),
            9.9,
            accuracy: 0.0001
        )
        XCTAssertEqual(
            TimelineMath.clampedTrimTime(1, edge: .trailing, clipStart: 4, clipEnd: 10),
            4.1,
            accuracy: 0.0001
        )
        XCTAssertEqual(
            TimelineMath.clampedTrimTime(99, edge: .trailing, clipStart: 4, clipEnd: 10),
            10
        )
        XCTAssertEqual(
            TimelineMath.clampedTrimTime(0, edge: .leading, clipStart: 2, clipEnd: 2.05, minimumDuration: 0.1),
            2
        )
    }
}
