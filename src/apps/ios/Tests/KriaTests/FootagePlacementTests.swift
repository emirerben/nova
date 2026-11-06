import XCTest
@testable import Kria

/// Item 24: which Footage clips look like screen recordings that belong in Overlays. Each test is a way the hint
/// could be wrong for the creator, not a restatement of the implementation.
final class FootagePlacementTests: XCTestCase {
    private func item(_ id: String, _ shape: FootageShape, screen: Bool = false) -> FootagePlacementItem {
        FootagePlacementItem(id: id, shape: shape, isScreenRecording: screen)
    }

    func testShapeBoundaries() {
        XCTAssertEqual(FootageShape(width: 1920, height: 1080), .landscape)
        XCTAssertEqual(FootageShape(width: 1080, height: 1920), .portrait)
        XCTAssertEqual(FootageShape(width: 1000, height: 1000), .square)
        XCTAssertEqual(FootageShape(width: 1200, height: 1000), .landscape)   // exactly 1.2x
        XCTAssertEqual(FootageShape(width: 1000, height: 1200), .portrait)
        XCTAssertEqual(FootageShape(width: 1100, height: 1000), .square)
        XCTAssertEqual(FootageShape(width: 0, height: 1000), .unknown)
        XCTAssertEqual(FootageShape(width: 1000, height: -1), .unknown)
    }

    func testScreenRecordingNames() {
        for name in ["Screen Recording 2026-11-04 at 10.12.33.mov", "ScreenRecording_11-04-2026 10-12-33_1.MP4",
                     "RPReplay_Final1730720000.MP4", "Simulator Screen Recording - iPhone 17 - 2026-11-04.mov",
                     "screen-recording.mp4"] {
            XCTAssertTrue(FootagePlacement.looksLikeScreenRecording(filename: name), name)
        }
        for name in ["Talking take.mov", "IMG_4821.MOV", "Screen test.mov", "recording.mov", ""] {
            XCTAssertFalse(FootagePlacement.looksLikeScreenRecording(filename: name), name)
        }
    }

    func testLandscapeRecordingNextToPortraitTakeMoves() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("take", .portrait), item("rec", .landscape, screen: true)]), ["rec"])
    }

    func testSingleTakeShowsNothing() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("take", .portrait)]), [])
    }

    func testSeveralPortraitTakesShowNothing() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("a", .portrait), item("b", .portrait), item("c", .portrait)]), [])
    }

    func testPortraitScreenRecordingIsCaughtByNameOnly() {
        // By shape alone it is a take; its name says otherwise, and then it needs a real take beside it.
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("take", .portrait), item("rec", .portrait, screen: true)]), ["rec"])
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("rec", .portrait, screen: true)]), [])
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("a", .portrait), item("b", .portrait)]), [])
    }

    func testEveryClipLandscapeShowsNothing() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("a", .landscape), item("b", .landscape)]), [])
        // A named recording alone, with no portrait take, also has nothing to hide.
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("a", .landscape, screen: true), item("b", .landscape)]), [])
    }

    func testLandscapeTakeNextToPortraitTakeIsFlagged() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("p", .portrait), item("l", .landscape)]), ["l"])
    }

    func testUnknownShapeWaitsForItsThumbnail() {
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("p", .portrait), item("u", .unknown)]), [])
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("u1", .unknown), item("u2", .unknown, screen: false)]), [])
        // A named recording whose thumbnail is not ready still counts once a real take is known.
        XCTAssertEqual(FootagePlacement.belongsInOverlays([item("p", .portrait), item("u", .unknown, screen: true)]), ["u"])
    }

    func testOrderFollowsInput() {
        let items = [item("l2", .landscape), item("p", .portrait), item("l1", .landscape, screen: true)]
        XCTAssertEqual(FootagePlacement.belongsInOverlays(items), ["l2", "l1"])
    }

    func testMissingPreviewFileReadsAsUnknown() {
        let missing = URL(fileURLWithPath: NSTemporaryDirectory()).appending(path: "no-such-\(UUID().uuidString).jpg")
        XCTAssertEqual(FootageShapeReader.pixelShape(of: missing), .unknown)
    }
}
