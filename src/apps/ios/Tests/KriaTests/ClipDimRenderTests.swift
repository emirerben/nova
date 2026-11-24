import AVFoundation
import CoreImage
import ImageIO
import KriaMediaEngine
import XCTest
@testable import Kria

/// The editor row's `dim`, compiled by the DEVICE compiler and exported to an MP4 by `KriaMediaEngine`'s production
/// exporter, then measured. A mid-grey clip sits under a white title: the clip's picture must darken by the set
/// strength and the title must stay at full brightness. Each export is compared with an undimmed export of the same
/// edit (not with source values: video-range encoding shifts absolutes). Set `KRIA_CLIP_DIM_RENDER_DIR` (as
/// `TEST_RUNNER_KRIA_CLIP_DIM_RENDER_DIR` for `xcodebuild`) to keep each measured frame as a PNG and a `report.json`.
@MainActor
final class ClipDimRenderTests: XCTestCase {
    private let width = 180, height = 320, grey: CGFloat = 0.5

    private struct Frame {
        let bytes: [UInt8], width: Int
        var height: Int { bytes.count / 4 / width }
        func luma(_ x: Int, _ y: Int) -> Double {
            let i = (y * width + x) * 4
            return 0.2126 * Double(bytes[i + 2]) + 0.7152 * Double(bytes[i + 1]) + 0.0722 * Double(bytes[i])
        }
    }

    private struct Fixture { let directory: URL; let video: URL; let source: ResolvedEditorSource }

