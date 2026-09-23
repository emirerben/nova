import XCTest
@testable import Kria

#if DEBUG
/// Failed Visuals in the editor's Visuals library: the tile explains the
/// failure without truncating away the next step, VoiceOver reads the whole
/// caption, and transient failures retry on their own on a budget that lives
/// on the editor session rather than the panel.
@MainActor final class NativeEditorVisualFailureTests: XCTestCase {
    private static let t0 = Date(timeIntervalSince1970: 1_758_600_000)
    private static let transientDetail = "Kria temporarily couldn't analyze this file. Try again."
    private static let unreadableDetail = "Kria couldn't read this file. Export it as JPG, PNG, WebP, HEIC, HEIF, MP4, or MOV."

    private func visual(
        _ id: String = "asset-1",
        status: String = "failed",
        code: String? = VisualAutoRetryScheduler.retryableErrorCode,
        detail: String? = NativeEditorVisualFailureTests.transientDetail,
        retryable: Bool? = true,
        filename: String? = "photo.jpg"
    ) -> CreationVisual {
        CreationVisual(id: id, kind: "image", status: status, sourceFilename: filename, displayURL: nil, previewURL: nil,
                       retryable: retryable, errorCode: code, errorDetail: detail)
    }
    private func unreadable(_ id: String = "asset-2") -> CreationVisual {
        visual(id, code: "analysis_unreadable", detail: Self.unreadableDetail, retryable: false)
    }
    /// What the pool reports once analysis restarted (queued, shown as uploaded).
    private func restarted(_ id: String = "asset-1") -> CreationVisual {
        visual(id, status: "uploaded", code: nil, detail: nil, retryable: false)
    }

    private func loadedSession(_ configure: (EditorCommitSpy) -> Void) async -> (NativeEditorSession, EditorCommitSpy) {
        let draft = NativeEditorUITestFixtures.allLanes
        let api = EditorCommitSpy(draftSnapshot: DraftSnapshot(
            draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 1,
            snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "g1",
            snapshot: draft.serverSnapshot, canUndo: false, createdAt: .now), authoritativeVariant: [
                "editor_capabilities": .object(["visual_blocks": .bool(true)])
            ])
        configure(api)
        let session = NativeEditorSession()
        await session.load(api: api, threadID: UUID())
        XCTAssertEqual(session.visualItemID, "item")
        return (session, api)
    }

    // MARK: Tile caption

    func testTileKeepsTheNextStepApartFromATruncatableExplanation() {
        XCTAssertEqual(visual().statusCaptionParts(), .init(explanation: Self.transientDetail, nextStep: nil),
                       "a retryable failure's next step is its Retry button")
        XCTAssertEqual(visual().statusCaptionParts(retryingAutomatically: true),
                       .init(explanation: "Kria temporarily couldn't analyze this file.", nextStep: "Retrying automatically…"))
        XCTAssertEqual(unreadable().statusCaptionParts(), .init(explanation: Self.unreadableDetail, nextStep: "Choose it again."))
        XCTAssertEqual(unreadable().statusCaptionParts(retryingAutomatically: true), unreadable().statusCaptionParts(),
                       "a failure the server won't retry never claims to retry")
        XCTAssertEqual(visual(detail: nil, retryable: false).statusCaptionParts(), .init(explanation: "Failed", nextStep: "Choose it again."))
        XCTAssertEqual(visual(detail: " \n", retryable: nil).statusCaptionParts(), .init(explanation: "Failed", nextStep: nil))
        XCTAssertEqual(restarted().statusCaptionParts(retryingAutomatically: true), .init(explanation: "Uploaded", nextStep: nil))
    }

    func testOneLineCaptionIsTheTileCaptionJoined() {
        let assets = [visual(), visual(detail: nil), unreadable(), visual(detail: nil, retryable: false), restarted()]
        for asset in assets {
            for retrying in [false, true] {
                let parts = asset.statusCaptionParts(retryingAutomatically: retrying)
                let line = asset.statusCaption(retryingAutomatically: retrying)
                guard let nextStep = parts.nextStep else {
                    XCTAssertEqual(line, parts.explanation)
                    continue
                }
                XCTAssertTrue(line.hasPrefix(parts.explanation), line)
                XCTAssertTrue(line.hasSuffix(" " + nextStep), line)
            }
        }
        XCTAssertEqual(visual(detail: nil, retryable: false).statusCaption(), "Failed. Choose it again.")
    }

