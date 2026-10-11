import Foundation
import XCTest
import AVFoundation
import CoreGraphics
import CoreImage
import ImageIO
import UIKit
import KriaMediaEngine
@testable import Kria

/// KRI-548 device evidence: the iPhone engine draws server-compiled phone
/// captions clear of the Kria watermark it composites above them.
///
/// `Tests/Fixtures/KRI548CaptionWatermark.json` holds the caption text layers
/// `phone_captions.compile_caption_layers` builds for three prod Kadıköy cues at
/// the default look (regenerated and drift-checked by the API's
/// `tests/pipeline/test_phone_caption_watermark_fixture.py`): `layers` as the
/// server compiles them now -- re-broken so no line runs under the mark -- and
/// `unbroken_layers` with that pass switched off, the layout prod shipped on
/// 2026-10-08 when "İlk durak..." lost its İ under the mark.
///
/// Each set is exported through the production exporter over a black still,
/// once with captions alone and once with the shipped watermark, and the
/// exported frame is read back inside the server's keep-out: the mark's opaque
/// rect plus a 12 px gap, checked here against where the engine actually
/// places the bundled mark. Caption fill is white; the mark ships at ~42% grey
/// (it peaks around 84 over black), so a pixel brighter than 200 in every
/// channel is caption ink whether or not the mark is drawn on top of it.
///
/// Frames are read once each cue has settled, which is the layout the server's
/// keep-out is computed for. The pop-in entrance overshoots to 1.15x for a
/// few frames and is not covered here.
@MainActor final class CaptionWatermarkClearanceTests: XCTestCase {
    /// Same near-white rule and codec allowance as `DeviceMontageRenderE2ETests`.
    private let captionInkCeiling = 5
    /// A cue's whole frame must carry at least this much caption ink, so a
    /// render that drew no text cannot pass as "clear of the mark".
    private let captionPresenceFloor = 2_000
    /// The negative control must put clearly more than codec noise in the corner.
    private let negativeControlFloor = 200
    /// Grey mark pixels inside its opaque rect on a branded frame.
    private let markPresenceFloor = 300
    /// After the 0.25 s pop-in has settled.
    private let restOffset = 0.5

    func testServerRebrokenKadikoyCaptionsExportClearOfTheWatermark() async throws {
        let fixture = try loadFixture()
        try assertKeepOutCoversTheEngineMark(fixture)
        let directory = try scratchDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        for watermark in [false, true] {
            let name = watermark ? "rebroken-branded" : "rebroken-captions-only"
            let frames = try await restFrames(fixture.layers, fixture: fixture, watermark: watermark,
                                              directory: directory, name: name)
            for (layer, frame) in zip(fixture.layers, frames) {
                let label = "\(name)/\(layer.id) \"\(lines(layer).joined(separator: " / "))\""
                XCTAssertGreaterThan(frame.captionInk(in: frame.bounds), captionPresenceFloor,
                                     "\(label): the caption must be on screen")
                let corner = frame.captionInk(in: fixture.keepOut)
                print("[kri-548] \(label): caption ink in keep-out = \(corner)")
                XCTAssertLessThanOrEqual(corner, captionInkCeiling,
                                         "\(label): caption ink under the watermark corner \(fixture.keepOut)")
                let mark = frame.markInk(in: fixture.markRect)
                if watermark {
                    XCTAssertGreaterThan(mark, markPresenceFloor, "\(label): the watermark must be drawn")
                } else {
                    XCTAssertLessThanOrEqual(mark, captionInkCeiling, "\(label): captions-only export carries no mark")
                }
                if corner > captionInkCeiling { attach(frame, named: label) }
            }
        }
    }

    /// Negative control: the same fixture's pre-KRI-548 layout, measured the
    /// same way, must ink the corner -- so the check above can fail.
    func testProdUnbrokenKadikoyCaptionsInkTheWatermarkCorner() async throws {
        let fixture = try loadFixture()
        try assertKeepOutCoversTheEngineMark(fixture)
        let directory = try scratchDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        for watermark in [false, true] {
            let name = watermark ? "unbroken-branded" : "unbroken-captions-only"
            let frames = try await restFrames(fixture.unbrokenLayers, fixture: fixture, watermark: watermark,
                                              directory: directory, name: name)
            for (layer, frame) in zip(fixture.unbrokenLayers, frames) {
                let label = "\(name)/\(layer.id) \"\(lines(layer).joined(separator: " / "))\""
                let corner = frame.captionInk(in: fixture.keepOut)
                print("[kri-548] \(label): caption ink in keep-out = \(corner)")
                XCTAssertGreaterThan(corner, negativeControlFloor,
                                     "\(label): prod's unbroken layout must reach the watermark corner")
                if watermark {
                    XCTAssertGreaterThan(frame.markInk(in: fixture.markRect), markPresenceFloor,
                                         "\(label): the watermark must be drawn")
                }
            }
        }
    }

