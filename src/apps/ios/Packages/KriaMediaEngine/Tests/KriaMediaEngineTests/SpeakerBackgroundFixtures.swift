#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
@testable import KriaMediaEngine

// Shared by the speaker-background tests (KRI-1116): stand-ins for Vision, synthetic media, a recipe
// builder and pixel sampling. Rows named in `docs/reviews/speaker-background-failure-modes.md`.

/// Answers a scripted confidence map per call (or throws), standing in for Vision.
final class ScriptedSegmenter: NativePersonSegmenting {
    var script: (Int) throws -> SubjectConfidenceMap?
    private(set) var calls = 0
    init(_ script: @escaping (Int) throws -> SubjectConfidenceMap?) { self.script = script }
    convenience init(map: SubjectConfidenceMap?) { self.init { _ in map } }
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        defer { calls += 1 }
        return try script(calls)
    }
}

/// A Vision stand-in that reads the frame it is handed: confidence 1 where the pixel is "person green"
/// (green high, red low), 0 elsewhere. It sees only what the compositor gives the matte, so a test can
/// tell which layer was segmented and whether its transform was applied first.
final class GreenPersonSegmenter: NativePersonSegmenting {
    private(set) var calls = 0
    private let context = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        calls += 1
        let width = Int(canvas.width), height = Int(canvas.height)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        context.render(frame.cropped(to: canvas).transformed(by: CGAffineTransform(translationX: -canvas.minX, y: -canvas.minY)),
                       toBitmap: &pixels, rowBytes: width * 4, bounds: CGRect(x: 0, y: 0, width: width, height: height),
                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let values = (0..<width * height).map { index -> Float in
            pixels[index * 4 + 1] > 150 && pixels[index * 4] < 100 ? 1 : 0
        }
        return SubjectConfidenceMap(width: width, height: height, values: values)
    }
}

/// Person-confidence map, row 0 = top: `value` inside the rectangle, `background` elsewhere.
func speakerConfidence(_ width: Int, _ height: Int, columns: Range<Int>, rows: Range<Int>? = nil,
                       value: Float = 1, background: Float = 0.02) -> SubjectConfidenceMap {
    var values = [Float](repeating: background, count: width * height)
    for y in rows ?? 0..<height { for x in columns { values[y * width + x] = value } }
    return SubjectConfidenceMap(width: width, height: height, values: values)
}

enum SpeakerColor {
    static let room = CGColor(red: 1, green: 0, blue: 1, alpha: 1)
    static let person = CGColor(red: 0, green: 1, blue: 0, alpha: 1)
    static let background = CGColor(red: 0, green: 0, blue: 1, alpha: 1)
}

/// A temporary directory removed when the test ends.
func speakerTestDirectory(_ test: XCTestCase) throws -> URL {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    test.addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
    return directory
}

/// An H.264 clip whose frame `index` is painted by `paint` (Core Graphics, origin bottom-left), with an
/// optional constant-amplitude AAC tone muxed in (the `TalkingHeadCutawayCompositionTests` pattern).
@MainActor func writeSpeakerTestVideo(directory: URL, name: String, size: (Int, Int), seconds: Double, fps: Int32 = 30,
                                      toneAmplitude: Float? = nil, paint: (CGContext, Int) -> Void) async throws -> URL {
    let (width, height) = size
    let videoURL = directory.appendingPathComponent("\(name)-video.mp4")
    let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mp4)
    let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
    let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
    ])
    writer.add(input)
    XCTAssertTrue(writer.startWriting())
    writer.startSession(atSourceTime: .zero)
    for index in 0..<Int((seconds * Double(fps)).rounded(.up)) {
        while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
        var buffer: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
        let pixel = try XCTUnwrap(buffer)
        CVPixelBufferLockBaseAddress(pixel, [])
        let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8,
                                              bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                              bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
        paint(context, index)
        CVPixelBufferUnlockBaseAddress(pixel, [])
        XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
    }
    input.markAsFinished()
    await writer.finishWriting()
    XCTAssertEqual(writer.status, .completed)
    guard let toneAmplitude else { return videoURL }

    let audioURL = directory.appendingPathComponent("\(name)-audio.m4a")
    let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
    let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
    pcm.frameLength = pcm.frameCapacity
    for index in 0..<Int(pcm.frameLength) {
        pcm.floatChannelData![0][index] = toneAmplitude * Float(sin(Double(index) * 2 * .pi * 440 / 48_000))
    }
    do {
        let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
                                                                    AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000],
                                   commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: pcm)
    }
    let composition = AVMutableComposition()
    for (url, kind) in [(videoURL, AVMediaType.video), (audioURL, AVMediaType.audio)] {
        // The asset must outlive the insert: a track only weakly references its asset.
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: kind)
        let track = try XCTUnwrap(tracks.first)
        let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
        try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
    }
    let output = directory.appendingPathComponent("\(name).mov")
    let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
    exporter.outputURL = output
    exporter.outputFileType = .mov
    await exporter.export()
    XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))")
    return output
}