    func testTileAccessibilityLabelReadsTheWholeCaption() {
        XCTAssertEqual(NativeVisualPanel.assetAccessibilityLabel(visual(status: "ready", code: nil, detail: nil, retryable: false),
                                                                 retryingAutomatically: false), "photo.jpg")
        XCTAssertEqual(NativeVisualPanel.assetAccessibilityLabel(unreadable(), retryingAutomatically: false),
                       "photo.jpg, " + Self.unreadableDetail + " Choose it again.")
        XCTAssertEqual(NativeVisualPanel.assetAccessibilityLabel(visual(), retryingAutomatically: true),
                       "photo.jpg, Kria temporarily couldn't analyze this file. Retrying automatically…")
        XCTAssertEqual(NativeVisualPanel.assetAccessibilityLabel(visual(status: "analyzing", code: nil, detail: nil, retryable: false, filename: nil),
                                                                 retryingAutomatically: false), "Visual, Analyzing")
    }

    // MARK: Polling

    func testSchedulerKeepsAPollAliveUntilATransientFailureSpendsItsBudget() {
        var scheduler = VisualAutoRetryScheduler()
        let failed = visual()
        XCTAssertTrue(scheduler.needsObservation(of: failed), "a failure no poll has seen yet still needs one to schedule its first attempt")
        XCTAssertFalse(scheduler.needsObservation(of: unreadable()))
        XCTAssertFalse(scheduler.needsObservation(of: visual(code: "analysis_timed_out")), "other retryable codes stay a manual decision")
        XCTAssertFalse(scheduler.needsObservation(of: visual(status: "ready", code: nil, detail: nil, retryable: false)))

        var now = Self.t0
        for delay in VisualAutoRetryScheduler.delays {
            XCTAssertEqual(scheduler.observe([failed], now: now), [])
            XCTAssertTrue(scheduler.needsObservation(of: failed), "scheduled")
            now += delay
            XCTAssertEqual(scheduler.observe([failed], now: now), ["asset-1"])
            XCTAssertTrue(scheduler.needsObservation(of: failed), "in flight")
            scheduler.recordAttempt(assetID: "asset-1", result: nil, now: now)
        }
        XCTAssertEqual(scheduler.attempts(for: "asset-1"), VisualAutoRetryScheduler.maximumAttempts)
        XCTAssertFalse(scheduler.needsObservation(of: failed))
        XCTAssertEqual(scheduler.observe([failed], now: now + 3600), [])
    }

    func testPanelKeepsPollingWhileAnalysisRestartsAndStopsOnSettledAssets() async {
        let (session, api) = await loadedSession { _ in }
        for status in ["uploaded", "queued", "analyzing"] {
            api.visualPool = [visual(status: status, code: nil, detail: nil, retryable: false)]
            let loaded = await session.refreshVisualLibrary()
            XCTAssertTrue(loaded)
            XCTAssertTrue(session.visualLibraryNeedsPolling, "\(status): a fresh upload or a reanalyze must be followed to its outcome")
        }
        for settled in [visual(status: "ready", code: nil, detail: nil, retryable: false), unreadable(),
                        visual(code: "analysis_timed_out", detail: "Kria took too long to analyze this file. Try again.")] {
            api.visualPool = [settled]
            let loaded = await session.refreshVisualLibrary()
            XCTAssertTrue(loaded)
            XCTAssertFalse(session.visualLibraryNeedsPolling, settled.errorCode ?? settled.status)
        }
        api.visualPool = [visual()]
        let loadedFailure = await session.refreshVisualLibrary()
        XCTAssertTrue(loadedFailure)
        XCTAssertTrue(session.visualLibraryNeedsPolling, "a transient failure first seen outside the poll still gets its automatic retry")
        api.visualPool = nil
        let loadedNothing = await session.refreshVisualLibrary()
        XCTAssertFalse(loadedNothing, "a failed load isn't a snapshot to retry from")
    }

    // MARK: Automatic retry in the editor

    func testEditorRetriesATransientFailureOnItsOwnWithinOneSessionBudget() async {
        let (session, api) = await loadedSession { api in
            api.visualPool = [self.visual(), self.unreadable()]
            // The server's dispatch fails the same way again, synchronously.
            api.retryVisualResults["asset-1"] = self.visual()
        }
        var now = Self.t0
        session.visualAutoRetryClock = { now }

        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, [], "the first attempt waits 10 s")
        XCTAssertTrue(session.visualAutoRetry.isRetryPending("asset-1"))
        XCTAssertFalse(session.visualAutoRetry.isRetryPending("asset-2"), "a non-retryable failure is never retried automatically")
        XCTAssertTrue(session.visualLibraryNeedsPolling)

