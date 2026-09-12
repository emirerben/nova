import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
@preconcurrency import AVFoundation
import ImageIO

final class PortableTextTests: XCTestCase {
    func testSelectionAcrossCaptionBoundariesNeverAllocatesBitmaps() throws {
        let font = try fontURL()
        let layers = try (0..<80).map { index in
            try AuthoredTextLayout.compile(id: "cue-\(index)", text: "Caption \(index)",
                start: Double(index), end: Double(index + 1),
                style: .init(fontAssetID: "font", size: 96, color: white),
                fontURL: font, canvas: Canvas(width: 600, height: 400))
        }
        let store = try NativeTextLayerStore(layers: layers, assetURLs: ["font": font],
            canvas: CGSize(width: 600, height: 400), maxBitmapBytes: 0)
        for index in (0..<80).reversed() {
            let bounds = try XCTUnwrap(store.selectionBounds(id: "cue-\(index)", at: Double(index) + 0.2))
            XCTAssertGreaterThan(bounds.width, 0)
            XCTAssertGreaterThan(bounds.height, 0)
            XCTAssertEqual(store.residentBitmapBytes, 0)
        }
        XCTAssertNil(store.selectionBounds(id: "cue-0", at: 1))
    }

    func testLongCaptionTimelineRetainsOnlyActiveBitmapsAndSupportsBackwardSeeks() throws {
        let font = try fontURL()
        let canvas = CGSize(width: 600, height: 400)
        let layers = try (0..<40).map { index in
            try AuthoredTextLayout.compile(id: "cue-\(index)", text: "Caption number \(index)",
                start: Double(index), end: Double(index + 1),
                style: .init(fontAssetID: "font", size: 96, widthFraction: 0.7, color: white),
                fontURL: font, canvas: Canvas(width: 600, height: 400))
        }
        let budget = 1024 * 1024
        let store = try NativeTextLayerStore(layers: layers, assetURLs: ["font": font], canvas: canvas, maxBitmapBytes: budget)
        XCTAssertEqual(store.residentBitmapBytes, 0)
        var firstPixels: [UInt8]?
        for time in [0.2, 20.2, 39.2, 0.2] {
            let visible = try store.activeLayers(at: time)
            XCTAssertEqual(visible.count, 1)
            XCTAssertEqual(visible.first?.portable?.id, "cue-\(Int(time))")
            XCTAssertGreaterThan(store.residentBitmapBytes, 0)
            XCTAssertLessThanOrEqual(store.residentBitmapBytes, budget)
            if time == 0.2 {
                let image = try XCTUnwrap(visible.first?.image)
                let rect = image.extent.integral
                var pixels = [UInt8](repeating: 0, count: Int(rect.width * rect.height) * 4)
                CIContext().render(image, toBitmap: &pixels, rowBytes: Int(rect.width) * 4, bounds: rect,
                                   format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                if let firstPixels { XCTAssertEqual(pixels, firstPixels) } else { firstPixels = pixels }
            }
        }
        XCTAssertTrue(try store.activeLayers(at: 40).isEmpty)
        XCTAssertEqual(store.residentBitmapBytes, 0)
    }

    func testAuthoredEmojiKeepsColorAndDoesNotRejectWholePreview() throws {
        let font = try fontURL()
        let canvas = CGSize(width: 600, height: 300)
        for effect in [PortableTextLayer.Effect.none, .typewriter] {
            let layer = try AuthoredTextLayout.compile(id: "emoji", text: "A sunny day ☀️ 🌈",
                start: 0, end: 3, style: .init(fontAssetID: "font", size: 48, color: white),
                fontURL: font, canvas: Canvas(width: 600, height: 300), legacyEffect: effect)
            let painter = try PortableTextVectorPainter(layer: layer, assetURLs: ["font": font], canvas: canvas, outlineGlyphs: true)
            let image = try painter.image(maxBitmapBytes: 8 * 1024 * 1024)
            let bounds = image.extent.integral
            var pixels = [UInt8](repeating: 0, count: Int(bounds.width * bounds.height) * 4)
            CIContext().render(image, toBitmap: &pixels, rowBytes: Int(bounds.width) * 4, bounds: bounds,
                               format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            var colored = 0
            for index in stride(from: 0, to: pixels.count, by: 4) {
                let channels = [pixels[index], pixels[index + 1], pixels[index + 2]]
                let spread = Int(channels.max()!) - Int(channels.min()!)
                if pixels[index + 3] > 100 && spread > 40 { colored += 1 }
            }
            XCTAssertGreaterThan(colored, 20, "Emoji must render in color, not disappear or become primary-font glyph IDs")
        }
    }

    func testHighlightedEmojiWordKeepsItsFallbackFontAndPosition() throws {
        let font = try fontURL()
        let layer = try AuthoredTextLayout.compileHighlightedWords(id: "emoji-words", text: "Hello 🌈 world", start: 0, end: 3,
            starts: [0, 1, 2], highlight: white, style: .init(fontAssetID: "font", size: 48, color: white),
            fontURL: font, canvas: Canvas(width: 600, height: 300))
        XCTAssertEqual(layer.runs.map(\.text), ["Hello", "🌈", "world"])
        XCTAssertNil(layer.runs[1].glyphs)
        XCTAssertTrue(layer.runs[1].shaped)
        XCTAssertGreaterThan(layer.runs[1].x, layer.runs[0].x)
        XCTAssertNoThrow(try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 600, height: 300)))
    }