    // MARK: - Fixture

    private struct Fixture {
        let canvas: Canvas
        /// Server keep-out, top-left frame pixels.
        let keepOut: CGRect
        /// The mark's opaque rect where the engine draws it, top-left frame pixels.
        let markRect: CGRect
        let manifest: RenderAssetManifest
        let layers: [PortableTextLayer]
        let unbrokenLayers: [PortableTextLayer]
    }

    private func loadFixture() throws -> Fixture {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Fixtures/KRI548CaptionWatermark.json")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        // The server's snake_case wire shape, decoded exactly as a device render request is.
        func decode<T: Decodable>(_ type: T.Type, _ key: String) throws -> T {
            try RecipeJSON.decoder().decode(T.self, from: JSONSerialization.data(withJSONObject: try XCTUnwrap(object[key])))
        }
        let canvas = try decode(Canvas.self, "canvas")
        let box = try XCTUnwrap(object["watermark_keepout"] as? [Double])
        XCTAssertEqual(box.count, 4)
        let gap = try XCTUnwrap(object["watermark_gap_px"] as? Double)
        let cues = try XCTUnwrap(object["cues"] as? [[String: Any]]).compactMap { $0["text"] as? String }
        let layers = try decode([PortableTextLayer].self, "layers")
        let unbroken = try decode([PortableTextLayer].self, "unbroken_layers")
        // The prod cues, every word kept in order by both layouts.
        XCTAssertEqual(cues.first, "İlk durak Moda'da, deniz kenarında küçücük bir yer.")
        XCTAssertEqual(cues.count, 3)
        for set in [layers, unbroken] {
            XCTAssertEqual(set.map { words($0) }, cues.map { $0.split(separator: " ").map(String.init) })
        }
        let keepOut = CGRect(x: box[0], y: box[1], width: box[2] - box[0], height: box[3] - box[1])
        return Fixture(canvas: canvas, keepOut: keepOut, markRect: keepOut.insetBy(dx: gap, dy: gap),
                       manifest: try decode(RenderAssetManifest.self, "asset_manifest"),
                       layers: layers, unbrokenLayers: unbroken)
    }

    /// The server's keep-out is a mirror of `KriaBranding`'s placement. Measure
    /// the bundled mark's opaque extent (alpha >= 8; the fainter fringe is its
    /// shadow) where the engine places its tile, and require the fixture's
    /// mark rect to be exactly that -- the corner checked is where the device
    /// really draws the mark.
    private func assertKeepOutCoversTheEngineMark(_ fixture: Fixture) throws {
        let url = try XCTUnwrap(KriaBranding.watermarkURL(.mist))
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let tile = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        let pixels = try RGBAFrame(tile)
        var left = pixels.width, top = pixels.height, right = -1, bottom = -1
        for y in 0..<pixels.height {
            for x in 0..<pixels.width where pixels.alpha(x, y) >= 8 {
                left = min(left, x); right = max(right, x); top = min(top, y); bottom = max(bottom, y)
            }
        }
        XCTAssertGreaterThanOrEqual(right, left, "the bundled mark has no opaque pixels")
        let canvas = CGSize(width: fixture.canvas.width, height: fixture.canvas.height)
        let placed = KriaBranding.watermarkTileRect(canvas: canvas, tileSize: CGSize(width: tile.width, height: tile.height))
        let scale = placed.width / CGFloat(tile.width)
        let mark = CGRect(x: placed.minX + CGFloat(left) * scale, y: placed.minY + CGFloat(top) * scale,
                          width: CGFloat(right - left + 1) * scale, height: CGFloat(bottom - top + 1) * scale)
        for (engine, server) in [(mark.minX, fixture.markRect.minX), (mark.minY, fixture.markRect.minY),
                                 (mark.maxX, fixture.markRect.maxX), (mark.maxY, fixture.markRect.maxY)] {
            XCTAssertEqual(engine, server, accuracy: 0.5, "engine mark \(mark) vs server keep-out \(fixture.keepOut)")
        }
    }

    // MARK: - Device export

