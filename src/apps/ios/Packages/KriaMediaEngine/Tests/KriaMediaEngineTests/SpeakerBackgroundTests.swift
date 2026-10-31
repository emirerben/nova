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

    // MARK: - Rendering (KRI-1116). An empty room makes the matte answer "background only", so every
    // pixel of the frame is the background asset as the compositor placed it.

    #if canImport(AVFoundation)
    private let canvas = Canvas(width: 120, height: 200)

    @MainActor private func backgroundFrames(_ background: SpeakerTestBackground, at times: [Double], directory: URL) async throws -> [SpeakerTestFrame] {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = directory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let room = try await writeEmptyRoomClip(directory: directory, size: (canvas.width, canvas.height), seconds: 3)
        let scene = try speakerRecipe(canvas: canvas, speaker: room, duration: 3, background: background)
        var frames: [SpeakerTestFrame] = []
        for time in times { frames.append(try await previewFrame(scene.recipe, urls: scene.urls, at: time)) }
        return frames
    }

    /// S9: a 1 s video background behind a 3 s edit loops from `source_start` (not from 0); with `loop` off
    /// it holds its last frame instead of going black.
    @MainActor func testShortVideoLoops() async throws {
        let directory = try speakerTestDirectory(self)
        // Quarter seconds: red, black, blue, white.
        let quarters = [CGColor(red: 1, green: 0, blue: 0, alpha: 1), CGColor(red: 0, green: 0, blue: 0, alpha: 1),
                        CGColor(red: 0, green: 0, blue: 1, alpha: 1), CGColor(red: 1, green: 1, blue: 1, alpha: 1)]
        let video = try await writeSpeakerTestVideo(directory: directory, name: "quarters", size: (120, 200), seconds: 1) { context, index in
            context.setFillColor(quarters[min(3, Int(Double(index) / 30 / 0.25))])
            context.fill(CGRect(x: 0, y: 0, width: 120, height: 200))
        }
        let times = [0.1, 0.35, 0.6, 1.1, 2.85]
        let looped = try await backgroundFrames(SpeakerTestBackground(url: video, kind: .video, sourceStart: 0.5), at: times, directory: directory)
        for (frame, (time, expected)) in zip(looped, zip(times, [2, 3, 2, 2, 3])) {
            XCTAssertTrue(isColor(frame.rgb(fx: 0.5, fy: 0.5), quarters[expected]), "looped background at \(time) s: \(frame.rgb(fx: 0.5, fy: 0.5))")
        }
        let once = try await backgroundFrames(SpeakerTestBackground(url: video, kind: .video, sourceStart: 0.5, loop: false), at: times, directory: directory)
        for (frame, (time, expected)) in zip(once, zip(times, [2, 3, 3, 3, 3])) {
            XCTAssertTrue(isColor(frame.rgb(fx: 0.5, fy: 0.5), quarters[expected]), "unlooped background at \(time) s: \(frame.rgb(fx: 0.5, fy: 0.5))")
        }
    }

    /// S10: a landscape photo cover-fits the portrait canvas: centered, no letterbox bars, no stretch.
    /// Thirds blue | red | white: the canvas shows only the red middle.
    @MainActor func testLandscapeBackgroundCoverFits() async throws {
        let directory = try speakerTestDirectory(self)
        let photo = try writeSpeakerTestImage(directory: directory, name: "landscape", size: (320, 180)) { context in
            for (index, color) in [CGColor(red: 0, green: 0, blue: 1, alpha: 1), CGColor(red: 1, green: 0, blue: 0, alpha: 1),
                                   CGColor(red: 1, green: 1, blue: 1, alpha: 1)].enumerated() {
                context.setFillColor(color)
                context.fill(CGRect(x: Double(index) * 320 / 3, y: 0, width: 320 / 3 + 1, height: 180))
            }
        }
        let frame = try await backgroundFrames(SpeakerTestBackground(url: photo), at: [1], directory: directory)[0]
        for (fx, fy) in [(0.02, 0.01), (0.98, 0.01), (0.02, 0.99), (0.98, 0.99), (0.5, 0.01), (0.5, 0.99), (0.5, 0.5)] {
            XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: fy), CGColor(red: 1, green: 0, blue: 0, alpha: 1)), "cover-fit red at (\(fx), \(fy)): \(frame.rgb(fx: fx, fy: fy))")
        }
    }

    /// S10: the photo's EXIF orientation is applied. Stored landscape with orientation 6 (rotate 90 degrees
    /// clockwise), its left half red, it displays upright with red on top, like an upright portrait control.
    @MainActor func testRotatedImageUsesOrientation() async throws {
        let directory = try speakerTestDirectory(self)
        let red = CGColor(red: 1, green: 0, blue: 0, alpha: 1), blue = CGColor(red: 0, green: 0, blue: 1, alpha: 1)
        let rotated = try writeSpeakerTestImage(directory: directory, name: "rotated", size: (320, 180), orientation: 6) { context in
            context.setFillColor(red); context.fill(CGRect(x: 0, y: 0, width: 160, height: 180))
            context.setFillColor(blue); context.fill(CGRect(x: 160, y: 0, width: 160, height: 180))
        }
        let upright = try writeSpeakerTestImage(directory: directory, name: "upright", size: (180, 320)) { context in
            context.setFillColor(red); context.fill(CGRect(x: 0, y: 160, width: 180, height: 160))
            context.setFillColor(blue); context.fill(CGRect(x: 0, y: 0, width: 180, height: 160))
        }
        for (name, url) in [("orientation 6", rotated), ("upright control", upright)] {
            let frame = try await backgroundFrames(SpeakerTestBackground(url: url), at: [1], directory: directory)[0]
            for fx in [0.1, 0.5, 0.9] {
                XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: 0.2), red), "\(name): red on top at x \(fx): \(frame.rgb(fx: fx, fy: 0.2))")
                XCTAssertTrue(isColor(frame.rgb(fx: fx, fy: 0.8), blue), "\(name): blue at the bottom at x \(fx): \(frame.rgb(fx: fx, fy: 0.8))")
            }
        }
    }
    #endif
}
