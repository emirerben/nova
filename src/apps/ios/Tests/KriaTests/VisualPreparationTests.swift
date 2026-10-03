import XCTest
@testable import Kria

/// KRI-294: uploaded Visuals that are still being analyzed say what they are
/// waiting for, the batch shows a filling progress bar, and a long wait tells
/// the creator what to do instead of sitting on a bare "Queued".
final class VisualPreparationTests: XCTestCase {
    private static let t0 = Date(timeIntervalSince1970: 1_791_000_000)

    private func visual(_ id: String, _ status: String, kind: String = "image") -> CreationVisual {
        CreationVisual(id: id, kind: kind, status: status, sourceFilename: "\(id).jpg", displayURL: nil, previewURL: nil, retryable: nil)
    }

    // MARK: Stage and caption

    func testQueuedVisualsSayTheyAreUploadedAndWaitingInsteadOfQueued() {
        for status in ["uploaded", "queued", "pending"] {
            let asset = visual("a", status)
            XCTAssertEqual(asset.preparationStage, .waiting, status)
            XCTAssertEqual(asset.statusCaption(), "Uploaded, waiting to be analyzed", status)
            XCTAssertEqual(asset.preparationCaption(short: true), "Waiting…", status)
        }
        for status in ["analyzing", "processing"] {
            XCTAssertEqual(visual("a", status).preparationStage, .analyzing, status)
            XCTAssertEqual(visual("a", status).statusCaption(), "Analyzing…", status)
        }
    }

    func testReadyFailedAndUnknownStatusesAreNotPreparing() {
        XCTAssertNil(visual("a", "ready").preparationStage)
        XCTAssertNil(visual("a", "failed").preparationStage)
        XCTAssertNil(visual("a", "archived").preparationCaption())
        XCTAssertEqual(visual("a", "archived").statusCaption(), "Archived", "an unknown status still shows itself")
    }

    // MARK: Clock

    func testAWaitTurnsSlowAfterAMinuteAndForgetsVisualsThatFinish() {
        var clock = VisualPreparationClock()
        clock.observe([visual("a", "queued"), visual("b", "ready")], now: Self.t0)
        XCTAssertEqual(Set(clock.firstSeen.keys), ["a"], "only preparing Visuals are timed")
        XCTAssertTrue(clock.slowIDs.isEmpty)

        clock.observe([visual("a", "analyzing"), visual("c", "queued")], now: Self.t0.addingTimeInterval(59))
        XCTAssertTrue(clock.slowIDs.isEmpty)
        XCTAssertEqual(clock.firstSeen["a"], Self.t0, "moving from queued to analyzing is the same wait")

        clock.observe([visual("a", "analyzing"), visual("c", "queued")], now: Self.t0.addingTimeInterval(60))
        XCTAssertEqual(clock.slowIDs, ["a"], "c has only waited one second")

        clock.observe([visual("a", "ready"), visual("c", "queued")], now: Self.t0.addingTimeInterval(61))
        XCTAssertNil(clock.firstSeen["a"])
        XCTAssertTrue(clock.slowIDs.isEmpty)
    }

    func testAVisualAnalyzedAgainStartsAFreshWait() {
        var clock = VisualPreparationClock()
        clock.observe([visual("a", "queued")], now: Self.t0)
        clock.observe([visual("a", "failed")], now: Self.t0.addingTimeInterval(90))
        clock.observe([visual("a", "queued")], now: Self.t0.addingTimeInterval(100))
        XCTAssertEqual(clock.firstSeen["a"], Self.t0.addingTimeInterval(100))
        XCTAssertTrue(clock.slowIDs.isEmpty)
    }

    // MARK: Summary

    func testSummaryCountsReadyAgainstEverythingStillOnItsWay() throws {
        let assets = [visual("a", "ready"), visual("b", "queued"), visual("c", "analyzing"), visual("d", "failed")]
        let summary = try XCTUnwrap(VisualPreparationSummary(assets: assets, uploading: 2, slowIDs: [], surface: .addMediaSheet))
        XCTAssertEqual(summary.ready, 1)
        XCTAssertEqual(summary.total, 5, "3 non-failed Visuals in the pool plus 2 still uploading; the failure explains itself")
        XCTAssertEqual(summary.count, "1 of 5 ready")
        XCTAssertEqual(summary.fraction, 0.2, accuracy: 0.0001)
        XCTAssertEqual(summary.title, "Getting your photos ready")
        XCTAssertFalse(summary.isSlow)
        XCTAssertEqual(summary.detail, "Kria looks at each one before it can use it. This usually takes under a minute.")
    }

    func testNoSummaryWhenThereIsNoWaitToExplain() {
        XCTAssertNil(VisualPreparationSummary(assets: [], slowIDs: [], surface: .addMediaSheet))
        XCTAssertNil(VisualPreparationSummary(assets: [visual("a", "ready"), visual("b", "failed")], slowIDs: [], surface: .editorLibrary))
        XCTAssertNotNil(VisualPreparationSummary(assets: [visual("a", "ready")], uploading: 1, slowIDs: [], surface: .editorLibrary),
                        "a file still uploading is a wait too")
    }

