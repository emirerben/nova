import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-374: the creator-uploaded `song` render asset mirrors the pinned `voiceover` kind.
final class SongRenderAssetTests: XCTestCase {
    private let sha = String(repeating: "b", count: 64)
    private func manifest(_ asset: String) -> Data {
        Data("""
        {"version":1,"assets":[\(asset)]}
        """.utf8)
    }
    private var songJSON: String {
        "{\"kind\":\"song\",\"id\":\"song\",\"plan_item_id\":\"item-1\",\"generation\":3,\"fingerprint\":{\"sha256\":\"\(sha)\",\"byte_count\":2048}}"
    }

    func testSongAssetDecodesIntegerGenerationAndRoundTripsUnchanged() throws {
        let decoded = try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: manifest(songJSON))
        XCTAssertEqual(decoded.assets.first?.source, .song(planItemID: "item-1", generation: "3"))
        let encoded = try RecipeJSON.encoder().encode(decoded)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: encoded) as? NSDictionary,
                       try JSONSerialization.jsonObject(with: manifest(songJSON)) as? NSDictionary)
        XCTAssertEqual(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: encoded), decoded)
    }

    func testSongAssetAcceptsStringGenerationLikeVoiceover() throws {
        let asset = songJSON.replacingOccurrences(of: "\"generation\":3", with: "\"generation\":\"gen-7\"")
        let decoded = try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: manifest(asset))
        XCTAssertEqual(decoded.assets.first?.source, .song(planItemID: "item-1", generation: "gen-7"))
        let encoded = try RecipeJSON.encoder().encode(decoded)
        XCTAssertEqual(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: encoded), decoded)
    }

    func testSongAssetRejectsForeignFieldsAndMissingIdentity() {
        for bad in [songJSON.replacingOccurrences(of: "\"plan_item_id\":\"item-1\",", with: ""),
                    songJSON.replacingOccurrences(of: "\"generation\":3", with: "\"generation\":3,\"media_id\":\"x\""),
                    songJSON.replacingOccurrences(of: "\"generation\":3", with: "\"generation\":3,\"url\":\"https://example.invalid\""),
                    songJSON.replacingOccurrences(of: "\"item-1\"", with: "\"item 1\"")] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: manifest(bad)), bad)
        }
    }

    func testSongAndVoiceoverSharingAnIdentityCannotDescribeDifferentBytes() throws {
        let other = String(repeating: "c", count: 64)
        let voiceover = "{\"kind\":\"voiceover\",\"id\":\"voice\",\"plan_item_id\":\"item-1\",\"generation\":\"3\",\"fingerprint\":{\"sha256\":\"\(other)\",\"byte_count\":10}}"
        // Different kinds are different identities, so both may coexist in one manifest.
        let both = try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: manifest("\(songJSON),\(voiceover)"))
        XCTAssertEqual(both.assets.count, 2)
        // The same song pinned to two fingerprints is rejected.
        let duplicate = songJSON.replacingOccurrences(of: "\"id\":\"song\"", with: "\"id\":\"song2\"").replacingOccurrences(of: sha, with: other)
        XCTAssertThrowsError(try RecipeJSON.decoder().decode(RenderAssetManifest.self, from: manifest("\(songJSON),\(duplicate)")))
    }

    func testSongAssetResolvesThroughTheLibraryCacheLikeVoiceover() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let download = root.appendingPathComponent("download")
        try Data("a creator song".utf8).write(to: download)
        let asset = RenderAssetReference(id: "song", fingerprint: try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: download)),
                                         source: .song(planItemID: "item-1", generation: "3"))
        let cache = RenderLibraryCache(root: root.appendingPathComponent("library"))
        _ = try await cache.install(downloadedFile: download, for: asset)
        let resolved = try await cache.resolve(asset)
        XCTAssertEqual(try Data(contentsOf: resolved), Data("a creator song".utf8))
        let store = SourceAssetStore(project: ProjectDirectory(root: root.appendingPathComponent("project")))
        let urls = try await PortableAssetResolver(originals: store, library: cache).resolve(RenderAssetManifest(assets: [asset]))
        XCTAssertEqual(urls["song"], resolved)
    }
}
