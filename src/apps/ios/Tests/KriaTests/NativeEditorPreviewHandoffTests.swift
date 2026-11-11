import XCTest
@testable import Kria

final class NativeEditorPreviewHandoffTests: XCTestCase {
    private let owner = NativeEditorPreviewOwner(projectID: UUID(), lifetime: 4, itemID: "item", jobID: UUID(), variant: "v", threadID: UUID())

    func testCachedAndDeferredWorkNeedsAnExplicitLiveOwner() {
        var work = NativeEditorPreviewWorkTracker()
        let ticket = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 7)
        XCTAssertFalse(work.isPending(ticket)) // An unsettled sequence alone is not work.
        work.begin(ticket)
        XCTAssertTrue(work.isPending(ticket)) // Includes cached and deferred builds.
        XCTAssertFalse(work.isPending(.init(owner: owner, generation: "g", sequence: 8)))
        XCTAssertFalse(work.isPending(.init(owner: owner, generation: "new-generation", sequence: 7)))
        XCTAssertFalse(work.isPending(.init(owner: owner, generation: "g", sequence: 7, documentRevision: 2)))
        XCTAssertTrue(work.end(ticket))
        XCTAssertFalse(work.isPending(ticket))
    }

    func testDelayedFrameReadinessKeepsRealWorkActiveUntilAdmissionOrTermination() {
        let ticket = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 7)
        var work = NativeEditorPreviewWorkTracker()
        work.begin(ticket)
        work.waitForFrame(ticket)
        XCTAssertTrue(work.awaitingFrame)
        XCTAssertTrue(work.isPending(ticket))
        // An obsolete item's readiness/cleanup cannot finish the current candidate.
        let obsolete = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 6)
        work.waitForFrame(obsolete)
        XCTAssertFalse(work.end(obsolete))
        XCTAssertTrue(work.awaitingFrame)
        XCTAssertTrue(work.end(ticket))
        XCTAssertFalse(work.awaitingFrame)
        XCTAssertFalse(work.isPending(ticket))
    }

    func testStaleCleanupCannotStopNewerWork() {
        var work = NativeEditorPreviewWorkTracker()
        let old = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 7)
        let next = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 8)
        work.begin(old); work.begin(next)
        XCTAssertFalse(work.end(old))
        XCTAssertTrue(work.isPending(next))
        work.reset()
        XCTAssertFalse(work.end(next))
        XCTAssertFalse(work.isPending(next))
    }

    func testReloadAndTargetReplacementCannotBorrowAdmissionOrWork() {
        let shown = NativeEditorPreviewWork(owner: owner, generation: "old-generation", sequence: 6)
        var work = NativeEditorPreviewWorkTracker()
        work.begin(shown)
        var replacements = [NativeEditorPreviewOwner]()
        var changed = owner; changed.lifetime += 1; replacements.append(changed)
        changed = owner; changed.projectID = UUID(); replacements.append(changed)
        changed = owner; changed.jobID = UUID(); replacements.append(changed)
        changed = owner; changed.itemID = "other"; replacements.append(changed)
        changed = owner; changed.variant = "other"; replacements.append(changed)
        changed = owner; changed.threadID = UUID(); replacements.append(changed)
        for replacement in replacements {
            XCTAssertFalse(work.isPending(.init(owner: replacement, generation: shown.generation, sequence: shown.sequence)))
            XCTAssertFalse(NativeEditorPreviewAdmission.canRetain(shown: shown, current: replacement, readyFrameShown: true))
        }
        // A previous generation is display-only within the same target/lifetime.
        XCTAssertTrue(NativeEditorPreviewAdmission.canRetain(shown: shown, current: owner, readyFrameShown: true))
        XCTAssertFalse(NativeEditorPreviewAdmission.canRetain(shown: shown, current: owner, readyFrameShown: false))
    }

    func testFailureAndCancellationStopWorkButDoNotClaimPreviousPictureIsUpdating() {
        for result in [NativeEditorPreviousPreviewStatus.failed, .cancelled] {
            var work = NativeEditorPreviewWorkTracker()
            let ticket = NativeEditorPreviewWork(owner: owner, generation: "g", sequence: 7)
            work.begin(ticket)
            XCTAssertTrue(work.end(ticket))
            XCTAssertFalse(work.isPending(ticket))
            XCTAssertFalse(result.message.contains("Updating"))
            XCTAssertTrue(result.message.contains("previous version"))
        }
    }

}
