import CoreImage
import XCTest
@testable import Kria

/// KRI-1119 failure modes written before the code:
/// 1. an old manifest (no `speakerBackground`) still decodes, and a take without a background encodes without the key;
/// 2. the background choice survives a retake and a relaunch, because it lives with the project, not the take;
/// 3. a picked file is copied into the store (the picker file is transient) and replaced files are pruned;
/// 4. with the capability off nothing is restored, stamped on a take, or accepted from the picker;
/// 5. a damaged choice file names a path outside `backgrounds/`: the store must still resolve inside it.
@MainActor final class TeleprompterBackgroundTests: XCTestCase {
    private var root: URL!
    private let selectionProject = UUID()

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("teleprompter-bg-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: root) }

    private func pickedFile(_ name: String = "picked.jpg", bytes: String = "photo") throws -> URL {
        let url = root.appendingPathComponent("picked-\(UUID().uuidString)-\(name)")
        try Data(bytes.utf8).write(to: url)
        return url
    }

    // MARK: Manifest

    func testManifestWithoutABackgroundEncodesWithoutTheKeyAndAnOldManifestDecodes() throws {
        let take = TeleprompterTakeManifest(script: TeleprompterScriptDraft(text: "hello"))
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(take)) as? [String: Any]
        XCTAssertNotNil(json)
        XCTAssertNil(json?["speakerBackground"], "a take with no background must stay byte-compatible with older builds")

        let decoded = try JSONDecoder().decode(TeleprompterTakeManifest.self, from: JSONSerialization.data(withJSONObject: json!))
        XCTAssertEqual(decoded, take)
        XCTAssertNil(decoded.speakerBackground)
    }

    func testManifestRoundTripsTheBackgroundReference() throws {
        var take = TeleprompterTakeManifest(script: TeleprompterScriptDraft(text: "hello"))
        let reference = SpeakerBackgroundReference(id: UUID(), fileName: "bg.mov", kind: .video)
        take.speakerBackground = reference
        let decoded = try JSONDecoder().decode(TeleprompterTakeManifest.self, from: JSONEncoder().encode(take))
        XCTAssertEqual(decoded.speakerBackground, reference)
        XCTAssertEqual(decoded, take)
    }

    // MARK: Store

