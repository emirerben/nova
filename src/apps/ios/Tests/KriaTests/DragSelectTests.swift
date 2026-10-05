import XCTest
@testable import Kria

/// KRI-282 follow-up: slide-to-select hit-testing and range logic (pure, no UI).
final class DragSelectTests: XCTestCase {
    /// 3 columns x 3 rows of 100x160 tiles with 8pt gaps; index = row * 3 + column.
    private let frames: [Int: CGRect] = Dictionary(uniqueKeysWithValues: (0..<9).map { i in
        (i, CGRect(x: CGFloat(i % 3) * 108, y: CGFloat(i / 3) * 168, width: 100, height: 160))
    })

    private func center(_ i: Int) -> CGPoint { CGPoint(x: frames[i]!.midX, y: frames[i]!.midY) }

    func testIndexAtPointFindsTileAndMissesGaps() {
        XCTAssertEqual(DragSelect.index(at: center(4), in: frames), 4)
        XCTAssertNil(DragSelect.index(at: CGPoint(x: 104, y: 50), in: frames), "the gutter belongs to no tile")
        XCTAssertNil(DragSelect.index(at: CGPoint(x: -5, y: 5), in: frames))
    }

    func testSlideAcrossARowTouchesTilesInOrder() {
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [center(0), center(2)]), [0, 1, 2])
    }

    func testFastFlickBetweenSparseSamplesDoesNotSkipTiles() {
        let path = [CGPoint(x: 10, y: 80), CGPoint(x: 290, y: 80)]
        XCTAssertEqual(DragSelect.touched(frames: frames, path: path), [0, 1, 2])
    }

    func testRangeFollowsTheFingerWhenReversing() {
        // out to tile 2 then back to tile 1: tile 2 is un-applied.
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [center(0), center(2), center(1)]), [0, 1])
        // all the way back to the first tile leaves only it.
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [center(0), center(2), center(0)]), [0])
    }

    func testReversingThenReenteringReappliesTiles() {
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [center(0), center(2), center(1), center(2)]), [0, 1, 2])
    }

    func testDraggingDownAColumnAndAcrossRows() {
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [center(0), center(6)]), [0, 3, 6])
    }

    func testPathThatStartsOffTileTouchesNothing() {
        XCTAssertEqual(DragSelect.touched(frames: frames, path: [CGPoint(x: 104, y: 50), center(2)]), [])
    }

    func testTrailSurvivesFramesChangingUnderTheFinger() {
        // Auto-scroll: the same finger point, but the grid has moved up one row.
        var trail = DragSelect.Trail()
        trail.start(at: CGPoint(x: 50, y: 250), tile: 3)
        let scrolled = frames.mapValues { $0.offsetBy(dx: 0, dy: -168) }
        trail.advance(to: CGPoint(x: 50, y: 250), in: scrolled)
        XCTAssertEqual(trail.indices, [3, 6])
    }

    func testModeIsDecidedByTheFirstTile() {
        XCTAssertEqual(DragSelect.Mode.forFirstTile(isSelected: false), .select)
        XCTAssertEqual(DragSelect.Mode.forFirstTile(isSelected: true), .deselect)
        XCTAssertTrue(DragSelect.Mode.select.targetsSelected)
        XCTAssertFalse(DragSelect.Mode.deselect.targetsSelected)
    }

    func testDirectionSplitsSelectionFromScroll() {
        XCTAssertTrue(DragSelect.isHorizontalIntent(translation: CGSize(width: 20, height: 4)))
        XCTAssertTrue(DragSelect.isHorizontalIntent(translation: CGSize(width: -20, height: 6)))
        XCTAssertFalse(DragSelect.isHorizontalIntent(translation: CGSize(width: 3, height: 20)))
    }

    func testAutoScrollOnlyNearEdgesAndRamps() {
        XCTAssertEqual(DragSelect.autoScrollStep(fingerY: 400, viewportHeight: 800), 0)
        XCTAssertLessThan(DragSelect.autoScrollStep(fingerY: 20, viewportHeight: 800), 0)
        XCTAssertGreaterThan(DragSelect.autoScrollStep(fingerY: 790, viewportHeight: 800), 0)
        XCTAssertGreaterThan(DragSelect.autoScrollStep(fingerY: 790, viewportHeight: 800), DragSelect.autoScrollStep(fingerY: 740, viewportHeight: 800))
        XCTAssertEqual(DragSelect.autoScrollStep(fingerY: 900, viewportHeight: 800), 14, "capped past the edge")
    }

    // MARK: state application

    private func state() throws -> ClipSelectionState {
        let payload: [String: JSONValue] = ["clip_question": .object([
            "version": .number(1), "question_id": .string("q1"), "allow_none": .bool(true),
            "categories": .array([
                .object(["key": .string("a"), "label": .string("A"), "op": .string("group"),
                         "candidate_media_ids": .array(["m0", "m1", "m2", "m3"].map(JSONValue.string)),
                         "suggested_media_ids": .array([.string("m1")])]),
                .object(["key": .string("b"), "label": .string("B"), "op": .string("group"),
                         "candidate_media_ids": .array(["m2", "m3"].map(JSONValue.string)),
                         "suggested_media_ids": .array([])]),
            ]),
        ])]
        return ClipSelectionState(question: try XCTUnwrap(ClipQuestion.parse(payload: payload)))
    }

    func testSetSelectsAndDeselectsOnlyTheGivenCandidatesInOneCategory() throws {
        var s = try state()
        s.set(["m2", "m3", "zzz"], selected: true, in: "a")
        XCTAssertEqual(s.count(in: "a"), 3, "m1 was pre-ticked (suggested); unknown ids ignored")
        XCTAssertEqual(s.count(in: "b"), 0, "other category untouched")
        s.set(["m1", "m2"], selected: false, in: "a")
        XCTAssertFalse(s.isSelected("m1", in: "a"))
        XCTAssertTrue(s.isSelected("m3", in: "a"))
    }

    func testSelectingClearsNoneAndPayloadKeepsCandidateOrder() throws {
        var s = try state()
        s.toggleNone("b")
        s.set(["m3", "m2"], selected: true, in: "b")
        XCTAssertFalse(s.isNone("b"))
        XCTAssertEqual(s.submission.answers.first { $0.key == "b" }?.mediaIDs, ["m2", "m3"])
    }

    func testReapplyingOntoSnapshotUnappliesReversedTiles() throws {
        let base = try state()
        var live = base
        live.set(["m0", "m2"], selected: true, in: "a")
        XCTAssertEqual(live.count(in: "a"), 3)
        live = base
        live.set(["m0"], selected: true, in: "a")
        XCTAssertEqual(live.count(in: "a"), 2, "m2 un-applied when the finger reversed")
    }
}
