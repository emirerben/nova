import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import ImageIO

/// KRI-1134: a clip's `transform.rotationDegrees` (clockwise on screen) and its `backdrop`, rendered through the
/// real compositor (AVAssetImageGenerator over the preview composition, and the exporter) and read back as pixels.
final class ClipTiltCompositionTests: XCTestCase {
    private let width = 180, height = 320
    private let sceneA = [40, 160, 90], sceneB = [30, 60, 200], sceneCentre = [240, 200, 30]
    private let backdropHex = "#E91E8C", backdropRGB = [233, 30, 140]

    private func makeDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func writePNG(to url: URL, width: Int, height: Int, color: (Int, Int) -> [UInt8]) throws {
        var pixels = [UInt8](repeating: 255, count: width * height * 4)
        for y in 0..<height { for x in 0..<width {
            let value = color(x, y)
            for channel in 0..<3 { pixels[(y * width + x) * 4 + channel] = value[channel] }
        } }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    /// Left half green, right half blue, a yellow block at the centre and a red 24 px marker at (120, 60) from the top left.
    private func writeScene(in directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("scene.png")
        try writePNG(to: url, width: width, height: height) { x, y in
            if (108..<132).contains(x) && (48..<72).contains(y) { return [255, 0, 0] }
            if (70..<110).contains(x) && (140..<180).contains(y) { return self.sceneCentre.map { UInt8($0) } }
            return (x < self.width / 2 ? self.sceneA : self.sceneB).map { UInt8($0) }
        }
        return url
    }

    private func recipe(backdrop: ClipBackdrop?, rotation: Double) -> EditRecipe {
        EditRecipe(canvas: Canvas(width: width, height: height), assets: [MediaAsset(id: "scene", relativePath: "scene")], tracks: [
            TimelineTrack(id: "photos", kind: .video, clips: [
                TimelineClip(id: "tilted", sourceAssetID: "scene", sourceDuration: 1,
                             transform: MediaTransform(rotationDegrees: rotation), backdrop: backdrop)
            ])
        ])
    }

    private struct Frame {
        let bytes: [UInt8], width: Int
        var height: Int { bytes.count / 4 / width }
        func pixel(_ x: Int, _ y: Int) -> [Int] { [Int(bytes[(y * width + x) * 4 + 2]), Int(bytes[(y * width + x) * 4 + 1]), Int(bytes[(y * width + x) * 4])] }
        var corners: [[Int]] { [pixel(3, 3), pixel(width - 4, 3), pixel(3, height - 4), pixel(width - 4, height - 4)] }
        func redCentroid() -> (x: Double, y: Double, count: Int)? {
            var sx = 0.0, sy = 0.0, n = 0
            for y in 0..<height { for x in 0..<width {
                let p = pixel(x, y)
                if p[0] > 200 && p[1] < 70 && p[2] < 70 { sx += Double(x); sy += Double(y); n += 1 }
            } }
            return n == 0 ? nil : (sx / Double(n), sy / Double(n), n)
        }
    }

    /// The compositor's own BGRA output at 0.5 s, read without any colour conversion.
    private func readFrame(from output: AVAssetReaderOutput, reader: AVAssetReader) throws -> Frame {
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

    private let bgra: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]

    @MainActor private func frame(of item: AVPlayerItem) async throws -> Frame {
        let reader = try AVAssetReader(asset: item.asset)
        let tracks = try await item.asset.loadTracks(withMediaType: .video)
        let output = AVAssetReaderVideoCompositionOutput(videoTracks: tracks, videoSettings: bgra)
        output.videoComposition = item.videoComposition
        reader.add(output)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.5, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        return try readFrame(from: output, reader: reader)
    }

    @MainActor private func previewFrame(_ recipe: EditRecipe, urls: [String: URL]) async throws -> Frame {
        try await frame(of: AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls).playerItem)
    }

    private func assertClose(_ actual: [Int], _ expected: [Int], within tolerance: Int, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
        let delta = zip(actual, expected).map { abs($0 - $1) }.max() ?? 0
        XCTAssertLessThanOrEqual(delta, tolerance, "\(message): \(actual) vs \(expected)", file: file, line: line)
    }

