import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-1134 wire contract. `tests/fixtures/clip-tilt-parity/v1.json` is read by the server's `ClipBackdrop`
/// tests too, so both decoders agree on what a valid backdrop is and when a clip needs `clipTilt`.
final class ClipTiltParityTests: XCTestCase {
    private func fixture(_ key: String) throws -> [[String: Any]] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/clip-tilt-parity/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root[key] as? [[String: Any]])
    }

    private func decode(_ object: Any?) throws -> ClipBackdrop {
        try RecipeJSON.decoder().decode(ClipBackdrop.self, from: JSONSerialization.data(withJSONObject: object as Any))
    }

    func testSharedFixtureValidBackdropsDecodeToTheNormalizedForm() throws {
        let valid = try fixture("valid")
        XCTAssertGreaterThanOrEqual(valid.count, 4)
        for item in valid {
            let backdrop = try decode(item["backdrop"])
            XCTAssertEqual(backdrop, try decode(item["normalized"]), item["name"] as? String ?? "")
            XCTAssertEqual(try decode(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(backdrop))), backdrop)
        }
    }

    func testSharedFixtureInvalidBackdropsAreRejected() throws {
        let invalid = try fixture("invalid")
        XCTAssertGreaterThanOrEqual(invalid.count, 12)
        for item in invalid {
            XCTAssertThrowsError(try decode(item["backdrop"]), item["name"] as? String ?? "")
        }
    }

    func testSharedFixtureCapabilityInference() throws {
        for item in try fixture("capability") {
            let name = item["name"] as? String ?? ""
            let backdrop = item["backdrop"] is NSNull ? nil : try decode(item["backdrop"])
            var assets = [MediaAsset(id: "a", relativePath: "a")]
            var manifest: RenderAssetManifest?
            if case .media(let poolID)? = backdrop {
                let kind = VisualMediaKind(rawValue: try XCTUnwrap(item["pool_media_kind"] as? String)) ?? .image
                let fingerprint = RenderFingerprint(sha256: String(repeating: "b", count: 64), byteCount: 50)
                assets.append(MediaAsset(id: "visual-" + poolID, relativePath: "visual-" + poolID))
                manifest = RenderAssetManifest(assets: [RenderAssetReference(
                    id: "visual-" + poolID, fingerprint: fingerprint, source: .visual(visualID: poolID, generation: "888", mediaKind: kind))])
            }
            let recipe = EditRecipe(assets: assets, tracks: [
                TimelineTrack(id: "video", kind: .video, clips: [
                    TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 1,
                                 transform: MediaTransform(rotationDegrees: try XCTUnwrap(item["rotation_degrees"] as? Double)), backdrop: backdrop)
                ])
            ], assetManifest: manifest)
            XCTAssertEqual(recipe.effectiveCapabilities.contains(.clipTilt), item["clip_tilt"] as? Bool, name)
            XCTAssertEqual(recipe.effectiveCapabilities.contains(.clipBackdropMedia), item["clip_backdrop_media"] as? Bool ?? false, name)
        }
    }
}
