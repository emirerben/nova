import AVFoundation
import CoreImage
import ImageIO
import KriaMediaEngine
import XCTest
@testable import Kria

/// The editor row's `dim`, compiled by the DEVICE compiler and exported to an MP4 by `KriaMediaEngine`'s production
/// exporter, then measured. A mid-grey clip sits under every kind of layer a creator puts over it: a title, pinned
/// corner text, an informative heading, a song lyric line, a caption, a pasted sticker and a drawn stroke. The clip's
/// picture must darken by the set strength and every layer must stay at full brightness. Each export is compared with
/// an undimmed export of the same edit (not with source values: video-range encoding shifts absolutes). Set
/// `KRIA_CLIP_DIM_RENDER_DIR` (as `TEST_RUNNER_KRIA_CLIP_DIM_RENDER_DIR` for `xcodebuild`) to keep each measured frame
/// as a PNG and a `report.json`.
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

    private struct Fixture { let directory: URL; let video: URL; let source: ResolvedEditorSource; let sticker: ResolvedEditorSource }

    private func makeFixture() async throws -> Fixture {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let video = directory.appendingPathComponent("grey.mp4")
        try await writeGreyVideo(video, seconds: 2)
        let duration = try await AVURLAsset(url: video).load(.duration).seconds
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "grey",
            asset: MediaAsset(id: "grey", relativePath: video.lastPathComponent, fingerprint: try SHA256Fingerprinter().fingerprint(file: video),
                duration: duration, naturalSize: MediaSize(width: Double(width), height: Double(height))), url: video)
        let png = directory.appendingPathComponent("sticker.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: CIColor(red: 1, green: 0, blue: 1)).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 200)),
            to: png, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let sticker = ResolvedEditorSource(clipIndex: -1, mediaID: "sticker",
            asset: MediaAsset(id: "sticker", relativePath: png.lastPathComponent, fingerprint: try SHA256Fingerprinter().fingerprint(file: png),
                naturalSize: MediaSize(width: 200, height: 200)), url: png)
        return Fixture(directory: directory, video: video, source: source, sticker: sticker)
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

    /// A text row over the whole clip, drawn in Inter at `size` px of the 1080-wide canvas.
    private func textRow(_ id: String, _ text: String, x: Double, y: Double, size: Double, color: String,
                         extra: [String: JSONValue] = [:]) -> JSONValue {
        .object(["id": .string(id), "text": .string(text), "start_s": .number(0), "end_s": .number(1.5), "role": .string("generative_intro"),
                 "position": .string("custom"), "x_frac": .number(x), "y_frac": .number(y), "font_family": .string("Inter"),
                 "size_px": .number(size), "color": .string(color)].merging(extra) { _, new in new })
    }

    /// The device compiler's recipe for one grey clip whose row carries `dim`, under every layer `Layer.all` names,
    /// read through the editor session the way the preview and the export read it.
    private func program(dim: JSONValue?, fixture: Fixture) throws -> NativeEditorRenderProgram {
        var slot: [String: JSONValue] = ["slot_id": .string("slot"), "clip_index": .number(0), "in_s": .number(0), "duration_s": .number(1.5),
                                         "source_duration_s": fixture.source.asset.duration.map(JSONValue.number) ?? .null]
        if let dim { slot["dim"] = dim }
        let title: JSONValue = .object(["id": .string("title"), "text": .string("MMMM"), "start_s": .number(0), "end_s": .number(1.5),
                                        "role": .string("generative_intro"), "x_frac": .number(0.5), "y_frac": .number(0.3)])
        let pinned = textRow("guided-pinned-0", "HOME", x: 0.06, y: 0.08, size: 72, color: "#80FF80", extra: ["alignment": .string("left")])
        let heading = textRow("design-heading", "TRAM 28", x: 0.07, y: 0.45, size: 72, color: "#FFE7A0", extra: [
            "alignment": .string("left"), "text_case": .string("upper"), "letter_spacing": .number(0.14), "max_width_frac": .number(0.8),
            "shadow_enabled": .bool(true), "shadow_color": .string("#000000"), "shadow_opacity": .number(0.55),
            "source_params": .object(["text_design": .string("A1"), "text_stack_v1": .object([
                "stack_id": .string("pair-0"), "heading_level": .number(2), "corner": .string("bottom_left"), "bottom_frac": .number(0.5),
                "shaped": .bool(true), "authored_text": .string("Tram 28"), "canvas_width": .number(1080), "canvas_height": .number(1920),
                "gap_before_px": .number(0)])])])
        let lyric = textRow("song-lyric-1", "LA LA LA", x: 0.5, y: 0.6, size: 72, color: "#A0C8FF",
                            extra: ["source_params": .object(["origin": .string("song")])])
        let caption: JSONValue = .object(["id": .string("cue-1"), "start_s": .number(0), "end_s": .number(1.5), "text": .string("WORDS")])
        let sticker: JSONValue = .object(["id": .string("sticker"), "start_s": .number(0), "end_s": .number(1.5),
                                          "x_frac": .number(0.78), "y_frac": .number(0.74), "scale": .number(0.25)])
        let stroke: JSONValue = .object(["id": .string("stroke"), "start_s": .number(0), "end_s": .number(1.5), "canvas_aspect": .number(0.5625),
            "center_x": .number(0.3), "center_y": .number(0.72), "scale": .number(1), "rotation_deg": .number(0),
            "animation": .object(["entrance": .string("none"), "draw_duration_s": .number(1), "exit": .string("none")]),
            "strokes": .array([.object(["color": .string("#00E5FF"), "opacity": .number(1), "width_frac": .number(0.03), "texture": .string("pen"),
                                        "seed": .number(1), "points": .array([.array([.number(0.08), .number(0.72)]), .array([.number(0.5), .number(0.72)])])])])])
        let sections: [String: JSONValue] = ["timeline_slots": .array([.object(slot)]), "text_elements": .array([title, pinned, heading, lyric]),
                                             "caption_cues": .array([caption]), "media_overlays": .array([sticker]), "drawings": .array([stroke])]
        let saved = DraftSnapshot(draftID: "fixture", itemID: "fixture", variantKey: "guided_story", draftRevision: 0, snapshotHash: "", etag: "",
            baseJobID: nil, baseGenerationID: nil, snapshot: ["editor_payload": .object(["sections": .object(sections)])],
            canUndo: false, createdAt: .now)
        let session = NativeEditorSession(draft: saved.editorDraft(projectID: UUID()))
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: session.document, clips: session.timelineClips, items: session.timelineItems,
                                    sources: [0: fixture.source], mediaSources: ["overlay:sticker": fixture.sticker])
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

    /// One layer drawn over the clip: the band of the frame it alone occupies (fractions, top-left origin) and the
    /// colour of its opaque interior. Only pixels whose undimmed luma is within 6 of that interior count, so anti-aliased
    /// edges, outlines and shadows, which legitimately blend with the dimmed picture beside them, stay out.
    private struct Layer {
        let name: String, x: ClosedRange<Double>, y: ClosedRange<Double>, ink: (Double, Double, Double)
        var luma: Double { 0.2126 * ink.0 + 0.7152 * ink.1 + 0.0722 * ink.2 }

        static let all = [
            Layer(name: "pinned_corner_text", x: 0...0.6, y: 0.03...0.14, ink: (128, 255, 128)),
            Layer(name: "title", x: 0...1, y: 0.25...0.35, ink: (255, 255, 255)),
            Layer(name: "informative_heading", x: 0...1, y: 0.38...0.53, ink: (255, 231, 160)),
            Layer(name: "song_lyric", x: 0...1, y: 0.55...0.65, ink: (160, 200, 255)),
            Layer(name: "sticker", x: 0.6...1, y: 0.65...0.83, ink: (255, 0, 255)),
            Layer(name: "drawing", x: 0...0.58, y: 0.68...0.77, ink: (0, 229, 255)),
            Layer(name: "caption", x: 0...1, y: 0.84...0.97, ink: (255, 255, 255)),
        ]
        /// Text-free picture, right of the pinned text and above the title.
        static let picture = Layer(name: "picture", x: 0.62...0.95, y: 0.15...0.23, ink: (128, 128, 128))
    }

    private func points(of layer: Layer, in plain: Frame, interiorOnly: Bool = true) -> [(Int, Int)] {
        var points: [(Int, Int)] = []
        let xs = Int(layer.x.lowerBound * Double(plain.width))..<Int(layer.x.upperBound * Double(plain.width))
        let ys = Int(layer.y.lowerBound * Double(plain.height))..<Int(layer.y.upperBound * Double(plain.height))
        for y in ys { for x in xs where !interiorOnly || abs(plain.luma(x, y) - layer.luma) <= 6 { points.append((x, y)) } }
        return points
    }

    private func mean(_ frame: Frame, _ points: [(Int, Int)]) -> Double {
        points.reduce(0) { $0 + frame.luma($1.0, $1.1) } / Double(max(points.count, 1))
    }

    func testADimmedClipExportsDarkerByItsStrengthUnderEveryTextStickerAndDrawing() async throws {
        let fixture = try await makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.directory) }

        let undimmed = try program(dim: nil, fixture: fixture)
        XCTAssertNil(undimmed.recipe.tracks.first { $0.kind == .video }?.clips.first?.dim)
        XCTAssertFalse(undimmed.recipe.effectiveCapabilities.contains(.clipDim))
        XCTAssertEqual(undimmed.recipe.tracks.filter { $0.kind == .overlay }.flatMap(\.clips).count, 1, "the sticker is an overlay clip")
        XCTAssertEqual(undimmed.recipe.drawingLayers.count, 1, "the stroke is a drawing layer")
        XCTAssertGreaterThanOrEqual(undimmed.recipe.textLayers.count, 5, "title, pinned text, heading, lyric and caption are text layers")
        let plain = try await export(undimmed, fixture: fixture, name: "dim-0")
        let picture = points(of: Layer.picture, in: plain, interiorOnly: false)
        let plainPicture = mean(plain, picture)
        XCTAssertEqual(plainPicture, 128, accuracy: 12, "the undimmed clip is mid-grey")
        var regions: [String: [(Int, Int)]] = [:]
        var report: [String: [String: Any]] = ["picture": ["pixels": picture.count, "luma_0": plainPicture]]
        for layer in Layer.all {
            regions[layer.name] = points(of: layer, in: plain)
            XCTAssertGreaterThan(regions[layer.name]!.count, 150, "\(layer.name) is drawn in the undimmed export")
            report[layer.name] = ["pixels": regions[layer.name]!.count, "luma_0": mean(plain, regions[layer.name]!)]
        }

        for (strength, tolerance) in [(0.5, 0.04), (1.0, 0.03)] {
            let dimmed = try program(dim: .number(strength), fixture: fixture)
            XCTAssertEqual(dimmed.recipe.tracks.first { $0.kind == .video }?.clips.first?.dim, strength)
            XCTAssertTrue(dimmed.recipe.effectiveCapabilities.contains(.clipDim))
            let percent = Int(strength * 100)
            let frame = try await export(dimmed, fixture: fixture, name: "dim-\(percent)")
            let pictureRatio = mean(frame, picture) / plainPicture
            XCTAssertEqual(pictureRatio, 1 - strength, accuracy: tolerance, "dim \(strength): the picture keeps 1 - strength of its luma")
            report["picture"]?["luma_\(percent)"] = mean(frame, picture)
            report["picture"]?["ratio_\(percent)"] = pictureRatio
            for layer in Layer.all {
                let points = regions[layer.name]!
                let ratio = mean(frame, points) / mean(plain, points)
                XCTAssertGreaterThanOrEqual(ratio, 0.98, "dim \(strength): \(layer.name) stays at full brightness")
                report[layer.name]?["luma_\(percent)"] = mean(frame, points)
                report[layer.name]?["ratio_\(percent)"] = ratio
            }
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
