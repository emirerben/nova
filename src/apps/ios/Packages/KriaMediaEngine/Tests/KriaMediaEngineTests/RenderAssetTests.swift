import Foundation
import XCTest
@testable import KriaMediaEngine

final class RenderAssetTests: XCTestCase {
    func testBackendV2RecipeAndSavedReceiptKeepManifest() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/kria_edit_recipe_v2.json").standardizedFileURL
        var recipe = try RecipeJSON.decode(Data(contentsOf: fixture))
        try recipe.validate()
        XCTAssertEqual(recipe.schemaVersion, 2)
        XCTAssertEqual(recipe.assetManifest?.assets.first?.source, .original(mediaID: "analysis-proxy-source-1"))
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .local)
        let restored = try JSONDecoder().decode(EditRecipe.self, from: JSONEncoder().encode(recipe))
        XCTAssertEqual(restored, recipe)
        var future = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any])
        future["future_render_lane"] = ["effect": "unknown"]
        XCTAssertThrowsError(try RecipeJSON.decode(JSONSerialization.data(withJSONObject: future)))
        recipe.assetManifest = nil
        XCTAssertThrowsError(try recipe.validate())
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
    }

    private func directory() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }
    private func reference(_ file: URL, id: String = "music") throws -> RenderAssetReference {
        RenderAssetReference(id: id, fingerprint: try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: file)),
                             source: .library(catalog: .music, catalogID: "track", generation: "1"))
    }

    func testBackendWireShapeAndUnknownFields() throws {
        let document = """
        {"version":1,"assets":[{"kind":"original","id":"clip","media_id":"proxy-id","fingerprint":{"sha256":"\(String(repeating: "a", count: 64))","byte_count":100}}]}
        """
        let manifest = try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(document.utf8))
        XCTAssertEqual(manifest.assets.first?.source, .original(mediaID: "proxy-id"))
        let encoded = try RecipeJSON.encoder().encode(manifest)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: encoded) as? NSDictionary,
                       try JSONSerialization.jsonObject(with: Data(document.utf8)) as? NSDictionary)
        for bad in [document.replacingOccurrences(of: "\"version\":1", with: "\"version\":2"),
                    document.replacingOccurrences(of: "\"kind\":\"original\"", with: "\"kind\":\"generated\""),
                    document.replacingOccurrences(of: "\"media_id\"", with: "\"url\":\"https://example.invalid\",\"media_id\""),
                    document.replacingOccurrences(of: "\"byte_count\":100", with: "\"byte_count\":0"),
                    document.replacingOccurrences(of: "\"byte_count\":100", with: "\"byte_count\":100,\"future\":true")] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(bad.utf8)))
        }
    }

    func testLibraryCacheVerifiesDownloadsAndRepairsCorruptBytes() async throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let download = root.appendingPathComponent("download")
        try Data("licensed song".utf8).write(to: download)
        let asset = try reference(download)
        let cache = RenderLibraryCache(root: root.appendingPathComponent("library"))
        let installed = try await cache.install(downloadedFile: download, for: asset)
        XCTAssertEqual(try Data(contentsOf: installed), try Data(contentsOf: download))
        let resolved = try await cache.resolve(asset)
        XCTAssertEqual(resolved, installed)
        try Data("corrupt".utf8).write(to: installed)
        do { _ = try await cache.resolve(asset); XCTFail("corrupt cache accepted") }
        catch { XCTAssertEqual(error as? RenderAssetError, .changedLibraryAsset("music")) }
        _ = try await cache.install(downloadedFile: download, for: asset)
        XCTAssertEqual(try Data(contentsOf: installed), try Data(contentsOf: download))
        try Data("bad download".utf8).write(to: download)
        do { _ = try await cache.install(downloadedFile: download, for: asset); XCTFail("bad download accepted") }
        catch { XCTAssertEqual(error as? RenderAssetError, .changedLibraryAsset("music")) }
        XCTAssertEqual(try Data(contentsOf: installed), Data("licensed song".utf8))
        XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("library").path).contains(where: { $0.hasSuffix("partial") }))
    }

    func testOriginalCannotFallBackToLibraryAndMustMatchRecipe() async throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        let project = ProjectDirectory(root: root); try project.createIfNeeded()
        let file = project.originals.appendingPathComponent("clip.mov")
        try Data("original".utf8).write(to: file)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: file)
        let store = SourceAssetStore(project: project)
        try store.bind(mediaID: "proxy", original: MediaAsset(id: "local", relativePath: "originals/clip.mov", fingerprint: fingerprint))
        let cache = RenderLibraryCache(root: root.appendingPathComponent("library"))
        let resolver = PortableAssetResolver(originals: store, library: cache)
        let original = RenderAssetReference(id: "clip", fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "proxy"))
        let resolved = try await resolver.resolve(RenderAssetManifest(assets: [original]))
        XCTAssertEqual(resolved["clip"], file)
        do { _ = try await cache.install(downloadedFile: file, for: original); XCTFail("original cached as library") }
        catch { XCTAssertEqual(error as? RenderAssetError, .invalidManifest) }
        let wrong = RenderAssetReference(id: "clip", fingerprint: RenderFingerprint(sha256: String(repeating: "a", count: 64), byteCount: fingerprint.byteCount), source: original.source)
        do { _ = try await resolver.resolve(RenderAssetManifest(assets: [wrong])); XCTFail("unrelated original accepted") }
        catch { XCTAssertEqual(error as? RenderAssetError, .originalIdentityMismatch("clip")) }
        try FileManager.default.removeItem(at: file)
        do { _ = try await resolver.resolve(RenderAssetManifest(assets: [original])); XCTFail("missing original accepted") }
        catch { XCTAssertEqual(error as? SourceAssetError, .missingOriginal("proxy")) }
    }

    func testConflictingSourcesAndSymlinkedCacheFailClosed() async throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let file = root.appendingPathComponent("source"); try Data("music".utf8).write(to: file)
        let asset = try reference(file)
        let conflict = RenderAssetReference(id: "other", fingerprint: RenderFingerprint(sha256: String(repeating: "a", count: 64), byteCount: 10), source: asset.source)
        XCTAssertThrowsError(try RenderAssetManifest(assets: [asset, conflict]).validate())
        XCTAssertThrowsError(try RenderAssetManifest(assets: [asset, asset]).validate())
        XCTAssertThrowsError(try RenderAssetManifest(assets: [asset]).validate(references: ["missing"]))
        let cacheRoot = root.appendingPathComponent("library")
        try FileManager.default.createDirectory(at: cacheRoot, withIntermediateDirectories: true)
        let destination = cacheRoot.appendingPathComponent(asset.fingerprint.sha256 + "-" + String(asset.fingerprint.byteCount))
        try FileManager.default.createSymbolicLink(at: destination, withDestinationURL: file)
        do { _ = try await RenderLibraryCache(root: cacheRoot).install(downloadedFile: file, for: asset); XCTFail("symlink accepted") }
        catch { XCTAssertEqual(error as? RenderAssetError, .invalidManifest) }
        XCTAssertEqual(try Data(contentsOf: file), Data("music".utf8))
    }
}
