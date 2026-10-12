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
            let result = RecipeVideoCompositor.positionedSource(layer.image!, layer: layer, canvas: canvas, time: 0)
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
        let plain = RecipeVideoCompositor.positionedSource(makeLayer(preferred: preferred).image!, layer: makeLayer(preferred: preferred), canvas: canvas, time: 0)
        let editedLayer = makeLayer(preferred: preferred, transform: MediaTransform(scale: 1.5, rotationDegrees: 90, positionX: 12, positionY: 8))
        let edited = RecipeVideoCompositor.positionedSource(editedLayer.image!, layer: editedLayer, canvas: canvas, time: 0)
        XCTAssertEqual(edited.extent.midX, canvas.midX + 12, accuracy: 0.001)
        XCTAssertEqual(edited.extent.midY, canvas.midY - 8, accuracy: 0.001)
        // Core Image rounds each extent outward to whole pixels.
        XCTAssertEqual(edited.extent.width, plain.extent.height * 1.5, accuracy: 1.5)
        XCTAssertEqual(edited.extent.height, plain.extent.width * 1.5, accuracy: 1.5)
    }

    @MainActor func testVisualPlacementFitsCroppedPixelsWithoutApplyingCorrectionTwice() {
        var layer = makeLayer(preferred: CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0))
        layer.visualPlacement = VisualMediaPlacement(order: 0, contain: true, widthFraction: 0.5,
            xFraction: 0.75, yFraction: 0.25, windowStart: 0, windowEnd: 1)
        let result = RecipeVideoCompositor.positionedSource(layer.image!, layer: layer, canvas: canvas, time: 0)
        XCTAssertEqual(result.extent.width, 40, accuracy: 0.001)
        // The crop is 30x80 in upright space, so the 40pt-wide card is 106.7pt tall;
        // Core Image rounds that vertical coverage outward to whole pixels.
        XCTAssertEqual(result.extent.height, 108, accuracy: 0.001)
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

    // Upright 80x120 scene: TL red, TR green, BL blue, BR yellow.
    private var quadrants: CIImage {
        func block(_ color: CIColor, _ x: CGFloat, _ y: CGFloat) -> CIImage {
            CIImage(color: color).cropped(to: CGRect(x: x, y: y, width: 40, height: 60))
        }
        return block(.red, 0, 60).composited(over: block(.green, 40, 60))
            .composited(over: block(.blue, 0, 0)).composited(over: block(CIColor(red: 1, green: 1, blue: 0), 40, 0))
    }

    /// Track preferred transforms in Core Image form: identity, +90, 180, -90 and a mirror.
    private var orientations: [(String, CGAffineTransform)] {
        func turn(_ t: Double) -> CGAffineTransform {
            CGAffineTransform(a: cos(t * .pi / 2).rounded(), b: sin(t * .pi / 2).rounded(),
                c: -sin(t * .pi / 2).rounded(), d: cos(t * .pi / 2).rounded(), tx: 0, ty: 0)
        }
        return [("0", .identity), ("90", turn(1)), ("180", turn(2)), ("270", turn(-1)),
            ("mirrored90", turn(1).concatenating(CGAffineTransform(scaleX: -1, y: 1)).concatenating(CGAffineTransform(translationX: 5, y: 7)))]
    }

    // The crop is defined in upright space, so the same rectangle must show the
    // same region whichever way the file happens to be stored.
    @MainActor func testUprightCropSelectsSameRegionForEveryStoredOrientation() {
        let quadrantCrops: [(String, NormalizedSourceRect, [UInt8])] = [
            ("TL", .init(x: 0, y: 0, width: 0.5, height: 0.5), [255, 0, 0]),
            ("TR", .init(x: 0.5, y: 0, width: 0.5, height: 0.5), [0, 255, 0]),
            ("BL", .init(x: 0, y: 0.5, width: 0.5, height: 0.5), [0, 0, 255]),
            ("BR", .init(x: 0.5, y: 0.5, width: 0.5, height: 0.5), [255, 255, 0])]
        let corners = [CGPoint(x: 2, y: 2), CGPoint(x: 77, y: 2), CGPoint(x: 2, y: 117), CGPoint(x: 77, y: 117)]
        for (name, preferred) in orientations {
            for (quadrant, rect, expected) in quadrantCrops {
                let layer = makeLayer(preferred: preferred, upright: quadrants, crop: rect)
                let result = RecipeVideoCompositor.positionedSource(layer.image!, layer: layer, canvas: canvas, time: 0)
                for point in corners {
                    let pixel = rgba(result, at: point)
                    assertChannels(Array(pixel[0..<3]), expected, accuracy: 6, "\(quadrant) crop of \(name) source at \(point)")
                    XCTAssertGreaterThan(pixel[3], 250)
                }
            }
            // Upright top half, middle two quarters: red on the left of the screen, green on the right.
            let band = makeLayer(preferred: preferred, upright: quadrants, crop: .init(x: 0.25, y: 0, width: 0.5, height: 0.5))
            let result = RecipeVideoCompositor.positionedSource(band.image!, layer: band, canvas: canvas, time: 0)
            assertChannels(Array(rgba(result, at: CGPoint(x: 10, y: 60))[0..<3]), [255, 0, 0], accuracy: 6, "band left, \(name)")
            assertChannels(Array(rgba(result, at: CGPoint(x: 70, y: 60))[0..<3]), [0, 255, 0], accuracy: 6, "band right, \(name)")
        }
    }

    func testUprightCropToEncodedRectMapsThroughPreferredTransform() {
        let extent = CGRect(x: 0, y: 0, width: 200, height: 100) // stored landscape
        let crop = NormalizedSourceRect(x: 0, y: 0, width: 0.5, height: 0.25)
        // Identity: unchanged top-left -> bottom-left conversion.
        XCTAssertEqual(RecipeVideoCompositor.uprightCropToEncodedRect(crop: crop, extent: extent, preferredTransform: .identity),
            CGRect(x: 0, y: 75, width: 100, height: 25))
        // Quarter turn: the upright frame is 100x200; its top-left quarter-height strip is a
        // 50x50 square of the stored frame, which a rotated file keeps in a corner.
        let quarter = CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 0, ty: 0)
        let rect = RecipeVideoCompositor.uprightCropToEncodedRect(crop: crop, extent: extent, preferredTransform: quarter)
        XCTAssertEqual(rect.width, 50, accuracy: 0.001)
        XCTAssertEqual(rect.height, 50, accuracy: 0.001)
        XCTAssertTrue(extent.contains(rect))
    }

    /// The stored (encoded) frame whose upright appearance under `preferred` is `upright`.
    private func encoded(_ upright: CIImage, preferred: CGAffineTransform) -> CIImage {
        let stored = upright.transformed(by: preferred.inverted())
        return stored.transformed(by: CGAffineTransform(translationX: -stored.extent.minX, y: -stored.extent.minY))
    }

    @MainActor private func makeLayer(preferred: CGAffineTransform, transform: MediaTransform = .identity,
                                      upright: CIImage? = nil, crop: NormalizedSourceRect? = nil) -> RecipeVideoLayer {
        let clip = TimelineClip(id: "crop", sourceAssetID: "source", sourceDuration: 1, transform: transform)
        let stored = encoded(upright ?? source, preferred: preferred)
        return RecipeVideoLayer(trackID: nil, image: stored,
            transform: AVPlayerPreviewComposer.transform(naturalSize: stored.extent.size, preferred: preferred, canvas: canvas.size, clip: clip),
            start: 0, end: 1, fadeIn: 0, naturalSize: stored.extent.size, preferredTransform: preferred, sourceCrop: crop ?? self.crop)
    }

    private func assertChannels(_ a: [UInt8], _ b: [UInt8], accuracy: Int, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(a.count, b.count, message, file: file, line: line)
        for (x, y) in zip(a, b) { XCTAssertLessThanOrEqual(abs(Int(x) - Int(y)), accuracy, "\(a) vs \(b): \(message)", file: file, line: line) }
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
