import XCTest
@testable import Kria

// Failure modes first: tap routing, reorder index math (incl. auto-scroll edges and cover identity),
// and the "new media joins the post by itself" rules (ordering, dedupe, limits, races, undo).

// MARK: Tap routing

final class SlidePostTextTapTests: XCTestCase {
    func testEmptyCanvasInBrowseDoesNothing() {
        XCTAssertNil(SlidePostTextTap.resolve(hit: nil, panelOpen: false, selectedID: nil, onEditTab: true, keyboardUp: false))
    }

    func testEmptyCanvasWhileEditingDeselects() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: nil, panelOpen: true, selectedID: "a", onEditTab: false, keyboardUp: false))
        XCTAssertNil(outcome.selectID)
        XCTAssertFalse(outcome.focusesField)
        XCTAssertFalse(outcome.opensPanel)
    }

    func testTapInBrowseSelectsOpensEditTabAndFocuses() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "a", panelOpen: false, selectedID: nil, onEditTab: false, keyboardUp: false))
        XCTAssertEqual(outcome, .init(selectID: "a", opensPanel: true, showsEditTab: true, focusesField: true))
    }

    func testTapWhilePanelOpenOnAnotherTextSwitchesAndFocusesWithoutReopening() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "b", panelOpen: true, selectedID: "a", onEditTab: true, keyboardUp: true))
        XCTAssertEqual(outcome, .init(selectID: "b", opensPanel: false, showsEditTab: true, focusesField: true))
    }

    func testTapOnStyleTabSwitchesToEditAndFocuses() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "a", panelOpen: true, selectedID: "a", onEditTab: false, keyboardUp: false))
        XCTAssertTrue(outcome.showsEditTab); XCTAssertTrue(outcome.focusesField)
    }

    func testTapOnTheTextAlreadyBeingTypedInLeavesTheFieldAlone() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "a", panelOpen: true, selectedID: "a", onEditTab: true, keyboardUp: true))
        XCTAssertFalse(outcome.focusesField, "no keyboard flicker")
    }

    func testTapAfterTheKeyboardWasDismissedBringsItBack() throws {
        let outcome = try XCTUnwrap(SlidePostTextTap.resolve(hit: "a", panelOpen: true, selectedID: "a", onEditTab: true, keyboardUp: false))
        XCTAssertTrue(outcome.focusesField)
    }
}

// MARK: Reorder math

final class SlidePostReorderMathTests: XCTestCase {
    private let pitch: CGFloat = 66

