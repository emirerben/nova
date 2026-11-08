import KriaMediaEngine
import XCTest
@testable import Kria

/// Use take starts the heavy work at review time, not at the tap. Failure modes written first:
/// 1. work starts for a take that cannot be used (silent) or was already handed over;
/// 2. Retake / Discard / replacing a pending take leaves the transcode running or a proxy on disk;
/// 3. a take re-recorded into the same slot reuses the previous take's hash or proxy;
/// 4. the Use take spinner stays up for the second (background) upload;
/// 5. Use take and a failed intake leave no way to retry with fresh work.
@MainActor final class TeleprompterPrewarmTests: XCTestCase {
    private var root: URL!

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("teleprompter-prewarm-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: root) }

    private final class Transcodes: @unchecked Sendable {
        private let lock = NSLock()
        private var _started: [String] = [], _cancelled = 0
        var started: [String] { lock.withLock { _started } }
        var cancelled: Int { lock.withLock { _cancelled } }
        var holds = true

        func make(_ source: URL, _ destination: URL) async throws -> URL {
            lock.withLock { _started.append(source.lastPathComponent) }
            while holds {
                if Task.isCancelled { lock.withLock { _cancelled += 1 }; throw CancellationError() }
                try? await Task.sleep(for: .milliseconds(5))
            }
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("proxy".utf8).write(to: destination)
            return destination
        }
    }

    private struct Saver: PhotoLibrarySaving { func saveVideo(at url: URL) async throws {} }

    private func makeFlow(_ transcodes: Transcodes, background: BackgroundIntake? = nil) -> (TeleprompterRecordingFlow, SourcePrewarmer) {
        let prewarmer = SourcePrewarmer(directory: root.appendingPathComponent("prewarm", isDirectory: true)) { try await transcodes.make($0, $1) }
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, photoSaver: Saver(), prewarmer: prewarmer,
                                             speakerBackgroundEnabled: background != nil, backgroundIntake: background)
        return (flow, prewarmer)
    }

    @discardableResult private func finalize(_ flow: TeleprompterRecordingFlow, verdict: TakeAudioCheck.Verdict = .ok, bytes: String = "movie") throws -> TeleprompterTakeManifest {
        let take = TeleprompterTakeManifest(script: flow.script.draft, slot: flow.pendingTake == nil ? .pending : .candidate)
        try flow.store.save(manifest: take)
        try FileManager.default.createDirectory(at: flow.store.takesDirectory, withIntermediateDirectories: true)
        try Data(bytes.utf8).write(to: flow.store.fileURL(for: take))
        flow.seedActiveTakeForTesting(take)
        flow.camera.seedFinalizedAudioCheckForTesting(TakeAudioCheck(verdict: verdict, peakDb: verdict == .silent ? -120 : -12, activeFraction: verdict == .silent ? 0 : 0.8))
        flow.camera.onFinalized?(flow.store.fileURL(for: take), nil)
        return take
    }

    private func waitFor(_ condition: @escaping () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(5)
        while !condition() {
            if ContinuousClock.now > deadline { return XCTFail("timed out") }
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    func testAReviewableTakeStartsItsWorkWithoutAnyTap() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        let take = try finalize(flow)
        try await waitFor { transcodes.started.count == 1 }
        XCTAssertEqual(transcodes.started, [flow.store.fileURL(for: take).lastPathComponent])
        XCTAssertEqual(prewarmer.heldCount, 1)
        transcodes.holds = false
    }

    func testASilentTakeIsNeverPrewarmed() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        try finalize(flow, verdict: .silent)
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(prewarmer.heldCount, 0)
        XCTAssertEqual(transcodes.started, [])
    }

    func testDiscardingACandidateStopsItsTranscodeAndKeepsThePendingTakesWork() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        let first = try finalize(flow)
        try await waitFor { transcodes.started.count == 1 }
        _ = first
        // The second take is a candidate beside the pending first; going back to the first discards it.
        let candidate = try finalize(flow)
        try await waitFor { transcodes.started.count == 2 }
        XCTAssertEqual(flow.selectedTake?.id, candidate.id)

