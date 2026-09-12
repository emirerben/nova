import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation) && canImport(JavaScriptCore)
import CoreImage
@preconcurrency import AVFoundation

final class NativeMotionPainterTests: XCTestCase {
    @MainActor func testMotionPreviewAndExportMatchAndEditsKeepSourceTracks() async throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { root.deleteLastPathComponent() }
        let font = root.appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("src/packages/motion-runtime/fixtures/route-trace-midpoint.json"))) as? [String: Any])
        let instances = try JSONDecoder().decode([MotionSceneValue].self, from: JSONSerialization.data(withJSONObject: fixture["instances"]!))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("base.png"), context = CIContext()
        let canvas = CGRect(x: 0, y: 0, width: 270, height: 480)
        try context.writePNGRepresentation(of: CIImage(color: .black).cropped(to: canvas), to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let photoPrint = try SHA256Fingerprinter().fingerprint(file: photo), fontPrint = try SHA256Fingerprinter().fingerprint(file: font)
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 270, height: 480),
            assets: [MediaAsset(id: "photo", relativePath: "photo", fingerprint: photoPrint), MediaAsset(id: "font", relativePath: "font", fingerprint: fontPrint)],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "photo", sourceDuration: 2.5)])],
            assetManifest: RenderAssetManifest(assets: [RenderAssetReference(id: "photo", fingerprint: try RenderFingerprint(photoPrint), source: .original(mediaID: "photo")),
                RenderAssetReference(id: "font", fingerprint: try RenderFingerprint(fontPrint), source: .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: fontPrint.hex))]),
            motionScenes: MotionSceneProgram(instances: instances, runtimeHash: fixture["runtime_hash"] as! String, fontAssetID: "font"))
        let assets = ["photo": photo, "font": font]
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: assets)
        let output = directory.appendingPathComponent("motion.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state"))).export(recipe: recipe, assetURLs: assets, outputURL: output)
        let native = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
        native.videoComposition = preview.preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        var framePixels: [[[UInt8]]] = []
        for generator in [native, exported] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            var samples: [Int] = []
            var rendered: [[UInt8]] = []
            for time in [0.0, 0.5, 1.0, 1.8, 2.0, 1.0] {
                let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: 270 * 480 * 4)
                context.render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 270 * 4, bounds: canvas, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                rendered.append(pixels)
                samples.append(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 90 || pixels[$0 + 1] > 90 }.count)
            }
            XCTAssertLessThan(samples[0], samples[2]); XCTAssertEqual(samples[4], 0)
            XCTAssertGreaterThan(samples[2], 0); XCTAssertEqual(samples[2], samples[5])
            framePixels.append(rendered)
        }
        // H.264 changes edge values across a fixed threshold. Compare the
        // actual RGB samples, allowing only the final codec's small error.
        for (native, encoded) in zip(framePixels[0], framePixels[1]) {
            let errors = native.indices.filter { $0 % 4 != 3 }.map { abs(Int(native[$0]) - Int(encoded[$0])) }
            XCTAssertLessThan(Double(errors.reduce(0, +)) / Double(errors.count), 2)
            XCTAssertLessThan(Double(errors.filter { $0 > 40 }.count) / Double(errors.count), 0.01)
        }
        let item = preview.preview.playerItem, source = preview.preview.playerItem.asset
        var updated = recipe; updated.motionScenes = nil
        try preview.updateText(recipe: updated)
        XCTAssertTrue(preview.preview.playerItem === item); XCTAssertTrue(item.asset === source)
    }

    func testEveryCatalogPresetRendersWithItsProductionDefaults() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { root.deleteLastPathComponent() }
        let font = root.appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let catalog = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("src/packages/motion-runtime/creator-blocks.catalog.json"))) as? [String: Any])
        let entries = try XCTUnwrap(catalog["presets"] as? [[String: Any]])
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let image = directory.appendingPathComponent("image.png")
        let context = CIContext()
        try context.writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 120, height: 80)), to: image, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        for entry in entries {
            let preset = try XCTUnwrap(entry["preset_id"] as? String)
            let end = try XCTUnwrap(entry["default_duration_frames"] as? Int)
            let defaults = try XCTUnwrap(entry["motion_defaults"] as? [String: Any])
            var params = try XCTUnwrap(entry["defaults"] as? [String: Any])
            var images: [String: URL] = [:]
            if entry["kind"] as? String == "media" {
                let count = entry["min_assets"] as? Int ?? 3
                params["assets"] = (0..<count).map { index in
                    let id = "image-\(index)"; images[id] = image
                    return ["asset_id": id]
                }
            }
            let instance: [String: Any] = ["id": preset, "preset_id": preset, "preset_version": 2,
                "start_frame": 0, "end_frame_exclusive": end, "palette": entry["palette_defaults"]!,
                "intensity": defaults["intensity"]!, "params": params,
                "motion": ["version": 2, "speed": defaults["speed"]!, "easing": defaults["easing"]!, "hold_frames": defaults["hold_frames"]!]]
            let painter = try NativeMotionPainter(instancesJSON: JSONSerialization.data(withJSONObject: [instance]),
                runtimeHash: "motion-v6:ck0.40.0:b2556106:2abfa191:creator-blocks-v5-text-appearance", duration: Double(end) / 30,
                fontURL: font, imageURLs: images, canvas: CGSize(width: 270, height: 480))
            for fraction in [0.1, 0.5, 0.9, 0.5] {
                let frame = try painter.image(at: Double(end) / 30 * fraction)
                let cg = try XCTUnwrap(context.createCGImage(frame, from: frame.extent))
                let data = try XCTUnwrap(cg.dataProvider?.data) as Data
                XCTAssertTrue(data.contains(where: { $0 != 0 }), preset)
            }
        }
        XCTAssertEqual(entries.count, 9)
    }

    func testProductionMotionFixturesAreDeterministicAcrossBackwardSeeks() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { root.deleteLastPathComponent() }
        let font = root.appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let fixtures = root.appendingPathComponent("src/packages/motion-runtime/fixtures")
        let context = CIContext()
        for name in ["route-trace-midpoint", "creator-block-text-midpoint", "evolving-type-sequence"] {
            let data = try Data(contentsOf: fixtures.appendingPathComponent(name + ".json"))
            let fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
            let instances = try XCTUnwrap(fixture["instances"] as? [[String: Any]])
            let end = instances.compactMap { $0["end_frame_exclusive"] as? Double }.max() ?? 120
            let painter = try NativeMotionPainter(instancesJSON: JSONSerialization.data(withJSONObject: instances),
                runtimeHash: XCTUnwrap(fixture["runtime_hash"] as? String), duration: end / 30,
                fontURL: font, imageURLs: [:], canvas: CGSize(width: 270, height: 480))
            func pixels(_ time: Double) throws -> Data {
                let frame = try painter.image(at: time)
                let image = try XCTUnwrap(context.createCGImage(frame, from: frame.extent))
                return try XCTUnwrap(image.dataProvider?.data) as Data
            }
            let time = min(1, end / 60)
            let first = try pixels(time)
            XCTAssertTrue(first.contains(where: { $0 != 0 }), name)
            _ = try pixels(max(time, end / 30 - 0.1))
            XCTAssertEqual(first, try pixels(time), name)
            XCTAssertTrue(try pixels(end / 30).allSatisfy { $0 == 0 }, name)
        }
    }
}
#endif