    func testTargetIndexRoundsToTheNearestSlotAndClamps() {
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: 0, pitch: pitch, count: 6), 2)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: 32, pitch: pitch, count: 6), 2, "just under half a slot stays")
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: 34, pitch: pitch, count: 6), 3)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: -100, pitch: pitch, count: 6), 0, "-1.5 slots rounds away from zero, then clamps")
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: 100_000, pitch: pitch, count: 6), 5)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 2, delta: -100_000, pitch: pitch, count: 6), 0)
    }

    func testTargetIndexSurvivesDegenerateInput() {
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 0, delta: 500, pitch: pitch, count: 1), 0, "a lone slide cannot move")
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 0, delta: 500, pitch: pitch, count: 0), 0)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 3, delta: 500, pitch: 0, count: 5), 0)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 1, delta: .greatestFiniteMagnitude, pitch: pitch, count: 4), 3)
    }

    func testAutoScrollOnlyNearTheEdgesAndRampsUp() {
        func v(_ x: CGFloat, content: CGFloat = 1000) -> CGFloat { SlidePostReorderMath.autoScrollVelocity(fingerX: x, viewport: 390, content: content) }
        XCTAssertEqual(v(195), 0, "the middle never scrolls")
        XCTAssertEqual(v(60), 0)
        XCTAssertEqual(v(330), 0)
        XCTAssertLessThan(v(28), 0, "near the start scrolls toward the start")
        XCTAssertGreaterThan(v(360), 0, "near the end scrolls toward the end")
        XCTAssertLessThan(v(10), v(40), "the closer to the edge the faster")
        XCTAssertEqual(v(0), -520, accuracy: 0.001)
        XCTAssertEqual(v(390), 520, accuracy: 0.001)
        XCTAssertEqual(v(-80), -520, accuracy: 0.001, "a finger dragged past the edge is capped")
        XCTAssertEqual(v(500), 520, accuracy: 0.001)
    }

    func testAutoScrollIsOffWhenTheContentFits() {
        XCTAssertEqual(SlidePostReorderMath.autoScrollVelocity(fingerX: 2, viewport: 390, content: 300), 0)
        XCTAssertEqual(SlidePostReorderMath.autoScrollVelocity(fingerX: 2, viewport: 390, content: 390), 0)
        XCTAssertEqual(SlidePostReorderMath.autoScrollVelocity(fingerX: 2, viewport: 0, content: 900), 0)
    }

    func testAutoScrollInATinyViewportNeverOverlapsItsOwnZones() {
        // Two 56pt zones would overlap in a 80pt viewport; each side gets at most half.
        XCTAssertEqual(SlidePostReorderMath.autoScrollVelocity(fingerX: 40, viewport: 80, content: 500), 0, accuracy: 0.001)
        XCTAssertLessThan(SlidePostReorderMath.autoScrollVelocity(fingerX: 10, viewport: 80, content: 500), 0)
    }

    func testClampedOffsetStaysInsideTheContent() {
        XCTAssertEqual(SlidePostReorderMath.clampedOffset(-30, content: 900, viewport: 390), 0)
        XCTAssertEqual(SlidePostReorderMath.clampedOffset(700, content: 900, viewport: 390), 510)
        XCTAssertEqual(SlidePostReorderMath.clampedOffset(40, content: 300, viewport: 390), 0, "content that fits has nowhere to scroll")
    }

    func testScrollingWhileHeldMovesTheTargetAndTheLiftedBlock() {
        // Finger parked at the right edge while the strip scrolls 330pt: the block travels with the content.
        let delta = SlidePostReorderMath.liftedOffset(fingerTravel: 40, scrollTravel: 330)
        XCTAssertEqual(delta, 370)
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 0, delta: delta, pitch: pitch, count: 12), 6)
        // Scrolling back the other way cancels it.
        XCTAssertEqual(SlidePostReorderMath.targetIndex(from: 4, delta: SlidePostReorderMath.liftedOffset(fingerTravel: -10, scrollTravel: -330), pitch: pitch, count: 12), 0)
    }

    func testNeighboursMakeRoomForTheLiftedBlock() {
        func shifts(from: Int, target: Int, count: Int = 6) -> [CGFloat] {
            (0..<count).map { SlidePostReorderMath.neighbourShift(index: $0, from: from, target: target, pitch: pitch) }
        }
        XCTAssertEqual(shifts(from: 1, target: 3), [0, 0, -pitch, -pitch, 0, 0])
        XCTAssertEqual(shifts(from: 4, target: 1), [0, pitch, pitch, pitch, 0, 0])
        XCTAssertEqual(shifts(from: 2, target: 2), [0, 0, 0, 0, 0, 0])
    }
}

// MARK: Auto-append plan

final class SlidePostAutoAppendPlanTests: XCTestCase {
    private func asset(_ id: String, kind: String = "image") -> SlidePostAsset { .init(id: id, kind: kind, status: "ready", mediaStatus: "available") }
    private func draft(_ ids: [String], profile: String = "instagram_carousel") -> SlidePostDraft {
        SlidePostDraft(version: 1, platformProfile: profile, slides: ids.map { .init(id: "s-\($0)", assetID: $0, kind: "image") })
    }

    func testFirstLookSeedsEverythingAndAppendsNothing() {
        let plan = SlidePostAutoAppend.plan(draft: draft(["a", "b"]), ready: [asset("a"), asset("b"), asset("old")], seen: nil)
        XCTAssertTrue(plan.toAppend.isEmpty, "an old draft's removed slides must never reappear")
        XCTAssertEqual(plan.seen, ["a", "b", "old"])
    }

    func testAppendsNewAssetsInPoolOrder() {
        let plan = SlidePostAutoAppend.plan(draft: draft(["a", "b"]), ready: [asset("a"), asset("c"), asset("b"), asset("d")], seen: ["a", "b"])
        XCTAssertEqual(plan.toAppend.map(\.id), ["c", "d"])
        XCTAssertEqual(plan.seen, ["a", "b", "c", "d"])
    }

