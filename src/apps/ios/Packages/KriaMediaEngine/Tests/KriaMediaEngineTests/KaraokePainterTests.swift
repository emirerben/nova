import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
#endif

final class KaraokePainterTests: XCTestCase {
    func testKaraokeRejectsMissingAndMalformedTimings() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_karaoke_layout.json").standardizedFileURL
        let rows = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as! [[String: Any]]
        let original = rows[0]["layer"] as! [String: Any]
        let font = try RecipeJSON.decoder().decode(RenderAssetReference.self,
            from: JSONSerialization.data(withJSONObject: rows[0]["font"]!))
        let manifest = RenderAssetManifest(assets: [font])
        try RecipeJSON.decoder().decode(PortableTextLayer.self,
            from: JSONSerialization.data(withJSONObject: original)).validate(duration: 5, manifest: manifest)
        for failure in ["count", "negative", "unknown", "shape", "missing", "effect"] {
            var layer = original
            var content = layer["karaoke"] as! [String: Any]
            if failure == "count" { content["starts"] = [0.0] }
            if failure == "negative" { content["starts"] = [-1.0, 0.2, 0.7, 0.0, 0.9] }
            if failure == "unknown" { content["remote_url"] = "https://example.invalid/image.png" }
            layer["karaoke"] = content
            if failure == "missing" { layer["karaoke"] = NSNull() }
            if failure == "effect" { layer["effect"] = "static" }
            if failure == "shape" {
                var runs = layer["runs"] as! [[String: Any]]
                runs[0]["shaped"] = true; layer["runs"] = runs
            }
            XCTAssertThrowsError(try {
                let decoded = try RecipeJSON.decoder().decode(PortableTextLayer.self,
                    from: JSONSerialization.data(withJSONObject: layer))
                try decoded.validate(duration: 5, manifest: manifest)
            }(), failure)
        }
    }

    #if canImport(AVFoundation)
    func testAuthoredWordCaptionKeepsOnlyCurrentWordHighlightedAcrossBackwardSeeks() throws {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let layer = try AuthoredTextLayout.compileHighlightedWords(id: "caption", text: "ONE ONE", start: 1, end: 3,
            starts: [0, 0.5], highlight: TextInk(red: 1, green: 0, blue: 0, alpha: 1),
            style: .init(fontAssetID: "font", size: 40, color: TextInk(red: 1, green: 1, blue: 1, alpha: 1)),
            fontURL: font, canvas: Canvas(width: 400, height: 200))
        let painted = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 400, height: 200))
        let painter = try XCTUnwrap(painted.karaoke)
        func bytes(_ time: Double) -> [UInt8] {
            let image = painter.image(localTime: time)
            let width = Int(image.extent.width), height = Int(image.extent.height)
            var pixels = [UInt8](repeating: 0, count: width * height * 4)
            CIContext().render(image, toBitmap: &pixels, rowBytes: width * 4, bounds: image.extent,
                format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            return pixels
        }
        let first = bytes(0.1), second = bytes(0.6)
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(bytes(0.1), first)
        for pixels in [first, second] {
            let white = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 && pixels[$0 + 1] > 180 && pixels[$0 + 2] > 180 }
            let red = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 && pixels[$0 + 1] < 100 && pixels[$0 + 2] < 100 }
            XCTAssertGreaterThan(white.count, 100)
            XCTAssertGreaterThan(red.count, 100)
        }
        let middle = try TextTransformTiming.sample(effect: .captionPop, text: "hello", localTime: 0.06, duration: 2, motion: nil)
        XCTAssertEqual(middle.alpha, 0.5, accuracy: 0.00001)
        XCTAssertEqual(middle.scale, 0.94 + 0.06 * (0.06 / 0.14), accuracy: 0.00001)
    }

    @MainActor func testKaraokeUsesCompositionTimeInPreviewAndExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let photo = directory.appendingPathComponent("black.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 300, height: 200)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["photo": photo, "font-Inter-Bold.ttf": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "photo" ? .original(mediaID: "photo") : .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: asset.fingerprint!.hex))
        }
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../../../api/tests/fixtures/phone_karaoke_layout.json").standardizedFileURL
        let rows = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as! [[String: Any]]
        let exportCases = try rows.prefix(1).map { row in
            var layer = row["layer"] as! [String: Any]
            layer["start"] = 1.0; layer["end"] = 5.0
            return try RecipeJSON.decoder().decode(PortableTextLayer.self, from: JSONSerialization.data(withJSONObject: layer))
        }
        for (index, test) in exportCases.enumerated() {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 300, height: 200), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 7)])],
                assetManifest: RenderAssetManifest(assets: references), textLayers: [test])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("karaoke-\(index).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
            reference.videoComposition = preview.playerItem.videoComposition
            let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            for generator in [reference, exported] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                var counts: [Int] = []
                var reds: [Int] = []
                for time in [0.5, 1.0, 1.6, 4.7, 1.0, 5.5] {
                    let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    var pixels = [UInt8](repeating: 0, count: 300 * 200 * 4)
                    CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 1200, bounds: CGRect(x: 0, y: 0, width: 300, height: 200),
                                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    counts.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 && pixels[$0 + 1] > 180 && pixels[$0 + 2] > 180 }.count)
                    reds.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 && pixels[$0 + 1] < 120 && pixels[$0 + 2] < 100 }.count)
                }
                XCTAssertEqual(counts[0], 0); XCTAssertEqual(counts[5], 0)
                XCTAssertEqual(reds[0], 0); XCTAssertEqual(reds[5], 0)
                XCTAssertGreaterThan(counts[1], 100)
                XCTAssertLessThan(counts[2], counts[1]); XCTAssertEqual(counts[3], 0)
                XCTAssertGreaterThan(reds[2], reds[1]); XCTAssertGreaterThan(reds[3], reds[2])
                XCTAssertEqual(counts[4], counts[1]); XCTAssertEqual(reds[4], reds[1])
            }
        }
    }

    #endif
}
