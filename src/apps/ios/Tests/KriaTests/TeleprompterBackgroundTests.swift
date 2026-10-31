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
