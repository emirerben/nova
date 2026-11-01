import AVFoundation
import CoreImage
import ImageIO
import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1136: the DEVICE compiler's recipe for a tilted row, rendered through `KriaMediaEngine`'s real compositor
/// and read back as pixels. A red marker sits up and right of the photo's centre, so a clockwise turn of the
/// row's `rotation_deg` moves it right and down. Set `KRIA_TILT_RENDER_DIR` (as `TEST_RUNNER_KRIA_TILT_RENDER_DIR`
/// for `xcodebuild`) to keep each frame as a PNG.
@MainActor
final class ClipTiltRenderTests: XCTestCase {
    private let sourceWidth = 180, sourceHeight = 320
    private let sceneA = [40, 160, 90], sceneB = [30, 60, 200], sceneCentre = [240, 200, 30]
    private let backdropHex = "#E91E8C", backdropRGB = [233, 30, 140]

    private struct Frame {
        let bytes: [UInt8], width: Int
        var height: Int { bytes.count / 4 / width }
        func pixel(_ x: Int, _ y: Int) -> [Int] { [Int(bytes[(y * width + x) * 4 + 2]), Int(bytes[(y * width + x) * 4 + 1]), Int(bytes[(y * width + x) * 4])] }
        var corners: [[Int]] { [pixel(6, 6), pixel(width - 7, 6), pixel(6, height - 7), pixel(width - 7, height - 7)] }
        func redCentroid() -> (x: Double, y: Double, count: Int)? {
            var sx = 0.0, sy = 0.0, n = 0
            for y in stride(from: 0, to: height, by: 2) { for x in stride(from: 0, to: width, by: 2) {
                let p = pixel(x, y)
                if p[0] > 200 && p[1] < 70 && p[2] < 70 { sx += Double(x); sy += Double(y); n += 1 }
            } }
            return n == 0 ? nil : (sx / Double(n), sy / Double(n), n)
        }
    }

    private func writeScene(to directory: URL) throws -> URL {
        var pixels = [UInt8](repeating: 255, count: sourceWidth * sourceHeight * 4)
        for y in 0..<sourceHeight { for x in 0..<sourceWidth {
            let value: [Int]
            if (108..<132).contains(x) && (48..<72).contains(y) { value = [255, 0, 0] }
            else if (70..<110).contains(x) && (140..<180).contains(y) { value = sceneCentre }
            else { value = x < sourceWidth / 2 ? sceneA : sceneB }
            for channel in 0..<3 { pixels[(y * sourceWidth + x) * 4 + channel] = UInt8(value[channel]) }
        } }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: sourceWidth, height: sourceHeight, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: sourceWidth * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let url = directory.appendingPathComponent("scene.png")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return url
    }