    @MainActor func testPositiveRotationTurnsClockwiseAndColorBackdropFillsTheCorners() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]

        let upright = try await previewFrame(recipe(backdrop: nil, rotation: 0), urls: urls)
        let start = try XCTUnwrap(upright.redCentroid())
        XCTAssertEqual(start.x, 119.5, accuracy: 1); XCTAssertEqual(start.y, 59.5, accuracy: 1)

        // The marker sits (+30, -100) from the centre; clockwise 30 degrees on screen carries it to (166, 88).
        let clockwise = try await previewFrame(recipe(backdrop: .color(backdropHex), rotation: 30), urls: urls)
        let moved = try XCTUnwrap(clockwise.redCentroid())
        XCTAssertEqual(moved.x, 166, accuracy: 3, "marker x after +30"); XCTAssertEqual(moved.y, 88, accuracy: 3, "marker y after +30")
        XCTAssertGreaterThan(moved.x, start.x); XCTAssertGreaterThan(moved.y, start.y)
        for (index, corner) in clockwise.corners.enumerated() { assertClose(corner, backdropRGB, within: 1, "colour backdrop corner \(index)") }
        assertClose(clockwise.pixel(width / 2, height / 2), sceneCentre, within: 2, "the centre still shows the source")

        let counter = try await previewFrame(recipe(backdrop: .color(backdropHex), rotation: -30), urls: urls)
        let back = try XCTUnwrap(counter.redCentroid())
        XCTAssertEqual(back.x, 66, accuracy: 3, "marker x after -30"); XCTAssertEqual(back.y, 58, accuracy: 3, "marker y after -30")
    }

    @MainActor func testBackdropStaysBehindTheClip() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let covered = try await previewFrame(recipe(backdrop: .color(backdropHex), rotation: 0), urls: urls)
        let plain = try await previewFrame(recipe(backdrop: nil, rotation: 0), urls: urls)
        XCTAssertEqual(covered.bytes, plain.bytes, "a backdrop under a clip that covers the canvas must not show")
    }

    @MainActor func testBlurBackdropIsNeitherBlackNorTheClipCentre() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let frame = try await previewFrame(recipe(backdrop: .blur, rotation: 30), urls: urls)
        let centre = frame.pixel(width / 2, height / 2)
        assertClose(centre, sceneCentre, within: 2, "the centre still shows the source")
        for (index, corner) in frame.corners.enumerated() {
            XCTAssertGreaterThan(corner.reduce(0, +), 120, "blur corner \(index) is black or empty: \(corner)")
            XCTAssertGreaterThan(zip(corner, centre).map { abs($0 - $1) }.max() ?? 0, 40, "blur corner \(index) equals the clip centre: \(corner)")
        }
        assertClose(frame.corners[0], sceneA, within: 45, "top-left blur is the left half of the cover")
        let marker = try XCTUnwrap(frame.redCentroid())
        XCTAssertEqual(marker.x, 166, accuracy: 3); XCTAssertEqual(marker.y, 88, accuracy: 3)
    }

    @MainActor func testBlankBackdropAndNoBackdropRenderIdentically() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        for rotation in [0.0, 30.0] {
            let none = try await previewFrame(recipe(backdrop: nil, rotation: rotation), urls: urls)
            let blank = try await previewFrame(recipe(backdrop: .blank, rotation: rotation), urls: urls)
            XCTAssertEqual(none.bytes, blank.bytes, "rotation \(rotation)")
        }
        let tilted = try await previewFrame(recipe(backdrop: nil, rotation: 30), urls: urls)
        for corner in tilted.corners { assertClose(corner, [0, 0, 0], within: 3, "uncovered corners stay black") }
    }

    @MainActor func testColorBackdropSurvivesTheExporter() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let tilted = recipe(backdrop: .color(backdropHex), rotation: 30)
        let output = directory.appendingPathComponent("tilt.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: tilted, assetURLs: urls, outputURL: output)
        let exported = AVURLAsset(url: output)
        let reader = try AVAssetReader(asset: exported)
        let tracks = try await exported.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let trackOutput = AVAssetReaderTrackOutput(track: track, outputSettings: bgra)
        reader.add(trackOutput)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.5, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        let frame = try readFrame(from: trackOutput, reader: reader)
        XCTAssertEqual([frame.width, frame.height], [width, height])
        for (index, corner) in frame.corners.enumerated() { assertClose(corner, backdropRGB, within: 8, "exported corner \(index)") }
        let marker = try XCTUnwrap(frame.redCentroid())
        XCTAssertEqual(marker.x, 166, accuracy: 3); XCTAssertEqual(marker.y, 88, accuracy: 3)
    }

    @MainActor func testBackdropAndTiltOnAVideoClip() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let video = try await SolidVideoFixture.make(directory: directory, name: "solid", color: CGColor(red: 0.2, green: 0.6, blue: 0.3, alpha: 1), seconds: 1.2)
        let urls = ["solid": video]
        func tilted(_ backdrop: ClipBackdrop?) -> EditRecipe {
            EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "solid", relativePath: "solid")], tracks: [
                TimelineTrack(id: "video", kind: .video, clips: [
                    TimelineClip(id: "clip", sourceAssetID: "solid", sourceDuration: 1, transform: MediaTransform(rotationDegrees: 30), backdrop: backdrop)
                ])
            ])
        }
        let colour = try await previewFrame(tilted(.color(backdropHex)), urls: urls)
        for corner in colour.corners { assertClose(corner, backdropRGB, within: 1, "video colour backdrop") }
        let bare = try await previewFrame(tilted(nil), urls: urls)
        for corner in bare.corners { assertClose(corner, [0, 0, 0], within: 3, "no backdrop leaves the corners black") }
        let clipColour = bare.pixel(48, 80)
        XCTAssertGreaterThan(clipColour[1], 100)
        let blur = try await previewFrame(tilted(.blur), urls: urls)
        for corner in blur.corners { assertClose(corner, clipColour, within: 4, "video blur backdrop is the blurred clip") }
    }

    private func makeCard(in directory: URL) throws -> (urls: [String: URL], recipe: (Double) -> EditRecipe) {
        let photo = directory.appendingPathComponent("photo.png")
        try writePNG(to: photo, width: 1200, height: 900) { _, _ in [0, 255, 0] }
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        let manifest = RenderAssetManifest(assets: [RenderAssetReference(id: "visual-photo", fingerprint: try RenderFingerprint(fingerprint),
                                                                         source: .visual(visualID: "photo", generation: "1"))])
        return (["visual-photo": photo], { rotation in
            EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: self.width, height: self.height),
                assets: [MediaAsset(id: "visual-photo", relativePath: "visual-photo", fingerprint: fingerprint)],
                tracks: [TimelineTrack(id: "video", kind: .video, clips: [
                    TimelineClip(id: "card", sourceAssetID: "visual-photo", sourceDuration: 1, transform: MediaTransform(rotationDegrees: rotation), stillLayout: .supportingCard)
                ])], assetManifest: manifest)
        })
    }

    @MainActor func testTiltedSupportingCardTurnsTheCardAndKeepsTheCoverUpright() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let (urls, card) = try makeCard(in: directory)
        let tilted = card(30)
        try tilted.validate()
        XCTAssertTrue(tilted.effectiveCapabilities.contains(.clipTilt))
        let flat = try await previewFrame(card(0), urls: urls)
        let turned = try await previewFrame(tilted, urls: urls)
        // The card is x 16..<163, y 44..<274. Clockwise 30 degrees carries its top-left corner to about (83, 22).
        assertClose(flat.pixel(86, 31), [0, 255, 0], within: 60, "above the upright card is the cover")
        assertClose(turned.pixel(86, 31), [0, 0, 0], within: 12, "the turned card's top edge reaches here")
        assertClose(turned.pixel(19, 47), [0, 255, 0], within: 60, "the upright card's corner is cover once the card turns")
        for corner in turned.corners { assertClose(corner, [0, 255, 0], within: 60, "the cover stays upright and fills the canvas") }
        assertClose(turned.pixel(width / 2, height / 2), [0, 255, 0], within: 6, "the photo is still at the centre")
    }

    @MainActor func testLivePreviewRefusesToRetiltASupportingCardInPlace() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let (urls, card) = try makeCard(in: directory)
        let live = try await LivePreviewComposition(recipe: card(0), assetURLs: urls)
        XCTAssertThrowsError(try live.updateText(recipe: card(10)))
    }

    @MainActor func testLivePreviewTakesATiltAndBackdropEditWithoutARebuild() async throws {
        let directory = try makeDirectory(); defer { try? FileManager.default.removeItem(at: directory) }
        let urls = ["scene": try writeScene(in: directory)]
        let live = try await LivePreviewComposition(recipe: recipe(backdrop: nil, rotation: 0), assetURLs: urls)
        try live.updateText(recipe: recipe(backdrop: .color(backdropHex), rotation: 30))
        let updated = try await frame(of: live.preview.playerItem)
        let rebuilt = try await previewFrame(recipe(backdrop: .color(backdropHex), rotation: 30), urls: urls)
        XCTAssertEqual(updated.bytes, rebuilt.bytes)
    }

    func testBackdropWireShapeIsStrict() throws {
        let encoder = RecipeJSON.encoder(); encoder.outputFormatting = [.sortedKeys]
        func json(_ value: ClipBackdrop) throws -> String { String(decoding: try encoder.encode(value), as: UTF8.self) }
        XCTAssertEqual(try json(.blank), ##"{"kind":"blank"}"##)
        XCTAssertEqual(try json(.blur), ##"{"kind":"blur"}"##)
        XCTAssertEqual(try json(.color("#E91E8C")), ##"{"color":"#E91E8C","kind":"color"}"##)
        for value in [ClipBackdrop.blank, .blur, .color("#e91e8c")] {
            XCTAssertEqual(try RecipeJSON.decoder().decode(ClipBackdrop.self, from: encoder.encode(value)), value)
        }
        for bad in [##"{"kind":"color"}"##, ##"{"kind":"blur","color":"#000000"}"##, ##"{"kind":"blank","color":"#000000"}"##,
                    ##"{"kind":"color","color":"red"}"##, ##"{"kind":"color","color":"#12345"}"##, ##"{"kind":"color","color":"#+12345"}"##,
                    ##"{"kind":"media","asset_id":"x"}"##, ##"{"kind":"blur","extra":1}"##, ##"{"color":"#000000"}"##] {
            XCTAssertThrowsError(try RecipeJSON.decoder().decode(ClipBackdrop.self, from: Data(bad.utf8)), bad)
        }
    }

    func testRecipeWithoutTiltEncodesNoBackdropKeyAndNeedsNoTiltCapability() throws {
        let plain = recipe(backdrop: nil, rotation: 0)
        XCTAssertFalse(String(decoding: try RecipeJSON.encode(plain), as: UTF8.self).contains("backdrop"))
        XCTAssertFalse(plain.effectiveCapabilities.contains(.clipTilt))
        XCTAssertTrue(recipe(backdrop: nil, rotation: 0.5).effectiveCapabilities.contains(.clipTilt))
        XCTAssertTrue(recipe(backdrop: .blank, rotation: 0).effectiveCapabilities.contains(.clipTilt))
        XCTAssertTrue(recipe(backdrop: .color("#000000"), rotation: 0).effectiveCapabilities.contains(.clipTilt))
        let tilted = recipe(backdrop: .blur, rotation: -45)
        XCTAssertEqual(try RecipeJSON.decode(RecipeJSON.encode(tilted)), tilted)
        XCTAssertEqual(CapabilityNegotiator(provider: DefaultRendererCapabilities()).decide(for: tilted).missingCapabilities, [.clipTilt])
    }

    func testBackdropIsRefusedOffTheMainVideoTrack() throws {
        var overlay = recipe(backdrop: nil, rotation: 0)
        overlay.tracks = [
            TimelineTrack(id: "base", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "scene", sourceDuration: 1)]),
            TimelineTrack(id: "overlay", kind: .overlay, clips: [TimelineClip(id: "card", sourceAssetID: "scene", sourceDuration: 1, backdrop: .blur)])
        ]
        XCTAssertThrowsError(try overlay.validate())
        XCTAssertNoThrow(try recipe(backdrop: .blur, rotation: 20).validate())
    }
}
#endif
