import XCTest
@testable import Kria

final class TeleprompterStoreTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("teleprompter-store-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: root) }

    func testDraftAndManifestsAreScopedToAccountAndProject() throws {
        let project = UUID()
        let first = TeleprompterStore(accountID: "subject-a", projectID: project, root: root)
        let second = TeleprompterStore(accountID: "subject-b", projectID: project, root: root)
        let draft = TeleprompterScriptDraft(text: "private script", anchorUTF16: 4)
        try first.save(draft: draft)
        XCTAssertEqual(try first.loadDraft(), draft)
        XCTAssertNil(try second.loadDraft())
        XCTAssertNotEqual(first.directory, second.directory)

        let take = TeleprompterTakeManifest(script: draft)
        try first.save(manifest: take)
        XCTAssertEqual(try first.loadManifests(), [take])
        XCTAssertTrue((try second.loadManifests()).isEmpty)
    }

    func testManifestPersistsIndependentScriptSnapshotAndStableIntakeID() throws {
        let store = TeleprompterStore(accountID: "subject", projectID: UUID(), root: root)
        let snapshot = TeleprompterScriptDraft(text: "words at capture", anchorUTF16: 6)
        let intakeID = UUID()
        var take = TeleprompterTakeManifest(script: snapshot, intakeID: intakeID)
        try store.save(manifest: take) // durable candidate record before capture
        take.status = .review
        take.slot = .pending
        try store.save(manifest: take) // atomic finalized state replaces the candidate record

        let restored = try XCTUnwrap(store.loadManifests().first)
        XCTAssertEqual(restored.script, snapshot)
        XCTAssertEqual(restored.intakeID, intakeID)
        XCTAssertEqual(restored.slot, .pending)
        XCTAssertEqual(restored.status, .review)
        XCTAssertEqual(store.fileURL(for: restored).deletingLastPathComponent(), store.takesDirectory)
    }

    func testRemovingMetadataRetainsMovieUntilExplicitDiscardAndProjectCleanupIsScoped() throws {
        let project = UUID()
        let store = TeleprompterStore(accountID: "subject", projectID: project, root: root)
        let take = TeleprompterTakeManifest(script: .init(text: "keep file"))
        let movie = store.fileURL(for: take)
        try FileManager.default.createDirectory(at: store.takesDirectory, withIntermediateDirectories: true)
        try Data("movie".utf8).write(to: movie)
        try store.save(manifest: take)
        try store.remove(manifest: take)
        XCTAssertTrue(FileManager.default.fileExists(atPath: movie.path))

        try TeleprompterStore.removeProject(accountID: "subject", projectID: project, root: root)
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.directory.path))
    }

    func testCorruptCandidateDoesNotHideValidPendingTakeDuringRecovery() throws {
        let store = TeleprompterStore(accountID: "subject", projectID: UUID(), root: root)
        let pending = TeleprompterTakeManifest(script: .init(text: "safe take"), slot: .pending)
        try store.save(manifest: pending)
        try Data("not JSON".utf8).write(to: store.takesDirectory.appendingPathComponent("broken-candidate.json"))

        let recovered = try store.loadManifestsRecoveringCorruption()
        XCTAssertEqual(recovered.manifests, [pending])
        XCTAssertEqual(recovered.corruptedManifestNames, ["broken-candidate.json"])
        XCTAssertNotNil(recovered.recoveryWarning)
    }
}
