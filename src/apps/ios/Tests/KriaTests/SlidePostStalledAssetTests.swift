import XCTest
@testable import Kria

/// KRI-820: one photo stuck "analyzing" must not hold back the slides that are ready.
///
/// Failure modes this guards (written before the change):
/// - a pending or timed-out photo that is NOT in the draft blocks export / asking Kria;
/// - photos still preparing inside their bound stop holding back "ask Kria", so the proposal
///   (built from ready rows only) silently drops media the creator just added;
/// - a photo past its declared bound keeps holding "ask Kria" back forever;
/// - a pending or failed photo that IS in the draft (as a slide or a sticker) does not block;
/// - removing the stalled photo leaves the remaining ready slides blocked;
/// - a photo whose AI analysis timed out but whose bytes the server verified can't be placed,
///   although the server's slide routes accept it (`media_status == "ready"`), including while
///   an analysis-only Retry re-analyzes it;
/// - the server's timeout payload (`analysis_deadline_at` with microseconds, `analysis_timed_out`)
///   fails to decode and the row falls back to "Analyzing…";
/// - the progress copy keeps promising "under a minute" with no bound the server will honour.
final class SlidePostStalledAssetTests: XCTestCase {
    private func asset(_ id: String, _ status: String, media: String? = nil) -> SlidePostAsset {
        SlidePostAsset(id: id, kind: "image", status: status, mediaStatus: media)
    }

    private func draft(_ ids: [String], sticker: (slide: String, asset: String)? = nil) -> SlidePostDraft {
        let slides = ids.map { id in
            var slide = SlidePostSlide(id: "slide-\(id)", assetID: id, kind: "image")
            if let sticker, sticker.slide == id {
                var edits = SlidePostEdits()
                edits.stickers = [SlidePostSticker(assetID: sticker.asset)]
                slide.edits = edits
            }
            return slide
        }
        return SlidePostDraft(version: 1, platformProfile: "tiktok_photo", slides: slides)
    }

    private func blocking(_ assets: [SlidePostAsset], _ draft: SlidePostDraft?) -> (pending: Bool, failed: Bool) {
        let used = SlidePostAssetGate.used(assets, by: draft)
        let pending = used.contains { SlidePostAssetGate.isInFlight($0) && !SlidePostAssetGate.isUsable($0) }
        return (pending, used.contains(where: SlidePostAssetGate.isFailed))
    }

    func testStalledOrTimedOutPhotoOutsideTheDraftNeverBlocks() {
        let ready = (1...5).map { asset("r\($0)", "ready", media: "ready") }
        let post = draft(ready.map(\.id))
        for stalled in [asset("s", "analyzing", media: "pending"), asset("s", "failed", media: "failed")] {
            let gate = blocking(ready + [stalled], post)
            XCTAssertFalse(gate.pending, stalled.status)
            XCTAssertFalse(gate.failed, stalled.status)
        }
    }

    func testStalledPhotoUsedByTheDraftStillBlocks() {
        let assets = [asset("r1", "ready", media: "ready"), asset("s", "analyzing", media: "pending")]
        XCTAssertTrue(blocking(assets, draft(["r1", "s"])).pending, "as a slide")
        XCTAssertTrue(blocking(assets, draft(["r1"], sticker: ("r1", "s"))).pending, "as a sticker")
        let failed = [asset("r1", "ready", media: "ready"), asset("f", "failed", media: "failed")]
        XCTAssertTrue(blocking(failed, draft(["r1", "f"])).failed)
    }

