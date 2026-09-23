import XCTest
@testable import Kria

/// Failed Visuals in the Add-media sheet: the server's explanation is decoded
/// and shown instead of a bare "Failed", and transient analysis failures retry
/// on their own, bounded per asset and per sheet.
@MainActor final class VisualAutoRetryTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    private static let t0 = Date(timeIntervalSince1970: 1_758_600_000)
    private static let transientCode = VisualAutoRetryScheduler.retryableErrorCode
    private static let transientDetail = "Kria temporarily couldn't analyze this file. Try again."
    private static let failedAgain = Data(#"{"id":"asset-1","kind":"image","status":"failed","source_filename":"photo.jpg","display_url":null,"preview_url":null,"retryable":true,"error_code":"analysis_temporarily_unavailable","error_detail":"Kria temporarily couldn't analyze this file. Try again."}"#.utf8)

    private func visual(
        _ id: String = "asset-1",
        status: String = "failed",
        code: String? = VisualAutoRetryTests.transientCode,
        detail: String? = VisualAutoRetryTests.transientDetail,
        retryable: Bool? = true
    ) -> CreationVisual {
        CreationVisual(id: id, kind: "image", status: status, sourceFilename: "photo.jpg", displayURL: nil, previewURL: nil,
                       retryable: retryable, errorCode: code, errorDetail: detail)
    }
    private func queued(_ id: String = "asset-1") -> CreationVisual { visual(id, status: "queued", code: nil, detail: nil, retryable: false) }

    // MARK: Decoding

    func testDecodesServerFailureFieldsAndToleratesServersWithoutThem() throws {
        let failed = try JSONDecoder().decode(CreationVisual.self, from: Self.failedAgain)
        XCTAssertEqual(failed.errorCode, "analysis_temporarily_unavailable")
        XCTAssertEqual(failed.errorDetail, Self.transientDetail)
        XCTAssertEqual(failed.retryable, true)
        XCTAssertTrue(VisualAutoRetryScheduler.qualifies(failed))

        let older = try JSONDecoder().decode(CreationVisual.self, from: Data(#"{"id":"a","kind":"image","status":"failed","source_filename":null,"display_url":null,"preview_url":null}"#.utf8))
        XCTAssertNil(older.errorCode)
        XCTAssertNil(older.errorDetail)
        XCTAssertNil(older.retryable)
        XCTAssertEqual(older.statusCaption(), "Failed")
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(older), "without a code there is nothing to retry on")
    }

    func testRetryVisualPostsReanalyzeWithoutABodyAndDecodesTheOutcome() async throws {
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/plan-items/item-1/assets/asset-1/reanalyze")
            XCTAssertTrue(NativeEditorTestSupport.bodyData(request).isEmpty, "reanalyze re-runs analysis on the uploaded object; the app never re-sends bytes")
            return (200, VisualAutoRetryTests.failedAgain)
        }
        let visual = try await NativeEditorTestSupport.api().retryVisual(itemID: "item-1", assetID: "asset-1")
        XCTAssertEqual(visual.status, "failed")
        XCTAssertEqual(visual.errorCode, "analysis_temporarily_unavailable")
        XCTAssertEqual(visual.errorDetail, Self.transientDetail)
    }

    // MARK: Caption

    func testFailedRowsExplainThemselves() {
        XCTAssertEqual(visual(status: "ready", code: nil, detail: nil, retryable: false).statusCaption(), "Ready")
        XCTAssertEqual(visual(status: "analyzing", code: nil, detail: nil, retryable: false).statusCaption(retryingAutomatically: true), "Analyzing")
        XCTAssertEqual(visual().statusCaption(), Self.transientDetail)
        XCTAssertEqual(visual(detail: nil).statusCaption(), "Failed")
        XCTAssertEqual(visual(detail: "  \n").statusCaption(), "Failed")
        XCTAssertEqual(visual(code: "analysis_timed_out", detail: "Kria took too long to analyze this file. Try again.").statusCaption(),
                       "Kria took too long to analyze this file. Try again.")
    }

    func testPendingAutomaticRetryReplacesTheServersTryAgainCue() {
        XCTAssertEqual(visual().statusCaption(retryingAutomatically: true),
                       "Kria temporarily couldn't analyze this file. Retrying automatically…")
        XCTAssertEqual(visual(detail: nil).statusCaption(retryingAutomatically: true), "Failed. Retrying automatically…")
        XCTAssertEqual(visual(detail: "Clip analysis is unavailable right now").statusCaption(retryingAutomatically: true),
                       "Clip analysis is unavailable right now. Retrying automatically…")
    }

    func testNonRetryableFailuresAskForTheFileAgainAndNeverClaimToRetry() {
        let unreadable = visual(code: "analysis_unreadable",
                                detail: "Kria couldn't read this file. Export it as JPG, PNG, WebP, HEIC, HEIF, MP4, or MOV.",
                                retryable: false)
        XCTAssertEqual(unreadable.statusCaption(),
                       "Kria couldn't read this file. Export it as JPG, PNG, WebP, HEIC, HEIF, MP4, or MOV. Choose it again.")
        XCTAssertEqual(unreadable.statusCaption(retryingAutomatically: true), unreadable.statusCaption())
        XCTAssertEqual(visual(code: "provider_outcome_unknown", detail: nil, retryable: false).statusCaption(), "Failed. Choose it again.")
        XCTAssertEqual(visual(detail: "Kria couldn't read this file", retryable: false).statusCaption(),
                       "Kria couldn't read this file. Choose it again.")
    }

    // MARK: Scheduler

    func testOnlyTheTransientCodeWithoutANonRetryableFlagQualifies() {
        XCTAssertTrue(VisualAutoRetryScheduler.qualifies(visual()))
        XCTAssertTrue(VisualAutoRetryScheduler.qualifies(visual(retryable: nil)), "a server that omits `retryable` still gets the bounded retry")
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(retryable: false)))
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(code: "analysis_unreadable", retryable: false)))
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(code: "analysis_timed_out")), "other retryable codes stay a manual decision")
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(code: "provider_quota_exceeded")))
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(code: nil)))
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(status: "ready")))
        XCTAssertFalse(VisualAutoRetryScheduler.qualifies(visual(status: "uploaded")))

        var scheduler = VisualAutoRetryScheduler()
        let never = [
            visual("unreadable", code: "analysis_unreadable", retryable: false),
            visual("timed-out", code: "analysis_timed_out"),
            visual("flagged", retryable: false),
            visual("legacy", code: nil, detail: nil, retryable: nil),
            visual("ready", status: "ready", code: nil, detail: nil, retryable: false),
        ]
        for offset in stride(from: 0.0, through: 600, by: 5) {
            XCTAssertEqual(scheduler.observe(never, now: Self.t0.addingTimeInterval(offset)), [])
        }
        XCTAssertTrue(scheduler.entries.isEmpty)
        for asset in never { XCTAssertFalse(scheduler.isRetryPending(asset.id)) }
    }

    func testTransientFailureRetriesThreeTimesTenTwentyFortySecondsApart() {
        var scheduler = VisualAutoRetryScheduler()
        let failed = [visual()]
        var now = Self.t0
        for (attempt, delay) in [(1, 10.0), (2, 20.0), (3, 40.0)] {
            XCTAssertEqual(scheduler.observe(failed, now: now), [], "attempt \(attempt) waits its full delay")
            XCTAssertTrue(scheduler.isRetryPending("asset-1"))
            XCTAssertEqual(scheduler.observe(failed, now: now.addingTimeInterval(delay - 1)), [])
            XCTAssertEqual(scheduler.observe(failed, now: now.addingTimeInterval(delay)), ["asset-1"])
            XCTAssertEqual(scheduler.attempts(for: "asset-1"), attempt)
            XCTAssertTrue(scheduler.isRetryPending("asset-1"), "in flight still reads as pending")
            XCTAssertEqual(scheduler.observe(failed, now: now.addingTimeInterval(delay + 5)), [],
                           "a poll that overlaps the request must not fire it twice")
            now = now.addingTimeInterval(delay + 7)
            scheduler.recordAttempt(assetID: "asset-1", result: queued(), now: now)
            XCTAssertFalse(scheduler.isRetryPending("asset-1"))
            XCTAssertEqual(scheduler.observe([visual(status: "analyzing", code: nil, detail: nil, retryable: false)], now: now), [])
            now = now.addingTimeInterval(30) // the reanalysis fails again
        }
        XCTAssertEqual(scheduler.observe(failed, now: now), [])
        XCTAssertFalse(scheduler.isRetryPending("asset-1"), "budget spent: the row keeps its explanation and the manual Retry button")
        XCTAssertEqual(scheduler.observe(failed, now: now.addingTimeInterval(3_600)), [])
        XCTAssertEqual(scheduler.attempts(for: "asset-1"), 3)
    }

    func testSynchronousFailureResponseSchedulesTheNextAttemptAtOnce() {
        var scheduler = VisualAutoRetryScheduler()
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0), [])
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(10)), ["asset-1"])
        // The server's dispatch path fails inside the POST itself and answers "failed" again.
        scheduler.recordAttempt(assetID: "asset-1", result: visual(), now: Self.t0.addingTimeInterval(11))
        XCTAssertTrue(scheduler.isRetryPending("asset-1"))
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(30)), [])
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(31)), ["asset-1"])
        XCTAssertEqual(scheduler.attempts(for: "asset-1"), 2)
        // A non-retryable answer ends it, whatever budget is left.
        scheduler.recordAttempt(assetID: "asset-1", result: visual(code: "analysis_unreadable", retryable: false), now: Self.t0.addingTimeInterval(32))
        XCTAssertFalse(scheduler.isRetryPending("asset-1"))
        XCTAssertEqual(scheduler.observe([visual(code: "analysis_unreadable", retryable: false)], now: Self.t0.addingTimeInterval(3_600)), [])
    }

    /// A 5 s poll loop whose reanalyze requests all fail (dead network, 404):
    /// the budget is spent on schedule and nothing loops.
    func testFailedRequestsSpendTheBudgetInsteadOfLooping() {
        var scheduler = VisualAutoRetryScheduler()
        var fired: [TimeInterval] = []
        for offset in stride(from: 0.0, through: 3_600, by: 5) {
            let now = Self.t0.addingTimeInterval(offset)
            for id in scheduler.observe([visual()], now: now) {
                fired.append(offset)
                scheduler.recordAttempt(assetID: id, result: nil, now: now)
            }
        }
        XCTAssertEqual(fired, [10, 35, 80])
        XCTAssertFalse(scheduler.isRetryPending("asset-1"))
    }

    func testRecoveryCancelsThePendingRetryButKeepsTheSessionBudget() {
        var scheduler = VisualAutoRetryScheduler()
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0), [])
        XCTAssertTrue(scheduler.isRetryPending("asset-1"))
        // The web (or a manual Retry) requeued it before the automatic one was due.
        XCTAssertEqual(scheduler.observe([queued()], now: Self.t0.addingTimeInterval(5)), [])
        XCTAssertFalse(scheduler.isRetryPending("asset-1"))
        XCTAssertEqual(scheduler.observe([queued()], now: Self.t0.addingTimeInterval(60)), [])
        // It fails again later: the first automatic attempt is still owed, with its 10 s delay.
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(100)), [])
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(110)), ["asset-1"])
        XCTAssertEqual(scheduler.attempts(for: "asset-1"), 1)
        scheduler.recordAttempt(assetID: "asset-1", result: queued(), now: Self.t0.addingTimeInterval(111))
        // Two more failures later use the remaining budget, then it stops.
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(200)), [])
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(220)), ["asset-1"])
        scheduler.recordAttempt(assetID: "asset-1", result: queued(), now: Self.t0.addingTimeInterval(221))
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(300)), [])
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(340)), ["asset-1"])
        scheduler.recordAttempt(assetID: "asset-1", result: queued(), now: Self.t0.addingTimeInterval(341))
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(400)), [])
        XCTAssertFalse(scheduler.isRetryPending("asset-1"))
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(4_000)), [])
    }

    func testManualRetryCancelsThePendingAutomaticOne() {
        var scheduler = VisualAutoRetryScheduler()
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0), [])
        scheduler.cancel("asset-1")
        XCTAssertFalse(scheduler.isRetryPending("asset-1"))
        // A poll that still shows the old failure reschedules rather than fires.
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(10)), [])
        XCTAssertEqual(scheduler.attempts(for: "asset-1"), 0)
        XCTAssertTrue(scheduler.isRetryPending("asset-1"))
        XCTAssertEqual(scheduler.observe([visual()], now: Self.t0.addingTimeInterval(20)), ["asset-1"])
    }

    func testAssetsAreTrackedIndependentlyAndForgottenWhenRemoved() {
        var scheduler = VisualAutoRetryScheduler()
        XCTAssertEqual(scheduler.observe([visual("a"), visual("b")], now: Self.t0), [])
        XCTAssertEqual(scheduler.observe([visual("a"), visual("b")], now: Self.t0.addingTimeInterval(10)), ["a", "b"])
        // "a" is removed while its request is in flight.
        XCTAssertEqual(scheduler.observe([visual("b")], now: Self.t0.addingTimeInterval(12)), [])
        XCTAssertFalse(scheduler.isRetryPending("a"))
        scheduler.recordAttempt(assetID: "a", result: visual("a"), now: Self.t0.addingTimeInterval(13))
        XCTAssertFalse(scheduler.isRetryPending("a"), "an outcome for a removed asset is ignored")
        XCTAssertTrue(scheduler.isRetryPending("b"))
        XCTAssertEqual(Set(scheduler.entries.keys), ["b"])
        scheduler.recordAttempt(assetID: "b", result: queued("b"), now: Self.t0.addingTimeInterval(14))
        // Re-adding "a" starts it from a fresh budget; "b" keeps its own.
        XCTAssertEqual(scheduler.observe([visual("a"), visual("b")], now: Self.t0.addingTimeInterval(100)), [])
        XCTAssertEqual(scheduler.observe([visual("a"), visual("b")], now: Self.t0.addingTimeInterval(110)), ["a"])
        XCTAssertEqual(scheduler.observe([visual("a"), visual("b")], now: Self.t0.addingTimeInterval(120)), ["b"])
        XCTAssertEqual(scheduler.attempts(for: "a"), 1)
        XCTAssertEqual(scheduler.attempts(for: "b"), 2)
    }
}
