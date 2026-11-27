#if canImport(AVFoundation)
import CoreGraphics
import XCTest
@testable import KriaMediaEngine

/// KRI-1618: an intro title whose accent row enters after the solid row and whose script row overlaps the serif row
/// under it, drawn by the export's own text pass (`TextLayerStill`, the compositor's `paintText`). The layers are the
/// server's compiled text lane (`tests/fixtures/intro-row-motion-parity/`, English and Turkish), so this is what a
/// phone is told to draw.
///
/// Failure modes: a row drawn into a box that crops its own ink (a script swash cut off), the later row's bitmap
/// hiding the earlier row's ink beyond its own glyphs, rows drawn in another order than the recipe's (a delayed row
/// jumping on top), the delayed row showing before its start, or the stagger holding back the solid row too.
final class IntroRowMotionRenderTests: XCTestCase {
    private struct Fixture: Decodable {
        let title: String
        let language: String
        let layers: [PortableTextLayer]
        let probeLayers: [PortableTextLayer]
        let fonts: [String: String]
        let accentLayer: Int
        let accentStartS: Double
        let cloudInkBoxes: [[Int]]
    }

    private static let repo: URL = {
        let path = #filePath
        let root = path.range(of: "/src/apps/ios/")!
        return URL(fileURLWithPath: String(path[..<root.lowerBound]))
    }()
    private let canvas = Canvas(width: 1080, height: 1920)
    private let settled = 2.0

    private func fixture(_ language: String) throws -> Fixture {
        let url = Self.repo.appendingPathComponent("tests/fixtures/intro-row-motion-parity/\(language).json")
        return try RecipeJSON.decoder().decode(Fixture.self, from: Data(contentsOf: url))
    }

    private func fontURLs(_ fixture: Fixture) -> [String: URL] {
        fixture.fonts.mapValues { Self.repo.appendingPathComponent("src/apps/api/assets/fonts/" + $0) }
    }

    private struct Pixels {
        let width: Int
        let height: Int
        let bytes: [UInt8]
        func rgb(_ x: Int, _ y: Int) -> (Int, Int, Int) {
            let i = (y * width + x) * 4
            return (Int(bytes[i]), Int(bytes[i + 1]), Int(bytes[i + 2]))
        }
        func matching(_ test: ((Int, Int, Int)) -> Bool) -> Set<Int> {
            var found = Set<Int>()
            for y in 0..<height { for x in 0..<width where test(rgb(x, y)) { found.insert(y * width + x) } }
            return found
        }
        func box(_ indices: Set<Int>) -> [Int]? {
            guard !indices.isEmpty else { return nil }
            let xs = indices.map { $0 % width }, ys = indices.map { $0 / width }
            return [xs.min()!, ys.min()!, xs.max()! + 1, ys.max()! + 1]
        }
    }

    private func draw(_ layers: [PortableTextLayer], _ fixture: Fixture, at time: Double) throws -> Pixels {
        let image = try TextLayerStill.render(layers: layers, fontURLs: fontURLs(fixture), over: nil,
                                              canvas: canvas, time: time, width: canvas.width)
        var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: image.width, height: image.height, bitsPerComponent: 8,
                                              bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return Pixels(width: image.width, height: image.height, bytes: bytes)
    }

    private static func white(_ p: (Int, Int, Int)) -> Bool { p.0 > 200 && p.1 > 200 && p.2 > 200 }
    private static func red(_ p: (Int, Int, Int)) -> Bool { p.0 > 200 && p.1 < 90 && p.2 < 90 }
    private static func lit(_ p: (Int, Int, Int)) -> Bool { p.0 + p.1 + p.2 > 60 }

    func testEachRowDrawsItsWholeInkAsTheCloudMeasuresIt() throws {
        for language in ["en", "tr"] {
            let fixture = try fixture(language)
            XCTAssertEqual(fixture.layers.count, fixture.cloudInkBoxes.count, language)
            for (index, layer) in fixture.layers.enumerated() {
                let alone = try draw([layer], fixture, at: settled)
                let box = try XCTUnwrap(alone.box(alone.matching(Self.white)), "\(language) row \(index) draws ink")
                for (side, (phone, cloud)) in zip(["left", "top", "right", "bottom"], zip(box, fixture.cloudInkBoxes[index])) {
                    XCTAssertEqual(Double(phone), Double(cloud), accuracy: 8,
                                   "\(language) row \(index) \(side) ink edge: phone \(box), cloud \(fixture.cloudInkBoxes[index])")
                }
            }
        }
    }

    func testOverlappingRowsKeepEveryInkAndTheLaterRowDrawsOnTop() throws {
        for language in ["en", "tr"] {
            let fixture = try fixture(language)
            let probe = fixture.probeLayers
            XCTAssertEqual(probe.count, 2, language)
            let first = try draw([probe[0]], fixture, at: settled).matching(Self.white)
            let second = try draw([probe[1]], fixture, at: settled).matching(Self.red)
            let both = try draw(probe, fixture, at: settled)
            let shared = first.intersection(second)
            XCTAssertGreaterThan(shared.count, 50, "\(language): the two rows' inks really cross")
            let onTop = shared.filter { Self.red(both.rgb($0 % both.width, $0 / both.width)) }
            XCTAssertGreaterThan(Double(onTop.count) / Double(shared.count), 0.95,
                                 "\(language): where the inks cross, the later recipe row is drawn on top")
            let uncovered = first.subtracting(second)
            let kept = uncovered.filter { Self.lit(both.rgb($0 % both.width, $0 / both.width)) }
            XCTAssertGreaterThan(Double(kept.count) / Double(uncovered.count), 0.99,
                                 "\(language): the later row's bitmap hides none of the earlier row's ink beyond its own glyphs")
        }
    }

    func testTheAccentRowEntersAfterTheSolidRow() throws {
        for language in ["en", "tr"] {
            let fixture = try fixture(language)
            let accent = fixture.layers[fixture.accentLayer]
            XCTAssertEqual(accent.start, fixture.accentStartS, accuracy: 1e-6, language)
            let solid = fixture.layers.enumerated().filter { $0.offset != fixture.accentLayer }.map(\.element)
            let early = fixture.accentStartS / 2
            let withAccent = try draw(fixture.layers, fixture, at: early)
            let without = try draw(solid, fixture, at: early)
            XCTAssertEqual(withAccent.bytes, without.bytes, "\(language): before its start the accent row draws nothing")
            XCTAssertFalse(without.matching(Self.lit).isEmpty, "\(language): the solid row is already up")
            let after = try draw([accent], fixture, at: settled)
            XCTAssertFalse(after.matching(Self.white).isEmpty, "\(language): the accent row draws once it has entered")
        }
    }
}
#endif