    /// The recipe the device compiler writes for one clip whose row carries `tilt`.
    private func recipe(tilt: ClipTilt?, scene: URL) throws -> NativeEditorRenderProgram {
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: scene)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "scene.png", fingerprint: fingerprint, duration: nil, naturalSize: MediaSize(width: Double(sourceWidth), height: Double(sourceHeight))),
            url: scene)
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        var raw: [String: JSONValue] = [:]
        if let value = tilt?.rowValue { raw["tilt"] = value }
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3, raw: raw)])
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source])
    }

    private func frame(of program: NativeEditorRenderProgram) async throws -> Frame {
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: program.recipe, assetURLs: program.assetURLs)
        let item = preview.playerItem
        let reader = try AVAssetReader(asset: item.asset)
        let tracks = try await item.asset.loadTracks(withMediaType: .video)
        let output = AVAssetReaderVideoCompositionOutput(videoTracks: tracks, videoSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        output.videoComposition = item.videoComposition
        reader.add(output)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.5, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        XCTAssertTrue(reader.startReading(), "\(String(describing: reader.error))")
        let sample = try XCTUnwrap(output.copyNextSampleBuffer())
        let buffer = try XCTUnwrap(CMSampleBufferGetImageBuffer(sample))
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let (w, h, stride) = (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer))
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(buffer)).assumingMemoryBound(to: UInt8.self)
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        for row in 0..<h { bytes.withUnsafeMutableBufferPointer { $0.baseAddress!.advanced(by: row * w * 4).update(from: base.advanced(by: row * stride), count: w * 4) } }
        return Frame(bytes: bytes, width: w)
    }

    private func keep(_ frame: Frame, as name: String) throws {
        guard let directory = ProcessInfo.processInfo.environment["KRIA_TILT_RENDER_DIR"], !directory.isEmpty else { return }
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
        let provider = try XCTUnwrap(CGDataProvider(data: Data(frame.bytes) as CFData))
        let image = try XCTUnwrap(CGImage(width: frame.width, height: frame.height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.noneSkipFirst.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let url = URL(fileURLWithPath: directory).appendingPathComponent(name + ".png")
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    private func assertClose(_ actual: [Int], _ expected: [Int], within tolerance: Int, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        let delta = zip(actual, expected).map { abs($0 - $1) }.max() ?? 0
        XCTAssertLessThanOrEqual(delta, tolerance, "\(message): \(actual) vs \(expected)", file: file, line: line)
    }

    func testARowTiltRendersClockwiseOverTheColourAndBlurBackdrops() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let scene = try writeScene(to: directory)

        let upright = try await frame(of: recipe(tilt: nil, scene: scene))
        try keep(upright, as: "upright")
        let scale = Double(upright.width) / Double(sourceWidth)
        let (centreX, centreY) = (Double(upright.width) / 2, Double(upright.height) / 2)
        let start = try XCTUnwrap(upright.redCentroid())
        XCTAssertEqual(start.x, centreX + 30 * scale, accuracy: 8)
        XCTAssertEqual(start.y, centreY - 100 * scale, accuracy: 8)

        // The marker is (+30, -100) source pixels from the centre; a clockwise 30 degrees on screen carries it to (+76, -72).
        let colour = try await frame(of: recipe(tilt: ClipTilt(rotationDeg: 30, backdrop: .color(backdropHex)), scene: scene))
        try keep(colour, as: "tilt-30-colour")
        let moved = try XCTUnwrap(colour.redCentroid())
        XCTAssertEqual(moved.x, centreX + 75.98 * scale, accuracy: 18, "marker x after +30")
        XCTAssertEqual(moved.y, centreY - 71.6 * scale, accuracy: 18, "marker y after +30")
        XCTAssertGreaterThan(moved.x, start.x, "+ turns clockwise: the marker moves right")
        XCTAssertGreaterThan(moved.y, start.y, "+ turns clockwise: the marker moves down")
        for (index, corner) in colour.corners.enumerated() { assertClose(corner, backdropRGB, within: 2, "colour backdrop corner \(index)") }
        assertClose(colour.pixel(colour.width / 2, colour.height / 2), sceneCentre, within: 3, "the centre still shows the photo")

        let counter = try await frame(of: recipe(tilt: ClipTilt(rotationDeg: -30, backdrop: .blur), scene: scene))
        try keep(counter, as: "tilt-minus-30-blur")
        let back = try XCTUnwrap(counter.redCentroid())
        XCTAssertLessThan(back.x, start.x, "- turns counter-clockwise: the marker moves left")
        XCTAssertEqual(back.x, centreX - 24.02 * scale, accuracy: 25, "marker x after -30")
        XCTAssertEqual(back.y, centreY - 101.6 * scale, accuracy: 25, "marker y after -30")
        for (index, corner) in counter.corners.enumerated() {
            XCTAssertGreaterThan(corner.max() ?? 0, 25, "blur backdrop corner \(index) is not black: \(corner)")
            XCTAssertGreaterThan(zip(corner, sceneCentre).map { abs($0 - $1) }.max() ?? 0, 30, "blur backdrop corner \(index) is not the photo centre")
        }
    }

    func testAResetRowRendersByteIdenticalToAnUntouchedOne() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let scene = try writeScene(to: directory)
        let untouched = try recipe(tilt: nil, scene: scene)
        let reset = try recipe(tilt: ClipTilt(rotationDeg: 360, backdrop: .blank), scene: scene)
        XCTAssertEqual(untouched.recipe, reset.recipe)
        let blankBackdropUpright = try await frame(of: reset)
        let plain = try await frame(of: untouched)
        XCTAssertEqual(blankBackdropUpright.bytes, plain.bytes)
    }
}
