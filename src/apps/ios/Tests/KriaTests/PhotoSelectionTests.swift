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

    private func entry(_ id: String, _ mediaID: String?, role: CreationMediaRole = .clip, itemID: String? = nil, boundAt: Date? = nil) -> PhotoSelectionEntry {
        PhotoSelectionEntry(assetIdentifier: id, recordID: UUID(), mediaID: mediaID, role: role,
                            itemID: ProjectPhotoSelection.scopedItemID(role: role, itemID: itemID), boundAt: boundAt)
    }

    private func ledger(_ entries: PhotoSelectionEntry...) -> ProjectPhotoSelection {
        var selection = ProjectPhotoSelection()
        for entry in entries {
            selection.entries[ProjectPhotoSelection.key(role: entry.role, itemID: entry.itemID, assetIdentifier: entry.assetIdentifier)] = entry
        }
        return selection
    }

    func testPreselectedIncludesInFlightAndStillAttachedButNotRemovedMedia() {
        let selection = ledger(entry("uploading", nil), entry("attached", "media-1"), entry("removed-on-web", "media-2"))
        XCTAssertEqual(selection.preselected(role: .clip, attachedMediaIDs: ["media-1"]), ["attached", "uploading"],
                       "an entry whose clip is gone must stop counting, or it could never be added again")
    }

    func testPreselectedIsScopedToTheRole() {
        let selection = ledger(entry("clip", nil, role: .clip), entry("visual", nil, role: .visual))
        XCTAssertEqual(selection.preselected(role: .visual, attachedMediaIDs: []), ["visual"])
    }

    /// B-roll of your own footage: the same video is a clip and a visual. Those are two choices; one
    /// must not overwrite or hide the other.
    func testTheSameAssetAsAClipAndAsAVisualAreIndependentChoices() {
        let selection = ledger(entry("shared", "clip-media", role: .clip), entry("shared", nil, role: .visual, itemID: "item-1"))
        XCTAssertEqual(selection.entries.count, 2)
        XCTAssertEqual(selection.preselected(role: .clip, attachedMediaIDs: ["clip-media"]), ["shared"])
        XCTAssertEqual(selection.preselected(role: .visual, itemID: "item-1", attachedMediaIDs: []), ["shared"])
    }

    func testVisualsAreScopedToTheirPlanItemButClipsAreNot() {
        let selection = ledger(entry("v", nil, role: .visual, itemID: "item-1"), entry("c", nil, role: .clip, itemID: "item-1"))
        XCTAssertEqual(selection.preselected(role: .visual, itemID: "item-2", attachedMediaIDs: []), [], "another item's visual pool")
        XCTAssertEqual(selection.preselected(role: .visual, itemID: "item-1", attachedMediaIDs: []), ["v"])
        XCTAssertEqual(selection.preselected(role: .clip, itemID: "anything", attachedMediaIDs: []), ["c"], "clips belong to the project")
    }

    /// Between "bound" and "visible in the attached list" (fetched separately, over the network for
    /// visuals) the picker's still-ticked asset would look newly chosen and the next tap would upload
    /// it a second time.
    func testABoundAssetKeepsCountingAsChosenBrieflyWhileTheAttachedListCatchesUp() {
        let now = Date()
        let justBound = entry("a", "media-1", boundAt: now.addingTimeInterval(-5))
        XCTAssertTrue(justBound.isLive(attachedMediaIDs: [], now: now), "attached list hasn't caught up yet")
        let longAgo = entry("a", "media-1", boundAt: now.addingTimeInterval(-(ProjectPhotoSelection.bindGrace + 1)))
        XCTAssertFalse(longAgo.isLive(attachedMediaIDs: [], now: now), "past the grace window, absence means it was removed")
        XCTAssertTrue(longAgo.isLive(attachedMediaIDs: ["media-1"], now: now), "and being attached always counts")
        XCTAssertFalse(entry("a", "media-1", boundAt: nil).isLive(attachedMediaIDs: [], now: now), "never bound recently")
    }

    func testLedgerRoundTripsThroughJSON() throws {
        let original = ledger(entry("asset", "media-1", boundAt: Date(timeIntervalSince1970: 1_700_000_000)))
        let decoded = try JSONDecoder().decode(ProjectPhotoSelection.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(decoded, original)
    }

    // MARK: display name

    func testDisplayFilenameStripsThePhotosExportPrefix() {
        let name = "\(UUID().uuidString)-IMG_0042.MOV"
        XCTAssertEqual(BackgroundUploadCoordinator.displayFilename(name), "IMG_0042.MOV")
        XCTAssertEqual(BackgroundUploadCoordinator.displayFilename("IMG_0042.MOV"), "IMG_0042.MOV")
        XCTAssertEqual(BackgroundUploadCoordinator.displayFilename("my-holiday-video.mov"), "my-holiday-video.mov", "a dash alone is not a prefix")
        let bare = UUID().uuidString
        XCTAssertEqual(BackgroundUploadCoordinator.displayFilename(bare), bare, "a bare UUID with no name after it is left alone")
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
