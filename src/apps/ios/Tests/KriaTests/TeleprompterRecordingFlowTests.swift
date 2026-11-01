import XCTest
@testable import Kria

@MainActor final class TeleprompterRecordingFlowTests: XCTestCase {
    // @Published emits the incoming phase before camera.phase has changed.
    // Capture acknowledgment must start scrolling from that incoming value.
    func testIncomingRecordingAcknowledgmentStartsScriptBeforeStoredPhaseChanges() {
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID())
        flow.script.updateText("Read this after recording starts.")
        flow.stage = .starting
        XCTAssertEqual(flow.camera.phase, .idle)
        flow.cameraChanged(.recording)
        XCTAssertEqual(flow.stage, .recording)
        XCTAssertTrue(flow.script.isPlaying)
    }

    func testIncomingFinalizationPausesTextAndFailureCancelsCountdown() {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root)
        flow.script.updateText("A recording can finish independently of this script.")
        flow.script.resume()
        flow.stage = .recording
        flow.cameraChanged(.finalizing)
        XCTAssertEqual(flow.stage, .finalizing)
        XCTAssertFalse(flow.script.isPlaying)

        flow.stage = .countdown
        flow.cameraChanged(.failed)
        XCTAssertEqual(flow.stage, .camera)
    }

    // MARK: KRI-1149 — a take ends only when the creator ends it

    private func recordingFlow(script: TeleprompterScript = TeleprompterScript(), grace: Duration = .milliseconds(30)) throws -> TeleprompterRecordingFlow {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, script: script, viewRemovalGrace: grace)
        flow.camera.seedRecordingForTesting()
        flow.seedActiveTakeForTesting(TeleprompterTakeManifest(script: flow.script.draft, slot: .pending))
        flow.stage = .recording
        return flow
    }

    func testTakeKeepsRecordingPastTheEndOfTheScriptAndPastSixtySeconds() throws {
        var now: TimeInterval = 0
        let script = TeleprompterScript(text: "one two", wordsPerMinute: 240, uptime: { now })
        script.setLayout(lines: [TeleprompterVisualLine(range: 0..<3, anchor: 0, text: "one"), TeleprompterVisualLine(range: 4..<7, anchor: 100, text: "two")], travelDistance: 100)
        let flow = try recordingFlow(script: script)
        script.resume()
        for second in [5.0, 20, 61, 70] {
            now = second; script.tick()
            XCTAssertEqual(flow.stage, .recording, "t=\(second)s")
            XCTAssertEqual(flow.camera.phase, .recording, "t=\(second)s")
        }
        XCTAssertTrue(script.hasEnded)
        XCTAssertNil(flow.camera.stopReason)
    }

    func testSceneInactiveWhileRecordingDoesNotStopTheTake() throws {
        let flow = try recordingFlow()
        flow.scenePhaseChanged(.inactive)
        flow.scenePhaseChanged(.active)
        XCTAssertEqual(flow.stage, .recording)
        XCTAssertEqual(flow.camera.phase, .recording)
        XCTAssertNil(flow.camera.stopReason)
    }

    func testBackgroundingWhileRecordingStopsTheTakeAsBackgrounded() throws {
        let flow = try recordingFlow()
        flow.scenePhaseChanged(.inactive)
        flow.scenePhaseChanged(.background)
        XCTAssertEqual(flow.stage, .finalizing)
        XCTAssertEqual(flow.camera.stopReason, .backgrounded)
    }

    func testTransientDisappearanceThatComesBackDoesNotStopTheTake() async throws {
        let flow = try recordingFlow(grace: .milliseconds(150))
        flow.viewDisappeared()
        try await Task.sleep(for: .milliseconds(30))
        flow.viewAppeared()
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertEqual(flow.stage, .recording)
        XCTAssertEqual(flow.camera.phase, .recording)
        XCTAssertNil(flow.camera.stopReason)
    }

    func testViewThatStaysGoneStopsTheTakeAfterTheGrace() async throws {
        let flow = try recordingFlow(grace: .milliseconds(30))
        flow.viewDisappeared()
        XCTAssertEqual(flow.stage, .recording, "the stop waits for the grace so a transient disappearance can come back")
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(flow.stage, .finalizing)
        XCTAssertEqual(flow.camera.stopReason, .viewRemoved)
    }

    func testCreatorStopIsRecordedAsUserAndNotAsAnInterruption() throws {
        let flow = try recordingFlow()
        flow.stop()
        XCTAssertEqual(flow.camera.stopReason, .user)
        XCTAssertFalse(flow.camera.interrupted)
    }

    func testFinalizedTakeManifestCarriesTheStopReason() async throws {
        let (flow, _) = try makeFlow(saver: RecordingSaver())
        let take = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        XCTAssertNil(flow.selectedTake?.stopReason)
        XCTAssertEqual(flow.selectedTake?.id, take.id)

        let (interruptedFlow, _) = try makeFlow(saver: RecordingSaver())
        interruptedFlow.camera.seedRecordingForTesting()
        interruptedFlow.camera.stopRecording(reason: .backgrounded)
        _ = try seedFinalizedTake(interruptedFlow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        XCTAssertEqual(interruptedFlow.selectedTake?.stopReason, .backgrounded)
        XCTAssertEqual(interruptedFlow.selectedTake?.interrupted, true)
    }

    // MARK: KRI-653 — silent takes and the Photos copy
    //
    // Failure modes written before the code:
    // 1. a silent take is never attached, however it got to review;
    // 2. a silent take is still the creator's footage, so it is still copied to Photos;
    // 3. the Photos copy is read during performChanges, so deleting the movie before it finishes
    //    would lose the copy: removal must wait for the save and must not wait forever on a denial;
    // 4. a denied Photos permission is a note, never a blocker.

    private func makeFlow(saver: PhotoLibrarySaving) throws -> (TeleprompterRecordingFlow, URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return (TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, photoSaver: saver), root)
    }

    private func seedFinalizedTake(_ flow: TeleprompterRecordingFlow, check: TakeAudioCheck?) throws -> TeleprompterTakeManifest {
        let take = TeleprompterTakeManifest(script: flow.script.draft, slot: .pending)
        try flow.store.save(manifest: take)
        try FileManager.default.createDirectory(at: flow.store.takesDirectory, withIntermediateDirectories: true)
        try Data("movie".utf8).write(to: flow.store.fileURL(for: take))
        flow.seedActiveTakeForTesting(take)
        flow.camera.seedFinalizedAudioCheckForTesting(check)
        flow.camera.onFinalized?(flow.store.fileURL(for: take), nil)
        return take
    }

    func testSilentTakeIsNeverAttachedAndExplainsWhy() async throws {
        let (flow, _) = try makeFlow(saver: RecordingSaver())
        let silent = TakeAudioCheck(verdict: .silent, peakDb: -120, activeFraction: 0)
        _ = try seedFinalizedTake(flow, check: silent)
        XCTAssertEqual(flow.selectedTake?.audioCheck?.verdict, .silent)
        XCTAssertEqual(flow.error, TeleprompterRecordingFlow.silentTakeMessage)

        var intakeCalls = 0
        await flow.useTake { _, _ in intakeCalls += 1; return .queued }
        XCTAssertEqual(intakeCalls, 0, "a silent take must not reach intake")
        XCTAssertFalse(flow.usingTake)
        XCTAssertEqual(flow.selectedTake?.status, .review)
    }

    func testAudibleTakeIsStillUsable() async throws {
        let (flow, _) = try makeFlow(saver: RecordingSaver())
        _ = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        XCTAssertNil(flow.error)
        var intakeCalls = 0
        await flow.useTake { _, _ in intakeCalls += 1; return .queued }
        XCTAssertEqual(intakeCalls, 1)
    }

    func testEveryPlayableTakeIsCopiedToPhotosEvenWhenSilent() async throws {
        let saver = RecordingSaver()
        let (flow, _) = try makeFlow(saver: saver)
        let take = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .silent, peakDb: -120, activeFraction: 0))
        await saver.waitForSaves(1)
        XCTAssertEqual(saver.savedFileNames, [flow.store.fileURL(for: take).lastPathComponent])
    }

    func testMovieOutlivesAPendingPhotosSave() async throws {
        let saver = RecordingSaver(gated: true)
        let (flow, _) = try makeFlow(saver: saver)
        let take = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        let movie = flow.store.fileURL(for: take)
        await saver.waitForSaves(1)

        flow.discardSelected()
        XCTAssertTrue(FileManager.default.fileExists(atPath: movie.path), "Photos is still reading this movie")

        saver.open()
        for _ in 0..<50 where FileManager.default.fileExists(atPath: movie.path) {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: movie.path), "removed once the save finished")
    }

    // MARK: KRI-1119 — S25: the matte never gates the take

    private struct ThrowingSegmenter: PersonSegmenting {
        struct Failure: Error {}
        func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer { throw Failure() }
    }

    private func solidFrame() -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, 36, 64, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
        return buffer!
    }

    func testRecordingDoesNotDependOnMatte() async throws {
        let png = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: png) }
        let space = CGColorSpaceCreateDeviceRGB()
        let context = CGContext(data: nil, width: 4, height: 4, bitsPerComponent: 8, bytesPerRow: 0, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        try XCTUnwrap(UIImage(cgImage: context.makeImage()!).pngData()).write(to: png)

        func finish(withMatte: Bool) async throws -> (TeleprompterRecordingFlow, TeleprompterTakeManifest) {
            let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            addTeardownBlock { try? FileManager.default.removeItem(at: root) }
            let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, photoSaver: RecordingSaver(),
                                                 speakerBackgroundEnabled: withMatte,
                                                 makeLivePreview: { LiveBackgroundPreview(segmenter: ThrowingSegmenter()) })
            if withMatte {
                flow.chooseBackground(from: png, kind: .image)
                for _ in 0..<100 where flow.livePreview == nil { try await Task.sleep(for: .milliseconds(20)) }
                let preview = try XCTUnwrap(flow.livePreview, "the background loaded and the live preview is on")
                for _ in 0..<10 { preview.consume(solidFrame(), mirrored: true) }
                XCTAssertEqual(preview.statistics.framesRaw, 10, "every frame fell back to the raw camera")
                XCTAssertEqual(preview.statistics.framesMatted, 0)
            }
            flow.stage = .recording
            flow.cameraChanged(.finalizing)
            let take = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
            return (flow, take)
        }

        let (withMatte, _) = try await finish(withMatte: true)
        let (without, _) = try await finish(withMatte: false)
        XCTAssertEqual(withMatte.stage, without.stage)
        XCTAssertEqual(withMatte.stage, .review)
        XCTAssertEqual(withMatte.selectedTake?.status, .review)
        XCTAssertEqual(withMatte.selectedTake?.status, without.selectedTake?.status)
        XCTAssertEqual(withMatte.selectedTake?.audioCheck, without.selectedTake?.audioCheck)
        XCTAssertNil(withMatte.error, "a failed matte is not a take error")
        XCTAssertEqual(withMatte.camera.phase, without.camera.phase)
        XCTAssertNotNil(withMatte.selectedTake?.speakerBackground, "the choice is recorded, the movie stays raw")
    }

    func testDeniedPhotosPermissionIsANoteNotABlocker() async throws {
        let saver = RecordingSaver(failure: PhotoLibraryError.notAuthorized)
        let (flow, _) = try makeFlow(saver: saver)
        _ = try seedFinalizedTake(flow, check: TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        for _ in 0..<50 where flow.photosNote == nil { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(flow.photosNote, TeleprompterRecordingFlow.photosNoteMessage)
        XCTAssertEqual(flow.selectedTake?.status, .review, "the take is still reviewable and usable")
    }
}

/// Stands in for `PhotoLibrarySaver`: records what it was asked to save, can hold a save open, can fail.
private final class RecordingSaver: PhotoLibrarySaving, @unchecked Sendable {
    private let lock = NSLock()
    private var names: [String] = []
    private var gate: CheckedContinuation<Void, Never>?
    private var opened: Bool
    private let failure: Error?
    init(gated: Bool = false, failure: Error? = nil) { opened = !gated; self.failure = failure }

    var savedFileNames: [String] { lock.lock(); defer { lock.unlock() }; return names }

    /// Synchronous so the lock is never held across a suspension point.
    private func record(_ name: String) -> Bool { lock.lock(); defer { lock.unlock() }; names.append(name); return !opened }
    private func park(_ continuation: CheckedContinuation<Void, Never>) {
        lock.lock()
        if opened { lock.unlock(); continuation.resume() } else { gate = continuation; lock.unlock() }
    }

    func saveVideo(at url: URL) async throws {
        if record(url.lastPathComponent) { await withCheckedContinuation { park($0) } }
        if let failure { throw failure }
    }

    func open() {
        lock.lock(); opened = true; let pending = gate; gate = nil; lock.unlock()
        pending?.resume()
    }

    func waitForSaves(_ count: Int) async {
        for _ in 0..<100 where savedFileNames.count < count { try? await Task.sleep(for: .milliseconds(20)) }
    }
}