    private func scratchDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri548-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// Exports `layers` over a black still through the production exporter and
    /// returns one decoded frame per layer, sampled at rest.
    private func restFrames(_ layers: [PortableTextLayer], fixture: Fixture, watermark: Bool,
                            directory: URL, name: String) async throws -> [RGBAFrame] {
        let rect = CGRect(x: 0, y: 0, width: fixture.canvas.width, height: fixture.canvas.height)
        let photo = directory.appendingPathComponent("black.png")
        if !FileManager.default.fileExists(atPath: photo.path) {
            try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: rect), to: photo, format: .RGBA8,
                                                   colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        }
        let photoFingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        var urls: [String: URL] = ["photo": photo]
        var assets = [MediaAsset(id: "photo", relativePath: "photo", fingerprint: photoFingerprint)]
        // Caption fonts install from the app bundle exactly as
        // `AuthorizedDeviceSourceResolver` installs a `.library` font: the
        // server's fingerprint must match the bundled bytes.
        let library = RenderLibraryCache(root: directory.appendingPathComponent("library", isDirectory: true))
        let bundledFonts = try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil))
        for font in fixture.manifest.assets {
            urls[font.id] = try await library.installBundledFont(font, directory: bundledFonts)
            assets.append(MediaAsset(id: font.id, relativePath: font.id,
                                     fingerprint: AssetFingerprint(hex: font.fingerprint.sha256, byteCount: font.fingerprint.byteCount)))
        }
        let duration = try XCTUnwrap(layers.map(\.end).max()) + 0.1
        let recipe = EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: fixture.canvas, assets: assets,
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "still", sourceAssetID: "photo", sourceDuration: duration)])],
            assetManifest: RenderAssetManifest(assets: [
                RenderAssetReference(id: "photo", fingerprint: try RenderFingerprint(photoFingerprint), source: .original(mediaID: "photo")),
            ] + fixture.manifest.assets),
            textLayers: layers)
        try recipe.validate()

        let output = directory.appendingPathComponent("\(name).mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(name)", isDirectory: true)),
            branding: KriaBranding.Options(watermark: watermark, outro: false)
        ).export(recipe: recipe, assetURLs: urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed, name)

        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var frames: [RGBAFrame] = []
        for layer in layers {
            XCTAssertGreaterThan(layer.end - layer.start, restOffset, layer.id)
            let image = try await generator.image(at: CMTime(seconds: layer.start + restOffset, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [fixture.canvas.width, fixture.canvas.height], name)
            frames.append(try RGBAFrame(image))
        }
        return frames
    }

    private func lines(_ layer: PortableTextLayer) -> [String] {
        Dictionary(grouping: layer.runs, by: \.baselineY).sorted { $0.key < $1.key }
            .map { $0.value.map(\.text).joined(separator: " ") }
    }

    private func words(_ layer: PortableTextLayer) -> [String] {
        lines(layer).joined(separator: " ").split(separator: " ").map(String.init)
    }

    private func attach(_ frame: RGBAFrame, named name: String) {
        guard let image = frame.image else { return }
        let attachment = XCTAttachment(image: UIImage(cgImage: image))
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

/// One decoded frame as sRGB RGBA8, rows from the top, as the cloud measures.
private struct RGBAFrame {
    let width: Int
    let height: Int
    let pixels: [UInt8]
    var bounds: CGRect { CGRect(x: 0, y: 0, width: width, height: height) }

    init(_ image: CGImage) throws {
        width = image.width; height = image.height
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let (w, h) = (width, height)
        let drawn = buffer.withUnsafeMutableBytes { bytes -> Bool in
            guard let context = CGContext(
                data: bytes.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
            else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        XCTAssertTrue(drawn, "could not decode a \(w)x\(h) frame")
        pixels = buffer
    }

    func alpha(_ x: Int, _ y: Int) -> UInt8 { pixels[(y * width + x) * 4 + 3] }

    /// Pixels brighter than 200 in every channel: white caption fill, with or
    /// without the ~42% grey mark composited over it.
    func captionInk(in rect: CGRect) -> Int {
        count(in: rect) { r, g, b in r > 200 && g > 200 && b > 200 }
    }

    /// The grey mark over black (peaks around 84); never white caption fill.
    func markInk(in rect: CGRect) -> Int {
        count(in: rect) { r, g, b in
            let peak = max(r, g, b)
            return peak > 40 && peak < 170
        }
    }

    var image: CGImage? {
        let data = Data(pixels) as CFData
        guard let provider = CGDataProvider(data: data) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
                       space: CGColorSpace(name: CGColorSpace.sRGB)!,
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }

    private func count(in rect: CGRect, _ matches: (UInt8, UInt8, UInt8) -> Bool) -> Int {
        let x0 = max(0, Int(rect.minX.rounded(.down))), x1 = min(width, Int(rect.maxX.rounded(.up)))
        let y0 = max(0, Int(rect.minY.rounded(.down))), y1 = min(height, Int(rect.maxY.rounded(.up)))
        var total = 0
        for y in y0..<max(y0, y1) {
            for x in x0..<max(x0, x1) {
                let i = (y * width + x) * 4
                if matches(pixels[i], pixels[i + 1], pixels[i + 2]) { total += 1 }
            }
        }
        return total
    }
}
