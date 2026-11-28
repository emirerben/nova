import XCTest
import UIKit
@testable import Kria

/// KRI-1419: a picked photo or video shows in the timeline and the preview at once, played from the local file,
/// while the upload and the server's admission run in the background. Each test names the failure mode it guards.
@MainActor
final class NativeInstantAddTests: XCTestCase {
    /// Holds each add's admission until the test lands or fails it, standing in for the upload plus the server probe.
    @MainActor final class AdmissionGate {
        private var waiting: [UUID: CheckedContinuation<EditorSourceRegistrationResponse, Error>] = [:]
        private(set) var started: [UUID] = []

        func admit(_ placement: PendingEditorSourcePlacement) async throws -> EditorSourceRegistrationResponse {
            started.append(placement.id)
            return try await withCheckedThrowingContinuation { waiting[placement.id] = $0 }
        }

        func isWaiting(_ id: UUID) -> Bool { waiting[id] != nil }

        func land(_ id: UUID, index: Int, sourceID: String) {
            waiting.removeValue(forKey: id)?.resume(returning: EditorSourceRegistrationResponse(importID: UUID(), status: "ready",
                sourceID: sourceID, sourceIndex: index, source: nil, error: nil, reasonCode: nil, retryable: false))
        }

        func fail(_ id: UUID) { waiting.removeValue(forKey: id)?.resume(throwing: APIError.requestFailed(status: 500)) }
    }

    private struct Fixture {
        let session: NativeEditorSession
        let uploads: BackgroundUploadCoordinator
        let spy: EditorCommitSpy
        let gate: AdmissionGate
    }

    /// One split section: Top holds a 2 s clip, Bottom is empty, 9 s long. The pool can admit indices 9 and 4.
    private func splitFixture() async throws -> Fixture {
        let (session, uploads, target, spy) = try await NativeEditorSessionTests.devicePlacementSessionWithSpy()
        var member = EditorTimelineSlot(id: "top", clipIndex: 0, inS: 0, durationS: 2, raw: ["source_duration_s": .number(4)])
        member.setSplitMembership(sectionID: "s", panelIndex: 0, offset: 0)
        var document = EditorDocument(clips: [member], capabilities: ["timeline": .init(editable: true), "split_screen": .init(editable: true)])
        document.revision.baseGeneration = target.baseGeneration
        document.splitScreenSections = [.init(id: "s", durationS: 9, panelCount: 2)]
        session.replaceDocumentForTesting(document)
        let gate = AdmissionGate()
        session.setPendingAddAdmissionForTesting { placement, _ in try await gate.admit(placement) }
        return Fixture(session: session, uploads: uploads, spy: spy, gate: gate)
    }

