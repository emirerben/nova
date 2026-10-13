import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import CoreImage
import ImageIO
import UniformTypeIdentifiers

/// KRI-646: arc text through the REAL compile -> `PortableTextVectorPainter` path, judged on the
/// rendered bitmap (where the ink is), not on the geometry helper's own numbers.
final class TextBendTests: XCTestCase {
    private let canvas = Canvas(width: 800, height: 500)
    private var font: URL {
        let root = #filePath.range(of: "/src/apps/ios/")!
        return URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
    }

    private struct Ink {
        let width: Int, height: Int
        let alpha: [UInt8]
        let picture: CGImage
        /// Rows are top-down. Vertical centre of the ink in `columns`, or nil without ink there.
        func centre(columns: Range<Int>) -> Double? {
            var total = 0.0, count = 0.0
            for y in 0..<height { for x in columns where alpha[y * width + x] > 16 { total += Double(y); count += 1 } }
            return count > 0 ? total / count : nil
        }
        var inkHeight: Int {
            let rows = (0..<height).filter { y in (0..<width).contains { alpha[y * width + $0] > 16 } }
            return (rows.last ?? 0) - (rows.first ?? 0) + 1
        }
        /// Any ink on the bitmap's outermost pixels means the text was cut off.
        var touchesEdge: Bool {
            (0..<width).contains { alpha[$0] > 0 || alpha[(height - 1) * width + $0] > 0 }
                || (0..<height).contains { alpha[$0 * width] > 0 || alpha[$0 * width + width - 1] > 0 }
        }
    }

    private func layer(_ text: String, bend: Double, rotation: Double = 0, strokeWidth: Double = 0,
                       shadows: [TextBlurLayer] = [], legacyEffect: PortableTextLayer.Effect = .none) throws -> PortableTextLayer {
        try AuthoredTextLayout.compile(id: "t", text: text, start: 0, end: 2,
            style: .init(fontAssetID: "font", size: 64, widthFraction: 0.9, rotation: rotation,
                         color: .init(red: 1, green: 1, blue: 1, alpha: 1), strokeWidth: strokeWidth, shadows: shadows, bend: bend),
            fontURL: font, canvas: canvas, legacyEffect: legacyEffect)
    }

    private func render(_ layer: PortableTextLayer) throws -> Ink {
        let painted = try RecipeTextLayer.make(layer, assetURLs: ["font": font], canvas: CGSize(width: canvas.width, height: canvas.height))
        let context = CIContext()
        let image = try XCTUnwrap(context.createCGImage(painted.image, from: painted.image.extent))
        let width = image.width, height = image.height
        let bitmap = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        bitmap.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let data = try XCTUnwrap(bitmap.data).assumingMemoryBound(to: UInt8.self)
        return Ink(width: width, height: height, alpha: (0..<(width * height)).map { data[$0 * 4 + 3] }, picture: image)
    }

