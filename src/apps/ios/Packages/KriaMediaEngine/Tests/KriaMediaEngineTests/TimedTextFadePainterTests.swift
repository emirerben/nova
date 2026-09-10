import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
#endif

final class TimedTextFadePainterTests: XCTestCase {
    #if canImport(AVFoundation)
    @MainActor func testLyricAndSequenceFadesUseCompositionTimeInPreviewAndExport() async throws {
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
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../../../api/tests/fixtures/phone_staggered_layout.json").standardizedFileURL
        let rows = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as! [[String: Any]]
        let exportCases = try ["lyric", "sequence"].map { kind in
            var layer = rows[0]["layer"] as! [String: Any]
            layer["effect"] = kind == "lyric" ? "lyric-line" : "fade-in"
            layer["staggered"] = NSNull(); layer["dissolve_seed"] = NSNull()
            layer["motion"] = NSNull()
            layer["fade"] = ["kind": kind, "in_ms": kind == "lyric" ? 150 : 0, "out_ms": 500, "curve": "square"]
            layer["start"] = 1.0; layer["end"] = 5.0
            return try RecipeJSON.decoder().decode(PortableTextLayer.self, from: JSONSerialization.data(withJSONObject: layer))
        }
        for (index, test) in exportCases.enumerated() {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 300, height: 200), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 7)])],
                assetManifest: RenderAssetManifest(assets: references), textLayers: [test])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("fade-\(index).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
            reference.videoComposition = preview.playerItem.videoComposition
            let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            for generator in [reference, exported] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                var counts: [Int] = []
                for time in [0.5, 1.0, 2.0, 4.8, 5.5] {
                    let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    var pixels = [UInt8](repeating: 0, count: 300 * 200 * 4)
                    CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 1200, bounds: CGRect(x: 0, y: 0, width: 300, height: 200),
                                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    counts.append(stride(from: 0, to: pixels.count, by: 4).reduce(0) { $0 + Int(pixels[$1]) })
                }
                // H.264 decoding can leave a few single-byte values across the
                // entire 60,000-pixel black frame; require visually blank output.
                XCTAssertLessThanOrEqual(counts[0], 10); XCTAssertLessThanOrEqual(counts[1], 10)
                XCTAssertLessThanOrEqual(counts[4], 10)
                XCTAssertGreaterThan(counts[2], 1000)
                XCTAssertGreaterThan(counts[3], 1000); XCTAssertLessThan(counts[3], counts[2])
            }
        }
    }

    #endif
}