    /// A picked file as the Photos export hands it over: a temporary copy the app may move.
    private func pickedVideo() throws -> URL {
        let bundled = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let picked = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString)-picked.mp4")
        try FileManager.default.copyItem(at: bundled, to: picked)
        return picked
    }

    private func pendingSlots(_ session: NativeEditorSession) -> [EditorTimelineSlot] {
        session.document.clips.filter { $0.raw["pending_local"] == .bool(true) }
    }

    private func placementID(of slot: EditorTimelineSlot) throws -> UUID {
        try XCTUnwrap(slot.raw["editor_source_placement_id"]?.stringValue.flatMap(UUID.init(uuidString:)))
    }

    private func waitForAdmission(_ f: Fixture, _ id: UUID) async -> Bool {
        await poll { f.gate.isWaiting(id) }
    }

    private func poll(timeout: Duration = .seconds(30), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }

    @MainActor private final class ReadGate {
        private var continuation: CheckedContinuation<Void, Never>?
        var isWaiting: Bool { continuation != nil }
        func wait() async { await withCheckedContinuation { continuation = $0 } }
        func open() { continuation?.resume(); continuation = nil }
    }

    private func serialFixture() async throws -> Fixture {
        let f = try await splitFixture()
        var document = f.session.document
        document.clips = [EditorTimelineSlot(id: "lead", clipIndex: 0, inS: 0, durationS: 2)]
        document.splitScreenSections = []
        f.session.replaceDocumentForTesting(document)
        return f
    }

    private func pickedPhoto() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString)-picked.png")
        let image = UIGraphicsImageRenderer(size: CGSize(width: 20, height: 20)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 20, height: 20))
        }
        try XCTUnwrap(image.pngData()).write(to: url)
        return url
    }

    // Failure modes: a canceled picker changes the cut; a failed pick drops later picks; the faster upload wins
    // the first timeline position; Save sees a provisional index or forgets a later pick still being read.
    func testOrderedMixedBatchKeepsSuccessfulPicksAndSavesInSelectionOrder() async throws {
        let f = try await serialFixture()
        let before = f.session.document
        await f.session.addClips(sources: [], uploadSource: .photos)
        XCTAssertEqual(f.session.document, before)
        XCTAssertNil(f.session.addClipError)

        let photo = try pickedPhoto()
        let video = try pickedVideo()
        let latch = ReadGate()
        let adding = Task { @MainActor in
            await f.session.addClips(sources: [
                { photo },
                { throw AddClipSourceUnreadable() },
                { await latch.wait(); return video },
            ], uploadSource: .photos)
        }
        while !latch.isWaiting { await Task.yield() }
        XCTAssertTrue(f.session.isAddingClip)
        XCTAssertNil(f.session.addClipError, "A partial failure is reported after the other selections are processed")
        let first = try placementID(of: XCTUnwrap(pendingSlots(f.session).first))
        let saving = Task { await f.session.save() }
        let savingWaits = await poll { f.session.finishingUploadCount > 0 }
        XCTAssertTrue(savingWaits)
        XCTAssertEqual(f.spy.commitCount, 0)
        latch.open()
        await adding.value
        let pending = pendingSlots(f.session)
        XCTAssertEqual(pending.map { $0.raw["media_kind"] }, [.string("image"), .string("video")])
        XCTAssertNotNil(f.session.addClipError)
        let second = try placementID(of: XCTUnwrap(pending.last))
        let admissionsStarted = await poll { f.gate.isWaiting(first) && f.gate.isWaiting(second) }
        XCTAssertTrue(admissionsStarted)
        f.gate.land(second, index: 9, sourceID: "proxy")
        let secondLanded = await poll { f.session.document.clips.last?.clipIndex == 9 }
        XCTAssertTrue(secondLanded)
        XCTAssertEqual(f.spy.commitCount, 0)
        f.gate.land(first, index: 4, sourceID: "photo")
        await saving.value
        XCTAssertEqual(f.spy.commitCount, 1)
        let request = try XCTUnwrap(f.spy.lastRequest)
        let rows = try XCTUnwrap(request.timelineSlots)
        XCTAssertEqual(rows.compactMap { $0.objectValue?["clip_index"]?.numberValue }, [0, 4, 9])
        XCTAssertFalse(rows.contains { $0.objectValue?["pending_local"] != nil })
        let reopened = EditorDocument(snapshot: f.session.document.encodeSnapshot())
        XCTAssertEqual(reopened.clips.map(\.clipIndex), [0, 4, 9])
    }

    // Failure mode: the block only appears after the upload and the server probe finish (the founder's complaint).
    func testPickedVideoIsInThePanelBeforeItsUploadFinishes() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let pending = try XCTUnwrap(pendingSlots(f.session).first, "The pick is on the timeline as soon as the add returns")
        XCTAssertEqual(pending.splitSectionID, "s")
        XCTAssertEqual(pending.splitPanelIndex, 1)
        XCTAssertLessThan(pending.clipIndex, 0, "A provisional index the server never sees")
        XCTAssertFalse(f.session.isAddingClip, "The picker is released once the block is in, not after the upload")
        XCTAssertGreaterThan(try XCTUnwrap(pending.durationS), 1, "Length comes from the local file")
        XCTAssertEqual(f.session.selection?.id, pending.id)
        XCTAssertEqual(f.session.document.splitScreenSections.first?.durationS, 9, "The add fits the panel's free time")
    }

    // Failure mode: the preview cannot play the new block until the server source arrives (missing source).
    func testPendingBlockPlaysFromTheLocalFile() async throws {
        let f = try await splitFixture()
        let picked = try pickedVideo()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: picked)
        let pending = try XCTUnwrap(pendingSlots(f.session).first)
        try? FileManager.default.removeItem(at: picked)
        let played = await poll {
            f.session.displayedSourcePreviewRecipe?.tracks.filter { $0.kind == .video }.flatMap(\.clips).count == 2
        }
        XCTAssertTrue(played, "Both panels' clips are in the live preview while the upload runs")
        XCTAssertFalse(f.session.sourcePreviewState.isFailure)
        let source = try XCTUnwrap(f.session.previewSourceURLForTesting(clipIndex: pending.clipIndex))
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path), "The preview plays the editor's own copy, not the picker's temporary file")
    }

    // Failure modes: the admission inserts a second block, drops edits made while it uploaded, or adds an undo step.
    func testAdmissionSwapsTheRealIndexIntoTheSameEditedBlocksWithoutAnUndoStep() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let pending = try XCTUnwrap(pendingSlots(f.session).first)
        let id = try placementID(of: pending)
        f.session.setClipTiming(clipID: try XCTUnwrap(pending.id), durationS: 1.5)
        f.session.select(EditorSelection(kind: .clip, id: try XCTUnwrap(pending.id)), seekToStart: false)
        let block = try XCTUnwrap(f.session.timelineClips.first { $0.slotID == pending.id })
        f.session.currentTime = block.start + 0.5
        f.session.splitSelectedClip()
        XCTAssertEqual(pendingSlots(f.session).count, 2, "A cut before admission makes two pending pieces")
        let edited = f.session.document.clips
        let undoDepth = f.session.undoDepthForTesting
        let ready = await waitForAdmission(f, id)
        XCTAssertTrue(ready)
        f.gate.land(id, index: 9, sourceID: "proxy")
        let landed = await poll { self.pendingSlots(f.session).isEmpty }
        XCTAssertTrue(landed, "Both pieces take the real index")
        XCTAssertEqual(f.session.document.clips.count, edited.count, "No second block is inserted")
        for (before, after) in zip(edited, f.session.document.clips) {
            XCTAssertEqual(after.id, before.id)
            XCTAssertEqual(after.durationS, before.durationS)
            XCTAssertEqual(after.inS, before.inS)
            XCTAssertEqual(after.splitOffsetS, before.splitOffsetS)
            XCTAssertEqual(after.clipIndex, before.raw["pending_local"] == .bool(true) ? 9 : before.clipIndex)
        }
        XCTAssertEqual(f.session.undoDepthForTesting, undoDepth, "Landing is not an undo step")
        for _ in 0..<3 { f.session.undo() }
        XCTAssertFalse(f.session.document.clips.contains { $0.splitPanelIndex == 1 }, "Undo of the cut, the trim and the add returns to before the add")
        f.session.redo()
        XCTAssertEqual(f.session.document.clips.first { $0.splitPanelIndex == 1 }?.clipIndex, 9,
                       "History holds the real index too, never the provisional one")
        XCTAssertFalse(f.uploads.containsEditorPlacement(id), "The landed add released its durable intent")
    }

    // Failure modes: Save sends a provisional index (TIMELINE_UNKNOWN_CLIP) or Save runs before the source exists.
    func testSaveWaitsForThePendingUploadThenSendsTheRealIndex() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let id = try placementID(of: try XCTUnwrap(pendingSlots(f.session).first))
        let ready = await waitForAdmission(f, id)
        XCTAssertTrue(ready)
        let save = Task { await f.session.save() }
        let waiting = await poll { f.session.finishingUploadCount == 1 }
        XCTAssertTrue(waiting)
        XCTAssertEqual(NativeEditorSaveControl(isSaving: f.session.showsSavingIndicator, hasUnsavedChanges: true,
                                               finishingUploads: f.session.finishingUploadCount).title, "Finishing 1 upload…")
        try? await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(f.spy.commitCount, 0, "Nothing is sent while the add uploads")
        f.gate.land(id, index: 9, sourceID: "proxy")
        await save.value
        XCTAssertEqual(f.spy.commitCount, 1, "Save runs on its own once the upload lands")
        let body = String(decoding: try JSONEncoder().encode(XCTUnwrap(f.spy.lastRequest)), as: UTF8.self)
        XCTAssertTrue(body.contains(#""clip_index":9"#), body)
        XCTAssertFalse(body.contains(#""clip_index":-"#), "A provisional index never reaches the server")
        XCTAssertFalse(body.contains("pending_local"))
        XCTAssertEqual(f.session.finishingUploadCount, 0)
    }

    // Failure modes: a failed upload hangs Save forever or is saved anyway; Retry cannot restart it; Remove leaves the block.
    func testFailedUploadStopsSaveAndOffersRetryAndRemove() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let id = try placementID(of: try XCTUnwrap(pendingSlots(f.session).first))
        let started = await waitForAdmission(f, id)
        XCTAssertTrue(started)
        f.gate.fail(id)
        let failed = await poll { f.session.pendingEditorImports.first { $0.id == id }?.status == "failed" }
        XCTAssertTrue(failed, "The failure shows the Retry/Remove row")
        XCTAssertEqual(pendingSlots(f.session).count, 1, "The block stays so the creator decides")
        await f.session.save()
        XCTAssertEqual(f.spy.commitCount, 0, "A block without a server source is never saved")
        guard case .failed = f.session.saveState else { return XCTFail("Save explains why it stopped: \(f.session.saveState)") }

        let placement = try XCTUnwrap(f.session.pendingEditorImports.first { $0.id == id })
        await f.session.retryPendingEditorImport(placement)
        let retried = await waitForAdmission(f, id)
        XCTAssertTrue(retried, "Retry restarts the upload from the editor's copy")
        f.gate.fail(id)
        let failedAgain = await poll { f.session.pendingEditorImports.first { $0.id == id }?.status == "failed" }
        XCTAssertTrue(failedAgain)
        await f.session.dismissPendingEditorImport(try XCTUnwrap(f.session.pendingEditorImports.first { $0.id == id }))
        XCTAssertTrue(pendingSlots(f.session).isEmpty, "Remove takes the block out")
        f.session.undo()
        XCTAssertTrue(pendingSlots(f.session).isEmpty, "Undo cannot bring back a removed upload")
        XCTAssertFalse(f.uploads.containsEditorPlacement(id))
    }

    // Failure modes: undo leaves the upload running for a block that is gone; redo restores a block that never lands.
    func testUndoKeepsTheUploadForRedoAndANewEditCancelsIt() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let id = try placementID(of: try XCTUnwrap(pendingSlots(f.session).first))
        let started = await waitForAdmission(f, id)
        XCTAssertTrue(started)
        f.session.undo()
        XCTAssertTrue(pendingSlots(f.session).isEmpty)
        XCTAssertTrue(f.uploads.containsEditorPlacement(id), "Redo can still bring the block back")
        f.session.redo()
        XCTAssertEqual(pendingSlots(f.session).count, 1)
        f.gate.land(id, index: 9, sourceID: "proxy")
        let landed = await poll { self.pendingSlots(f.session).isEmpty }
        XCTAssertTrue(landed, "The redone block lands")

        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let second = try placementID(of: try XCTUnwrap(pendingSlots(f.session).first))
        let secondStarted = await waitForAdmission(f, second)
        XCTAssertTrue(secondStarted)
        f.session.undo()
        f.session.setClipTiming(clipID: "top", durationS: 1.5)
        let cancelled = await poll { !f.uploads.containsEditorPlacement(second) }
        XCTAssertTrue(cancelled, "Once no history step holds the block its upload is cancelled")
        XCTAssertEqual(f.session.finishingUploadCount, 0)
    }

    // Failure mode: two adds in one panel share a provisional index or swap into each other's block.
    func testTwoPendingAddsInOnePanelEachLandOnTheirOwnBlock() async throws {
        let f = try await splitFixture()
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        f.session.currentTime = 8.9
        await f.session.addSplitPanelClip(sectionID: "s", panel: 1, fileURL: try pickedVideo())
        let slots = pendingSlots(f.session)
        XCTAssertEqual(slots.count, 2)
        XCTAssertNotEqual(slots[0].clipIndex, slots[1].clipIndex)
        XCTAssertLessThan(slots[0].splitOffsetS, slots[1].splitOffsetS, "The second add goes after the first")
        let first = try placementID(of: slots[0]), second = try placementID(of: slots[1])
        let both = await poll { f.gate.isWaiting(first) && f.gate.isWaiting(second) }
        XCTAssertTrue(both)
        f.gate.land(second, index: 4, sourceID: "photo")
        let secondLanded = await poll { self.pendingSlots(f.session).count == 1 }
        XCTAssertTrue(secondLanded)
        f.gate.land(first, index: 9, sourceID: "proxy")
        let firstLanded = await poll { self.pendingSlots(f.session).isEmpty }
        XCTAssertTrue(firstLanded)
        XCTAssertEqual(f.session.document.clips.first { $0.id == slots[0].id }?.clipIndex, 9)
        XCTAssertEqual(f.session.document.clips.first { $0.id == slots[1].id }?.clipIndex, 4)
    }

    // Failure mode: after a relaunch the unsaved block is gone and the durable admission lands on the main timeline.
    func testRelaunchedAdmissionLandsInItsSplitPanel() async throws {
        let f = try await splitFixture()
        let target = EditorSourceRegistrationTarget(itemID: "item", variantID: "variant", clientImportID: UUID(),
            baseGeneration: f.session.document.revision.baseGeneration, guidedRevisionNumber: 7, sourceKind: .footage)
        let placement = PendingEditorSourcePlacement(id: UUID(), target: target, lane: .timeline, visual: nil, localDurationS: 4,
            splitPanel: .init(sectionID: "s", panel: 1, offset: 0))
        f.uploads.beginEditorPlacement(placement)
        let restored = try JSONDecoder().decode(PendingEditorSourcePlacement.self, from: JSONEncoder().encode(placement))
        try await f.session.placeEditorSource(restored, response: .init(importID: target.clientImportID, status: "ready",
            sourceID: "proxy", sourceIndex: 9, source: nil, error: nil, reasonCode: nil, retryable: false))
        let added = try XCTUnwrap(f.session.document.clips.first { $0.id == placement.id.uuidString })
        XCTAssertEqual(added.splitSectionID, "s", "The persisted intent names its panel")
        XCTAssertEqual(added.splitPanelIndex, 1)
    }
}