    /// Set KRIA_BEND_MONTAGE_DIR to look at the renders (the checks below are the gate, this is the eyeball).
    private func dump(_ ink: Ink, as name: String) {
        guard let directory = ProcessInfo.processInfo.environment["KRIA_BEND_MONTAGE_DIR"],
              let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: directory).appendingPathComponent(name + ".png") as CFURL,
                                                                UTType.png.identifier as CFString, 1, nil) else { return }
        // White text on a dark card, so the PNG is readable on its own.
        guard let card = CGContext(data: nil, width: ink.width, height: ink.height, bitsPerComponent: 8, bytesPerRow: 0,
                                   space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
        card.setFillColor(CGColor(red: 0.25, green: 0.27, blue: 0.32, alpha: 1))
        card.fill(CGRect(x: 0, y: 0, width: ink.width, height: ink.height))
        card.draw(ink.picture, in: CGRect(x: 0, y: 0, width: ink.width, height: ink.height))
        guard let flattened = card.makeImage() else { return }
        CGImageDestinationAddImage(destination, flattened, nil); CGImageDestinationFinalize(destination)
    }

    func testUpBendRaisesTheMiddleAndMirrorsForDownBend() throws {
        let text = "Rainbow text bends"
        let straight = try render(layer(text, bend: 0))
        let up = try render(layer(text, bend: 60))
        let down = try render(layer(text, bend: -60))
        dump(straight, as: "bend-0"); dump(up, as: "bend-plus60"); dump(down, as: "bend-minus60")
        XCTAssertGreaterThan(up.inkHeight, Int(Double(straight.inkHeight) * 1.5), "a 108 degree arc is much taller than a line")
        // Ascenders and descenders sit on opposite sides of the baseline, so the mirror is close, not exact.
        XCTAssertEqual(Double(up.inkHeight), Double(down.inkHeight), accuracy: Double(up.inkHeight) * 0.15, "down bend mirrors up bend")
        func strips(_ ink: Ink) -> (left: Range<Int>, middle: Range<Int>, right: Range<Int>) {
            (0..<(ink.width / 6), (ink.width * 5 / 12)..<(ink.width * 7 / 12), (ink.width * 5 / 6)..<ink.width)
        }
        let upStrips = strips(up), downStrips = strips(down)
        for end in [upStrips.left, upStrips.right] {
            XCTAssertLessThan(try XCTUnwrap(up.centre(columns: upStrips.middle)), try XCTUnwrap(up.centre(columns: end)) - 20, "rainbow: middle is higher (smaller y) than both ends")
        }
        for end in [downStrips.left, downStrips.right] {
            XCTAssertGreaterThan(try XCTUnwrap(down.centre(columns: downStrips.middle)), try XCTUnwrap(down.centre(columns: end)) + 20, "smile: middle is lower than both ends")
        }
        for ink in [up, down] { XCTAssertFalse(ink.touchesEdge, "the bitmap must hold the whole arc") }
    }

    func testFullBendMultiLineWithStrokeShadowAndRotationIsNeverClipped() throws {
        let shadow = TextBlurLayer(color: .init(red: 0, green: 0, blue: 0, alpha: 0.8), sigma: 6, dx: 4, dy: 6)
        for bend in [100.0, -100.0, 35.0] {
            let value = try layer("Two lines of\nbent headline text", bend: bend, rotation: 12, strokeWidth: 3, shadows: [shadow])
            XCTAssertEqual(value.bend, bend)
            let ink = try render(value)
            dump(ink, as: "multiline-\(Int(bend))")
            XCTAssertGreaterThan(ink.inkHeight, 100, "bend \(bend) draws ink")
            XCTAssertFalse(ink.touchesEdge, "bend \(bend): stroke + shadow + rotation stay inside the bitmap")
        }
    }

    func testBendIsDroppedWhereTheBaselineIsNotStraight() throws {
        XCTAssertEqual(try layer("Hi there", bend: 50).bend, 50)
        for effect in [PortableTextLayer.Effect.inkReveal, .typewriter, .streamIn, .smoothType, .dissolveOut] {
            XCTAssertNil(try layer("Hi there", bend: 50, legacyEffect: effect).bend, "\(effect) keeps a straight baseline")
        }
        XCTAssertNil(try layer("Hi there", bend: 0.2).bend, "a hair of bend is straight text")
    }

    func testBendOnTheWireIsOptionalAndValidated() throws {
        let straight = try layer("Hi", bend: 0)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(straight), as: UTF8.self).contains("bend"), "no key when straight keeps old recipes byte-identical")
        let bent = try layer("Hi", bend: 40)
        let decoded = try JSONDecoder().decode(PortableTextLayer.self, from: JSONEncoder().encode(bent))
        XCTAssertEqual(decoded.bend, 40)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: font)
        let manifest = RenderAssetManifest(assets: [RenderAssetReference(id: "font", fingerprint: try RenderFingerprint(fingerprint),
            source: .library(catalog: .font, catalogID: "Inter-Bold.ttf", generation: fingerprint.hex))])
        try decoded.validate(duration: 10, manifest: manifest)
        let tooBent = PortableTextLayer(id: "t", start: 0, end: 1, anchorX: 0, anchorY: 0, rotationDegrees: 0, runs: bent.runs, bend: 101)
        XCTAssertThrowsError(try tooBent.validate(duration: 10, manifest: manifest))
    }
}
#endif
