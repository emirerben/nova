import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import AVFoundation
import CoreImage

/// KRI-182: an overlay clip's `overlayFadeIn`/`overlayFadeOut` fades its
/// opacity on the shared placement curve while its `transform` positioning
/// (the editor's cover-fit pip math) stays exactly as it was.
final class OverlayFadeCompositionTests: XCTestCase {
    private let width = 108, height = 192

    @MainActor func testFadedPipRampsOpacityWithoutMovingOrResizingTheCard() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let context = CIContext()
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        func write(_ name: String, _ color: CIColor, _ size: CGSize) throws -> URL {
            let url = directory.appendingPathComponent(name + ".png")
            try context.writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(origin: .zero, size: size)),
                to: url, format: .RGBA8, colorSpace: space)
            return url
        }
        // A non-square card: the source shape where transform and placement sizing diverge.
        let urls = ["base": try write("base", CIColor(red: 0.1, green: 0.1, blue: 0.1), CGSize(width: 16, height: 16)),
                    "card": try write("card", CIColor(red: 0, green: 1, blue: 0), CGSize(width: 60, height: 20))]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        // NativeEditorRenderCompiler's pip transform: cover-fit, then 0.4 of the canvas width.
        let cover = 60 * max(Double(width) / 60, Double(height) / 20)
        let transform = MediaTransform(scale: (Double(width) * 0.4).rounded(.toNearestOrEven) / cover,
            positionX: (0.6 * Double(width)).rounded(.toNearestOrEven) - Double(width) / 2,
            positionY: (0.7 * Double(height)).rounded(.toNearestOrEven) - Double(height) / 2)
        func render(fade: Bool, at times: [Double]) async throws -> [[UInt8]] {
            let card = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1, transform: transform,
                volume: 0, overlayAboveText: true, overlayPreserveAlpha: true,
                overlayFadeIn: fade ? true : nil, overlayFadeOut: fade ? true : nil)
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
            var frames: [[UInt8]] = []
            for time in times {
                let frame = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: width * height * 4)
                context.render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: width * 4,
                               bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
                frames.append(pixels)
            }
            return frames
        }
        // Frame-aligned samples: two frames in, fully in, mid-window, two frames before the end.
        let times = [1 + 2.0 / 30, 1 + 4.0 / 30, 2, 3 - 2.0 / 30]
        let faded = try await render(fade: true, at: times)
        let plain = try await render(fade: false, at: [2])[0]

        // Fully faded in, the card is byte-identical to the unfaded one:
        // the fade changed opacity only, never size or position.
        XCTAssertEqual(faded[2], plain, "an opaque faded card must match the plain card pixel for pixel")

        // The card's center: 0.6 * 108 = 65 across, 0.7 * 192 = 134 down (bitmap rows are top-down).
        func green(_ pixels: [UInt8], x: Int, y: Int) -> Double { Double(pixels[(y * width + x) * 4 + 1]) }
        let (cx, cy) = (65, 134)
        let base = green(plain, x: 5, y: 5), opaque = green(plain, x: cx, y: cy)
        XCTAssertGreaterThan(opaque - base, 150, "the plain card must be visible over the base")
        for (index, time) in times.enumerated() {
            let expected = VisualMediaPlacement.fadeEnvelope(at: time, windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true)
            let measured = (green(faded[index], x: cx, y: cy) - base) / (opaque - base)
            XCTAssertEqual(measured, expected, accuracy: 0.03, "card opacity at t=\(time)")
            // Pixels well away from the card are never touched by its fade.
            XCTAssertEqual(green(faded[index], x: 5, y: 5), base)
            XCTAssertEqual(green(faded[index], x: 100, y: 20), green(plain, x: 100, y: 20))
        }
        XCTAssertLessThan(green(faded[0], x: cx, y: cy), green(faded[1], x: cx, y: cy), "fade-in must rise")
        XCTAssertLessThan(green(faded[3], x: cx, y: cy), opaque, "fade-out must fall before the window ends")
    }

    func testOverlayFadeAlphaFollowsThePlacementCurve() {
        let clip = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1,
                                overlayFadeIn: true, overlayFadeOut: true)
        let placement = VisualMediaPlacement(order: 0, windowStart: 1, windowEnd: 3, fadeIn: true, fadeOut: true)
        for time in stride(from: 0.9, through: 3.1, by: 0.025) {
            XCTAssertEqual(clip.overlayFadeAlpha(at: time), placement.alpha(at: time), accuracy: 1e-12, "t=\(time)")
        }
        XCTAssertEqual(clip.overlayFadeAlpha(at: 1), 0)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 1.075), 0.5, accuracy: 1e-9)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 2), 1)
        XCTAssertEqual(clip.overlayFadeAlpha(at: 3), 0)
        var inOnly = clip; inOnly.overlayFadeOut = nil
        XCTAssertEqual(inOnly.overlayFadeAlpha(at: 2.99), 1)
        let plain = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, timelineStart: 1)
        XCTAssertEqual(plain.overlayFadeAlpha(at: 1), 1)
        // Like the placement path, a window of 0.3 s or less never fades.
        let short = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 0.25, timelineStart: 1,
                                 overlayFadeIn: true, overlayFadeOut: true)
        XCTAssertEqual(short.overlayFadeAlpha(at: 1), 1)
    }

    func testOverlayFadeIsOverlayOnlyAndNeverDoublesAPlacementFade() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 10)
        let asset = MediaAsset(id: "card", relativePath: "card", fingerprint: fingerprint)
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "card",
            fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "card"))])
        func recipe(onOverlayTrack: Bool, placement: VisualMediaPlacement? = nil) -> EditRecipe {
            let base = TimelineClip(id: "base", sourceAssetID: "card", sourceDuration: 2)
            let faded = TimelineClip(id: "faded", sourceAssetID: "card", sourceDuration: 1,
                timelineStart: onOverlayTrack ? 0.5 : 2, volume: 0, visualPlacement: placement, overlayFadeIn: true)
            let tracks = onOverlayTrack
                ? [TimelineTrack(id: "v", kind: .video, clips: [base]), TimelineTrack(id: "o", kind: .overlay, clips: [faded])]
                : [TimelineTrack(id: "v", kind: .video, clips: [base, faded])]
            return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 16, height: 16),
                              assets: [asset], tracks: tracks, assetManifest: manifest)
        }
        XCTAssertNoThrow(try recipe(onOverlayTrack: true).validate())
        XCTAssertTrue(recipe(onOverlayTrack: true).effectiveCapabilities.contains(.editorMedia))
        XCTAssertThrowsError(try recipe(onOverlayTrack: false).validate(), "an overlay fade on the video track")
        XCTAssertThrowsError(try recipe(onOverlayTrack: true, placement: VisualMediaPlacement(order: 0,
            windowStart: 0.5, windowEnd: 1.5, fadeIn: true)).validate(), "a placed clip fades through its placement only")
    }

    func testUnfadedClipEncodesWithoutFadeKeys() throws {
        // Recipes without a fade must encode exactly as before (stable digests).
        let data = try JSONEncoder().encode(TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 1))
        let json = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertFalse(json.contains("overlayFade"))
        let faded = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 1, overlayFadeIn: true)
        XCTAssertEqual(try JSONDecoder().decode(TimelineClip.self, from: JSONEncoder().encode(faded)), faded)
    }
}
#endif
