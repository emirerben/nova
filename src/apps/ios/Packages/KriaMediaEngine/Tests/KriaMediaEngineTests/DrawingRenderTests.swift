import Foundation
import XCTest
#if canImport(AVFoundation)
import CoreImage
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
@testable import KriaMediaEngine

/// KRI-589: every texture rendered through the real painter (and, for the frame-level cases, through the
/// compositor's own `NativeDrawingStore`) at t < start, t = start, mid-reveal and settled.
final class DrawingRenderTests: XCTestCase {
    private let canvas = CGSize(width: 270, height: 480)
    private static let colors: [DrawingTexture: String] = [
        .pen: "#FF3B30", .marker: "#FFD60A", .highlighter: "#FFE066", .neon: "#32D4FF", .chalk: "#FFFFFF", .spray: "#FF7AD9",
    ]

    /// A looping scribble that crosses itself, so overlap behaviour shows.
    private func scribble(seed: Int = 0) -> [[Double]] {
        (0...40).map { index in
            let t = Double(index) / 40
            return [0.15 + 0.7 * t, 0.5 + 0.22 * sin(t * 2 * .pi * 2 + Double(seed)) + 0.12 * cos(t * 2 * .pi * 5)]
        }
    }

    private func layer(_ texture: DrawingTexture, entrance: DrawingEntrance = .draw, exit: DrawingExit = .none,
                       width: Double = 0.03, start: Double = 1, end: Double = 4, draw: Double = 1.2) -> PortableDrawingLayer {
        let opacity = texture == .highlighter ? 0.35 : 1
        let stroke = DrawingStroke(color: Self.colors[texture]!, opacity: opacity, widthFrac: width, texture: texture, seed: 1000 + (DrawingTexture.allCases.firstIndex(of: texture) ?? 0),
                                   points: scribble().map { $0.map { min(1, max(0, $0)) } })
        return PortableDrawingLayer(id: "d-\(texture.rawValue)", start: start, end: end, canvasAspect: 270.0 / 480.0,
                                    entrance: entrance, drawDuration: draw, exit: exit, strokes: [stroke])
    }