    func testAskingKriaWaitsForPhotosStillPreparingInsideTheirBound() {
        let now = Date()
        let iso = ISO8601DateFormatter()
        func preparing(_ id: String, deadline: Date?) -> SlidePostAsset {
            var row = asset(id, "analyzing", media: "pending")
            row.analysisDeadlineAt = deadline.map(iso.string(from:))
            return row
        }
        // New post (no draft yet): 2 ready, 6 still preparing well inside the bound.
        let pool = (1...2).map { asset("r\($0)", "ready", media: "ready") }
            + (1...6).map { preparing("p\($0)", deadline: now.addingTimeInterval(300)) }
        XCTAssertTrue(pool.contains { SlidePostAssetGate.isAwaited($0, now: now) })
        // Past the bound the row stops holding the request back (the server reports it failed).
        let expired = preparing("x", deadline: now.addingTimeInterval(-1))
        XCTAssertFalse(SlidePostAssetGate.isAwaited(expired, now: now))
        // No deadline yet (legacy row): still awaited, never silently dropped.
        XCTAssertTrue(SlidePostAssetGate.isAwaited(preparing("l", deadline: nil), now: now))
        XCTAssertFalse(SlidePostAssetGate.isAwaited(asset("f", "failed", media: "failed"), now: now))
    }

    func testRemovingTheStalledPhotoLeavesTheReadySlidesUnblocked() {
        let assets = (1...5).map { asset("r\($0)", "ready", media: "ready") }
        let gate = blocking(assets, draft(assets.map(\.id)))
        XCTAssertFalse(gate.pending)
        XCTAssertFalse(gate.failed)
    }

    func testTimedOutAnalysisWithVerifiedBytesStaysPlaceable() {
        let verified = asset("v", "failed", media: "ready")
        XCTAssertTrue(SlidePostAssetGate.isUsable(verified))
        XCTAssertFalse(SlidePostAssetGate.isFailed(verified))
        XCTAssertFalse(SlidePostAssetGate.isUsable(asset("u", "failed", media: "failed")))
        XCTAssertFalse(SlidePostAssetGate.isUsable(asset("m", "ready", media: "missing")))
        // Retrying only the analysis of verified bytes keeps the slide in the exportable post.
        let retrying = asset("a", "analyzing", media: "ready")
        XCTAssertTrue(SlidePostAssetGate.isUsable(retrying))
        XCTAssertFalse(blocking([retrying], draft(["a"])).pending)
        XCTAssertFalse(SlidePostAssetGate.isUsable(asset("p", "analyzing", media: "pending")))
    }

    func testServerTimeoutPayloadDecodes() throws {
        let json = Data("""
        {"id":"s","kind":"image","status":"failed","media_status":"failed","error_code":"analysis_timed_out",
         "retryable":true,"analysis_deadline_at":null}
        """.utf8)
        let decoded = try JSONDecoder().decode(SlidePostAsset.self, from: json)
        XCTAssertEqual(decoded.errorCode, "analysis_timed_out")
        XCTAssertEqual(decoded.retryable, true)
        XCTAssertTrue(SlidePostAssetGate.isFailed(decoded))

        let visual = try JSONDecoder().decode(CreationVisual.self, from: Data("""
        {"id":"v","kind":"image","status":"analyzing","retryable":false,
         "analysis_deadline_at":"2026-10-22T14:42:51.123456Z"}
        """.utf8))
        XCTAssertNotNil(visual.analysisDeadline)
    }

    func testProgressCopyNamesTheServerBound() throws {
        let deadline = "2026-10-22T14:42:51.123456Z"
        let preparing = CreationVisual(id: "s", kind: "image", status: "analyzing", sourceFilename: "s.jpg", displayURL: nil, previewURL: nil, retryable: nil, analysisDeadlineAt: deadline)
        let summary = try XCTUnwrap(VisualPreparationSummary(assets: [preparing], slowIDs: [], surface: .addMediaSheet))
        let resolvesBy = try XCTUnwrap(preparing.analysisDeadline)
        XCTAssertEqual(summary.resolvesBy, resolvesBy)
        XCTAssertTrue(summary.detail.contains(resolvesBy.formatted(date: .omitted, time: .shortened)), summary.detail)
        XCTAssertTrue(summary.detail.hasSuffix("can be retried or removed."), summary.detail)
    }
}