/// A speaker clip: a magenta room with a green person standing in the middle, from 30% to 70% of the
/// width and from the bottom edge up to 75% of the height.
@MainActor func writeSpeakerClip(directory: URL, size: (Int, Int), seconds: Double, toneAmplitude: Float? = nil) async throws -> URL {
    try await writeSpeakerTestVideo(directory: directory, name: "speaker", size: size, seconds: seconds, toneAmplitude: toneAmplitude) { context, _ in
        let (width, height) = (Double(size.0), Double(size.1))
        context.setFillColor(SpeakerColor.room)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.setFillColor(SpeakerColor.person)
        context.fill(CGRect(x: width * 0.3, y: 0, width: width * 0.4, height: height * 0.75))
    }
}

/// A speaker clip with nobody in it (the room alone), so the matte answers "background only" and every
/// pixel of the frame is the background.
@MainActor func writeEmptyRoomClip(directory: URL, size: (Int, Int), seconds: Double) async throws -> URL {
    try await writeSpeakerTestVideo(directory: directory, name: "room", size: size, seconds: seconds) { context, _ in
        context.setFillColor(SpeakerColor.room)
        context.fill(CGRect(x: 0, y: 0, width: size.0, height: size.1))
    }
}

/// Largest absolute sample of the file's first audio track, mixed to mono float.
@MainActor func peakAudio(_ url: URL) async throws -> Float {
    let asset = AVURLAsset(url: url)
    let tracks = try await asset.loadTracks(withMediaType: .audio)
    let track = try XCTUnwrap(tracks.first)
    let reader = try AVAssetReader(asset: asset)
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
        AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32,
        AVLinearPCMIsNonInterleaved: false, AVNumberOfChannelsKey: 1, AVSampleRateKey: 48_000,
    ])
    reader.add(output)
    XCTAssertTrue(reader.startReading())
    var peak: Float = 0
    while let buffer = output.copyNextSampleBuffer() {
        guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
        var bytes = Data(count: CMBlockBufferGetDataLength(block))
        _ = bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }
        bytes.withUnsafeBytes { peak = max(peak, $0.bindMemory(to: Float32.self).map(abs).max() ?? 0) }
    }
    return peak
}

/// A still written with an EXIF orientation (1 = upright). JPEG keeps the orientation tag everywhere.
func writeSpeakerTestImage(directory: URL, name: String, size: (Int, Int), orientation: Int = 1, paint: (CGContext) -> Void) throws -> URL {
    let (width, height) = size
    let context = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                          space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
    paint(context)
    let image = try XCTUnwrap(context.makeImage())
    let url = directory.appendingPathComponent("\(name).jpg")
    let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
    CGImageDestinationAddImage(destination, image, [kCGImagePropertyOrientation: orientation, kCGImageDestinationLossyCompressionQuality: 1.0] as CFDictionary)
    XCTAssertTrue(CGImageDestinationFinalize(destination))
    return url
}

func solidSpeakerTestImage(directory: URL, name: String = "background", color: CGColor = SpeakerColor.background, size: (Int, Int) = (64, 64)) throws -> URL {
    try writeSpeakerTestImage(directory: directory, name: name, size: size) { context in
        context.setFillColor(color)
        context.fill(CGRect(x: 0, y: 0, width: size.0, height: size.1))
    }
}

struct SpeakerTestBackground {
    var url: URL
    var kind: VisualMediaKind = .image
    var sourceStart: Double = 0
    var loop = true
}

