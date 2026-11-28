import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
import AVFoundation
import CoreImage

/// KRI-1616: a card hanging past the side of the frame, and a transparent sticker, on the phone as on the server.
/// `tests/fixtures/edge-bleed-bound/v1.json` is the renderer bound both sides validate against
/// (`tests/routes/test_edge_bleed.py`); `tests/fixtures/sticker-bleed-parity/v1.json` is what the cloud card pass drew
/// for two bled stickers (`tests/pipeline/test_sticker_bleed_parity.py`), measured the same way here on the phone
/// compositor's frame.
final class StickerBleedParityTests: XCTestCase {
    private static let repo: URL = {
        let path = #filePath
        let root = path.range(of: "/src/apps/ios/")!
        return URL(fileURLWithPath: String(path[..<root.lowerBound]))
    }()

    private struct Bound: Decodable {
        struct Placement: Decodable { let name: String; let width: Double?; let x: Double; let valid: Bool }
        struct Drag: Decodable { let name: String; let width: Double; let x: Double; let kept: Double }
        let keepOnCanvas: Double
        let placements: [Placement]
        let drags: [Drag]
    }

    private struct Parity: Decodable {
        struct Card: Decodable { let id: String; let scale: Double; let xFrac: Double; let yFrac: Double }
        struct Measure: Decodable { let id: String; let inkBox: [Int]; let coverage: Double; let meanRgb: [Double] }
        let canvas: [Int]
        let baseRgb: [Int]
        let inkDiff: Int
        let frameAtS: Double
        let cards: [Card]
        let cloud: [Measure]
    }

    private func decode<T: Decodable>(_ type: T.Type, _ path: String) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: Data(contentsOf: Self.repo.appendingPathComponent(path)))
    }

    func testThePlacementBoundIsTheServersFixture() throws {
        let bound = try decode(Bound.self, "tests/fixtures/edge-bleed-bound/v1.json")
        XCTAssertEqual(bound.keepOnCanvas, VisualMediaPlacement.edgeBleedKeepOnCanvas)
        for case_ in bound.placements {
            let placement = VisualMediaPlacement(order: 1, widthFraction: case_.width, xFraction: case_.x, windowStart: 0, windowEnd: 1)
            XCTAssertEqual((try? placement.validate()) != nil, case_.valid, case_.name)
        }
        for drag in bound.drags {
            let range = VisualMediaPlacement.edgeBleedXRange(width: drag.width)
            XCTAssertEqual(min(max(drag.x, range.lowerBound), range.upperBound), drag.kept, accuracy: 1e-9, drag.name)
        }
    }

    /// Edges within 2 px: the cloud scales with ffmpeg's bicubic filter to an even height and encodes 4:2:0 H.264 (chroma
    /// in 2 x 2 blocks), the phone samples with Core Image at the exact height. Coverage within 0.03 and colour within 12
    /// a channel: the same subsampling and YUV round trip on the cloud side.
    @MainActor func testBledStickersDrawWithTheirAlphaWhereTheCloudDrawsThem() async throws {
        let parity = try decode(Parity.self, "tests/fixtures/sticker-bleed-parity/v1.json")
        let (width, height) = (parity.canvas[0], parity.canvas[1])
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let base = directory.appendingPathComponent("base.png")
        let rgb = parity.baseRgb.map { CGFloat($0) / 255 }
        try CIContext().writePNGRepresentation(of: CIImage(color: CIColor(red: rgb[0], green: rgb[1], blue: rgb[2], colorSpace: space)!)
            .cropped(to: CGRect(x: 0, y: 0, width: 16, height: 16)), to: base, format: .RGBA8, colorSpace: space)
        let urls = ["base": base, "sticker": Self.repo.appendingPathComponent("tests/fixtures/sticker-bleed-parity/sticker.png")]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        let clips = parity.cards.enumerated().map { index, card in
            TimelineClip(id: card.id, sourceAssetID: "sticker", sourceDuration: 1, timelineStart: 0, volume: 0,
                visualPlacement: VisualMediaPlacement(order: index, widthFraction: card.scale, xFraction: card.xFrac,
                    yFraction: card.yFrac, windowStart: 0, windowEnd: 1))
        }
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 1)]),
                TimelineTrack(id: "subtitled-overlays", kind: .overlay, clips: clips),
            ], assetManifest: manifest)
        try recipe.validate()
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let frame = try await generator.image(at: CMTime(seconds: parity.frameAtS, preferredTimescale: 600)).image
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        CIContext().render(CIImage(cgImage: frame), toBitmap: &pixels, rowBytes: width * 4,
                           bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: space)

        for (card, cloud) in zip(parity.cards, parity.cloud) {
            let cardWidth = card.scale * Double(width)
            let (cx, cy) = (card.xFrac * Double(width), card.yFrac * Double(height))
            let (left, top, right, bottom) = (cx - cardWidth / 2, cy - cardWidth / 2, cx + cardWidth / 2, cy + cardWidth / 2)
            let (x0, y0) = (max(0, Int(left) - 6), max(0, Int(top) - 6))
            let (x1, y1) = (min(width, Int(right) + 7), min(height, Int(bottom) + 7))
            var ink = 0, sums = [0, 0, 0], box = [x1, y1, -1, -1]
            for y in y0..<y1 {
                for x in x0..<x1 {
                    let offset = (y * width + x) * 4
                    let pixel = (0..<3).map { Int(pixels[offset + $0]) }
                    guard zip(pixel, parity.baseRgb).map({ abs($0 - $1) }).max()! > parity.inkDiff else { continue }
                    ink += 1
                    sums = zip(sums, pixel).map(+)
                    box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
                }
            }
            let clipped = (min(right, Double(width)) - max(left, 0)) * (min(bottom, Double(height)) - max(top, 0))
            let coverage = Double(ink) / clipped
            print("sticker-bleed-parity \(card.id): phone ink_box \(box) coverage \(coverage) mean \(sums.map { Double($0) / Double(max(ink, 1)) }); cloud \(cloud.inkBox) \(cloud.coverage) \(cloud.meanRgb)")
            for (phone, server) in zip(box, cloud.inkBox) { XCTAssertEqual(phone, server, accuracy: 2, "\(card.id) ink box") }
            XCTAssertEqual(coverage, cloud.coverage, accuracy: 0.03, "\(card.id): its alpha is kept, not a box")
            for (phone, server) in zip(sums.map { Double($0) / Double(max(ink, 1)) }, cloud.meanRgb) {
                XCTAssertEqual(phone, server, accuracy: 12, "\(card.id) colour")
            }
        }
    }
}
#endif
