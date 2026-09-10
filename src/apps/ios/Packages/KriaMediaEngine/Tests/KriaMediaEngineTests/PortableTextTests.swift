import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
import ImageIO

final class PortableTextTests: XCTestCase {
    func testSharedPositionedTextContractRejectsUnknownTreatments() throws {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/kria_positioned_text_v2.json").standardizedFileURL
        let data = try Data(contentsOf: fixture)
        let recipe = try RecipeJSON.decode(data)
        try recipe.validate()
        XCTAssertEqual(recipe.textLayers.first?.runs.first?.fontAssetID, "font")
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.positionedText))
        XCTAssertEqual(CapabilityNegotiator().decide(for: recipe).route, .cloud)
        let restored = try JSONDecoder().decode(EditRecipe.self, from: JSONEncoder().encode(recipe))
        XCTAssertEqual(restored, recipe)
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        var layers = try XCTUnwrap(value["text_layers"] as? [[String: Any]])
        layers[0]["effect"] = "unknown"
        value["text_layers"] = layers
        XCTAssertThrowsError(try RecipeJSON.decode(JSONSerialization.data(withJSONObject: value)))
    }
    private let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1)
    private let black = TextInk(red: 0, green: 0, blue: 0, alpha: 1)
    private func fontURL() throws -> URL {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        return URL(fileURLWithPath: String(#filePath[..<root.lowerBound]))
            .appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
    }
    private func layer(text: String = "Hello", rotation: Double = 0) -> PortableTextLayer {
        PortableTextLayer(id: "caption", start: 0.25, end: 1.25, anchorX: 100, anchorY: 100,
                          rotationDegrees: rotation, runs: [PositionedTextRun(text: text, fontAssetID: "font", fontSize: 36,
                          x: 30, baselineY: 100, letterSpacing: 0, shaped: true, fill: white, stroke: black, strokeWidth: 2)])
    }
    func testDrawsExactFontAtResolvedBaselineAndKeepsOwnTiming() throws {
        let painted = try RecipeTextLayer.make(layer(), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        XCTAssertEqual(painted.start, 0.25); XCTAssertEqual(painted.end, 1.25)
        XCTAssertGreaterThan(painted.frame.width, 70)
        XCTAssertLessThan(painted.frame.width, 120)
        XCTAssertGreaterThan(painted.frame.minY, 90)
        XCTAssertLessThan(painted.frame.maxY, 140)
        let image = try XCTUnwrap(CIContext().createCGImage(painted.image, from: painted.image.extent))
        let data = try XCTUnwrap(image.dataProvider?.data) as Data
        XCTAssertTrue(data.contains { $0 > 0 })
        var pixels = [UInt8](repeating: 0, count: Int(painted.image.extent.width * painted.image.extent.height) * 4)
        CIContext().render(painted.image, toBitmap: &pixels, rowBytes: Int(painted.image.extent.width) * 4,
                           bounds: painted.image.extent, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let whitePixels = stride(from: 0, to: pixels.count, by: 4).filter {
            pixels[$0] > 220 && pixels[$0 + 1] > 220 && pixels[$0 + 2] > 220 && pixels[$0 + 3] > 220
        }.count
        // Drawing an outline over the fill erodes this to thin hairlines.
        XCTAssertGreaterThan(whitePixels, 600)
        if let output = ProcessInfo.processInfo.environment["KRIA_TEXT_REFERENCE_OUTPUT"] {
            let full = painted.image.transformed(by: CGAffineTransform(translationX: painted.frame.minX, y: painted.frame.minY))
                .composited(over: CIImage(color: .clear).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)))
            try CIContext().writePNGRepresentation(of: full, to: URL(fileURLWithPath: output), format: .RGBA8,
                                                  colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        }
    }
    func testShadowHasVisibleBleedAndPreservesFill() throws {
        let base = layer()
        let run = base.runs[0]
        let shadowRun = PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
            x: run.x, baselineY: run.baselineY, letterSpacing: run.letterSpacing, shaped: true,
            fill: run.fill, stroke: run.stroke, strokeWidth: run.strokeWidth,
            blurLayers: [TextBlurLayer(color: TextInk(red: 0, green: 0, blue: 0, alpha: 160.0 / 255), sigma: 12, dx: 0, dy: 6)])
        let cue = PortableTextLayer(id: base.id, start: base.start, end: base.end, anchorX: base.anchorX,
            anchorY: base.anchorY, rotationDegrees: 0, runs: [shadowRun])
        let painted = try RecipeTextLayer.make(cue, assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        let plain = try RecipeTextLayer.make(base, assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        XCTAssertGreaterThan(painted.frame.width, plain.frame.width + 40)
        let full = painted.image.transformed(by: CGAffineTransform(translationX: painted.frame.minX, y: painted.frame.minY))
            .composited(over: CIImage(color: .clear).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)))
        var pixels = [UInt8](repeating: 0, count: 200 * 200 * 4)
        CIContext().render(full, toBitmap: &pixels, rowBytes: 800, bounds: CGRect(x: 0, y: 0, width: 200, height: 200),
                           format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let shadowPixels = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] < 10 && pixels[$0 + 3] > 3 && pixels[$0 + 3] < 100 }.count
        XCTAssertGreaterThan(shadowPixels, 1000)
        let whitePixels = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 220 && pixels[$0 + 3] > 220 }.count
        XCTAssertGreaterThan(whitePixels, 600)
        if let output = ProcessInfo.processInfo.environment["KRIA_SHADOW_REFERENCE_OUTPUT"] {
            try CIContext().writePNGRepresentation(of: full.cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)),
                to: URL(fileURLWithPath: output), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        }
    }

    @MainActor func testIndependentTextAppearsOnlyInsideItsWindowInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: false)
    }
    @MainActor func testAnimatedFadeUsesCompositionTimeInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: true)
    }
    @MainActor private func verifyTextWindow(animated: Bool) async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("black.png")
        let black = CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200))
        try CIContext().writePNGRepresentation(of: black, to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let font = try fontURL()
        let urls = ["photo": photo, "font": font]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map {
            MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: "photo"))
        }
        var cue = layer()
        cue = PortableTextLayer(id: cue.id, start: 0.25, end: 0.75, anchorX: cue.anchorX, anchorY: cue.anchorY, rotationDegrees: 0, runs: cue.runs,
            effect: animated ? .fadeIn : .static, motion: animated ? try motionFixture() : nil)
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 200, height: 200),
            assets: assets, tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 1)])],
            assetManifest: RenderAssetManifest(assets: references), textLayers: [cue])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("out.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
        reference.videoComposition = preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        for generator in [reference, exported] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            for seconds in [0.1, 8.0 / 30, 0.3, 0.5, 0.7, 0.9] {
                let image = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: 200 * 200 * 4)
                CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 200 * 4,
                    bounds: CGRect(x: 0, y: 0, width: 200, height: 200), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                let bright = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count
                if seconds >= 0.25 && seconds < 0.75 {
                    if animated && seconds <= 0.3 { XCTAssertEqual(bright, 0) }
                    else { XCTAssertGreaterThan(bright, 600) }
                } else { XCTAssertEqual(bright, 0) }
            }
        }
    }

    private func motionFixture() throws -> TextMotionParameters {
        try RecipeJSON.decoder().decode(TextMotionParameters.self, from: Data("""
        {"speed":1,"intensity":1,"easing":"ease-out-cubic","stagger_ms":0,"order":"forward",
         "direction":"none","travel_px":0,"overshoot":0.15,"blur_px":0,"cursor_style":"none",
         "cursor_blink_ms":500,"hold_s":1,"exit_s":0,"reveal_ramp_ms":100}
        """.utf8))
    }

    func testRejectsMissingFontAndUnlicensedFallback() throws {
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200), maxBitmapBytes: 1))
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(), assetURLs: [:], canvas: CGSize(width: 200, height: 200)))
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(text: "Hello 🦖"), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200)))
    }
    func testRotationUsesResolvedAnchor() throws {
        let plain = try RecipeTextLayer.make(layer(), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        let rotated = try RecipeTextLayer.make(layer(rotation: 90), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        XCTAssertEqual(plain.frame.width, rotated.frame.height, accuracy: 2)
        XCTAssertEqual(plain.frame.height, rotated.frame.width, accuracy: 2)
    }
}
#endif
