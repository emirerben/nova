import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// KRI-1264: a phone Talking picture card's zoom and animation ride on its clip's `visualPlacement.editorStyle`
/// (the angle stays on `transform.rotationDegrees`, KRI-1141). `tests/fixtures/card-style-parity/v1.json` holds the
/// clips the server's Talking compile writes (`tests/routes/test_overlay_card_style.py` pins them); this decodes them
/// with the app's recipe codec and draws them through the real compositor.
final class PlacedCardStyleTests: XCTestCase {
    private let width = 216, height = 384

    private struct Frame {
        let bytes: [UInt8], width: Int, height: Int
        func pixel(_ x: Int, _ y: Int) -> [Int] { (0..<3).map { Int(bytes[(y * width + x) * 4 + $0]) } }
        func isCard(_ x: Int, _ y: Int) -> Bool { let p = pixel(x, y); return p[1] > 100 || p[0] > 150 }
        /// The card's pixel box: the green photo plus its red corner marker, against the dark base.
        func cardBox() -> (minX: Int, maxX: Int, minY: Int, maxY: Int)? {
            var box: (Int, Int, Int, Int)?
            for y in 0..<height { for x in 0..<width where isCard(x, y) {
                box = box.map { (min($0.0, x), max($0.1, x), min($0.2, y), max($0.3, y)) } ?? (x, x, y, y)
            } }
            return box.map { (minX: $0.0, maxX: $0.1, minY: $0.2, maxY: $0.3) }
        }
        func redCentroid() -> (x: Double, y: Double)? {
            var sx = 0.0, sy = 0.0, n = 0.0
            for y in 0..<height { for x in 0..<width {
                let p = pixel(x, y)
                if p[0] > 150 && p[1] < 90 && p[2] < 90 { sx += Double(x); sy += Double(y); n += 1 }
            } }
            return n == 0 ? nil : (sx / n, sy / n)
        }
    }

    private struct Case { let name: String; let clip: TimelineClip; let zoom: Double; let entrance: String; let xFraction: Double }

