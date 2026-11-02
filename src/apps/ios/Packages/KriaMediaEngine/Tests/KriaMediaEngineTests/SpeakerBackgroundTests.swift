import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-1115 wire contract. `tests/fixtures/speaker-background-parity/v1.json` is read by the server's
/// `SpeakerBackgroundLayer` tests too, so both decoders agree on what a valid layer is.
final class SpeakerBackgroundTests: XCTestCase {
    private struct Case { let name: String; let layer: Data; let normalized: Data? }

    private func cases(_ key: String, file: String = "v1") throws -> [Case] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/speaker-background-parity/\(file).json"))
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

    // MARK: - KRI-1162 wire (v2.json: timed windows + speaker_frame)

    func testSharedFixtureV2ValidLayersDecodeToTheNormalizedForm() throws {
        let valid = try cases("valid", file: "v2")
        XCTAssertGreaterThanOrEqual(valid.count, 4)
        for item in valid {
            let layer = try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: item.layer)
            XCTAssertEqual(layer, try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: XCTUnwrap(item.normalized)), item.name)
            try layer.validate()
            // The normalized form re-encodes to itself: nothing the decoder defaulted is lost or invented.
            let rewritten = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(layer)) as? NSDictionary)
            XCTAssertEqual(rewritten, try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(item.normalized)) as? NSDictionary), item.name)
        }
    }

    func testSharedFixtureV2InvalidLayersAreRejected() throws {
        let invalid = try cases("invalid", file: "v2")
        XCTAssertGreaterThanOrEqual(invalid.count, 15)
        for item in invalid {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: item.layer), item.name)
        }
    }

    func testNullWindowsAndFrameReadAsAbsent() throws {
        let layer = try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: Data(
            #"{"asset_id":"a","media_kind":"image","windows":null,"speaker_frame":null}"#.utf8))
        XCTAssertNil(layer.windows)
        XCTAssertNil(layer.speakerFrame)
        XCTAssertEqual(layer, SpeakerBackgroundLayer(assetID: "a", mediaKind: .image))
    }

    /// S28: the compositor picks THE window containing the time, so out-of-order or overlapping windows
    /// are refused at decode, while windows that merely touch are fine.
    func testWindowsMustBeSortedAndDisjoint() throws {
        func window(_ id: String, _ start: Double, _ end: Double) -> SpeakerBackgroundWindow {
            SpeakerBackgroundWindow(assetID: id, mediaKind: .image, start: start, end: end)
        }
        func layer(_ windows: [SpeakerBackgroundWindow]) -> SpeakerBackgroundLayer {
            SpeakerBackgroundLayer(assetID: "fill", mediaKind: .image, windows: windows)
        }
        XCTAssertNoThrow(try layer([window("a", 0, 2), window("b", 2, 4), window("c", 5, 6)]).validate(), "touching and gapped windows are valid")
        XCTAssertThrowsError(try layer([window("a", 0, 2), window("b", 1.9, 4)]).validate(), "overlap")
        XCTAssertThrowsError(try layer([window("b", 2, 4), window("a", 0, 2)]).validate(), "unsorted")
        XCTAssertThrowsError(try layer([]).validate(), "an empty list is never written")
        XCTAssertThrowsError(try layer((0..<41).map { window("w\($0)", Double($0), Double($0) + 1) }).validate(), "41 windows")
        XCTAssertNoThrow(try layer((0..<40).map { window("w\($0)", Double($0), Double($0) + 1) }).validate(), "40 windows")
        XCTAssertThrowsError(try window("a", 1, 1.4).validate(), "shorter than 0.5 s")
        XCTAssertNoThrow(try window("a", 1, 1.5).validate())
        XCTAssertThrowsError(try SpeakerBackgroundWindow(assetID: "a", mediaKind: .image, sourceStart: 1, start: 0, end: 2).validate(), "a photo has no source start")
        XCTAssertNoThrow(try SpeakerBackgroundWindow(assetID: "a", mediaKind: .video, sourceStart: 1, start: 0, end: 2).validate())
        XCTAssertThrowsError(try SpeakerFrame(widthFraction: 0.19, xFraction: 0.5, yFraction: 0.5).validate())
        XCTAssertNoThrow(try SpeakerFrame(widthFraction: 0.2, xFraction: 0, yFraction: 1).validate())
    }

    /// A layer without the new keys encodes exactly as the first cut did, so every old recipe keeps its digest.
    func testLayerWithoutTheNewKeysEncodesAsBefore() throws {
        let layer = SpeakerBackgroundLayer(assetID: "visual-9f", mediaKind: .image)
        let wire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(layer)) as? NSDictionary)
        XCTAssertEqual(wire, ["asset_id": "visual-9f", "media_kind": "image", "source_start": 0.0, "loop": true, "subject_track_id": "subtitled"] as NSDictionary)
        let timed = SpeakerBackgroundLayer(assetID: "visual-9f", mediaKind: .image,
                                           windows: [SpeakerBackgroundWindow(assetID: "w", mediaKind: .video, sourceStart: 1.5, start: 1, end: 3)],
                                           speakerFrame: SpeakerFrame(widthFraction: 0.42, xFraction: 0.76, yFraction: 0.8))
        let timedWire = try XCTUnwrap(JSONSerialization.jsonObject(with: RecipeJSON.encoder().encode(timed)) as? [String: Any])
        XCTAssertEqual(Set(timedWire.keys), ["asset_id", "media_kind", "source_start", "loop", "subject_track_id", "windows", "speaker_frame"])
        XCTAssertEqual(try RecipeJSON.decoder().decode(SpeakerBackgroundLayer.self, from: RecipeJSON.encoder().encode(timed)), timed)
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

    /// A recipe with windows or a frame derives `speakerBackgroundWindows` on top of `speakerBackground`;
    /// one with neither does not.
    func testWindowsAndFrameDeriveTheWindowsCapability() throws {
        var object = try recipeObject()
        let plain = try decode(object)
        XCTAssertTrue(plain.effectiveCapabilities.contains(.speakerBackground))
        XCTAssertFalse(plain.effectiveCapabilities.contains(.speakerBackgroundWindows))
        let window: [String: Any] = ["asset_id": "media-1", "media_kind": "image", "start": 0, "end": 4]
        let frame: [String: Any] = ["width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.8]
        for extra in [["windows": [window]], ["speaker_frame": frame], ["windows": [window], "speaker_frame": frame]] as [[String: Any]] {
            object["speaker_background"] = ["asset_id": "media-1", "media_kind": "image"].merging(extra) { $1 }
            let recipe = try decode(object)
            try recipe.validate()
            XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.speakerBackground, .speakerBackgroundWindows]), "\(extra.keys)")
            XCTAssertEqual(try decode(JSONSerialization.jsonObject(with: RecipeJSON.encode(recipe)) as! [String: Any]), recipe)
            let decision = CapabilityNegotiator().decide(for: recipe)
            XCTAssertEqual(decision.route, .cloud)
            XCTAssertTrue(decision.missingCapabilities.contains(.speakerBackgroundWindows))
        }
    }

    /// Every window names a matching-kind visual of the manifest, as the fill does.
    func testWindowMustNameAMatchingVisualsAsset() throws {
        var object = try recipeObject()
        for window in [["asset_id": "media-1", "media_kind": "video", "start": 0, "end": 4],
                       ["asset_id": "visual-missing", "media_kind": "image", "start": 0, "end": 4]] as [[String: Any]] {
            object["speaker_background"] = ["asset_id": "media-1", "media_kind": "image", "windows": [window]] as [String: Any]
            XCTAssertThrowsError(try decode(object).validate(), "\(window)")
        }
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

    /// S30: a 1 s video window inside a 4 s edit loops inside its window from `source_start`, and the fill
    /// shows before and after it. Quarters: red, black, blue, white; the window plays the blue and white
    /// quarters over and over from 1.0 s to 3.0 s.
    @MainActor func testShortVideoWindowLoopsInsideItsWindow() async throws {
        useSpeakerSegmenter(self, GreenPersonSegmenter())
        let directory = try speakerTestDirectory(self)
        let quarters = [CGColor(red: 1, green: 0, blue: 0, alpha: 1), CGColor(red: 0, green: 0, blue: 0, alpha: 1),
                        CGColor(red: 0, green: 0, blue: 1, alpha: 1), CGColor(red: 1, green: 1, blue: 1, alpha: 1)]
        let video = try await writeSpeakerTestVideo(directory: directory, name: "quarters", size: (120, 200), seconds: 1) { context, index in
            context.setFillColor(quarters[min(3, Int(Double(index) / 30 / 0.25))])
            context.fill(CGRect(x: 0, y: 0, width: 120, height: 200))
        }
        let gray = CGColor(red: 0.5, green: 0.5, blue: 0.5, alpha: 1)
        let fill = try solidSpeakerTestImage(directory: directory, name: "fill", color: gray)
        let room = try await writeEmptyRoomClip(directory: directory, size: (120, 200), seconds: 4)
        let scene = try speakerRecipe(canvas: canvas, speaker: room, duration: 4, background: SpeakerTestBackground(url: fill),
                                      windows: [SpeakerTestWindow(url: video, kind: .video, sourceStart: 0.5, start: 1, end: 3)])
        // (time, expected): the fill outside the window; inside it blue, white, blue, blue, white.
        let expectations: [(Double, CGColor)] = [(0.5, gray), (1.1, quarters[2]), (1.35, quarters[3]), (1.6, quarters[2]),
                                                 (2.1, quarters[2]), (2.85, quarters[3]), (3.5, gray)]
        for (time, expected) in expectations {
            let frame = try await previewFrame(scene.recipe, urls: scene.urls, at: time)
            XCTAssertTrue(isColor(frame.rgb(fx: 0.5, fy: 0.5), expected), "window background at \(time) s: \(frame.rgb(fx: 0.5, fy: 0.5))")
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