    func testSummaryNamesWhatIsPreparing() throws {
        let videos = try XCTUnwrap(VisualPreparationSummary(assets: [visual("a", "queued", kind: "video")], slowIDs: [], surface: .addMediaSheet))
        XCTAssertEqual(videos.title, "Getting your videos ready")
        let mixed = try XCTUnwrap(VisualPreparationSummary(assets: [visual("a", "queued", kind: "video"), visual("b", "ready")], slowIDs: [], surface: .addMediaSheet))
        XCTAssertEqual(mixed.title, "Getting your visuals ready")
    }

    func testASlowWaitSaysWhatToDoWithoutPromisingTheVideoCanBeCreatedYet() throws {
        let assets = [visual("a", "queued"), visual("b", "ready")]
        let sheet = try XCTUnwrap(VisualPreparationSummary(assets: assets, slowIDs: ["a"], surface: .addMediaSheet))
        XCTAssertTrue(sheet.isSlow)
        XCTAssertEqual(sheet.title, "Still getting your photos ready")
        XCTAssertTrue(sheet.detail.hasPrefix("This is taking longer than usual."), sheet.detail)
        XCTAssertTrue(sheet.detail.contains("keep chatting"), sheet.detail)
        XCTAssertTrue(sheet.detail.contains("Wait until they’re ready before you create the video."),
                      "a phone render approved while a Visual is preparing is refused")
        let editor = try XCTUnwrap(VisualPreparationSummary(assets: assets, slowIDs: ["a"], surface: .editorLibrary))
        XCTAssertTrue(editor.detail.contains("keep editing"), editor.detail)
        XCTAssertEqual(editor.accessibilityLabel, editor.title + ", 1 of 2 ready. " + editor.detail)

        let finished = try XCTUnwrap(VisualPreparationSummary(assets: [visual("a", "ready"), visual("c", "queued")], slowIDs: ["a"], surface: .addMediaSheet))
        XCTAssertFalse(finished.isSlow, "only a Visual that is still preparing can make the wait slow")
    }

    func testUploadsStuckOfflineSayTheyAreWaitingForAConnection() throws {
        let assets = [visual("a", "queued")]
        let offline = try XCTUnwrap(VisualPreparationSummary(assets: assets, uploading: 2, online: false, slowIDs: ["a"], surface: .addMediaSheet))
        XCTAssertTrue(offline.waitingForConnection)
        XCTAssertEqual(offline.detail, "Waiting for an internet connection. Uploads pick up again on their own once you’re back online.",
                       "a missing connection explains the wait better than slow analysis does")
        let uploaded = try XCTUnwrap(VisualPreparationSummary(assets: assets, uploading: 0, online: false, slowIDs: [], surface: .addMediaSheet))
        XCTAssertFalse(uploaded.waitingForConnection, "nothing left to upload; analysis runs on the server")
    }

    // MARK: Upload row caption

    func testUploadRowSaysWhetherItIsWaitingMovingOrOffline() {
        XCTAssertEqual(UploadProgressCaption.text(progress: nil, completed: false, online: true), "Waiting to upload…")
        XCTAssertEqual(UploadProgressCaption.text(progress: 0.456, completed: false, online: true), "Uploading… 45%")
        XCTAssertEqual(UploadProgressCaption.text(progress: 0.456, completed: false, online: false), "Waiting for an internet connection…")
        XCTAssertEqual(UploadProgressCaption.text(progress: nil, completed: false, online: false), "Waiting for an internet connection…")
        XCTAssertEqual(UploadProgressCaption.text(progress: 1, completed: false, online: false), "Finishing up…",
                       "the bytes are already stored; attaching is all that's left")
        XCTAssertEqual(UploadProgressCaption.text(progress: 0.2, completed: true, online: true), "Finishing up…")
    }

    // MARK: Uploads on their way

    @MainActor func testActiveUploadsCountEachFileOnceAndSkipFailedOnes() {
        let project = UUID()
        func record(_ id: UUID, role: CreationMediaRole = .visual, failed: Bool = false, in projectID: UUID? = nil) -> UploadRecoveryRecord {
            var record = UploadRecoveryRecord(id: id, projectID: projectID ?? project, localFilePath: "/tmp/\(id).jpg", filename: "\(id).jpg",
                                              source: .photos, purpose: .cloudRenderSource, taskIdentifier: 1, retryCount: 0)
            record.mediaRole = role
            record.uploadFailed = failed ? true : nil
            return record
        }
        let uploading = UUID(), handingOver = UUID(), failed = UUID(), preparing = UUID()
        let records = [record(uploading), record(handingOver), record(failed, failed: true),
                       record(UUID(), role: .clip), record(UUID(), in: UUID())]
        let inFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [
            handingOver: .init(projectID: project, role: .visual),
            preparing: .init(projectID: project, role: .visual),
            UUID(): .init(projectID: project, role: .clip),
        ]
        XCTAssertEqual(BackgroundUploadCoordinator.activeUploadCount(projectID: project, role: .visual, inFlight: inFlight, records: records, selections: [:]), 3,
                       "uploading + handing over (recorded and still in flight, counted once) + preparing; never the failed one")
    }
}