    func testImportCopiesThePickedFileAndPruneKeepsOnlyNamedFiles() throws {
        let store = TeleprompterStore(accountID: "a", projectID: UUID(), root: root)
        let source = try pickedFile()
        let first = try store.importBackground(from: source, kind: .image)
        let second = try store.importBackground(from: try pickedFile("two.png"), kind: .image)
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path), "the picker file is copied, never moved")
        XCTAssertEqual(try Data(contentsOf: store.backgroundFileURL(for: first)), Data("photo".utf8))
        XCTAssertEqual(first.fileName.split(separator: ".").last.map(String.init), "jpg")
        XCTAssertEqual(second.fileName.split(separator: ".").last.map(String.init), "png")

        store.pruneBackgrounds(keeping: [second.fileName])
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.backgroundFileURL(for: first).path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.backgroundFileURL(for: second).path))
    }

    func testAnUnnamedExtensionFallsBackByKind() throws {
        let store = TeleprompterStore(accountID: "a", projectID: UUID(), root: root)
        let bare = root.appendingPathComponent("noext-\(UUID().uuidString)")
        try Data("x".utf8).write(to: bare)
        XCTAssertTrue(try store.importBackground(from: bare, kind: .video).fileName.hasSuffix(".mov"))
        XCTAssertTrue(try store.importBackground(from: bare, kind: .image).fileName.hasSuffix(".jpg"))
    }

    func testADamagedReferenceCannotEscapeTheBackgroundsFolder() {
        let store = TeleprompterStore(accountID: "a", projectID: UUID(), root: root)
        let reference = SpeakerBackgroundReference(id: UUID(), fileName: "../../../etc/passwd", kind: .image)
        XCTAssertEqual(store.backgroundFileURL(for: reference).deletingLastPathComponent().lastPathComponent, "backgrounds")
        XCTAssertEqual(store.backgroundFileURL(for: reference).lastPathComponent, "passwd")
    }

    // MARK: Flow

    private func makeFlow(enabled: Bool, project: UUID = UUID()) -> TeleprompterRecordingFlow {
        TeleprompterRecordingFlow(accountID: "test", projectID: project, root: root, photoSaver: DiscardingSaver(), speakerBackgroundEnabled: enabled)
    }

    func testChoiceSurvivesARelaunchAndClearingRemovesIt() async throws {
        let project = UUID()
        let flow = makeFlow(enabled: true, project: project)
        flow.chooseBackground(from: try pickedFile(), kind: .image)
        let chosen = try XCTUnwrap(flow.background)
        XCTAssertTrue(FileManager.default.fileExists(atPath: flow.store.backgroundFileURL(for: chosen.reference).path))

        let relaunched = makeFlow(enabled: true, project: project)
        await relaunched.load()
        XCTAssertEqual(relaunched.background?.reference, chosen.reference, "a retake after a relaunch keeps the background")

        relaunched.clearBackground()
        XCTAssertNil(relaunched.background)
        XCTAssertFalse(FileManager.default.fileExists(atPath: relaunched.store.backgroundFileURL(for: chosen.reference).path))
        let again = makeFlow(enabled: true, project: project)
        await again.load()
        XCTAssertNil(again.background)
    }

    func testReplacingTheBackgroundDeletesTheOldFileUnlessATakeStillNamesIt() throws {
        let flow = makeFlow(enabled: true)
        flow.chooseBackground(from: try pickedFile(), kind: .image)
        let first = try XCTUnwrap(flow.background).reference
        var take = TeleprompterTakeManifest(status: .review, script: flow.script.draft, slot: .pending)
        take.speakerBackground = first
        flow.seedSelectedTakeForTesting(take)
        flow.chooseBackground(from: try pickedFile("two.png"), kind: .image)
        XCTAssertTrue(FileManager.default.fileExists(atPath: flow.store.backgroundFileURL(for: first).path),
                      "a take that was filmed in front of it still needs the file to upload")
        let second = try XCTUnwrap(flow.background).reference
        XCTAssertNotEqual(first, second)
    }

    func testReplacingTheBackgroundWithNoTakeAttachedDeletesTheOldFile() throws {
        let flow = makeFlow(enabled: true)
        flow.chooseBackground(from: try pickedFile(), kind: .image)
        let first = try XCTUnwrap(flow.background).reference
        flow.chooseBackground(from: try pickedFile("two.png"), kind: .image)
        XCTAssertFalse(FileManager.default.fileExists(atPath: flow.store.backgroundFileURL(for: first).path))
    }

    func testCapabilityOffNeverRestoresStampsOrAcceptsABackground() async throws {
        let project = UUID()
        let on = makeFlow(enabled: true, project: project)
        on.chooseBackground(from: try pickedFile(), kind: .image)
        XCTAssertNotNil(on.background)

        let off = makeFlow(enabled: false, project: project)
        await off.load()
        XCTAssertNil(off.background, "a persisted choice is ignored while the server does not offer the feature")
        off.chooseBackground(from: try pickedFile(), kind: .image)
        XCTAssertNil(off.background)

        let take = TeleprompterTakeManifest(script: off.script.draft, slot: .pending)
        off.seedActiveTakeForTesting(take)
        off.camera.onFinalized?(off.store.fileURL(for: take), nil)
        XCTAssertNil(off.selectedTake?.speakerBackground)
    }

    func testTheTakeIsStampedWithTheChoiceAtTheEndOfFilming() throws {
        let flow = makeFlow(enabled: true)
        let take = TeleprompterTakeManifest(script: flow.script.draft, slot: .pending)
        flow.seedActiveTakeForTesting(take)
        flow.chooseBackground(from: try pickedFile(), kind: .video)
        let chosen = try XCTUnwrap(flow.background).reference
        flow.camera.onFinalized?(flow.store.fileURL(for: take), nil)
        XCTAssertEqual(flow.selectedTake?.speakerBackground, chosen)
        XCTAssertEqual(try flow.store.loadManifests().first?.speakerBackground, chosen)
    }

    // MARK: Use take (S25 spirit: the take never waits on, or fails because of, its background)

    private final class Calls: @unchecked Sendable {
        var order: [String] = []
    }

    private func finalizedFlow(enabled: Bool = true, withBackground: Bool = true, calls: Calls,
                               background: @escaping BackgroundIntake) throws -> TeleprompterRecordingFlow {
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, photoSaver: DiscardingSaver(),
                                             speakerBackgroundEnabled: enabled, backgroundIntake: background)
        if withBackground { flow.chooseBackground(from: try pickedFile(), kind: .image) }
        let take = TeleprompterTakeManifest(script: flow.script.draft, slot: .pending)
        try flow.store.save(manifest: take)
        try FileManager.default.createDirectory(at: flow.store.takesDirectory, withIntermediateDirectories: true)
        try Data("movie".utf8).write(to: flow.store.fileURL(for: take))
        flow.seedActiveTakeForTesting(take)
        flow.camera.seedFinalizedAudioCheckForTesting(TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        flow.camera.onFinalized?(flow.store.fileURL(for: take), nil)
        return flow
    }

    private func selection(for reference: SpeakerBackgroundReference) -> CameraBackgroundSelection {
        CameraBackgroundSelection(projectID: selectionProject, itemID: "item", recordID: reference.id, kind: reference.kind, fileName: reference.fileName)
    }

    func testUseTakeHandsOverTheClipFirstThenTheBackgroundOnce() async throws {
        let calls = Calls()
        let flow = try finalizedFlow(calls: calls) { [self] _, reference, url in
            calls.order.append("background:\(FileManager.default.fileExists(atPath: url.path))")
            return .queued(selection(for: reference))
        }
        await flow.useTake { _, _ in calls.order.append("clip"); return .queued }
        XCTAssertEqual(calls.order, ["clip", "background:true"], "the take is handed over before its background")
        let reference = try XCTUnwrap(flow.selectedTake?.speakerBackground)
        XCTAssertEqual(flow.queuedBackground, selection(for: reference))
        XCTAssertEqual(flow.queuedBackground?.clientUploadID, "ios-\(reference.id.uuidString)")
        XCTAssertEqual(flow.background?.uploaded, true)
        XCTAssertNil(flow.backgroundNote)

        await flow.uploadBackgroundIfNeeded()
        XCTAssertEqual(calls.order.count, 2, "polling after attach must not upload the background a second time")
    }

    func testAFailedBackgroundUploadStillAttachesTheTakeAndSaysSo() async throws {
        let calls = Calls()
        let flow = try finalizedFlow(calls: calls) { _, _, _ in calls.order.append("background"); return .failed("No connection.") }
        await flow.useTake { _, _ in .attached }
        XCTAssertTrue(flow.attachmentFinished, "the take is attached whatever happens to the background")
        XCTAssertEqual(flow.selectedTake?.status, .accepted)
        XCTAssertEqual(flow.backgroundNote, "Your take was added, but its background couldn’t be uploaded. No connection.")
        XCTAssertNotEqual(flow.background?.uploaded, true, "a failed upload is retried on the next call")

        calls.order = []
        await flow.uploadBackgroundIfNeeded()
        XCTAssertEqual(calls.order, ["background"])
    }

    func testAFailedClipIntakeNeverTouchesTheBackground() async throws {
        let calls = Calls()
        let flow = try finalizedFlow(calls: calls) { _, _, _ in calls.order.append("background"); return .failed("x") }
        await flow.useTake { _, _ in .failed("Couldn’t add this take.") }
        XCTAssertEqual(calls.order, [])
        XCTAssertEqual(flow.selectedTake?.status, .review, "retry stays possible and will do both")
        XCTAssertNil(flow.queuedBackground)
    }

    func testNoBackgroundOrCapabilityOffMeansNoBackgroundUpload() async throws {
        for (enabled, picked) in [(true, false), (false, true)] {
            let calls = Calls()
            let flow = try finalizedFlow(enabled: enabled, withBackground: picked, calls: calls) { _, _, _ in calls.order.append("background"); return .failed("x") }
            await flow.useTake { _, _ in .queued }
            XCTAssertEqual(calls.order, [], "enabled \(enabled), picked \(picked)")
            XCTAssertNil(flow.backgroundNote)
        }
    }

    func testAMissingBackgroundFileIsANoteNotAFailure() async throws {
        let calls = Calls()
        let flow = try finalizedFlow(calls: calls) { _, _, _ in calls.order.append("background"); return .failed("x") }
        let reference = try XCTUnwrap(flow.selectedTake?.speakerBackground)
        try FileManager.default.removeItem(at: flow.store.backgroundFileURL(for: reference))
        await flow.useTake { _, _ in .attached }
        XCTAssertEqual(calls.order, [])
        XCTAssertTrue(flow.attachmentFinished)
        XCTAssertNotNil(flow.backgroundNote)
    }

    // MARK: Video background restarts when the recording really starts

    private final class FakeProvider: LiveBackgroundProviding, @unchecked Sendable {
        var restarts = 0, stops = 0
        func frame() -> CIImage? { nil }
        func setActive(_ active: Bool) {}
        func restart() { restarts += 1 }
        func stop() { stops += 1 }
    }

    private final class Providers: @unchecked Sendable {
        private let lock = NSLock()
        private(set) var made: [FakeProvider] = []
        func make() -> FakeProvider { lock.lock(); defer { lock.unlock() }; let p = FakeProvider(); made.append(p); return p }
    }

    private func flowWithFakeProviders(_ providers: Providers) -> TeleprompterRecordingFlow {
        TeleprompterRecordingFlow(accountID: "test", projectID: UUID(), root: root, photoSaver: DiscardingSaver(),
                                  speakerBackgroundEnabled: true,
                                  makeLivePreview: { LiveBackgroundPreview(segmenter: FailingNoMask()) },
                                  makeBackgroundProvider: { _, _ in providers.make() })
    }

    private struct FailingNoMask: PersonSegmenting {
        struct Failure: Error {}
        func mask(for frame: CVPixelBuffer) throws -> CVPixelBuffer { throw Failure() }
    }

    private func waitForPreview(_ flow: TeleprompterRecordingFlow, count: Int, _ providers: Providers) async throws {
        for _ in 0..<150 where providers.made.count < count || flow.livePreview == nil { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertGreaterThanOrEqual(providers.made.count, count)
    }

    func testRecordStartRestartsTheVideoOnceAndNotBeforeTheMovieOutputStarts() async throws {
        let providers = Providers()
        let flow = flowWithFakeProviders(providers)
        flow.chooseBackground(from: try pickedFile("clip.mov"), kind: .video)
        try await waitForPreview(flow, count: 1, providers)
        let provider = try XCTUnwrap(providers.made.first)

        flow.stage = .countdown
        flow.cameraChanged(.ready)
        XCTAssertEqual(provider.restarts, 0, "not at pick time and not during the countdown")
        flow.stage = .starting
        XCTAssertEqual(provider.restarts, 0, "not at the button tap")
        flow.cameraChanged(.recording)
        XCTAssertEqual(provider.restarts, 1, "the moment the movie output has started")
        XCTAssertEqual(flow.stage, .recording)
        flow.cameraChanged(.recording)
        XCTAssertEqual(provider.restarts, 1, "a repeated phase report is not a second start")
    }

    func testARetakeRestartsTheVideoAgain() async throws {
        let providers = Providers()
        let flow = flowWithFakeProviders(providers)
        flow.chooseBackground(from: try pickedFile("clip.mov"), kind: .video)
        try await waitForPreview(flow, count: 1, providers)
        let provider = try XCTUnwrap(providers.made.first)
        for take in 1...2 {
            flow.stage = .starting
            flow.cameraChanged(.recording)
            XCTAssertEqual(provider.restarts, take)
            flow.stage = .camera
        }
    }

    func testClearingAndRepickingLeavesNoStalePlayer() async throws {
        let providers = Providers()
        let flow = flowWithFakeProviders(providers)
        flow.chooseBackground(from: try pickedFile("one.mov"), kind: .video)
        try await waitForPreview(flow, count: 1, providers)
        let first = try XCTUnwrap(providers.made.first)
        let preview = try XCTUnwrap(flow.livePreview)
        flow.clearBackground()
        XCTAssertNil(flow.livePreview)
        flow.stage = .starting
        flow.cameraChanged(.recording)
        XCTAssertEqual(first.restarts, 0, "nothing restarts once the background is cleared")

        flow.stage = .camera
        flow.chooseBackground(from: try pickedFile("two.mov"), kind: .video)
        try await waitForPreview(flow, count: 2, providers)
        let second = providers.made[1]
        flow.stage = .starting
        flow.cameraChanged(.recording)
        XCTAssertEqual(second.restarts, 1)
        XCTAssertEqual(first.restarts, 0)
        _ = preview
    }

    func testReplacingTheVideoStopsTheOldPlayer() async throws {
        let providers = Providers()
        let flow = flowWithFakeProviders(providers)
        flow.chooseBackground(from: try pickedFile("one.mov"), kind: .video)
        try await waitForPreview(flow, count: 1, providers)
        flow.chooseBackground(from: try pickedFile("two.mov"), kind: .video)
        try await waitForPreview(flow, count: 2, providers)
        for _ in 0..<50 where providers.made[0].stops == 0 { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertEqual(providers.made[0].stops, 1, "the old player is released")
        XCTAssertEqual(providers.made[1].stops, 0)
    }

    // MARK: Statement to the server (PUT speaker-background)

    private final class Statements: @unchecked Sendable {
        var sent: [SpeakerBackgroundIntent] = []
        var outcomes: [SpeakerBackgroundOutcome] = []
        func next(_ intent: SpeakerBackgroundIntent) -> SpeakerBackgroundOutcome {
            sent.append(intent)
            return outcomes.isEmpty ? .saved : outcomes.removeFirst()
        }
    }

    private func statedFlow(_ statements: Statements, project: UUID = UUID(), picked: Bool = true) throws -> TeleprompterRecordingFlow {
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: project, root: root, photoSaver: DiscardingSaver(),
                                             speakerBackgroundEnabled: true,
                                             backgroundIntake: { [self] _, reference, _ in .queued(selection(for: reference)) },
                                             backgroundStatement: { statements.next($0) })
        if picked { flow.chooseBackground(from: try pickedFile(), kind: .image) }
        return flow
    }

    private func finish(_ flow: TeleprompterRecordingFlow) throws {
        let take = TeleprompterTakeManifest(script: flow.script.draft, slot: .pending)
        try flow.store.save(manifest: take)
        try FileManager.default.createDirectory(at: flow.store.takesDirectory, withIntermediateDirectories: true)
        try Data("movie".utf8).write(to: flow.store.fileURL(for: take))
        flow.seedActiveTakeForTesting(take)
        flow.camera.seedFinalizedAudioCheckForTesting(TakeAudioCheck(verdict: .ok, peakDb: -12, activeFraction: 0.8))
        flow.camera.onFinalized?(flow.store.fileURL(for: take), nil)
    }

    func testUseTakeStatesTheUploadedBackgroundAfterTheUploadOnce() async throws {
        let statements = Statements()
        let flow = try statedFlow(statements)
        try finish(flow)
        let reference = try XCTUnwrap(flow.selectedTake?.speakerBackground)
        await flow.useTake { _, _ in .attached }
        XCTAssertEqual(statements.sent, [.set(clientUploadID: "ios-\(reference.id.uuidString)", eventID: "speaker-bg-set-\(reference.id.uuidString)")])
        XCTAssertEqual(flow.background?.stated, true)
        await flow.syncBackgroundStatement()
        XCTAssertEqual(statements.sent.count, 1, "stated once, not on every poll")
    }

    func testAFailedStatementKeepsTheTakeAndTheUploadAndRetriesAtTheNextOpeningWithTheSameEventId() async throws {
        let project = UUID()
        let statements = Statements(); statements.outcomes = [.failed]
        let flow = try statedFlow(statements, project: project)
        try finish(flow)
        await flow.useTake { _, _ in .attached }
        XCTAssertTrue(flow.attachmentFinished, "the take stays attached")
        XCTAssertEqual(flow.selectedTake?.status, .accepted)
        XCTAssertEqual(flow.background?.uploaded, true, "the background upload stays handed over")
        XCTAssertNotEqual(flow.background?.stated, true)
        XCTAssertEqual(flow.backgroundNote, TeleprompterRecordingFlow.statementFailureNote)
        XCTAssertEqual(TeleprompterRecordingFlow.statementFailureNote, "Couldn’t tell Kria which picture is your background. Your take is saved.")

        let reopened = try statedFlow(statements, project: project, picked: false)
        await reopened.load()
        await reopened.syncBackgroundStatement()
        XCTAssertEqual(statements.sent.count, 2)
        XCTAssertEqual(statements.sent[0], statements.sent[1], "same upload id and same event id: the server replays instead of applying twice")
        XCTAssertEqual(reopened.background?.stated, true)
    }

    func testTheServerTurningItOffIsQuietAndNotRetried() async throws {
        let statements = Statements(); statements.outcomes = [.unavailable]
        let flow = try statedFlow(statements)
        try finish(flow)
        await flow.useTake { _, _ in .attached }
        XCTAssertNil(flow.backgroundNote)
        XCTAssertEqual(flow.background?.stated, true)
        await flow.syncBackgroundStatement()
        XCTAssertEqual(statements.sent.count, 1)
    }

    func testABackgroundClearedBeforeUseTakeSendsNothing() async throws {
        let statements = Statements()
        let flow = try statedFlow(statements)
        flow.clearBackground()
        try finish(flow)
        await flow.useTake { _, _ in .attached }
        await flow.syncBackgroundStatement()
        XCTAssertEqual(statements.sent, [])
    }

    func testClearingAPickTheServerKnowsSendsOneClearAndRetriesItUntilItLands() async throws {
        let project = UUID()
        let statements = Statements()
        let flow = try statedFlow(statements, project: project)
        try finish(flow)
        await flow.useTake { _, _ in .attached }
        XCTAssertEqual(statements.sent.count, 1)

        statements.outcomes = [.failed]
        flow.clearBackground()
        await flow.syncBackgroundStatement()
        guard case .clear(let eventID) = statements.sent.last else { return XCTFail("\(statements.sent)") }
        XCTAssertEqual(flow.backgroundNote, TeleprompterRecordingFlow.statementFailureNote)

        let reopened = try statedFlow(statements, project: project, picked: false)
        await reopened.load()
        await reopened.syncBackgroundStatement()
        XCTAssertEqual(statements.sent.last, .clear(eventID: eventID), "the retry replays the same clear")
        await reopened.syncBackgroundStatement()
        XCTAssertEqual(statements.sent.count, 3, "once it landed it is not sent again")
    }

    func testAnotherPickReplacesTheOldOneSoNoClearIsSent() async throws {
        let statements = Statements()
        let flow = try statedFlow(statements)
        try finish(flow)
        await flow.useTake { _, _ in .attached }
        flow.clearBackground()
        flow.chooseBackground(from: try pickedFile("two.png"), kind: .image)
        let second = try XCTUnwrap(flow.background).reference
        var take = try XCTUnwrap(flow.selectedTake)
        take.speakerBackground = second
        flow.seedSelectedTakeForTesting(take)
        await flow.uploadBackgroundIfNeeded()
        XCTAssertEqual(statements.sent.count, 2)
        XCTAssertEqual(statements.sent.last?.clientUploadID, "ios-\(second.id.uuidString)")
        XCTAssertFalse(statements.sent.contains { if case .clear = $0 { true } else { false } })
    }

    // MARK: Capability gating

    func testSpeakerBackgroundIsOffUnlessTheServerAdvertisesIt() throws {
        func caps(_ extra: String) throws -> CreationCapabilities {
            try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]\#(extra)}"#.utf8))
        }
        XCTAssertFalse(try caps("").speakerBackgroundEnabled, "an older server omits it")
        XCTAssertFalse(try caps(#","speaker_background":false"#).speakerBackgroundEnabled)
        XCTAssertTrue(try caps(#","speaker_background":true"#).speakerBackgroundEnabled)
    }
}

/// The Photos copy is out of scope here and must never touch the simulator's library.
private struct DiscardingSaver: PhotoLibrarySaving { func saveVideo(at url: URL) async throws {} }
