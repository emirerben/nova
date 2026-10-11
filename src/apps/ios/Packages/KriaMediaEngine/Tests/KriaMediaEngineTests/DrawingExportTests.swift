import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers

/// KRI-589: a recipe with drawings goes through the production exporter, and frames are grabbed from the
/// exported MP4 (not just the preview) before the pen starts, mid-draw, drawn, and fading out. Preview and
/// export share one compositor; this proves the file shows what the preview shows.
final class DrawingExportTests: XCTestCase {
    private let width = 360, height = 640

    private func stroke(_ texture: DrawingTexture, _ color: String, opacity: Double = 1, width frac: Double, y: Double, seed: Int) -> DrawingStroke {
        var points: [[Double]] = []
        for index in 0...24 {
            let t = Double(index)
            let px: Double = 0.12 + 0.76 * t / 24
            let py: Double = y + 0.05 * sin(t / 3)
            points.append([px, py])
        }
        return DrawingStroke(color: color, opacity: opacity, widthFrac: frac, texture: texture, seed: seed, points: points)
    }

    @MainActor private func grab(_ asset: AVAsset, _ composition: AVVideoComposition?, at time: Double) async throws -> [UInt8] {
        let context = CIContext()
        let generator = AVAssetImageGenerator(asset: asset)
        generator.videoComposition = composition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let frame = try generator.copyCGImage(at: CMTime(seconds: time, preferredTimescale: 600), actualTime: nil)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: width * 4,
                       bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
        return pixels
    }

    @MainActor func testExportedFramesShowTheDrawingGrowingAndFadingAndMatchThePreview() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let base = directory.appendingPathComponent("base.png")
        try context.writePNGRepresentation(of: CIImage(color: CIColor(red: 0.45, green: 0.5, blue: 0.55)).cropped(to: CGRect(x: 0, y: 0, width: width, height: height)),
                                           to: base, format: .RGBA8, colorSpace: space)
        let asset = MediaAsset(id: "base", relativePath: "base", fingerprint: try SHA256Fingerprinter().fingerprint(file: base))
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "base", fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: "base"))])
        let aspect = Double(width) / Double(height)
        // A fresh drawing sits at its own bounding-box centre (the identity transform).
        func layer(_ id: String, _ stroke: DrawingStroke, exit: DrawingExit) -> PortableDrawingLayer {
            let ys = stroke.points.map { $0[1] }, xs = stroke.points.map { $0[0] }
            return PortableDrawingLayer(id: id, start: 1, end: 3.5, canvasAspect: aspect, centerX: ((xs.min() ?? 0) + (xs.max() ?? 0)) / 2,
                                        centerY: ((ys.min() ?? 0) + (ys.max() ?? 0)) / 2, entrance: .draw, drawDuration: 1, exit: exit, strokes: [stroke])
        }
        let layers = [
            layer("pen", stroke(.pen, "#FF3B30", width: 0.016, y: 0.3, seed: 1), exit: .none),
            layer("highlighter", stroke(.highlighter, "#FFE066", opacity: 0.35, width: 0.06, y: 0.5, seed: 2), exit: .none),
            layer("neon", stroke(.neon, "#32D4FF", width: 0.016, y: 0.7, seed: 3), exit: .fade),
        ]
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height), assets: [asset],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 4)])],
            assetManifest: manifest, drawingLayers: layers)
        try recipe.validate()
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.freehandDrawing))
        let urls = ["base": base]

        let output = directory.appendingPathComponent("drawing.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)

        let times: [(String, Double)] = [("before", 0.5), ("start", 1.0), ("mid-draw", 1.5), ("drawn", 2.4), ("fading-out", 3.4)]
        var exported: [[UInt8]] = [], previewed: [[UInt8]] = []
        for (_, time) in times {
            exported.append(try await grab(AVURLAsset(url: output), nil, at: time))
            previewed.append(try await grab(preview.playerItem.asset, preview.playerItem.videoComposition, at: time))
        }
        // Ink = pixels that left the flat base colour (H.264 noise stays within a few levels).
        func ink(_ pixels: [UInt8]) -> Int {
            var count = 0
            for index in stride(from: 0, to: pixels.count, by: 4) {
                let dr = abs(Int(pixels[index]) - 115), dg = abs(Int(pixels[index + 1]) - 128), db = abs(Int(pixels[index + 2]) - 140)
                if dr > 14 || dg > 14 || db > 14 { count += 1 }
            }
            return count
        }
        let counts = exported.map(ink)
        XCTAssertEqual(counts[0], 0, "nothing before the window")
        XCTAssertLessThan(counts[1], 30, "the pen has not moved at the window start")
        XCTAssertGreaterThan(counts[2], 400, "mid-draw shows part of the strokes")
        XCTAssertGreaterThan(counts[3], Int(Double(counts[2]) * 1.4), "the settled drawing is more than the half-drawn one")
        XCTAssertLessThan(counts[4], counts[3], "the neon stroke fades out while the others stay")
        // Preview and export are one compositor: the same pixels up to H.264.
        for (index, (name, _)) in times.enumerated() {
            var diff = 0
            for (a, b) in zip(exported[index], previewed[index]) { diff += abs(Int(a) - Int(b)) }
            XCTAssertLessThan(Double(diff) / Double(exported[index].count), 6, "\(name): export differs from preview")
        }

        // Montage: preview over export, one column per sampled time.
        let sheet = try XCTUnwrap(CGContext(data: nil, width: width * times.count / 2, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        for (index, pixels) in exported.enumerated() {
            let image = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData).flatMap {
                CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4, space: space,
                        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue), provider: $0, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
            })
            sheet.draw(image, in: CGRect(x: index * width / 2, y: 0, width: width / 2, height: height / 2))
        }
        for (index, pixels) in previewed.enumerated() {
            let image = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData).flatMap {
                CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4, space: space,
                        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue), provider: $0, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
            })
            sheet.draw(image, in: CGRect(x: index * width / 2, y: height / 2, width: width / 2, height: height / 2))
        }
        let folder = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KRIA_DRAWING_MONTAGE_DIR"] ?? NSTemporaryDirectory())
            .appendingPathComponent("kria-drawing-montage", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent("export-frames.png")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, try XCTUnwrap(sheet.makeImage()), nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        print("DRAWING_EXPORT_FRAMES \(url.path) (top: preview, bottom: export; columns: \(times.map(\.0).joined(separator: ", ")))")
    }
}
#endif