        now += 9
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, [])
        now += 1
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1"])

        now += 19
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs.count, 1)
        now += 1
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs.count, 2)
        now += 40
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1", "asset-1", "asset-1"])

        // The budget is the session's: a later poll (the panel reopened) doesn't start another round.
        now += 3600
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs.count, VisualAutoRetryScheduler.maximumAttempts)
        XCTAssertFalse(session.visualAutoRetry.isRetryPending("asset-1"))
        XCTAssertFalse(session.visualLibraryNeedsPolling, "a spent budget stops the poll; the tile keeps its Retry button")
        XCTAssertEqual(session.visualLibrary.first?.statusCaptionParts(retryingAutomatically: false).explanation, Self.transientDetail)
    }

    func testManualRetryCancelsThePendingAutomaticAttempt() async {
        let (session, api) = await loadedSession { api in
            api.visualPool = [self.visual()]
            api.retryVisualResults["asset-1"] = self.restarted()
        }
        var now = Self.t0
        session.visualAutoRetryClock = { now }
        await session.pollVisualLibrary()
        XCTAssertTrue(session.visualAutoRetry.isRetryPending("asset-1"))

        api.visualPool = [restarted()]
        await session.retryLibraryVisual("asset-1")
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1"])
        XCTAssertFalse(session.visualAutoRetry.isRetryPending("asset-1"))
        XCTAssertTrue(session.visualLibraryNeedsPolling, "the panel follows the manual retry to its outcome")

        now += 10
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1"], "the cancelled automatic attempt never fires")
    }

    func testRetryTapWhileAnAutomaticAttemptIsOnTheWireSendsNothing() async {
        let (session, api) = await loadedSession { api in
            api.visualPool = [self.visual()]
            api.retryVisualResults["asset-1"] = self.restarted()
        }
        var now = Self.t0
        session.visualAutoRetryClock = { now }
        await session.pollVisualLibrary()
        now += 10
        api.suspendNextRetryVisual = true
        let poll = Task { await session.pollVisualLibrary() }
        for _ in 0..<100 where !api.retryVisualIsSuspended {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(api.retryVisualIsSuspended, "the automatic reanalyze did not reach the wire in time")
        XCTAssertFalse(session.visualAutoRetry.canRetryManually("asset-1"), "the tile's Retry button is disabled")

        await session.retryLibraryVisual("asset-1")
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1"], "a tap during the automatic request sends no duplicate")
        XCTAssertNil(session.visualError)

        api.visualPool = [restarted()]
        api.resumeRetryVisual()
        await poll.value
        XCTAssertTrue(session.visualAutoRetry.canRetryManually("asset-1"))
        XCTAssertEqual(session.visualLibrary.first?.status, "uploaded")
    }

    func testManualRetryLeavesTheAutomaticBudgetWhole() async {
        let (session, api) = await loadedSession { api in
            api.visualPool = [self.visual()]
            api.retryVisualResults["asset-1"] = self.visual()
        }
        var now = Self.t0
        session.visualAutoRetryClock = { now }
        await session.retryLibraryVisual("asset-1")
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1"])
        XCTAssertEqual(session.visualAutoRetry.attempts(for: "asset-1"), 0)
        XCTAssertTrue(session.visualAutoRetry.isRetryPending("asset-1"), "the same transient failure schedules the first automatic attempt")

        now += 10
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs, ["asset-1", "asset-1"])
        XCTAssertEqual(session.visualAutoRetry.attempts(for: "asset-1"), 1)
    }

    func testAutomaticRetryThatCannotReachTheServerStillSpendsBudget() async {
        let (session, api) = await loadedSession { api in api.visualPool = [self.visual()] }
        var now = Self.t0
        session.visualAutoRetryClock = { now }
        for delay in VisualAutoRetryScheduler.delays {
            await session.pollVisualLibrary()
            now += delay
            await session.pollVisualLibrary()
        }
        await session.pollVisualLibrary()
        XCTAssertEqual(api.retriedVisualIDs.count, VisualAutoRetryScheduler.maximumAttempts, "a dead network can't turn into a retry loop")
        XCTAssertFalse(session.visualLibraryNeedsPolling)
    }
}
#endif