    func testDedupesAgainstTheDraftAndWithinThePool() {
        let plan = SlidePostAutoAppend.plan(draft: draft(["a", "x"]), ready: [asset("x"), asset("n"), asset("n")], seen: ["a"])
        XCTAssertEqual(plan.toAppend.map(\.id), ["n"], "x is already a slide; n listed twice is one slide")
    }

    func testRemovedSlidesAreNotReadded() {
        let plan = SlidePostAutoAppend.plan(draft: draft(["a"]), ready: [asset("a"), asset("removed")], seen: ["a", "removed"])
        XCTAssertTrue(plan.toAppend.isEmpty)
    }

    func testRunningTwiceAddsOnce() {
        let first = SlidePostAutoAppend.plan(draft: draft(["a"]), ready: [asset("a"), asset("n")], seen: ["a"])
        let second = SlidePostAutoAppend.plan(draft: draft(["a", "n"]), ready: [asset("a"), asset("n")], seen: first.seen)
        XCTAssertEqual(first.toAppend.map(\.id), ["n"])
        XCTAssertTrue(second.toAppend.isEmpty)
    }

    func testInstagramLimitIsTwentyAndOverflowIsReportedNotRetriedForever() {
        let existing = (0..<19).map { "e\($0)" }
        let plan = SlidePostAutoAppend.plan(draft: draft(existing), ready: existing.map { asset($0) } + [asset("n1"), asset("n2"), asset("n3")], seen: Set(existing))
        XCTAssertEqual(plan.toAppend.map(\.id), ["n1"])
        XCTAssertEqual(plan.skippedForLimit, ["n2", "n3"])
        XCTAssertEqual(plan.notice, "Not added: 2 over the slide limit.")
        XCTAssertTrue(plan.seen.contains("n3"), "decided once, so a later removal does not pull it in")
    }

    func testTikTokLimitIsThirtyFiveAndSkipsVideos() {
        let existing = (0..<34).map { "e\($0)" }
        let plan = SlidePostAutoAppend.plan(draft: draft(existing, profile: "tiktok_photo"),
                                            ready: existing.map { asset($0) } + [asset("vid", kind: "video"), asset("p1"), asset("p2")], seen: Set(existing))
        XCTAssertEqual(plan.toAppend.map(\.id), ["p1"])
        XCTAssertEqual(plan.skippedForVideo, ["vid"])
        XCTAssertEqual(plan.skippedForLimit, ["p2"])
        XCTAssertEqual(SlidePostAutoAppend.maxSlides(profile: "tiktok_photo"), 35)
        XCTAssertEqual(SlidePostAutoAppend.maxSlides(profile: "instagram_carousel"), 20)
    }

    func testNoNoticeWhenEverythingFit() {
        XCTAssertNil(SlidePostAutoAppend.plan(draft: draft(["a"]), ready: [asset("a"), asset("n")], seen: ["a"]).notice)
    }
}

// MARK: Pending tiles

final class SlidePostPendingMediaTests: XCTestCase {
    private let project = UUID()
    private func record(_ id: UUID = UUID(), failed: Bool = false, mediaID: String? = nil, project: UUID? = nil) -> UploadRecoveryRecord {
        var value = UploadRecoveryRecord(id: id, projectID: project ?? self.project, localFilePath: "/tmp/x.jpg", filename: "x.jpg", source: .photos, purpose: .cloudRenderSource, taskIdentifier: 1, retryCount: 0)
        value.uploadFailed = failed ? true : nil; value.mediaID = mediaID
        return value
    }
    private func tiles(assets: [SlidePostAsset] = [], inDraft: Set<String> = [], records: [UploadRecoveryRecord] = [], progress: [UUID: Double] = [:], failures: [UploadFailure] = []) -> [SlidePostPendingTile] {
        SlidePostPendingMedia.tiles(assets: assets, draftAssetIDs: inDraft, records: records, progress: progress, inFlight: [:], failures: failures, projectID: project)
    }

    func testProcessingAssetShowsAPlaceholderUntilItIsASlide() {
        let processing = SlidePostAsset(id: "a", kind: "image", status: "processing")
        XCTAssertEqual(tiles(assets: [processing]).map(\.phase), [.processing])
        XCTAssertTrue(tiles(assets: [processing], inDraft: ["a"]).isEmpty)
        XCTAssertTrue(tiles(assets: [SlidePostAsset(id: "r", kind: "image", status: "ready")]).isEmpty, "ready media is auto-added, not a placeholder")
    }

