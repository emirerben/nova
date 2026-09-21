import XCTest
@testable import Kria

/// KRI-125: the rules behind "start uploading while choosing" and "show what's already chosen",
/// exercised without SwiftUI or the network.
@MainActor final class PhotoSelectionTests: XCTestCase {
    // MARK: diff

    func testDiffSplitsAddedFromRemovedAndKeepsPickerOrder() {
        let diff = PhotoSelectionDiff(current: ["c", "a", "d"], known: ["a", "b"])
        XCTAssertEqual(diff.added, ["c", "d"], "new picks start uploading in the order they were chosen")
        XCTAssertEqual(diff.removed, ["b"])
        XCTAssertFalse(diff.isEmpty)
    }

    func testDiffOfAnUnchangedSelectionIsEmpty() {
        XCTAssertTrue(PhotoSelectionDiff(current: ["a", "b"], known: ["b", "a"]).isEmpty,
                      "seeding the picker with what is already chosen must not look like a change")
    }

    // MARK: ledger reconciliation

    private func entry(_ mediaID: String?, role: CreationMediaRole = .clip) -> PhotoSelectionEntry {
        PhotoSelectionEntry(recordID: UUID(), mediaID: mediaID, role: role)
    }

    func testPreselectedIncludesInFlightAndStillAttachedButNotRemovedMedia() {
        let selection = ProjectPhotoSelection(byIdentifier: [
            "uploading": entry(nil),
            "attached": entry("media-1"),
            "removed-on-web": entry("media-2"),
        ])
        XCTAssertEqual(selection.preselected(role: .clip, attachedMediaIDs: ["media-1"]), ["attached", "uploading"],
                       "an entry whose clip is gone must stop counting, or it could never be added again")
    }

    func testPreselectedIsScopedToTheRole() {
        let selection = ProjectPhotoSelection(byIdentifier: ["clip": entry(nil, role: .clip), "visual": entry(nil, role: .visual)])
        XCTAssertEqual(selection.preselected(role: .visual, attachedMediaIDs: []), ["visual"])
    }

    func testLedgerRoundTripsThroughJSON() throws {
        let original = ProjectPhotoSelection(byIdentifier: ["asset": entry("media-1")])
        let decoded = try JSONDecoder().decode(ProjectPhotoSelection.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(decoded, original)
    }

    // MARK: admission gate

    func testGateAdmitsUpToItsLimitThenQueues() async throws {
        let gate = UploadAdmissionGate(limit: 2)
        try await gate.acquire()
        try await gate.acquire()
        XCTAssertEqual(gate.activeCount, 2)

        let third = Task { try await gate.acquire() }
        while gate.waitingCount < 1 { await Task.yield() }
        XCTAssertEqual(gate.activeCount, 2, "a third caller waits instead of running")

        gate.release()
        try await third.value
        XCTAssertEqual(gate.activeCount, 2, "the freed slot passes straight to the waiter")
        XCTAssertEqual(gate.waitingCount, 0)
    }

    func testGateWakesWaitersInArrivalOrder() async throws {
        let gate = UploadAdmissionGate(limit: 1)
        try await gate.acquire()
        let log = OrderLog()
        var tasks: [Task<Void, Error>] = []
        for index in 1...3 {
            tasks.append(Task { @MainActor in
                try await gate.acquire()
                log.values.append(index)
                gate.release()
            })
            while gate.waitingCount < index { await Task.yield() }   // enqueue them in a known order
        }

        gate.release()
        for task in tasks { try await task.value }

        XCTAssertEqual(log.values, [1, 2, 3], "uploads must start in the order the user chose them")
        XCTAssertEqual(gate.activeCount, 0)
    }

    func testCancelledWaiterLeavesTheQueueAndNeverHoldsASlot() async throws {
        let gate = UploadAdmissionGate(limit: 1)
        try await gate.acquire()
        let waiter = Task { try await gate.acquire() }
        while gate.waitingCount < 1 { await Task.yield() }

        waiter.cancel()
        do {
            try await waiter.value
            XCTFail("a cancelled waiter must throw")
        } catch { XCTAssertTrue(error is CancellationError) }

        XCTAssertEqual(gate.waitingCount, 0)
        XCTAssertEqual(gate.activeCount, 1, "the running caller's slot is untouched")
        gate.release()
        XCTAssertEqual(gate.activeCount, 0, "and nothing was leaked: the slot is genuinely free")
    }

    func testAlreadyCancelledCallerNeverTakesASlot() async {
        let gate = UploadAdmissionGate(limit: 1)
        let task = Task { try await gate.acquire() }
        task.cancel()
        _ = try? await task.value
        XCTAssertEqual(gate.activeCount, 0)
    }

    func testGateLimitIsAtLeastOne() async throws {
        let gate = UploadAdmissionGate(limit: 0)
        try await gate.acquire()
        XCTAssertEqual(gate.activeCount, 1, "a zero limit would deadlock every upload")
    }
}

/// Shared by two main-actor closures; a captured `var` would not be allowed under Swift 6.
@MainActor private final class OrderLog { var values: [Int] = [] }
