import XCTest
import KriaMediaEngine
@testable import Kria

/// Filesystem journey: a shared original outlives its first post and stays account-scoped.
final class MediaLibraryOriginalsTests: XCTestCase {
    func testReuseSurvivesFirstPostDeletionAndStoreRecreation() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let source = root.appendingPathComponent("picked.mov")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try Data("full quality original".utf8).write(to: source)
        let first = ProjectDirectory(root: root.appendingPathComponent("post-one"))
        let original = try await AssetImportCoordinator(project: first).importAsset(from: source)
        let fingerprint = try XCTUnwrap(original.fingerprint)
        let assetID = UUID()
        let library = MediaLibraryOriginals(owner: "account-a", base: root)
        try await library.retain(assetID: assetID, from: first, mediaID: "one", original: original)
        try FileManager.default.removeItem(at: first.root)
        let reopened = MediaLibraryOriginals(owner: "account-a", base: root)
        let second = ProjectDirectory(root: root.appendingPathComponent("post-two"))
        try await reopened.bind(assetID: assetID, fingerprint: fingerprint, mediaID: "two", to: second)
        let files = try SourceAssetStore(project: second).resolve(mediaIDs: ["two"])
        XCTAssertEqual(try Data(contentsOf: XCTUnwrap(files["two"])), Data("full quality original".utf8))
        let other = MediaLibraryOriginals(owner: "account-b", base: root)
        XCTAssertNil(other.localURL(assetID: assetID, fingerprint: fingerprint))
        try reopened.removeAccount()
        XCTAssertNil(reopened.localURL(assetID: assetID, fingerprint: fingerprint))
        XCTAssertNotNil(try SourceAssetStore(project: second).resolve(mediaIDs: ["two"])["two"])
    }

    func testChangedFileAndWrongFingerprintCannotBeUsedAsAnOriginal() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let source = root.appendingPathComponent("picked.mov")
        try Data("original".utf8).write(to: source)
        let first = ProjectDirectory(root: root.appendingPathComponent("post"))
        let original = try await AssetImportCoordinator(project: first).importAsset(from: source)
        let library = MediaLibraryOriginals(owner: "owner", base: root)
        let assetID = UUID()
        try await library.retain(assetID: assetID, from: first, mediaID: "one", original: original)
        let fingerprint = try XCTUnwrap(original.fingerprint)
        let shared = try XCTUnwrap(library.localURL(assetID: assetID, fingerprint: fingerprint))
        try Data("changed".utf8).write(to: shared)
        do {
            try await library.bind(assetID: assetID, fingerprint: fingerprint, mediaID: "two", to: ProjectDirectory(root: root.appendingPathComponent("other")))
            XCTFail("Changed bytes must never authorize export")
        } catch {}
    }
}