/// The Talk-to-camera recipe shape `phone_subtitled_plan.py` compiles: the speaker clip on the
/// `subtitled` track (plus an optional ending clip after it), optional extra tracks, and the background
/// as a Visuals-pool asset. `extraAssets` are camera originals; `visuals` are Visuals-pool media.
func speakerRecipe(canvas: Canvas, speaker: URL, duration: Double, transform: MediaTransform = .identity,
                   background: SpeakerTestBackground?, endingClip: TimelineClip? = nil, extraTracks: [TimelineTrack] = [],
                   extraAssets: [String: URL] = [:], visuals: [String: (url: URL, kind: VisualMediaKind)] = [:],
                   audio: AudioMixRecipe = .default) throws -> (recipe: EditRecipe, urls: [String: URL]) {
    var visuals = visuals
    if let background { visuals["visual-background"] = (background.url, background.kind) }
    let urls = extraAssets.merging(["speaker": speaker]) { $1 }.merging(visuals.mapValues(\.url)) { $1 }
    let assets = try urls.sorted(by: { $0.key < $1.key }).map {
        MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
    }
    let references = try assets.map { asset in
        RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
            source: visuals[asset.id].map { .visual(visualID: asset.id, generation: "1", mediaKind: $0.kind) } ?? .original(mediaID: asset.id))
    }
    let recipe = EditRecipe(
        schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: assets,
        tracks: [TimelineTrack(id: "subtitled", kind: .video, clips: [
            TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceDuration: duration, transform: transform),
        ] + [endingClip].compactMap { $0 })] + extraTracks,
        audio: audio, assetManifest: RenderAssetManifest(assets: references),
        speakerBackground: background.map {
            SpeakerBackgroundLayer(assetID: "visual-background", mediaKind: $0.kind, sourceStart: $0.sourceStart, loop: $0.loop)
        })
    try recipe.validate()
    return (recipe, urls)
}

/// RGB at (x, y), y counted from the TOP of the frame.
struct SpeakerTestFrame {
    let width: Int
    let height: Int
    private let bytes: [UInt8]
    init(_ image: CGImage) throws {
        width = image.width; height = image.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                              space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        self.bytes = bytes
    }
    func rgb(_ x: Int, _ y: Int) -> [Int] { (0..<3).map { Int(bytes[(y * width + x) * 4 + $0]) } }
    /// Fractions of the frame, (0, 0) = top-left.
    func rgb(fx: Double, fy: Double) -> [Int] { rgb(min(width - 1, Int(fx * Double(width))), min(height - 1, Int(fy * Double(height)))) }
}

func isColor(_ pixel: [Int], _ color: CGColor, tolerance: Int = 60) -> Bool {
    let expected = (color.components ?? []).prefix(3).map { Int(($0 * 255).rounded()) }
    return zip(pixel, expected).allSatisfy { abs($0 - $1) <= tolerance }
}

/// The frame at `seconds` from the preview composition (the AVPlayer path), drawn by a fresh compositor.
@MainActor func previewFrame(_ recipe: EditRecipe, urls: [String: URL], at seconds: Double) async throws -> SpeakerTestFrame {
    let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
    let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
    generator.videoComposition = preview.playerItem.videoComposition
    generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
    return try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image)
}

/// Exports `recipe` through the real exporter and returns its file.
@MainActor func exportSpeakerRecipe(_ recipe: EditRecipe, urls: [String: URL], directory: URL, name: String = "export") async throws -> URL {
    let output = directory.appendingPathComponent("\(name).mp4")
    let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(name)")),
                                                         branding: .none)
        .export(recipe: recipe, assetURLs: urls, outputURL: output)
    XCTAssertEqual(checkpoint.status, .completed)
    return output
}

@MainActor func fileFrame(_ url: URL, at seconds: Double) async throws -> SpeakerTestFrame {
    let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
    generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
    return try SpeakerTestFrame(try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image)
}

/// Swaps the speaker matte's segmenter for the duration of one test.
func useSpeakerSegmenter(_ test: XCTestCase, _ segmenter: NativePersonSegmenting?) {
    let previous = SpeakerMatte.segmenterFactory
    SpeakerMatte.segmenterFactory = { segmenter }
    test.addTeardownBlock { SpeakerMatte.segmenterFactory = previous }
}
/// Swaps the config new speaker mattes use for the duration of one test.
func useSpeakerConfig(_ test: XCTestCase, _ config: SpeakerMatteConfig) {
    let previous = SpeakerMatte.defaultConfig
    SpeakerMatte.defaultConfig = config
    test.addTeardownBlock { SpeakerMatte.defaultConfig = previous }
}

/// The shipping config and the alternatives a device check may switch to (KRI-1116): every test that
/// renders through a config runs each of them.
let speakerMatteConfigsUnderTest: [(name: String, config: SpeakerMatteConfig)] = {
    var accurate = SpeakerMatteConfig.shipping
    accurate.quality = .accurate
    accurate.maskRate = nil
    var accurateAt15 = accurate
    accurateAt15.maskRate = 15
    return [("shipping", .shipping), ("accurate every frame", accurate), ("accurate at 15 Hz", accurateAt15)]
}()
#endif
