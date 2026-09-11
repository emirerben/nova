import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
#endif

final class GiantTitleCompositionTests: XCTestCase {
    #if canImport(AVFoundation)
    @MainActor func testGiantTitleUsesCompositionTimeAndSupportsBackwardSeekInPreviewAndExport() async throws {
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
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../../../../api/tests/fixtures/phone_giant_title.json").standardizedFileURL
        let document = try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as! [String: Any]
        let rows = document["cases"] as! [[String: Any]]
        let exportCases = try rows.filter { ["static", "scheduled-typewriter", "late-karaoke", "smooth-motion", "staggered-late"].contains($0["id"] as? String ?? "") }.map { row in
            var layer = row["layer"] as! [String: Any]
            layer["start"] = 1.0; layer["end"] = 5.0
            return try RecipeJSON.decoder().decode(PortableTextLayer.self, from: JSONSerialization.data(withJSONObject: layer))
        }
        for (index, test) in exportCases.enumerated() {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 300, height: 200), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 7)])],
                assetManifest: RenderAssetManifest(assets: references), textLayers: [test])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let output = directory.appendingPathComponent("giant-\(index).mp4")
            _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
                .export(recipe: recipe, assetURLs: urls, outputURL: output)
            let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
            reference.videoComposition = preview.playerItem.videoComposition
            let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
            for generator in [reference, exported] {
                generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
                var counts: [Int] = []
                for time in [0.5, 2.0, 3.9, 5.5, 2.0] {
                    let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                    var pixels = [UInt8](repeating: 0, count: 300 * 200 * 4)
                    CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 1200, bounds: CGRect(x: 0, y: 0, width: 300, height: 200),
                                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                    counts.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count)
                }
                XCTAssertEqual(counts[0], 0); XCTAssertEqual(counts[3], 0)
                XCTAssertGreaterThan(counts[1], 0); XCTAssertGreaterThan(counts[2], counts[1])
                XCTAssertEqual(counts[4], counts[1])
            }
        }
    }

    #endif
}
