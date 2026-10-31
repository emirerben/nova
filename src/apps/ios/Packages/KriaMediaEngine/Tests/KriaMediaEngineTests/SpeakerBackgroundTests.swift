import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-1115 wire contract. `tests/fixtures/speaker-background-parity/v1.json` is read by the server's
/// `SpeakerBackgroundLayer` tests too, so both decoders agree on what a valid layer is.
final class SpeakerBackgroundTests: XCTestCase {
    private struct Case { let name: String; let layer: Data; let normalized: Data? }

    private func cases(_ key: String) throws -> [Case] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/speaker-background-parity/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root[key] as? [[String: Any]]).map { item in
            Case(name: item["name"] as! String,
                 layer: try JSONSerialization.data(withJSONObject: item["layer"] as Any),
                 normalized: try (item["normalized"]).map { try JSONSerialization.data(withJSONObject: $0) })
        }
    }

    func testSharedFixtureValidLayersDecodeToTheNormalizedForm() throws {
        let valid = try cases("valid")
        XCTAssertGreaterThanOrEqual(valid.count, 3)
        for item in valid {
            let layer = try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: item.layer)
            XCTAssertEqual(layer, try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: XCTUnwrap(item.normalized)), item.name)
            try layer.validate()
        }
    }

    func testSharedFixtureInvalidLayersAreRejected() throws {
        let invalid = try cases("invalid")
        XCTAssertGreaterThanOrEqual(invalid.count, 10)
        for item in invalid {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: item.layer), item.name)
        }
    }

    func testLayerRoundTripsOnTheSnakeCaseWire() throws {
        let layer = SpeakerBackgroundLayer(assetID: "visual-9f", mediaKind: .video, sourceStart: 2.5, loop: false, subjectTrackID: "speaker")
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(layer)) as? [String: Any])
        XCTAssertEqual(Set(wire.keys), ["asset_id", "media_kind", "source_start", "loop", "subject_track_id"])
        XCTAssertEqual(try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: RecipeJSON.encoder().encode(layer)), layer)
    }

    // MARK: - Recipe

    private var recipeFixture: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/kria_edit_recipe_v2.json").standardizedFileURL
    }

    private func recipeObject(mediaKind: String = "image") throws -> [String: Any] {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: recipeFixture)) as? [String: Any])
        var manifest = try XCTUnwrap(object["asset_manifest"] as? [String: Any])
        var assets = try XCTUnwrap(manifest["assets"] as? [[String: Any]])
        let original = assets[0]
        var visual: [String: Any] = ["kind": "visual", "id": original["id"] as Any, "visual_id": "5b2f9d1e", "generation": "777",
                                     "fingerprint": original["fingerprint"] as Any]
        if mediaKind == "video" { visual["media_kind"] = "video" }
        assets[0] = visual
        manifest["assets"] = assets
        object["asset_manifest"] = manifest
        object["speaker_background"] = ["asset_id": original["id"] as Any, "media_kind": mediaKind]
        return object
    }

    private func decode(_ object: [String: Any]) throws -> EditRecipe {
        try RecipeJSON.decode(JSONSerialization.data(withJSONObject: object))
    }

    func testRecipeWithoutABackgroundDecodesUnchanged() throws {
        let recipe = try RecipeJSON.decode(Data(contentsOf: recipeFixture))
        XCTAssertNil(recipe.speakerBackground)
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.speakerBackground))
        try recipe.validate()
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)) as? [String: Any])
        XCTAssertNil(wire["speaker_background"])
    }

    func testRecipeBackgroundDecodesValidatesAndDerivesTheCapability() throws {
        for kind in ["image", "video"] {
            let recipe = try decode(recipeObject(mediaKind: kind))
            try recipe.validate()
            XCTAssertEqual(recipe.speakerBackground?.mediaKind, kind == "video" ? .video : .image)
            XCTAssertTrue(recipe.effectiveCapabilities.contains(.speakerBackground))
            XCTAssertEqual(try decode(JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)) as! [String: Any]), recipe)
        }
    }

    func testRecipeWithAnOmittedCapabilityStillRoutesAwayFromADeviceThatLacksIt() throws {
        let recipe = try decode(recipeObject())
        XCTAssertTrue(recipe.requiredCapabilities.isEmpty)
        let decision = CapabilityNegotiator().decide(for: recipe)
        XCTAssertEqual(decision.route, .cloud)
        XCTAssertTrue(decision.missingCapabilities.contains(.speakerBackground))
    }

    func testUnknownKeyInsideTheLayerRejectsTheRecipe() throws {
        var object = try recipeObject()
        object["speaker_background"] = ["asset_id": "media-1", "media_kind": "image", "blur": 0.5] as [String: Any]
        XCTAssertThrowsError(try decode(object))
    }

    func testBackgroundMustNameAMatchingVisualsAsset() throws {
        var wrongKind = try recipeObject()
        wrongKind["speaker_background"] = ["asset_id": "media-1", "media_kind": "video"]
        XCTAssertThrowsError(try decode(wrongKind).validate())

        var missing = try recipeObject()
        missing["speaker_background"] = ["asset_id": "visual-missing", "media_kind": "image"]
        XCTAssertThrowsError(try decode(missing).validate())

        var original = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: recipeFixture)) as? [String: Any])
        original["speaker_background"] = ["asset_id": "media-1", "media_kind": "image"]
        XCTAssertThrowsError(try decode(original).validate())
    }

    func testV1RecipeCannotCarryABackground() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: recipeFixture)) as? [String: Any])
        object["schema_version"] = 1
        object["renderer_version"] = "kria-ios-1"
        object.removeValue(forKey: "asset_manifest")
        object["speaker_background"] = ["asset_id": "media-1", "media_kind": "image"]
        XCTAssertThrowsError(try decode(object))
    }
}