    func testUploadRecordShowsProgressAndFailureOffersRetry() {
        let id = UUID()
        XCTAssertEqual(tiles(records: [record(id)], progress: [id: 0.4]).first?.phase, .uploading(progress: 0.4))
        XCTAssertEqual(tiles(records: [record(id)]).first?.phase, .uploading(progress: nil))
        let failed = tiles(records: [record(id, failed: true)], failures: [UploadFailure(id: id, projectID: project, role: .visual, filename: "x.jpg", message: "Network lost", cause: .uploadFailed)])
        XCTAssertEqual(failed.first?.phase, .failed(message: "Network lost", recordID: id))
    }

    func testOtherProjectsAndAlreadyListedFilesNeverShow() {
        XCTAssertTrue(tiles(records: [record(project: UUID())]).isEmpty)
        let listed = SlidePostAsset(id: "m1", kind: "image", status: "processing")
        XCTAssertEqual(tiles(assets: [listed], records: [record(mediaID: "m1")]).count, 1, "one block for one file, not two")
    }

    func testFailedAssetAndUnreadableFileNeedChoosingAgain() {
        let failedAsset = SlidePostAsset(id: "f", kind: "image", status: "failed")
        XCTAssertEqual(tiles(assets: [failedAsset]).first?.phase, .failed(message: "This file couldn't be prepared.", recordID: nil))
        let unreadable = UploadFailure(id: UUID(), projectID: project, role: .visual, filename: "bad.jpg", message: "Couldn't read it")
        XCTAssertEqual(tiles(failures: [unreadable]).first?.phase, .failed(message: "Couldn't read it", recordID: nil))
    }
}

// MARK: Session: auto-append and reorder

@MainActor final class SlidePostSessionAutoAppendTests: XCTestCase {
    private let itemID = "11111111-1111-1111-1111-111111111111"
    private var defaults: UserDefaults!
    private var suite: String!
    private var remote: SlidePostState!