    private func makeFixture() async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let video = directory.appendingPathComponent("grey.mp4")
        try await writeGreyVideo(video, seconds: 2)
        let duration = try await AVURLAsset(url: video).load(.duration).seconds
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "grey",
            asset: MediaAsset(id: "grey", relativePath: video.lastPathComponent, fingerprint: try SHA256Fingerprinter().fingerprint(file: video),
                duration: duration, naturalSize: MediaSize(width: Double(width), height: Double(height))), url: video)
        return Fixture(directory: directory, video: video, source: source)
    }

    private func writeGreyVideo(_ url: URL, seconds: Int) async throws {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<(seconds * 30) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer); CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(CGColor(srgbRed: grey, green: grey, blue: grey, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: width, height: height))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        writer.endSession(atSourceTime: CMTime(value: Int64(seconds * 30), timescale: 30))
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed)
    }

    /// The device compiler's recipe for one grey clip whose row carries `dim`, under a white title in the upper third,
    /// read through the editor session the way the preview and the export read it.
    private func program(dim: JSONValue?, fixture: Fixture) throws -> NativeEditorRenderProgram {
        var slot: [String: JSONValue] = ["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(1.5),
                                         "source_duration_s": fixture.source.asset.duration.map(JSONValue.number) ?? .null]
        if let dim { slot["dim"] = dim }
        let title: JSONValue = .object(["id": .string("title"), "text": .string("MMMM"), "start_s": .number(0), "end_s": .number(1.5),
                                        "role": .string("generative_intro"), "x_frac": .number(0.5), "y_frac": .number(0.3)])
        let saved = DraftSnapshot(draftID: "fixture", itemID: "fixture", variantKey: "guided_story", draftRevision: 0, snapshotHash: "", etag: "",
            baseJobID: nil, baseGenerationID: nil,
            snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": .array([.object(slot)]), "text_elements": .array([title])])])],
            canUndo: false, createdAt: .now)
        let session = NativeEditorSession(draft: saved.editorDraft(projectID: UUID()))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems, sources: [0: fixture.source])
    }

    private func export(_ program: NativeEditorRenderProgram, fixture: Fixture, name: String) async throws -> Frame {
        let output = fixture.directory.appendingPathComponent("\(name).mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: fixture.directory.appendingPathComponent("state-\(name)")), branding: .none)
            .export(recipe: program.recipe, assetURLs: program.assetURLs, outputURL: output)
        let asset = AVURLAsset(url: output)
        let reader = try AVAssetReader(asset: asset)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let trackOutput = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(trackOutput)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.75, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        XCTAssertTrue(reader.startReading(), "\(String(describing: reader.error))")
        let sample = try XCTUnwrap(trackOutput.copyNextSampleBuffer())
        let buffer = try XCTUnwrap(CMSampleBufferGetImageBuffer(sample))
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let (w, h, stride) = (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer))
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(buffer)).assumingMemoryBound(to: UInt8.self)
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        for row in 0..<h { bytes.withUnsafeMutableBufferPointer { $0.baseAddress!.advanced(by: row * w * 4).update(from: base.advanced(by: row * stride), count: w * 4) } }
        let frame = Frame(bytes: bytes, width: w)
        try keep(frame, as: name)
        return frame
    }

    private func keep(_ frame: Frame, as name: String) throws {
        guard let directory = Self.artifactDirectory else { return }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(frame.bytes) as CFData))
        let image = try XCTUnwrap(CGImage(width: frame.width, height: frame.height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: frame.width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGBitmapInfo.byteOrder32Little.rawValue | CGImageAlphaInfo.noneSkipFirst.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(directory.appendingPathComponent(name + ".png") as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    private static var artifactDirectory: URL? {
        guard let path = ProcessInfo.processInfo.environment["KRIA_CLIP_DIM_RENDER_DIR"], !path.isEmpty else { return nil }
        let url = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// The title's pixels in the undimmed export, and a picture band well below it.
    private func regions(of plain: Frame) -> (text: [(Int, Int)], picture: [(Int, Int)]) {
        var text: [(Int, Int)] = [], picture: [(Int, Int)] = []
        for y in 0..<plain.height { for x in 0..<plain.width {
            if plain.luma(x, y) > 235 { text.append((x, y)) }
            if y > plain.height * 3 / 4 && y < plain.height - 4 && x > 4 && x < plain.width - 4 { picture.append((x, y)) }
        } }
        return (text, picture)
    }

    private func mean(_ frame: Frame, _ points: [(Int, Int)]) -> Double {
        points.reduce(0) { $0 + frame.luma($1.0, $1.1) } / Double(max(points.count, 1))
    }

    func testADimmedClipExportsDarkerByItsStrengthUnderAFullBrightnessTitle() async throws {
        let fixture = try await makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        let undimmed = try program(dim: nil, fixture: fixture)
        XCTAssertNil(undimmed.recipe.tracks.first { $0.kind == .video }?.clips.first?.dim)
        XCTAssertFalse(undimmed.recipe.effectiveCapabilities.contains(.clipDim))
        let plain = try await export(undimmed, fixture: fixture, name: "dim-0")
        let (text, picture) = regions(of: plain)
        XCTAssertGreaterThan(text.count, 200, "the white title is in the undimmed export")
        let plainPicture = mean(plain, picture), plainText = mean(plain, text)
        XCTAssertEqual(plainPicture, 128, accuracy: 12, "the undimmed clip is mid-grey")

        var report: [String: Any] = ["undimmed": ["picture_luma": plainPicture, "text_luma": plainText, "text_pixels": text.count]]
        for (strength, tolerance) in [(0.5, 0.04), (1.0, 0.03)] {
            let dimmed = try program(dim: .number(strength), fixture: fixture)
            XCTAssertEqual(dimmed.recipe.tracks.first { $0.kind == .video }?.clips.first?.dim, strength)
            XCTAssertTrue(dimmed.recipe.effectiveCapabilities.contains(.clipDim))
            let frame = try await export(dimmed, fixture: fixture, name: "dim-\(Int(strength * 100))")
            let pictureRatio = mean(frame, picture) / plainPicture
            let textRatio = mean(frame, text) / plainText
            XCTAssertEqual(pictureRatio, 1 - strength, accuracy: tolerance, "dim \(strength): the picture keeps 1 - strength of its luma")
            XCTAssertGreaterThan(textRatio, 0.97, "dim \(strength): the title stays at full brightness")
            report["dim_\(Int(strength * 100))"] = ["picture_luma": mean(frame, picture), "picture_ratio": pictureRatio,
                                                     "text_luma": mean(frame, text), "text_ratio": textRatio]
        }
        if let directory = Self.artifactDirectory {
            let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("report.json"))
        }
    }

    func testANoDimRowCompilesByteIdenticalToAnUntouchedOne() async throws {
        let fixture = try await makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }
        let untouched = try program(dim: nil, fixture: fixture)
        XCTAssertEqual(try program(dim: .number(0), fixture: fixture).recipe, untouched.recipe)
        XCTAssertEqual(try program(dim: .null, fixture: fixture).recipe, untouched.recipe)
        let encoded = try XCTUnwrap(String(data: RecipeJSON.encoder().encode(untouched.recipe), encoding: .utf8))
        XCTAssertFalse(encoded.contains("\"dim\""))
        XCTAssertThrowsError(try program(dim: .number(40), fixture: fixture), "a percent is not a strength")
    }
}
