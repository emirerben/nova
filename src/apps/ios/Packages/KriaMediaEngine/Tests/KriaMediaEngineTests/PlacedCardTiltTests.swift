import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

/// KRI-1141: a phone Talking picture card is a clip with a `visualPlacement`; its `transform.rotationDegrees`
/// (clockwise on screen) turns the card about its centre. Rendered through the real compositor and read back as pixels.
final class PlacedCardTiltTests: XCTestCase {
    private let width = 216, height = 384

    private struct Frame {
        let bytes: [UInt8], width: Int, height: Int
        func pixel(_ x: Int, _ y: Int) -> [Int] { (0..<3).map { Int(bytes[(y * width + x) * 4 + $0]) } }
        func redCentroid() -> (x: Double, y: Double, count: Int)? {
            var sx = 0.0, sy = 0.0, n = 0
            for y in 0..<height { for x in 0..<width {
                let p = pixel(x, y)
                if p[0] > 200 && p[1] < 70 && p[2] < 70 { sx += Double(x); sy += Double(y); n += 1 }
            } }
            return n == 0 ? nil : (sx / Double(n), sy / Double(n), n)
        }
    }

    private func write(_ directory: URL, _ name: String, _ size: CGSize, marker: Bool) throws -> URL {
        let url = directory.appendingPathComponent(name + ".png")
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        var image = CIImage(color: CIColor(red: 0, green: marker ? 0.8 : 0.1, blue: 0, colorSpace: space)!).cropped(to: CGRect(origin: .zero, size: size))
        if marker {
            // A red square in the card's top-right corner (Core Image's y axis points up).
            let square = CGRect(x: size.width - 40, y: size.height - 40, width: 24, height: 24)
            image = CIImage(color: CIColor(red: 1, green: 0, blue: 0, colorSpace: space)!).cropped(to: square).composited(over: image)
        }
        try CIContext().writePNGRepresentation(of: image, to: url, format: .RGBA8, colorSpace: space)
        return url
    }

    @MainActor private func render(rotation: Double?, directory: URL) async throws -> Frame {
        let urls = ["base": try write(directory, "base", CGSize(width: 16, height: 16), marker: false),
                    "card": try write(directory, "card", CGSize(width: 160, height: 160), marker: true)]
        let assets = try urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0,
            fingerprint: try SHA256Fingerprinter().fingerprint(file: urls[$0]!)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!), source: .original(mediaID: asset.id))
        })
        let placement = VisualMediaPlacement(order: 1, widthFraction: 0.5, xFraction: 0.5, yFraction: 0.4, windowStart: 0, windowEnd: 2)
        let card = TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2, transform: MediaTransform(rotationDegrees: rotation ?? 0),
                                volume: 0, visualPlacement: placement)
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height),
            assets: assets, tracks: [
                TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 2)]),
                TimelineTrack(id: "overlays", kind: .overlay, clips: [card]),
            ], assetManifest: manifest)
        try recipe.validate()
        XCTAssertEqual(recipe.effectiveCapabilities.contains(.clipTilt), rotation != nil && rotation != 0)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let frame = try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        CIContext().render(CIImage(cgImage: frame, options: [.colorSpace: NSNull()]), toBitmap: &pixels, rowBytes: width * 4,
                           bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBA8, colorSpace: nil)
        return Frame(bytes: pixels, width: width, height: height)
    }

    @MainActor func testPositiveRotationTurnsAPlacedCardClockwiseAndNegativeTurnsItBack() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        // The card is 108 wide (0.5 of 216) centred at (108, 153.6); its marker sits up and to the right of the centre.
        let upright = try await render(rotation: nil, directory: directory)
        let start = try XCTUnwrap(upright.redCentroid())
        XCTAssertGreaterThan(start.x, 108); XCTAssertLessThan(start.y, 153.6)

        let clockwise = try await render(rotation: 30, directory: directory)
        let counter = try await render(rotation: -30, directory: directory)
        let right = try XCTUnwrap(clockwise.redCentroid()), left = try XCTUnwrap(counter.redCentroid())
        // Turning clockwise carries a top-right marker down and to the right of where it was; counter-clockwise
        // carries it up and to the left, towards the top edge of the card.
        XCTAssertGreaterThan(right.x, start.x + 5, "+30 moves the marker right"); XCTAssertGreaterThan(right.y, start.y + 5, "+30 moves the marker down")
        XCTAssertLessThan(left.x, start.x - 5, "-30 moves the marker left"); XCTAssertLessThan(left.y, start.y - 5, "-30 moves the marker up")
        // The card turns about its own centre: the marker keeps its distance from it.
        func distance(_ point: (x: Double, y: Double, count: Int)) -> Double { hypot(point.x - 108, point.y - 153.6) }
        XCTAssertEqual(distance(right), distance(start), accuracy: 2); XCTAssertEqual(distance(left), distance(start), accuracy: 2)
        // The card's own centre pixel is unmoved.
        XCTAssertEqual(clockwise.pixel(108, 154), upright.pixel(108, 154)); XCTAssertEqual(counter.pixel(108, 154), upright.pixel(108, 154))
    }

    @MainActor func testAnUntiltedPlacedCardIsByteIdenticalWhetherOrNotItCarriesAZeroRotation() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let omitted = try await render(rotation: nil, directory: directory)
        let zero = try await render(rotation: 0, directory: directory)
        XCTAssertEqual(omitted.bytes, zero.bytes)
    }

    func testAPlacementOnlyReadsTheClipRotationAndAnOutOfRangeAngleIsRefused() throws {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 10)
        let manifest = RenderAssetManifest(assets: [RenderAssetReference(id: "card",
            fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "card"))])
        func recipe(rotation: Double) -> EditRecipe {
            let placement = VisualMediaPlacement(order: 1, widthFraction: 0.5, windowStart: 0, windowEnd: 2)
            return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "card", relativePath: "card", fingerprint: fingerprint)],
                tracks: [TimelineTrack(id: "overlays", kind: .overlay, clips: [TimelineClip(id: "card", sourceAssetID: "card", sourceDuration: 2,
                    transform: MediaTransform(rotationDegrees: rotation), volume: 0, visualPlacement: placement)])], assetManifest: manifest)
        }
        XCTAssertNoThrow(try recipe(rotation: 360).validate())
        XCTAssertThrowsError(try recipe(rotation: 361).validate())
        let placed = recipe(rotation: 30).tracks[0].clips[0]
        XCTAssertEqual(placed.placementTiltDegrees, 30)
        var plain = placed; plain.visualPlacement = nil
        XCTAssertEqual(plain.placementTiltDegrees, 0, "an unplaced clip keeps turning through its transform, not the placement path")
    }
}
#endif