    private func bitmap(_ layer: PortableDrawingLayer, at time: Double) throws -> [UInt8] {
        let context = try XCTUnwrap(CGContext(data: nil, width: Int(canvas.width), height: Int(canvas.height), bitsPerComponent: 8, bytesPerRow: Int(canvas.width) * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.translateBy(x: 0, y: canvas.height); context.scaleBy(x: 1, y: -1)
        NativeDrawingPainter.paint(layer, at: time, canvas: canvas, in: context)
        let data = try XCTUnwrap(context.data)
        return Array(UnsafeBufferPointer(start: data.assumingMemoryBound(to: UInt8.self), count: context.bytesPerRow * context.height))
    }

    /// Ink mass: summed alpha, in whole-pixel units.
    private func coverage(_ pixels: [UInt8]) -> Double {
        stride(from: 3, to: pixels.count, by: 4).reduce(0.0) { $0 + Double(pixels[$1]) / 255 }
    }

    func testNothingIsPaintedBeforeTheStartAndTheRevealOnlyGrows() throws {
        for texture in DrawingTexture.allCases {
            let layer = layer(texture)
            XCTAssertEqual(coverage(try bitmap(layer, at: 0.5)), 0, "\(texture) before start")
            XCTAssertEqual(coverage(try bitmap(layer, at: 1.0)), 0, "\(texture) at start: the pen has not moved yet")
            var previous = 0.0
            for time in stride(from: 1.1, through: 2.4, by: 0.1) {
                let value = coverage(try bitmap(layer, at: time))
                XCTAssertGreaterThanOrEqual(value, previous * 0.98, "\(texture) ink shrank at t=\(time)")
                previous = max(previous, value)
            }
            let settled = coverage(try bitmap(layer, at: 3.5))
            XCTAssertGreaterThan(settled, 40, "\(texture) never appeared")
            XCTAssertGreaterThan(settled, coverage(try bitmap(layer, at: 1.4)) * 1.3, "\(texture) mid-reveal is not partial")
            XCTAssertEqual(coverage(try bitmap(layer, at: 4.0)), 0, "\(texture) after end")
        }
    }

    func testRevealNeverReshufflesGrainOrSprayDots() throws {
        // Anything inked early is still inked (same position, same dots) once the stroke is fully drawn.
        for texture in [DrawingTexture.spray, .chalk] {
            let layer = layer(texture)
            let early = try bitmap(layer, at: 1.5), settled = try bitmap(layer, at: 3.5)
            var lost = 0.0, early_mass = 0.0
            for index in stride(from: 3, to: early.count, by: 4) {
                early_mass += Double(early[index])
                lost += Double(max(0, Int(early[index]) - Int(settled[index])))
            }
            XCTAssertLessThan(lost / max(early_mass, 1), 0.08, "\(texture) reshuffled while revealing")
        }
    }

    func testTexturesAreDeterministicAcrossTwoRenders() throws {
        for texture in DrawingTexture.allCases {
            let layer = layer(texture)
            XCTAssertEqual(try bitmap(layer, at: 1.7), try bitmap(layer, at: 1.7), "\(texture) mid-reveal")
            XCTAssertEqual(try bitmap(layer, at: 3.0), try bitmap(layer, at: 3.0), "\(texture) settled")
        }
    }

    func testChalkIsGrainierThanPenAndSprayIsSparserThanMarker() throws {
        let pen = coverage(try bitmap(layer(.pen), at: 3)), chalk = coverage(try bitmap(layer(.chalk), at: 3))
        XCTAssertLessThan(chalk, pen * 0.95, "chalk should leave speckled gaps")
        XCTAssertGreaterThan(chalk, pen * 0.3)
        XCTAssertLessThan(coverage(try bitmap(layer(.spray), at: 3)), coverage(try bitmap(layer(.marker), at: 3)))
    }

    func testFadeEntranceAndExitRampTheWholeLayer() throws {
        let fading = layer(.pen, entrance: .fade, exit: .fade)
        let early = coverage(try bitmap(fading, at: 1.05)), full = coverage(try bitmap(fading, at: 2.5)), late = coverage(try bitmap(fading, at: 3.95))
        XCTAssertGreaterThan(early, 0)
        XCTAssertLessThan(early, full * 0.5)
        XCTAssertLessThan(late, full * 0.5)
        // Entrance fade shows the whole drawing at once (no reveal): the same shape, only fainter.
        XCTAssertEqual(NativeDrawingPainter.revealProgress(of: fading, at: 1.05), 1)
        XCTAssertEqual(NativeDrawingPainter.fadeAlpha(of: layer(.pen, entrance: .fade, end: 1.2), at: 1.1), 1, accuracy: 1e-9)
    }

    func testTransformAspectFitsRotatesAndScalesAboutTheBoundingBoxCentre() throws {
        let base = layer(.pen, entrance: .none)
        var moved = base
        moved = PortableDrawingLayer(id: "m", start: 1, end: 4, canvasAspect: base.canvasAspect, centerX: 0.25, centerY: 0.75, scale: 0.5,
                                     rotationDeg: 90, entrance: .none, strokes: base.strokes)
        let pixels = try bitmap(moved, at: 2)
        var box = CGRect.null
        for row in 0..<Int(canvas.height) { for column in 0..<Int(canvas.width) {
            let offset = row * Int(canvas.width) * 4 + column * 4 + 3
            if pixels[offset] > 128 { box = box.union(CGRect(x: column, y: row, width: 1, height: 1)) }
        } }
        XCTAssertFalse(box.isNull)
        XCTAssertEqual(box.midX / canvas.width, 0.25, accuracy: 0.03)
        XCTAssertEqual(box.midY / canvas.height, 0.75, accuracy: 0.03)
        let original = try bitmap(base, at: 2)
        var originalBox = CGRect.null
        for row in 0..<Int(canvas.height) { for column in 0..<Int(canvas.width) where original[row * Int(canvas.width) * 4 + column * 4 + 3] > 128 {
            originalBox = originalBox.union(CGRect(x: column, y: row, width: 1, height: 1))
        } }
        // Rotated a quarter turn and halved: the wide scribble becomes a tall, half-size one.
        XCTAssertEqual(box.height, originalBox.width * 0.5, accuracy: originalBox.width * 0.12)
        XCTAssertEqual(box.width, originalBox.height * 0.5, accuracy: originalBox.height * 0.2)
    }

    func testAZeroLengthStrokeIsADot() throws {
        let dot = PortableDrawingLayer(id: "dot", start: 0, end: 2, canvasAspect: 270.0 / 480.0, entrance: .none, strokes: [
            DrawingStroke(color: "#00FF00", widthFrac: 0.08, points: [[0.5, 0.5], [0.5, 0.5]])])
        XCTAssertGreaterThan(coverage(try bitmap(dot, at: 1)), 100)
    }

    // MARK: - Compositor path

    private func render(_ image: CIImage, extent: CGRect) throws -> [UInt8] {
        var buffer = [UInt8](repeating: 0, count: Int(extent.width * extent.height) * 4)
        CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!, .outputColorSpace: CGColorSpace(name: CGColorSpace.sRGB)!])
            .render(image, toBitmap: &buffer, rowBytes: Int(extent.width) * 4, bounds: extent, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return buffer
    }

    func testHighlighterMultipliesAgainstTheFrameAndNeverLightensIt() throws {
        let extent = CGRect(origin: .zero, size: canvas)
        let grey = CIImage(color: CIColor(red: 0.5, green: 0.5, blue: 0.5)).cropped(to: extent)
        let store = NativeDrawingStore(layers: [layer(.highlighter, entrance: .none)], canvas: canvas)
        let plain = try render(grey, extent: extent)
        let marked = try render(store.compose(over: grey, at: 2, canvas: extent), extent: extent)
        var darker = 0, lighter = 0
        for index in stride(from: 0, to: plain.count, by: 4) {
            for channel in 0..<3 {
                if Int(marked[index + channel]) < Int(plain[index + channel]) - 2 { darker += 1 }
                if Int(marked[index + channel]) > Int(plain[index + channel]) + 1 { lighter += 1 }
            }
        }
        XCTAssertGreaterThan(darker, 300, "a yellow highlighter must darken mid-grey")
        XCTAssertEqual(lighter, 0, "multiply can never lighten the picture")
        // A source-over pen of the same colour does lighten the grey: proves the blend, not the colour, is what matters.
        let pen = NativeDrawingStore(layers: [layer(.pen, entrance: .none)], canvas: canvas)
        let lit = try render(pen.compose(over: grey, at: 2, canvas: extent), extent: extent)
        XCTAssertTrue(zip(plain, lit).contains { Int($1) > Int($0) + 40 })
    }

    func testStorePaintsSettledLayersFromCacheIdentically() throws {
        let extent = CGRect(origin: .zero, size: canvas)
        let grey = CIImage(color: CIColor(red: 0.2, green: 0.2, blue: 0.2)).cropped(to: extent)
        let store = NativeDrawingStore(layers: [layer(.neon)], canvas: canvas)
        let first = try render(store.compose(over: grey, at: 3, canvas: extent), extent: extent)
        let second = try render(store.compose(over: grey, at: 3.5, canvas: extent), extent: extent)
        XCTAssertEqual(first, second)
        XCTAssertEqual(try render(store.compose(over: grey, at: 0.5, canvas: extent), extent: extent), try render(grey, extent: extent))
    }

    // MARK: - Montage artifact

    func testWritesAMontageOfEveryTextureThroughTheCompositor() throws {
        let cell = CGSize(width: 180, height: 320)
        let times: [(String, Double)] = [("t<start", 0.5), ("start", 1.0), ("mid-draw", 1.6), ("settled", 3.0), ("fade-out", 3.9)]
        let sheet = try XCTUnwrap(CGContext(data: nil, width: Int(cell.width) * times.count, height: Int(cell.height) * DrawingTexture.allCases.count,
            bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        let extent = CGRect(origin: .zero, size: canvas)
        let ci = CIContext()
        for (row, texture) in DrawingTexture.allCases.enumerated() {
            let background = CIFilter(name: "CILinearGradient", parameters: [
                "inputPoint0": CIVector(x: 0, y: 0), "inputPoint1": CIVector(x: canvas.width, y: canvas.height),
                "inputColor0": CIColor(red: 0.18, green: 0.2, blue: 0.3), "inputColor1": CIColor(red: 0.85, green: 0.78, blue: 0.6),
            ])!.outputImage!.cropped(to: extent)
            let store = NativeDrawingStore(layers: [layer(texture, exit: .fade)], canvas: canvas)
            for (column, entry) in times.enumerated() {
                let image = try XCTUnwrap(ci.createCGImage(store.compose(over: background, at: entry.1, canvas: extent), from: extent))
                sheet.draw(image, in: CGRect(x: CGFloat(column) * cell.width, y: CGFloat(DrawingTexture.allCases.count - 1 - row) * cell.height, width: cell.width, height: cell.height))
            }
        }
        let directory = URL(fileURLWithPath: ProcessInfo.processInfo.environment["KRIA_DRAWING_MONTAGE_DIR"] ?? NSTemporaryDirectory())
            .appendingPathComponent("kria-drawing-montage", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("montage.png")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, try XCTUnwrap(sheet.makeImage()), nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        print("DRAWING_MONTAGE \(url.path)")
        XCTAssertGreaterThan(try Data(contentsOf: url).count, 10_000)
    }

    // MARK: - Wire

    func testTheSharedWireFixtureDecodesAndRejectsInvalidLayers() throws {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<8 { url.deleteLastPathComponent() }
        let root = url.appendingPathComponent("tests/fixtures/drawing-parity")
        struct Compile: Decodable { struct Case: Decodable { let name: String; let expectedLayers: [PortableDrawingLayer] }; let cases: [Case] }
        let compile = try RecipeJSON.decoder().decode(Compile.self, from: Data(contentsOf: root.appendingPathComponent("compile_v1.json")))
        XCTAssertGreaterThanOrEqual(compile.cases.count, 8)
        for item in compile.cases { for layer in item.expectedLayers { try layer.validate() } }
        // Round trip through the recipe wire.
        let layer = try XCTUnwrap(compile.cases.first?.expectedLayers.first)
        let again = try RecipeJSON.decoder().decode(PortableDrawingLayer.self, from: RecipeJSON.encoder().encode(layer))
        XCTAssertEqual(again, layer)
    }

    func testStrokeRevealIsShared() {
        // Handwriting and drawing walk the same arc-length helper: a two-segment path revealed at 75% of its length.
        let points = [TextStrokePoint(x: 0, y: 0), TextStrokePoint(x: 10, y: 0), TextStrokePoint(x: 10, y: 10)]
        let prefix = StrokeReveal.prefix(points, fraction: 0.75)
        XCTAssertEqual(prefix.count, 3)
        XCTAssertEqual(prefix[2].y, 5, accuracy: 1e-9)
    }
}
#endif