        flow.previousTake()

        try await waitFor { transcodes.cancelled == 1 }
        XCTAssertEqual(prewarmer.heldCount, 1, "only the kept pending take is still being prepared")
        XCTAssertNil(prewarmer.claim(source: flow.store.fileURL(for: candidate)))
        transcodes.holds = false
    }

    func testDiscardingTheOnlyTakeStopsItsWork() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        try finalize(flow)
        try await waitFor { transcodes.started.count == 1 }
        flow.discardSelected()
        try await waitFor { transcodes.cancelled == 1 }
        XCTAssertEqual(prewarmer.heldCount, 0)
    }

    func testUseTakeFindsTheWorkItStartedAndANewTakeDoesNotInheritIt() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        let take = try finalize(flow)
        try await waitFor { transcodes.started.count == 1 }
        var claimed: SourcePrewarmer.Claim?
        await flow.useTake { _, url in
            claimed = prewarmer.claim(source: url)
            return .queued
        }
        XCTAssertNotNil(claimed, "the intake sees the work the review started, by the file's identity")
        XCTAssertEqual(transcodes.started.count, 1, "no second transcode for the same take")
        _ = take

        // The take at the same path replaced by different bytes is not the prewarmed one.
        let (other, otherPrewarmer) = makeFlow(Transcodes())
        let replaced = try finalize(other, bytes: "first")
        try Data("second, different".utf8).write(to: other.store.fileURL(for: replaced))
        XCTAssertNil(otherPrewarmer.claim(source: other.store.fileURL(for: replaced)))
        transcodes.holds = false
    }

    func testAFailedIntakeStartsFreshWorkForTheRetry() async throws {
        let transcodes = Transcodes()
        let (flow, prewarmer) = makeFlow(transcodes)
        try finalize(flow)
        try await waitFor { transcodes.started.count == 1 }
        await flow.useTake { _, url in
            _ = prewarmer.claim(source: url)
            return .failed("offline")
        }
        XCTAssertEqual(flow.selectedTake?.status, .review)
        try await waitFor { transcodes.started.count == 2 }
        XCTAssertEqual(prewarmer.heldCount, 1)
        transcodes.holds = false
    }

    func testTheSpinnerEndsBeforeTheBackgroundUploadFinishes() async throws {
        let gate = AsyncGate()
        let transcodes = Transcodes(); transcodes.holds = false
        let uploads = Counter()
        let (flow, _) = makeFlow(transcodes, background: { _, reference, _ in
            uploads.bump()
            await gate.wait()
            return .queued(CameraBackgroundSelection(projectID: UUID(), itemID: "item", recordID: reference.id, kind: reference.kind, fileName: reference.fileName))
        })
        let picked = root.appendingPathComponent("bg-\(UUID().uuidString).png")
        try Data("photo".utf8).write(to: picked)
        flow.chooseBackground(from: picked, kind: .image)
        try finalize(flow)

        await flow.useTake { _, _ in .queued }

        XCTAssertFalse(flow.usingTake, "the creator is not held for the second file")
        XCTAssertTrue(flow.queueAccepted)
        try await waitFor { uploads.count == 1 }
        XCTAssertNil(flow.queuedBackground)
        flow.clearBackground()
        XCTAssertNotNil(flow.background, "the background cannot be swapped out from under its own upload")

        await gate.open()
        await flow.finishBackgroundHandover()
        XCTAssertNotNil(flow.queuedBackground)
        XCTAssertEqual(uploads.count, 1)
    }

    private actor AsyncGate {
        private var isOpen = false
        private var waiters: [CheckedContinuation<Void, Never>] = []
        func wait() async { if isOpen { return }; await withCheckedContinuation { waiters.append($0) } }
        func open() { isOpen = true; waiters.forEach { $0.resume() }; waiters = [] }
    }

    private final class Counter: @unchecked Sendable {
        private let lock = NSLock(); private var value = 0
        func bump() { lock.withLock { value += 1 } }
        var count: Int { lock.withLock { value } }
    }
}