    func testAuthoredLayoutWrapsLocallyAndCompilesGraphemeReveal() throws {
        let font = try fontURL()
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: font)
        let manifest = RenderAssetManifest(assets: [RenderAssetReference(id: "font",
            fingerprint: try RenderFingerprint(fingerprint),
            source: .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: fingerprint.hex))])
        let style = AuthoredTextLayout.Style(fontAssetID: "font", size: 36, widthFraction: 0.6,
                                            color: .init(red: 1, green: 1, blue: 1, alpha: 1))
        let layer = try AuthoredTextLayout.compile(id: "edited", text: "Hello world from the editor",
            start: 0, end: 3, style: style, fontURL: font, canvas: Canvas(width: 300, height: 300),
            phases: TextAnimationPhases(entrance: .typewriter, exit: .fade, loop: .float))
        try layer.validate(duration: 3, manifest: manifest)
        XCTAssertGreaterThan(layer.runs.count, 1)
        XCTAssertEqual(layer.discreteReveal?.text, "Hello world from the editor")
        let paint = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 300, height: 300))
        XCTAssertNotNil(paint.discreteReveal)
        XCTAssertGreaterThan(paint.frame.width, 50)
        XCTAssertEqual(layer.animationPhases?.exit, .fade)
    }

    func testAuthoredLegacyStreamUsesWordTimingAndMeasuredCursorOffsets() throws {
        let font = try fontURL()
        let layer = try AuthoredTextLayout.compile(id: "stream", text: "Hello world", start: 0, end: 3,
            style: .init(fontAssetID: "font", size: 36, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
            fontURL: font, canvas: Canvas(width: 600, height: 600), legacyEffect: .streamIn)
        XCTAssertNil(layer.animationPhases)
        XCTAssertEqual(layer.effect, .streamIn)
        let content = try XCTUnwrap(layer.discreteReveal)
        let sample = try TextRevealTiming.sample(effect: .streamIn, text: content.text, localTime: 0, motion: nil)
        XCTAssertEqual(sample.visibleText, "Hello")
        XCTAssertTrue(sample.showCursor)
        XCTAssertGreaterThan(content.lines[0].cursorOffsets[5], 60)
        let paint = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: 600, height: 600))
        XCTAssertNotNil(paint.discreteReveal)
    }

    func testAuthoredAlignmentUsesCloudAnchorAndOutlineRadius() throws {
        let font = try fontURL()
        let layer = try AuthoredTextLayout.compile(id: "left", text: "Left anchor", start: 0, end: 3,
            style: .init(fontAssetID: "font", size: 36, xFraction: 0.2, alignment: .left,
                         color: .init(red: 1, green: 1, blue: 1, alpha: 1), strokeWidth: 3),
            fontURL: font, canvas: Canvas(width: 600, height: 600))
        XCTAssertEqual(layer.runs[0].x, 120)
        XCTAssertEqual(layer.runs[0].strokeWidth, 6)
    }

    func testTintPreservesColorAndAppliesCoverageOnlyOnce() {
        let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
        let context = CIContext(options: [.workingColorSpace: colorSpace, .outputColorSpace: colorSpace])
        let mask = CIImage(color: CIColor(red: 1, green: 1, blue: 1, alpha: 0.5))
        let tinted = TextInk(red: 0.8, green: 0.2, blue: 0.4, alpha: 0.7).tint(mask: mask)
        var pixel = [UInt8](repeating: 0, count: 4)
        context.render(tinted, toBitmap: &pixel, rowBytes: 4, bounds: CGRect(x: 0, y: 0, width: 1, height: 1),
                       format: .RGBA8, colorSpace: colorSpace)
        for (actual, expected) in zip(pixel, [71, 18, 36, 89]) {
            XCTAssertEqual(Double(actual), Double(expected), accuracy: 1)
        }
    }

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
    func testLegacyGlyphPositionsDrawWithoutNativeReshaping() throws {
        struct Case: Decodable { let text: String; let fontSize: Double; let spacing: Double; let glyphs: [PositionedGlyph] }
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_legacy_glyphs.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        var widths: [Double] = []
        for (index, test) in cases.enumerated() {
            let run = PositionedTextRun(text: test.text, fontAssetID: "font", fontSize: test.fontSize, x: 20, baselineY: 100,
                letterSpacing: test.spacing, shaped: false, fill: white, stroke: black, strokeWidth: 2, glyphs: test.glyphs)
            let cue = PortableTextLayer(id: "legacy", start: 0, end: 1, anchorX: 100, anchorY: 100, rotationDegrees: 0, runs: [run])
            let painted = try RecipeTextLayer.make(cue, assetURLs: ["font": fontURL()], canvas: CGSize(width: 400, height: 200))
            var pixels = [UInt8](repeating: 0, count: Int(painted.image.extent.width * painted.image.extent.height) * 4)
            CIContext().render(painted.image, toBitmap: &pixels, rowBytes: Int(painted.image.extent.width) * 4,
                bounds: painted.image.extent, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            XCTAssertGreaterThan(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 220 && pixels[$0 + 3] > 220 }.count, 400)
            widths.append(painted.frame.width)
            XCTAssertGreaterThan(painted.frame.width, 60)
            XCTAssertGreaterThan(painted.frame.height, 20)
            if index == 0, let output = ProcessInfo.processInfo.environment["KRIA_GLYPH_REFERENCE_OUTPUT"] {
                let full = painted.image.transformed(by: CGAffineTransform(translationX: painted.frame.minX, y: painted.frame.minY))
                    .composited(over: CIImage(color: .clear).cropped(to: CGRect(x: 0, y: 0, width: 400, height: 200)))
                try CIContext().writePNGRepresentation(of: full, to: URL(fileURLWithPath: output), format: .RGBA8,
                    colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            }
        }
        XCTAssertGreaterThan(widths[1], widths[0] + 7)
        let invalid = PositionedTextRun(text: "X", fontAssetID: "font", fontSize: 36, x: 20, baselineY: 100,
            letterSpacing: 0, shaped: false, fill: white, stroke: black, strokeWidth: 2,
            glyphs: [PositionedGlyph(glyphID: 65535, x: 0, y: 0)])
        XCTAssertThrowsError(try RecipeTextLayer.make(PortableTextLayer(id: "invalid", start: 0, end: 1, anchorX: 100,
            anchorY: 100, rotationDegrees: 0, runs: [invalid]), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200)))
    }

    func testGradientFillsGlyphsWithResolvedEndpoints() throws {
        try verifyGradient(useGlyphs: false)
        try verifyGradient(useGlyphs: true)
    }
    private func verifyGradient(useGlyphs: Bool) throws {
        struct Case: Decodable { let text: String; let glyphs: [PositionedGlyph] }
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../../api/tests/fixtures/phone_legacy_glyphs.json").standardizedFileURL
        let cases = try RecipeJSON.decoder().decode([Case].self, from: Data(contentsOf: fixture))
        let glyphs = try XCTUnwrap(cases.first { $0.text == "Hello" }?.glyphs)
        let base = layer(), run = layer().runs[0]
        let gradient = TextGradient(startX: 30, startY: 100, endX: 117, endY: 100,
            stops: [TextGradientStop(position: 0, color: TextInk(red: 1, green: 0, blue: 0, alpha: 1)),
                    TextGradientStop(position: 1, color: TextInk(red: 0, green: 0, blue: 1, alpha: 1))])
        let gradientRun = PositionedTextRun(text: run.text, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
            x: run.x, baselineY: run.baselineY, letterSpacing: 0, shaped: !useGlyphs, fill: white, stroke: black,
            strokeWidth: 2, gradient: gradient, glyphs: useGlyphs ? glyphs : nil)
        let cue = PortableTextLayer(id: base.id, start: base.start, end: base.end, anchorX: base.anchorX,
            anchorY: base.anchorY, rotationDegrees: 0, runs: [gradientRun])
        let painted = try RecipeTextLayer.make(cue, assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        let full = painted.image.transformed(by: CGAffineTransform(translationX: painted.frame.minX, y: painted.frame.minY))
            .composited(over: CIImage(color: .clear).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)))
        var pixels = [UInt8](repeating: 0, count: 200 * 200 * 4)
        CIContext().render(full, toBitmap: &pixels, rowBytes: 800, bounds: CGRect(x: 0, y: 0, width: 200, height: 200),
                           format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let red = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 150 && pixels[$0 + 2] < 100 && pixels[$0 + 3] > 220 }
        let blue = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0 + 2] > 150 && pixels[$0] < 100 && pixels[$0 + 3] > 220 }
        XCTAssertGreaterThan(red.count, 100); XCTAssertGreaterThan(blue.count, 100)
        XCTAssertTrue(stride(from: 0, to: pixels.count, by: 4).allSatisfy { pixels[$0 + 1] <= 1 }) // sRGB red/blue must not acquire green
        XCTAssertTrue(red.allSatisfy { ($0 / 4) % 200 < 80 })
        XCTAssertTrue(blue.allSatisfy { ($0 / 4) % 200 > 70 })
        let colored = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 10 || pixels[$0 + 2] > 10 }.count
        XCTAssertLessThan(colored, 1500) // gradient must be clipped to glyphs, not the bitmap rectangle
        if let output = ProcessInfo.processInfo.environment["KRIA_GRADIENT_REFERENCE_OUTPUT"] {
            try CIContext().writePNGRepresentation(of: full.cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)),
                to: URL(fileURLWithPath: output), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
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

    @MainActor func testLiveTextUpdateReusesSourcesAndUnchangedBitmapsAtomically() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("black.png")
        try CIContext().writePNGRepresentation(
            of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let urls = ["photo": photo, "font": try fontURL()]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map {
            MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: "photo"))
        }
        let cue = layer()
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 200, height: 200),
            assets: assets, tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 5)])],
            assetManifest: RenderAssetManifest(assets: references), textLayers: [cue])
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let item = live.preview.playerItem
        let source = item.asset
        let geometry = try XCTUnwrap(live.textSelectionBounds(id: cue.id, time: cue.start + 0.1))
        XCTAssertGreaterThan(geometry.width, 0)
        XCTAssertGreaterThan(geometry.height, 0)
        XCTAssertNil(live.textSelectionBounds(id: cue.id, time: cue.end))
        let before = try XCTUnwrap(item.videoComposition?.instructions.first as? RecipeVideoInstruction)
        try live.updateText(recipe: recipe)
        let unchanged = try XCTUnwrap(item.videoComposition?.instructions.first as? RecipeVideoInstruction)
        XCTAssertTrue(before.text[0].image === unchanged.text[0].image)
        recipe.textLayers = []
        recipe.assets.removeAll { $0.id == "font" }
        recipe.assetManifest = RenderAssetManifest(assets: references.filter { $0.id != "font" })
        try live.updateText(recipe: recipe, assetURLs: ["photo": photo])
        XCTAssertTrue(item === live.preview.playerItem)
        XCTAssertTrue(source === item.asset)
        XCTAssertTrue(try XCTUnwrap(item.videoComposition?.instructions.first as? RecipeVideoInstruction).text.isEmpty)
        recipe.assets = assets
        recipe.assetManifest = RenderAssetManifest(assets: references)
        recipe.textLayers = [cue]
        try live.updateText(recipe: recipe, assetURLs: urls)
        XCTAssertTrue(source === item.asset)
        XCTAssertEqual(try XCTUnwrap(item.videoComposition?.instructions.first as? RecipeVideoInstruction).text.count, 1)
        let committed = item.videoComposition
        var invalid = recipe
        invalid.canvas = Canvas(width: 400, height: 400)
        XCTAssertThrowsError(try live.updateText(recipe: invalid))
        XCTAssertTrue(item.videoComposition === committed)
        XCTAssertEqual(live.recipe, recipe)
    }

    @MainActor func testIndependentTextAppearsOnlyInsideItsWindowInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: false)
    }
    @MainActor func testAnimatedFadeUsesCompositionTimeInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: true)
    }
    @MainActor func testLegacyFadeUsesCompositionTimeInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: true, legacy: true)
    }
    @MainActor func testInkRevealClipsRotatedTextInPreviewAndExport() async throws {
        try await verifyTextWindow(animated: false, legacy: true, inkReveal: true)
    }
    @MainActor private func verifyTextWindow(animated: Bool, legacy: Bool = false, inkReveal: Bool = false) async throws {
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
        cue = PortableTextLayer(id: cue.id, start: 0.25, end: 0.75, anchorX: cue.anchorX, anchorY: cue.anchorY, rotationDegrees: inkReveal ? 20 : 0, runs: cue.runs,
            effect: inkReveal ? .inkReveal : animated ? .fadeIn : .static, motion: animated && !legacy ? try motionFixture() : nil,
            revealBounds: inkReveal ? TextRevealBounds(left: 26, top: 50, right: 123, bottom: 110) : nil)
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
            var partialPixels: Int?, settledPixels: Int?
            for seconds in [0.1, 8.0 / 30, 0.3, 0.5, 0.7, 0.9] {
                let image = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
                var pixels = [UInt8](repeating: 0, count: 200 * 200 * 4)
                CIContext().render(CIImage(cgImage: image), toBitmap: &pixels, rowBytes: 200 * 4,
                    bounds: CGRect(x: 0, y: 0, width: 200, height: 200), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
                let bright = stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 }.count
                if seconds >= 0.25 && seconds < 0.75 {
                    if (animated || inkReveal) && seconds <= 0.3 { XCTAssertEqual(bright, 0) }
                    else if inkReveal && seconds == 0.5 { XCTAssertGreaterThan(bright, 100); partialPixels = bright }
                    else { XCTAssertGreaterThan(bright, 600) }
                } else { XCTAssertEqual(bright, 0) }
                if inkReveal && seconds == 0.7 { settledPixels = bright }
            }
            if inkReveal { XCTAssertLessThan(try XCTUnwrap(partialPixels), try XCTUnwrap(settledPixels)) }
        }
    }

    private func motionFixture() throws -> TextMotionParameters {
        try RecipeJSON.decoder().decode(TextMotionParameters.self, from: Data("""
        {"speed":1,"intensity":1,"easing":"ease-out-cubic","stagger_ms":0,"order":"forward",
         "direction":"none","travel_px":0,"overshoot":0.15,"blur_px":0,"cursor_style":"none",
         "cursor_blink_ms":500,"hold_s":1,"exit_s":0,"reveal_ramp_ms":100}
        """.utf8))
    }

    func testRejectsMissingFontAndUnrelatedFontFallback() throws {
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200), maxBitmapBytes: 1))
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(), assetURLs: [:], canvas: CGSize(width: 200, height: 200)))
        XCTAssertThrowsError(try RecipeTextLayer.make(layer(text: "Hello 漢字"), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200)))
    }
    func testRotationUsesResolvedAnchor() throws {
        let plain = try RecipeTextLayer.make(layer(), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        let rotated = try RecipeTextLayer.make(layer(rotation: 90), assetURLs: ["font": fontURL()], canvas: CGSize(width: 200, height: 200))
        XCTAssertEqual(plain.frame.width, rotated.frame.height, accuracy: 2)
        XCTAssertEqual(plain.frame.height, rotated.frame.width, accuracy: 2)
    }
}
#endif