    private func fixture() throws -> [String: Case] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/card-style-parity/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var cases: [String: Case] = [:]
        for item in try XCTUnwrap(root["cases"] as? [[String: Any]]) {
            let name = try XCTUnwrap(item["name"] as? String)
            var clip = try RecipeJSON.decoder().decode(TimelineClip.self, from: JSONSerialization.data(withJSONObject: try XCTUnwrap(item["clip"])))
            clip.sourceAssetID = "card"
            cases[name] = Case(name: name, clip: clip, zoom: try XCTUnwrap(item["zoom"] as? Double),
                               entrance: try XCTUnwrap(item["entrance"] as? String), xFraction: try XCTUnwrap(item["x_frac"] as? Double))
        }
        return cases
    }

    private func write(_ directory: URL, _ name: String, _ size: CGSize, marker: Bool) throws -> URL {
        let url = directory.appendingPathComponent(name + ".png")
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        var image = CIImage(color: CIColor(red: 0, green: marker ? 0.8 : 0.1, blue: 0, colorSpace: space)!).cropped(to: CGRect(origin: .zero, size: size))
        if marker {
            let square = CGRect(x: size.width - 40, y: size.height - 40, width: 24, height: 24)
            image = CIImage(color: CIColor(red: 1, green: 0, blue: 0, colorSpace: space)!).cropped(to: square).composited(over: image)
        }
        try CIContext().writePNGRepresentation(of: image, to: url, format: .RGBA8, colorSpace: space)
        return url
    }

    @MainActor private func render(_ card: TimelineClip, at times: [Double], directory: URL) async throws -> [Frame] {
        let urls = ["base": try write(directory, "base", CGSize(width: 16, height: 16), marker: false),
                    "card": try write(directory, "card", CGSize(width: 160, height: 160), marker: true)]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)]),
                TimelineTrack(id: "overlays", kind: .overlay, clips: [card]),
            ], assetManifest: manifest)
        try recipe.validate()
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        var frames: [Frame] = []
        for time in times {
            let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            CIContext().render(CIImage(cgImage: image, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: width * 4,
                               bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
            frames.append(Frame(bytes: pixels, width: width, height: height))
        }
        return frames
    }

    private func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return directory
    }

    func testEveryServerCompiledCardClipDecodesWithTheRecipeCodec() throws {
        let cases = try fixture()
        XCTAssertEqual(cases.count, 6)
        XCTAssertNil(try XCTUnwrap(cases["plain"]).clip.visualPlacement?.editorStyle, "a plain card carries no style")
        for item in cases.values where item.name != "plain" {
            let style = try XCTUnwrap(item.clip.visualPlacement?.editorStyle, item.name)
            XCTAssertEqual(style.zoom, item.zoom, item.name)
            XCTAssertEqual(style.animation.entrance.rawValue, item.entrance, item.name)
            XCTAssertEqual(style.rotationDegrees, 0, "\(item.name): the angle stays on the clip transform")
        }
        XCTAssertEqual(try XCTUnwrap(cases["zoom_2_pop_tilt_30"]).clip.placementTiltDegrees, 30)
    }

    func testAStyleSurvivesTheRecipeWireBothWays() throws {
        let style = VisualEditorStyle(rotationDegrees: 25, fitMode: "contain", zoom: 2,
            animation: .init(entrance: .pop, exit: .slide, loop: .pulse, speed: 2))
        let wire = try RecipeJSON.encoder().encode(style)
        XCTAssertEqual(try RecipeJSON.decoder().decode(VisualEditorStyle.self, from: wire), style)
        let text = String(decoding: wire, as: UTF8.self)
        XCTAssertTrue(text.contains("\"rotation_deg\"") && text.contains("\"fit_mode\""), text)
        XCTAssertEqual(try JSONDecoder().decode(VisualEditorStyle.self, from: wire), style, "the editor document reads the same keys")
    }

    @MainActor func testZoomScalesTheCardAboutItsCentreAndFourTimesIsCroppedNotRefused() async throws {
        let cases = try fixture(), directory = try temporaryDirectory()
        let plain = try await render(try XCTUnwrap(cases["plain"]).clip, at: [2], directory: directory)[0]
        let doubled = try await render(try XCTUnwrap(cases["zoom_2"]).clip, at: [2], directory: directory)[0]
        let quadrupled = try await render(try XCTUnwrap(cases["zoom_4"]).clip, at: [2], directory: directory)[0]
        let one = try XCTUnwrap(plain.cardBox()), two = try XCTUnwrap(doubled.cardBox()), four = try XCTUnwrap(quadrupled.cardBox())
        let plainWidth = Double(one.maxX - one.minX + 1), doubledWidth = Double(two.maxX - two.minX + 1)
        XCTAssertEqual(plainWidth, 0.35 * Double(width), accuracy: 2, "the plain card is 0.35 of the frame")
        XCTAssertEqual(doubledWidth / plainWidth, 2, accuracy: 0.05, "2x zoom doubles the card")
        XCTAssertEqual(Double(two.minX + two.maxX) / 2, Double(one.minX + one.maxX) / 2, accuracy: 1.5, "the card keeps its centre")
        XCTAssertEqual(Double(two.minY + two.maxY) / 2, Double(one.minY + one.maxY) / 2, accuracy: 1.5)
        XCTAssertEqual(four.minX, 0, "a 4x card runs past both edges and is cropped by the frame")
        XCTAssertEqual(four.maxX, width - 1)
    }

    @MainActor func testAPopEntranceStartsSmallerAndSettlesOnTheStillCard() async throws {
        let cases = try fixture(), directory = try temporaryDirectory()
        let still = try await render(try XCTUnwrap(cases["zoom_2"]).clip, at: [1.1, 2], directory: directory)
        let popped = try await render(try XCTUnwrap(cases["zoom_2_pop"]).clip, at: [1.1, 2], directory: directory)
        let entering = try XCTUnwrap(popped[0].cardBox()), settled = try XCTUnwrap(still[1].cardBox())
        let ratio = Double(entering.maxX - entering.minX + 1) / Double(settled.maxX - settled.minX + 1)
        XCTAssertGreaterThan(ratio, 0.75, "0.1 s into a 0.4 s pop the card is part way up from 0.7x")
        XCTAssertLessThan(ratio, 0.95)
        XCTAssertEqual(still[0].bytes, still[1].bytes, "a card with no animation does not move")
        XCTAssertEqual(popped[1].bytes, still[1].bytes, "after the entrance the popped card is the still card")
        let centre = (x: Int(Double(width) * 0.5), y: Int(Double(height) * 0.4))
        XCTAssertLessThan(popped[0].pixel(centre.x, centre.y)[1], still[0].pixel(centre.x, centre.y)[1], "the card fades in while it pops")
    }

    /// The editor preview keeps the angle inside the style (`NativeEditorRenderCompiler`); the pinned recipe keeps it on
    /// the clip transform. A popped, tilted card must land on the same pixels either way.
    @MainActor func testAPoppedTiltedCardDrawsLikeTheEditorPreview() async throws {
        let cases = try fixture(), directory = try temporaryDirectory()
        let pinned = try XCTUnwrap(cases["zoom_2_pop_tilt_30"]).clip
        var preview = pinned
        preview.transform.rotationDegrees = 0
        preview.visualPlacement?.editorStyle?.rotationDegrees = 30
        let times = [1.1, 1.25, 2.0]
        let device = try await render(pinned, at: times, directory: directory)
        let editor = try await render(preview, at: times, directory: directory)
        for (index, time) in times.enumerated() {
            let a = try XCTUnwrap(device[index].redCentroid(), "t=\(time)"), b = try XCTUnwrap(editor[index].redCentroid(), "t=\(time)")
            XCTAssertEqual(a.x, b.x, accuracy: 1, "t=\(time)"); XCTAssertEqual(a.y, b.y, accuracy: 1, "t=\(time)")
            let boxA = try XCTUnwrap(device[index].cardBox()), boxB = try XCTUnwrap(editor[index].cardBox())
            XCTAssertEqual(boxA.minX, boxB.minX, accuracy: 1, "t=\(time)"); XCTAssertEqual(boxA.maxX, boxB.maxX, accuracy: 1, "t=\(time)")
            XCTAssertEqual(boxA.minY, boxB.minY, accuracy: 1, "t=\(time)"); XCTAssertEqual(boxA.maxY, boxB.maxY, accuracy: 1, "t=\(time)")
        }
        let upright = try await render(try XCTUnwrap(cases["zoom_2"]).clip, at: [2], directory: directory)[0]
        let turned = try XCTUnwrap(device[2].redCentroid()), straight = try XCTUnwrap(upright.redCentroid())
        XCTAssertGreaterThan(turned.y, straight.y + 5, "30 degrees clockwise carries the top-right marker down")
    }

    @MainActor func testACardZoomedPastTheRightEdgeIsCroppedThere() async throws {
        let cases = try fixture(), directory = try temporaryDirectory()
        let edge = try XCTUnwrap(cases["edge_zoom_2"])
        let frame = try await render(edge.clip, at: [2], directory: directory)[0]
        let box = try XCTUnwrap(frame.cardBox())
        let cardWidth = 2 * 0.35 * Double(width)
        XCTAssertEqual(Double(box.minX), edge.xFraction * Double(width) - cardWidth / 2, accuracy: 2)
        XCTAssertEqual(box.maxX, width - 1, "the part past the frame is cropped")
    }
}
#endif