    override func setUp() {
        super.setUp(); suite = "SlidePostSessionAutoAppendTests.\(UUID())"; defaults = UserDefaults(suiteName: suite)
        remote = state(assets: ["asset-1", "asset-2"])
    }
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        defaults.removePersistentDomain(forName: suite); defaults = nil
        super.tearDown()
    }

    /// A saved draft of asset-1 and asset-2 plus any further assets sitting in the pool.
    private func state(assets: [String], statuses: [String: String] = [:], profile: String = "instagram_carousel") -> SlidePostState {
        let refs = (1...2).map { SlidePostSlide(id: "slide-\($0)", assetID: "asset-\($0)", kind: "image") }
        let draft = SlidePostDraft(version: 1, platformProfile: profile, slides: refs, caption: "", renderedVersion: nil)
        return SlidePostState(itemID: itemID, title: "T", jobID: nil, draft: draft,
            assets: assets.map { .init(id: $0, kind: "image", status: statuses[$0] ?? "ready", sourceURL: URL(string: "https://storage.test/\($0)"), mediaStatus: "available") },
            renderStatus: "not_rendered", renderedVersion: nil, slides: [], bundleURL: nil)
    }
    private func openSession() async -> SlidePostSession {
        NativeEditorURLProtocol.handler = { [unowned self] _ in (200, try JSONEncoder().encode(self.remote)) }
        let session = SlidePostSession(defaults: defaults)
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        _ = session.appendNewlyReadyAssets()   // first look: seeds the seen set
        return session
    }
    private func poll(_ session: SlidePostSession) async {
        await session.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
    }

    func testFirstLookNeverAppendsPreexistingPool() async {
        remote = state(assets: ["asset-1", "asset-2", "leftover"])
        let session = await openSession()
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["asset-1", "asset-2"])
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0)
    }

    func testNewlyReadyAssetsAppendInOrderAsOneUndoStep() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1", "n2", "n3"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 3)
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["asset-1", "asset-2", "n1", "n2", "n3"])
        XCTAssertTrue(session.hasUnsavedChanges)
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["asset-1", "asset-2"], "all three leave in a single undo")
        XCTAssertFalse(session.canUndoEdit)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0, "an undone auto-add is not re-added by the next poll")
        session.redoEdit()
        XCTAssertEqual(session.draft?.slides.count, 5)
    }

    func testAssetIsAddedOnlyOnceAcrossRepeatedPolls() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        for _ in 0..<4 { await poll(session); _ = session.appendNewlyReadyAssets() }
        XCTAssertEqual(session.draft?.slides.filter { $0.assetID == "n1" }.count, 1)
    }

    func testProcessingAssetWaitsUntilItIsReady() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1"], statuses: ["n1": "processing"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0)
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
    }

    func testRemovedSlideIsNotReaddedByALaterPoll() async {
        let session = await openSession()
        session.removeSlide(id: "slide-2")
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["asset-1", "n1"], "asset-2 stays removed")
    }

    func testLocalEditsSurviveAndAreNotClobberedByTheAppend() async {
        let session = await openSession()
        session.setCaption("My caption")
        session.updateText(slideID: "slide-1", textID: session.addText(slideID: "slide-1") ?? "") { $0.text = "Athens" }
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
        XCTAssertEqual(session.draft?.caption, "My caption")
        XCTAssertEqual(session.draft?.slides.first?.edits?.effectiveTexts.first?.text, "Athens")
        XCTAssertEqual(session.draft?.slides.last?.assetID, "n1")
    }

    func testDefersWhileASaveIsInFlightThenAppendsAfterwards() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        await poll(session)
        session.setCaption("Edit")
        let save = Task { await session.save(api: NativeEditorTestSupport.api(), itemID: itemID) }
        await Task.yield()
        XCTAssertTrue(session.isBusy, "the save is mid-flight")
        XCTAssertEqual(session.appendNewlyReadyAssets(), 0, "never mutates the draft during a save")
        XCTAssertFalse(session.seenAssetIDs?.contains("n1") ?? true, "and does not mark it decided")
        await save.value
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
    }

    func testTikTokPostTakesNoVideoAndRespectsTheLimit() async {
        remote = state(assets: ["asset-1", "asset-2"], profile: "tiktok_photo")
        let session = await openSession()
        var next = state(assets: ["asset-1", "asset-2", "clip", "p1"], profile: "tiktok_photo")
        next.assets = next.assets.map { $0.id == "clip" ? SlidePostAsset(id: "clip", kind: "video", status: "ready", mediaStatus: "available") : $0 }
        remote = next
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
        XCTAssertEqual(session.draft?.slides.map(\.assetID), ["asset-1", "asset-2", "p1"])
        XCTAssertNotNil(session.autoAppendNotice)
    }

    func testSeenSetSurvivesRelaunchSoNothingIsAddedTwice() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1"])
        await poll(session)
        XCTAssertEqual(session.appendNewlyReadyAssets(), 1)
        session.undoEdit()
        let reopened = SlidePostSession(defaults: defaults)
        NativeEditorURLProtocol.handler = { [unowned self] _ in (200, try JSONEncoder().encode(self.remote)) }
        await reopened.refresh(api: NativeEditorTestSupport.api(), itemID: itemID)
        XCTAssertEqual(reopened.appendNewlyReadyAssets(), 0)
    }

    func testReorderIsOneUndoStepAndKeepsTheCoverOnItsSlide() async {
        let session = await openSession()
        remote = state(assets: ["asset-1", "asset-2", "n1", "n2"])
        await poll(session)
        _ = session.appendNewlyReadyAssets()
        session.setCover(id: "slide-2")
        let order = session.draft?.slides.map(\.id)
        session.moveSlide(id: "slide-2", toIndex: 3)
        XCTAssertEqual(session.draft?.slides.last?.id, "slide-2")
        XCTAssertEqual(session.draft?.coverIndex, 3, "the cover followed its slide")
        session.moveSlide(id: "slide-1", toIndex: 99)
        XCTAssertEqual(session.draft?.slides.last?.id, "slide-2", "an out-of-range target is ignored")
        session.undoEdit()
        XCTAssertEqual(session.draft?.slides.map(\.id), order, "one undo step puts the move back")
        XCTAssertEqual(session.draft?.coverIndex, 1)
    }
}
