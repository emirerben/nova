import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import ImageIO

/// KRI-121: Visuals-pool photos on the main track render like the cloud's guided
/// image moments: matted over black, and whole inside a card when asked.
final class StillCardCompositionTests: XCTestCase {
    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// `color` returns straight-alpha RGBA for a pixel, addressed from the top-left.
    private func writePNG(to url: URL, width: Int, height: Int, alpha: CGImageAlphaInfo, color: (Int, Int) -> [UInt8]) throws {
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        for y in 0..<height {
            for x in 0..<width {
                let value = color(x, y)
                for channel in 0..<4 { pixels[(y * width + x) * 4 + channel] = value[channel] }
            }
        }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                                          space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: alpha.rawValue),
                                          provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    /// Opaque red, and an opaque blue frame around a fully transparent middle
    /// that hides white, the way design tools export cut-outs.
    private func writeStills(in directory: URL) throws -> [String: URL] {
        let red = directory.appendingPathComponent("red.png"), hole = directory.appendingPathComponent("hole.png")
        try writePNG(to: red, width: 96, height: 160, alpha: .noneSkipLast) { _, _ in [255, 0, 0, 255] }
        try writePNG(to: hole, width: 96, height: 160, alpha: .last) { x, y in
            (24..<72).contains(x) && (40..<120).contains(y) ? [255, 255, 255, 0] : [0, 0, 255, 255]
        }
        return ["red": red, "hole": hole]
    }

    private func rgba(_ image: CGImage) -> [UInt8] {
        var data = [UInt8](repeating: 0, count: image.width * image.height * 4)
        data.withUnsafeMutableBytes { bytes in
            let context = CGContext(data: bytes.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
        return data
    }

    @MainActor func testTransparentStoryPhotoCrossfadesAsABlackMatte() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = try writeStills(in: directory)
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "red", relativePath: "red"), MediaAsset(id: "hole", relativePath: "hole")], tracks: [
            TimelineTrack(id: "photos", kind: .video, clips: [
                TimelineClip(id: "first", sourceAssetID: "red", sourceDuration: 0.6),
                TimelineClip(id: "second", sourceAssetID: "hole", sourceDuration: 0.6, timelineStart: 0.4, transition: Transition(duration: 0.2))
            ])
        ])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("matte.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state"))).export(recipe: recipe, assetURLs: urls, outputURL: output)
        let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
        reference.videoComposition = preview.playerItem.videoComposition
        for (name, generator) in [("preview", reference), ("export", AVAssetImageGenerator(asset: AVURLAsset(url: output)))] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            func pixel(_ x: Int, _ y: Int, at seconds: Double) async throws -> [Int] {
                let bytes = rgba(try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image)
                return (0..<3).map { Int(bytes[(y * 96 + x) * 4 + $0]) }
            }
            let before = try await pixel(48, 80, at: 0.1)
            XCTAssertTrue(before[0] > 220 && before[1] < 40 && before[2] < 40, "\(name) before the transition \(before)")
            // Halfway, the cloud blends red with the matte's black. An unflattened
            // still would leave the outgoing red at full strength here.
            let middle = try await pixel(48, 80, at: 0.5)
            XCTAssertTrue((70...190).contains(middle[0]) && middle[1] < 40 && middle[2] < 40, "\(name) mid-transition centre \(middle)")
            let middleFrame = try await pixel(6, 6, at: 0.5)
            XCTAssertTrue(middleFrame[0] > 60 && middleFrame[2] > 60, "\(name) mid-transition frame \(middleFrame)")
            // Afterwards neither the outgoing clip nor the hidden white shows.
            let after = try await pixel(48, 80, at: 0.8)
            XCTAssertTrue(after.allSatisfy { $0 < 30 }, "\(name) centre after the transition \(after)")
            let frame = try await pixel(6, 6, at: 0.8)
            XCTAssertTrue(frame[0] < 40 && frame[1] < 40 && frame[2] > 220, "\(name) frame after the transition \(frame)")
        }
    }

    @MainActor func testTransparentOverlayPhotoStillShowsTheBase() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = try writeStills(in: directory)
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) })
        // nil is a legacy overlay, true is editor media that keeps its cut-out.
        for preserveAlpha in [nil, true] as [Bool?] {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
                tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "red", sourceDuration: 1)]),
                         TimelineTrack(id: "overlays", kind: .overlay, clips: [TimelineClip(id: "overlay", sourceAssetID: "hole", sourceDuration: 1, volume: 0, overlayPreserveAlpha: preserveAlpha)])],
                assetManifest: manifest)
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
            let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generator.videoComposition = preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let bytes = rgba(try await generator.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)).image)
            let center = (80 * 96 + 48) * 4, frame = (6 * 96 + 6) * 4
            XCTAssertTrue(bytes[center] > 220 && bytes[center + 1] < 40 && bytes[center + 2] < 40, "preserveAlpha \(String(describing: preserveAlpha)): base hidden, \(Array(bytes[center..<(center + 3)]))")
            XCTAssertTrue(bytes[frame] < 40 && bytes[frame + 2] > 220, "preserveAlpha \(String(describing: preserveAlpha)): overlay missing, \(Array(bytes[frame..<(frame + 3)]))")
        }
    }

    /// The same geometry `StillFrameTests` pins, through the compositor and the
    /// encoder: the card is canvas-sized, so the clip's cover fit must leave it alone.
    @MainActor func testSupportingCardPreviewsAndExportsWithTheCloudGeometry() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("photo.png")
        try writePNG(to: photo, width: 1200, height: 900, alpha: .noneSkipLast) { _, _ in [0, 255, 0, 255] }
        let urls = ["visual-photo": photo]
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        let manifest = RenderAssetManifest(assets: [RenderAssetReference(id: "visual-photo", fingerprint: try RenderFingerprint(fingerprint),
                                                                         source: .visual(visualID: "photo", generation: "1"))])
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "visual-photo", relativePath: "visual-photo", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "card", sourceAssetID: "visual-photo", sourceDuration: 1, stillLayout: .supportingCard)])],
            assetManifest: manifest)
        try recipe.validate()
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.stillImages))
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("card.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state"))).export(recipe: recipe, assetURLs: urls, outputURL: output)
        let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
        reference.videoComposition = preview.playerItem.videoComposition
        // The card is x 96..<981, y 268..<1650; the photo is 885x664 around row 959.
        // Samples sit a few pixels off each edge so 4:2:0 chroma cannot smear them.
        let green: [(Int, Int)] = [(540, 959), (104, 959), (972, 959), (540, 640), (540, 1278),
                                   (88, 400), (988, 400), (540, 260), (540, 1657), (20, 20), (1060, 1900)]
        let black: [(Int, Int)] = [(104, 400), (972, 400), (540, 276), (540, 1641), (104, 276), (972, 1641), (540, 614), (540, 1304)]
        for (name, generator) in [("preview", reference), ("export", AVAssetImageGenerator(asset: AVURLAsset(url: output)))] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let image = try await generator.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [1080, 1920], name)
            let bytes = rgba(image)
            func pixel(_ point: (Int, Int)) -> [Int] { (0..<3).map { Int(bytes[(point.1 * 1080 + point.0) * 4 + $0]) } }
            for point in green {
                let value = pixel(point)
                XCTAssertTrue(value[0] < 50 && value[1] > 200 && value[2] < 50, "\(name) \(point) should be the photo or its cover, is \(value)")
            }
            for point in black {
                let value = pixel(point)
                XCTAssertTrue(value.allSatisfy { $0 < 30 }, "\(name) \(point) should be the card, is \(value)")
            }
        }
    }
}
#endif
