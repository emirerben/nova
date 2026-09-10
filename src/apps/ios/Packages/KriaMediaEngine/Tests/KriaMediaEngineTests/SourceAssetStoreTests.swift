import Foundation
import XCTest
@testable import KriaMediaEngine

final class SourceAssetStoreTests: XCTestCase {
    func testBindingSurvivesRestartAndRejectsChangedOrMissingOriginal() throws {
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let original = project.originals.appendingPathComponent("a.mov")
        try Data("original footage".utf8).write(to: original)
        let asset = MediaAsset(id: "local-a", relativePath: "originals/a.mov", fingerprint: try SHA256Fingerprinter().fingerprint(file: original))
        try SourceAssetStore(project: project).bind(mediaID: "server-a", original: asset)
        let restored = SourceAssetStore(project: project)
        XCTAssertEqual(try restored.resolve(mediaIDs: ["server-a"])["server-a"], original)
        try restored.bind(mediaID: "server-a", original: asset)
        XCTAssertEqual(try restored.bindings().count, 1)
        try Data("changed footage".utf8).write(to: original)
        XCTAssertThrowsError(try restored.resolve(mediaIDs: ["server-a"])) {
            XCTAssertEqual($0 as? SourceAssetError, .changedOriginal("server-a"))
        }
        try FileManager.default.removeItem(at: original)
        XCTAssertThrowsError(try restored.resolve(mediaIDs: ["server-a"])) {
            XCTAssertEqual($0 as? SourceAssetError, .missingOriginal("server-a"))
        }
    }

    func testProxyTraversalAndSymlinkCannotResolveAsOriginal() throws {
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let proxy = project.proxies.appendingPathComponent("a.mp4")
        try Data("proxy".utf8).write(to: proxy)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: proxy)
        let store = SourceAssetStore(project: project)
        for path in ["proxies/a.mp4", "originals/../proxies/a.mp4", proxy.path, "originals/", "originals/.."] {
            XCTAssertThrowsError(try store.bind(mediaID: "a", original: MediaAsset(id: "a", relativePath: path, fingerprint: fingerprint)))
        }
        try FileManager.default.createSymbolicLink(at: project.originals.appendingPathComponent("a.mp4"), withDestinationURL: proxy)
        XCTAssertThrowsError(try store.bind(mediaID: "a", original: MediaAsset(id: "a", relativePath: "originals/a.mp4", fingerprint: fingerprint)))
    }

    func testCapabilitiesAreDerivedAndFutureRecipesFailClosed() {
        let clip = TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, rate: 2, text: TextTreatment(text: "Hello"))
        var recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "opaque")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [clip])])
        let minimal = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: [.basicComposition, .local1080Export]))
        XCTAssertEqual(minimal.decide(for: recipe).missingCapabilities, [.variableSpeed, .animatedText])
        recipe.schemaVersion = 99
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
        recipe.schemaVersion = 1
        recipe.rendererVersion = "future"
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
        recipe.rendererVersion = "kria-ios-1"
        recipe.audio.musicAssetID = "missing-music"
        XCTAssertThrowsError(try recipe.validate())
    }

    func testNonfiniteTransformsAndStorageOverflowFailSafely() {
        var clip = TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2)
        clip.transform.positionX = .nan
        let recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [clip])])
        XCTAssertThrowsError(try recipe.validate())
        XCTAssertEqual(StorageEstimate.forAssetBytes(.max).requiredBytes, .max)
        XCTAssertEqual(StorageEstimate.forAssetBytes(1, projectCount: .max).requiredBytes, .max)
    }
    func testTextAnimationWireCompatibilityAndUnknownEffects() throws {
        for wire in ["fade_scale", "fadeScale"] {
            let decoded = try JSONDecoder().decode(TextAnimation.self, from: Data("\"\(wire)\"".utf8))
            XCTAssertEqual(decoded, .fadeScale)
            XCTAssertEqual(String(data: try JSONEncoder().encode(decoded), encoding: .utf8), "\"fade_scale\"")
        }
        XCTAssertThrowsError(try JSONDecoder().decode(TextAnimation.self, from: Data("\"future_effect\"".utf8)))
        var recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2)])])
        recipe.audio.duckOriginalDuringMusic = true
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
    }

}
