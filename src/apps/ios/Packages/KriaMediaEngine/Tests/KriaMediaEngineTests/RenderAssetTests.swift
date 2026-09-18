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

    // MARK: - KRI-121: Visuals-pool photos

    private let visualID = "5f0c6a2e-8c1d-4b7e-9a3f-2d6e1b0c4a77"
    private func photo(_ file: URL) throws -> RenderAssetReference {
        RenderAssetReference(id: "visual-a", fingerprint: try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: file)),
                             source: .visual(visualID: "a", generation: "7"))
    }

    /// Mirrors `VisualRenderAsset` in `app/kria/render_assets.py`: the server sends
    /// `visual_id`, which the snake_case decoder maps onto the `visualId` key.
    func testVisualWireShapeRoundTripsAndRejectsMixedIdentities() throws {
        let fingerprint = #"{"sha256":"\#(String(repeating: "b", count: 64))","byte_count":2048}"#
        let document = """
        {"version":1,"assets":[{"kind":"visual","id":"visual-\(visualID)","visual_id":"\(visualID)","generation":"1757000000000001","fingerprint":\(fingerprint)}]}
        """
        let manifest = try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(document.utf8))
        XCTAssertEqual(manifest.assets.first?.id, "visual-\(visualID)")
        XCTAssertEqual(manifest.assets.first?.source, .visual(visualID: visualID, generation: "1757000000000001"))
        let encoded = try RecipeJSON.encoder().encode(manifest)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: encoded) as? NSDictionary,
                       try JSONSerialization.jsonObject(with: Data(document.utf8)) as? NSDictionary)
        XCTAssertEqual(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: encoded), manifest)
        let original = #"{"version":1,"assets":[{"kind":"original","id":"clip","media_id":"proxy-id","fingerprint":\#(fingerprint)}]}"#
        let library = #"{"version":1,"assets":[{"kind":"library","id":"music","catalog":"music","catalog_id":"track","generation":"1","fingerprint":\#(fingerprint)}]}"#
        XCTAssertNoThrow(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(original.utf8)))
        XCTAssertNoThrow(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(library.utf8)))
        // One kind never borrows another kind's identity, and a photo never
        // carries a storage path: the grant resolves the pool row server-side.
        for bad in [document.replacingOccurrences(of: "\"visual_id\"", with: "\"media_id\":\"proxy-id\",\"visual_id\""),
                    document.replacingOccurrences(of: "\"visual_id\"", with: "\"catalog\":\"music\",\"visual_id\""),
                    document.replacingOccurrences(of: "\"visual_id\"", with: "\"catalog_id\":\"track\",\"visual_id\""),
                    document.replacingOccurrences(of: "\"visual_id\"", with: "\"gcs_path\":\"users/u/plan/i/pool/a.jpg\",\"visual_id\""),
                    document.replacingOccurrences(of: "\"visual_id\":\"\(visualID)\",", with: ""),
                    document.replacingOccurrences(of: "\"generation\":\"1757000000000001\",", with: ""),
                    document.replacingOccurrences(of: "\"visual_id\":\"\(visualID)\"", with: "\"visual_id\":\"\(visualID) \""),
                    document.replacingOccurrences(of: "\"generation\":\"1757000000000001\"", with: "\"generation\":\"\""),
                    original.replacingOccurrences(of: "\"media_id\"", with: "\"visual_id\":\"\(visualID)\",\"media_id\""),
                    library.replacingOccurrences(of: "\"catalog_id\"", with: "\"visual_id\":\"\(visualID)\",\"catalog_id\"")] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: Data(bad.utf8)), bad)
        }
        let valid = try XCTUnwrap(manifest.assets.first)
        for source in [RenderAssetReference.Source.visual(visualID: "", generation: "1"),
                       .visual(visualID: "a b", generation: "1"),
                       .visual(visualID: "a", generation: "1\n")] {
            let bad = RenderAssetReference(id: valid.id, fingerprint: valid.fingerprint, source: source)
            XCTAssertThrowsError(try bad.validate())
            XCTAssertThrowsError(try RecipeJSON.encoder().encode(bad))
        }
        XCTAssertThrowsError(try RenderAssetReference(id: "visual a", fingerprint: valid.fingerprint, source: valid.source).validate())
    }

    /// A pinned generation names one set of bytes; a new generation may carry new ones.
    func testVisualIdentityIsGenerationPinned() throws {
        let first = RenderFingerprint(sha256: String(repeating: "b", count: 64), byteCount: 2048)
        let replaced = RenderFingerprint(sha256: String(repeating: "c", count: 64), byteCount: 4096)
        let pinned = RenderAssetReference(id: "visual-a", fingerprint: first, source: .visual(visualID: "a", generation: "1"))
        let conflict = RenderAssetReference(id: "visual-b", fingerprint: replaced, source: pinned.source)
        XCTAssertThrowsError(try RenderAssetManifest(assets: [pinned, conflict]).validate())
        let regenerated = RenderAssetReference(id: "visual-b", fingerprint: replaced, source: .visual(visualID: "a", generation: "2"))
        XCTAssertNoThrow(try RenderAssetManifest(assets: [pinned, regenerated]).validate())
        let alias = RenderAssetReference(id: "visual-b", fingerprint: first, source: pinned.source)
        XCTAssertNoThrow(try RenderAssetManifest(assets: [pinned, alias]).validate())
    }

    func testLibraryCacheStoresVerifiedVisualPhotosButNeverOriginals() async throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let download = root.appendingPathComponent("download")
        try Data("pool photo".utf8).write(to: download)
        let asset = try photo(download)
        let cache = RenderLibraryCache(root: root.appendingPathComponent("library"))
        let installed = try await cache.install(downloadedFile: download, for: asset)
        XCTAssertEqual(try Data(contentsOf: installed), Data("pool photo".utf8))
        let resolved = try await cache.resolve(asset)
        XCTAssertEqual(resolved, installed)
        try Data("replaced photo".utf8).write(to: download)
        do { _ = try await cache.install(downloadedFile: download, for: asset); XCTFail("changed photo accepted") }
        catch { XCTAssertEqual(error as? RenderAssetError, .changedLibraryAsset("visual-a")) }
        XCTAssertEqual(try Data(contentsOf: installed), Data("pool photo".utf8))
        let original = RenderAssetReference(id: "clip", fingerprint: asset.fingerprint, source: .original(mediaID: "proxy"))
        do { _ = try await cache.resolve(original); XCTFail("original resolved from the cache") }
        catch { XCTAssertEqual(error as? RenderAssetError, .invalidManifest) }
        do { _ = try await cache.install(downloadedFile: download, for: original); XCTFail("original cached") }
        catch { XCTAssertEqual(error as? RenderAssetError, .invalidManifest) }
    }

    func testResolverReadsVisualPhotosOnlyFromTheVerifiedCache() async throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        let project = ProjectDirectory(root: root); try project.createIfNeeded()
        let clip = project.originals.appendingPathComponent("clip.mov")
        try Data("original".utf8).write(to: clip)
        let clipFingerprint = try SHA256Fingerprinter().fingerprint(file: clip)
        let store = SourceAssetStore(project: project)
        try store.bind(mediaID: "proxy", original: MediaAsset(id: "local", relativePath: "originals/clip.mov", fingerprint: clipFingerprint))
        let download = root.appendingPathComponent("photo.jpg")
        try Data("pool photo".utf8).write(to: download)
        let asset = try photo(download)
        let original = RenderAssetReference(id: "clip", fingerprint: try RenderFingerprint(clipFingerprint), source: .original(mediaID: "proxy"))
        let cache = RenderLibraryCache(root: root.appendingPathComponent("library"))
        let resolver = PortableAssetResolver(originals: store, library: cache)
        let manifest = RenderAssetManifest(assets: [original, asset])
        do { _ = try await resolver.resolve(manifest); XCTFail("photo resolved before its grant was installed") }
        catch { XCTAssertEqual(error as? RenderAssetError, .missingLibraryAsset("visual-a")) }
        let installed = try await cache.install(downloadedFile: download, for: asset)
        let resolved = try await resolver.resolve(manifest)
        XCTAssertEqual(resolved, ["clip": clip, "visual-a": installed])
        // A photo is never looked up among device originals, even when its
        // identifier and bytes match a bound original.
        let lookalike = RenderAssetReference(id: "visual-proxy", fingerprint: original.fingerprint, source: .visual(visualID: "proxy", generation: "1"))
        do { _ = try await resolver.resolve(RenderAssetManifest(assets: [lookalike])); XCTFail("photo resolved from device originals") }
        catch { XCTAssertEqual(error as? RenderAssetError, .missingLibraryAsset("visual-proxy")) }
    }

    /// Derived from the manifest even when the server omits it from
    /// `required_capabilities`, so a build without still support routes the
    /// recipe away instead of rendering it without its photos.
    func testVisualManifestAssetsRequireStillImages() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/kria_edit_recipe_v2.json").standardizedFileURL
        var recipe = try RecipeJSON.decode(Data(contentsOf: fixture))
        XCTAssertTrue(recipe.requiredCapabilities.isEmpty)
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.stillImages))
        let still = RenderAssetReference(id: "visual-\(visualID)", fingerprint: RenderFingerprint(sha256: String(repeating: "b", count: 64), byteCount: 2048),
                                         source: .visual(visualID: visualID, generation: "1757000000000001"))
        let manifest = try XCTUnwrap(recipe.assetManifest)
        recipe.assetManifest = RenderAssetManifest(assets: manifest.assets + [still])
        recipe.assets.append(MediaAsset(id: still.id, relativePath: still.id,
                                        fingerprint: AssetFingerprint(hex: still.fingerprint.sha256, byteCount: still.fingerprint.byteCount)))
        recipe.tracks[0].clips.append(TimelineClip(id: "photo-1", sourceAssetID: still.id, sourceDuration: 2.5, timelineStart: 2))
        try recipe.validate()
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.stillImages))
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(recipe)), recipe)
        let base: Set<MediaCapability> = [.basicComposition, .local1080Export]
        let withoutStills = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: base)).decide(for: recipe)
        XCTAssertEqual(withoutStills.route, .cloud)
        XCTAssertEqual(withoutStills.missingCapabilities, [.stillImages])
        XCTAssertEqual(CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: base.union([.stillImages]))).decide(for: recipe).route, .local)
        XCTAssertEqual(try RecipeJSON.decoder().decode([MediaCapability].self, from: Data(#"["stillImages"]"#.utf8)), [.stillImages])
    }
}
