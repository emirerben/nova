import XCTest
@testable import Kria

/// KRI-282 follow-up: ordered, limit-capped selection behind the in-app Photos gallery.
final class LibraryGallerySelectionTests: XCTestCase {
    func testTapKeepsPickOrderAndNumbers() {
        var s = LibraryGallerySelection(limit: 5)
        XCTAssertEqual(s.toggle("c"), .added)
        XCTAssertEqual(s.toggle("a"), .added)
        XCTAssertEqual(s.number(of: "c"), 1)
        XCTAssertEqual(s.number(of: "a"), 2)
        XCTAssertEqual(s.toggle("c"), .removed)
        XCTAssertEqual(s.number(of: "a"), 1, "numbers renumber after a removal")
    }

    func testRefusesPastTheLimitButLimitOneSwaps() {
        var s = LibraryGallerySelection(ids: ["a", "b"], limit: 2)
        XCTAssertEqual(s.toggle("c"), .refusedAtLimit)
        XCTAssertEqual(s.ids, ["a", "b"])
        var one = LibraryGallerySelection(ids: ["a"], limit: 1)
        XCTAssertEqual(one.toggle("b"), .swapped)
        XCTAssertEqual(one.ids, ["b"])
    }

    func testSlideSelectAddsInTouchOrderUntilTheLimit() {
        var s = LibraryGallerySelection(ids: ["x"], limit: 3)
        s.apply(["a", "b", "c"], selected: true)
        XCTAssertEqual(s.ids, ["x", "a", "b"], "capped at the limit, seeded selection counts")
    }

    func testSlideDeselectRemovesOnlyTouchedAndIgnoresUntouched() {
        var s = LibraryGallerySelection(ids: ["a", "b", "c"], limit: 5)
        s.apply(["b", "z"], selected: false)
        XCTAssertEqual(s.ids, ["a", "c"])
    }

    func testReapplyingOntoSnapshotFollowsTheFinger() {
        let base = LibraryGallerySelection(ids: ["x"], limit: 10)
        var live = base
        live.apply(["a", "b", "c"], selected: true)
        XCTAssertEqual(live.ids, ["x", "a", "b", "c"])
        live = base
        live.apply(["a", "b"], selected: true)
        XCTAssertEqual(live.ids, ["x", "a", "b"], "c un-applied when the finger reversed")
    }

    func testDuplicateSeedIdsAreCollapsed() {
        XCTAssertEqual(LibraryGallerySelection(ids: ["a", "a", "b"], limit: 3).ids, ["a", "b"])
    }

    func testDurationFormatting() {
        XCTAssertEqual(LibraryGalleryGrid.duration(7.4), "0:07")
        XCTAssertEqual(LibraryGalleryGrid.duration(125), "2:05")
    }
}
