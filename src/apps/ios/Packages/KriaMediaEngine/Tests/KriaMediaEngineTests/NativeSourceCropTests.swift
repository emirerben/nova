import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import AVFoundation
import CoreImage

final class NativeSourceCropTests: XCTestCase {
    private let canvas = CGRect(x: 0, y: 0, width: 80, height: 120)
    private let crop = NormalizedSourceRect(x: 0.1, y: 0.1, width: 0.3, height: 0.4)

    // Only the selected top-left region is green. A bottom-origin crop samples
    // red, while keeping the old full-source fit exposes uncovered pixels.
    private var source: CIImage {
        CIImage(color: .green).cropped(to: CGRect(x: 10, y: 100, width: 30, height: 80))
            .composited(over: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 100, height: 200)))
    }

    @MainActor func testAsymmetricCropCoversCanvasAcrossSourceRotations() {
        for turn in [0.0, 1.0, 2.0, -1.0] {
            let preferred = CGAffineTransform(a: cos(turn * .pi / 2).rounded(), b: sin(turn * .pi / 2).rounded(),
                c: -sin(turn * .pi / 2).rounded(), d: cos(turn * .pi / 2).rounded(), tx: 0, ty: 0)
                .concatenating(CGAffineTransform(translationX: 17, y: -9))
            let layer = makeLayer(preferred: preferred)
            let result = RecipeVideoCompositor.positionedSource(source, layer: layer, canvas: canvas, time: 0)
            for point in [CGPoint(x: 2, y: 2), CGPoint(x: 77, y: 2), CGPoint(x: 2, y: 117), CGPoint(x: 77, y: 117)] {
                let pixel = rgba(result, at: point)
                XCTAssertLessThan(pixel[0], 5, "Crop must exclude red source pixels at turn \(turn)")
                XCTAssertGreaterThan(pixel[1], 250)
                XCTAssertGreaterThan(pixel[3], 250, "Crop must fill the canvas at turn \(turn)")
            }
        }
    }

    @MainActor func testCropRetainsUserTranslationZoomAndRotation() {
        let preferred = CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0)
        let plain = RecipeVideoCompositor.positionedSource(source, layer: makeLayer(preferred: preferred), canvas: canvas, time: 0)
        let edited = RecipeVideoCompositor.positionedSource(source,
            layer: makeLayer(preferred: preferred, transform: MediaTransform(scale: 1.5, rotationDegrees: 90, positionX: 12, positionY: 8)),
            canvas: canvas, time: 0)
        XCTAssertEqual(edited.extent.midX, canvas.midX + 12, accuracy: 0.001)
        XCTAssertEqual(edited.extent.midY, canvas.midY - 8, accuracy: 0.001)
        XCTAssertEqual(edited.extent.width, plain.extent.height * 1.5, accuracy: 0.001)
        XCTAssertEqual(edited.extent.height, plain.extent.width * 1.5, accuracy: 0.001)
    }

    @MainActor func testVisualPlacementFitsCroppedPixelsWithoutApplyingCorrectionTwice() {
        var layer = makeLayer(preferred: CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0))
        layer.visualPlacement = VisualMediaPlacement(order: 0, contain: true, widthFraction: 0.5,
            xFraction: 0.75, yFraction: 0.25, windowStart: 0, windowEnd: 1)
        let result = RecipeVideoCompositor.positionedSource(source, layer: layer, canvas: canvas, time: 0)
        XCTAssertEqual(result.extent.width, 40, accuracy: 0.001)
        // Core Image rounds the 82.5...97.5 vertical coverage outward to pixels.
        XCTAssertEqual(result.extent.height, 16, accuracy: 0.001)
        XCTAssertEqual(result.extent.midX, 60, accuracy: 0.001)
        XCTAssertEqual(result.extent.midY, 90, accuracy: 0.001)
        let pixel = rgba(result, at: CGPoint(x: 60, y: 90))
        XCTAssertLessThan(pixel[0], 5)
        XCTAssertGreaterThan(pixel[1], 250)
    }

    @MainActor func testMediaSplitKeepsNativeCaptionOutOfSelectedPixelsAndPreservesCropBounds() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("source.png")
        try CIContext().writePNGRepresentation(of: source, to: photo, format: .RGBA8,
            colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound]))
            .appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["photo": photo, "font": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        })
        let text = try AuthoredTextLayout.compile(id: "caption", text: "Hello", start: 0, end: 1,
            style: .init(fontAssetID: "font", size: 16, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
            fontURL: font, canvas: Canvas(width: 80, height: 120))
        for aboveText in [false, true] {
            let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 80, height: 120), assets: assets,
                tracks: [TimelineTrack(id: "base", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "photo", sourceDuration: 1)]),
                    TimelineTrack(id: "overlay", kind: .overlay, clips: [TimelineClip(id: "overlay", sourceAssetID: "photo", sourceDuration: 1,
                        transform: MediaTransform(scale: 0.5, positionX: 4, positionY: 6), overlayAboveText: aboveText, sourceCrop: crop)])],
                assetManifest: manifest, textLayers: [text])
            let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
            let split = try live.mediaInteractionLayers(id: "overlay", time: 0.5)
            func captionIDs(_ composition: AVVideoComposition) throws -> [String] {
                let instruction = try XCTUnwrap(composition.instructions.first as? RecipeVideoInstruction)
                return try instruction.activeText(at: 0.5).compactMap { $0.portable?.id }
            }
            XCTAssertEqual(try captionIDs(split.media), [], "Moving media must not move or duplicate captions")
            XCTAssertEqual(try captionIDs(split.below), aboveText ? ["caption"] : [])
            XCTAssertEqual(try captionIDs(split.above), aboveText ? [] : ["caption"])
            let bounds = try XCTUnwrap(live.mediaSelectionBounds(id: "overlay", time: 0.5))
            XCTAssertEqual(bounds.centerX, 0.55, accuracy: 0.001)
            XCTAssertEqual(bounds.centerY, 0.55, accuracy: 0.001)
            XCTAssertEqual(bounds.width, 0.5, accuracy: 0.001)
            // 80/30 cover fit expands the crop's 80px height to 213.3px,
            // then the creator's 0.5 zoom leaves 106.7px of visible source.
            XCTAssertEqual(bounds.height, 107.0 / 120, accuracy: 0.01)
        }
    }

    @MainActor private func makeLayer(preferred: CGAffineTransform, transform: MediaTransform = .identity) -> RecipeVideoLayer {
        let clip = TimelineClip(id: "crop", sourceAssetID: "source", sourceDuration: 1, transform: transform)
        return RecipeVideoLayer(trackID: nil, image: source,
            transform: AVPlayerPreviewComposer.transform(naturalSize: source.extent.size, preferred: preferred, canvas: canvas.size, clip: clip),
            start: 0, end: 1, fadeIn: 0, naturalSize: source.extent.size, preferredTransform: preferred, sourceCrop: crop)
    }

    private func rgba(_ image: CIImage, at point: CGPoint) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: 4)
        CIContext().render(image, toBitmap: &bytes, rowBytes: 4,
            bounds: CGRect(origin: point, size: CGSize(width: 1, height: 1)), format: .RGBA8,
            colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return bytes
    }
}
#endif
