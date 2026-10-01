#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// KRI-136: a multi-clip Talking head on the phone is the Subtitled recipe
/// plus a `talking-head-cutaways` overlay track. The speaker clip is the only
/// main-track clip, so its own audio runs the whole way. Each cutaway is a
/// muted clip with a `VisualMediaPlacement` and no `widthFraction`, so it
/// cover-fills the frame. The recipe below mirrors
/// `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan(cutaways=...)`
/// and goes through the real exporter. A landscape cutaway must fill the
/// portrait frame, and the cutaway's own (louder) audio must never be heard
/// while the speaker's keeps playing underneath it.
final class TalkingHeadCutawayCompositionTests: XCTestCase {
    private let width = 96
    private let height = 160
    private let speakerAmplitude: Float = 0.2
    private let cutawayAmplitude: Float = 0.9

    @MainActor func testCutawayCoversTheFrameWhileTheSpeakerAudioKeepsPlaying() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let speakerDuration = 4.0
        let window = (start: 1.5, end: 3.0)
        let speaker = try await makeVideo(directory: directory, name: "speaker", size: (width, height),
                                          color: CGColor(red: 0, green: 0, blue: 1, alpha: 1),
                                          seconds: speakerDuration, amplitude: speakerAmplitude)
        // Landscape, like most b-roll: it must be cover-cropped, not letterboxed.
        let cutaway = try await makeVideo(directory: directory, name: "broll", size: (height, width),
                                          color: CGColor(red: 1, green: 0, blue: 0, alpha: 1),
                                          seconds: 3.0, amplitude: cutawayAmplitude)
        let urls = ["speaker": speaker, "broll": cutaway]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map {
            MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value))
        }
        let references = try assets.map {
            RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id))
        }
        let recipe = EditRecipe(
            schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: width, height: height), assets: assets,
            tracks: [
                TimelineTrack(id: "subtitled", kind: .video, clips: [
                    TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceDuration: speakerDuration),
                ]),
                TimelineTrack(id: "talking-head-cutaways", kind: .overlay, clips: [
                    TimelineClip(id: "cutaway-0", sourceAssetID: "broll", sourceDuration: window.end - window.start,
                                 timelineStart: window.start, volume: 0,
                                 visualPlacement: VisualMediaPlacement(order: 1, windowStart: window.start, windowEnd: window.end)),
                ]),
            ],
            audio: AudioMixRecipe(originalVolume: 1),
            assetManifest: RenderAssetManifest(assets: references)
        )
        try recipe.validate()
        XCTAssertEqual(TimelineMath.totalDuration(of: recipe), speakerDuration, accuracy: 0.001)

        let output = directory.appendingPathComponent("talking-head.mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")),
            branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)

        let asset = AVURLAsset(url: output)
        let exportedDuration = try await asset.load(.duration).seconds
        XCTAssertEqual(exportedDuration, speakerDuration, accuracy: 0.05,
                       "a cutaway must never extend the speaker's timeline")

        // Picture: speaker, then the cutaway edge to edge, then the speaker again.
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let corners = [(4, 4), (width - 5, 4), (width / 2, height / 2), (4, height - 5), (width - 5, height - 5)]
        for (seconds, expectRed) in [(0.75, false), (2.25, true), (3.5, false)] {
            let frame = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
            let rgb = try pixels(frame)
            for (x, y) in corners {
                let pixel = rgb(x, y)
                if expectRed {
                    XCTAssertGreaterThan(pixel[0], 180, "cutaway must fill the frame at \(seconds)s (\(x),\(y)): \(pixel)")
                    XCTAssertLessThan(pixel[2], 80, "speaker must be covered at \(seconds)s (\(x),\(y)): \(pixel)")
                } else {
                    XCTAssertGreaterThan(pixel[2], 180, "speaker must show at \(seconds)s (\(x),\(y)): \(pixel)")
                    XCTAssertLessThan(pixel[0], 80, "no cutaway at \(seconds)s (\(x),\(y)): \(pixel)")
                }
            }
        }

        // Sound: the speaker's tone runs through the cutaway; the cutaway's
        // louder tone is never heard.
        let samples = try await monoSamples(asset)
        let peak = samples.samples.map(abs).max() ?? 0
        XCTAssertLessThan(peak, (speakerAmplitude + cutawayAmplitude) / 2, "the cutaway's own audio leaked into the export")
        let during = rms(samples, from: window.start + 0.2, to: window.end - 0.2)
        let before = rms(samples, from: 0.3, to: window.start - 0.2)
        XCTAssertGreaterThan(during, 0.05, "the speaker's audio must keep playing under the cutaway")
        XCTAssertEqual(during, before, accuracy: 0.05, "the speaker's level must not change under the cutaway")
    }

    // MARK: - Helpers

    private func pixels(_ image: CGImage) throws -> (Int, Int) -> [Int] {
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                                              space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let width = width
        return { x, y in (0..<3).map { Int(bytes[(y * width + x) * 4 + $0]) } }
    }

    @MainActor private func monoSamples(_ asset: AVURLAsset) async throws -> (samples: [Float], rate: Double) {
        let tracks = try await asset.loadTracks(withMediaType: .audio)
        let track = try XCTUnwrap(tracks.first)
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsNonInterleaved: false, AVNumberOfChannelsKey: 1, AVSampleRateKey: 48_000,
        ])
        reader.add(output)
        XCTAssertTrue(reader.startReading())
        var samples: [Float] = []
        while let buffer = output.copyNextSampleBuffer() {
            guard let block = CMSampleBufferGetDataBuffer(buffer) else { continue }
            var bytes = Data(count: CMBlockBufferGetDataLength(block))
            _ = bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }
            bytes.withUnsafeBytes { samples.append(contentsOf: $0.bindMemory(to: Float32.self)) }
        }
        return (samples, 48_000)
    }

    private func rms(_ audio: (samples: [Float], rate: Double), from start: Double, to end: Double) -> Float {
        let lower = max(0, Int(start * audio.rate)), upper = min(audio.samples.count, Int(end * audio.rate))
        guard upper > lower else { return 0 }
        let slice = audio.samples[lower..<upper]
        return (slice.reduce(0) { $0 + $1 * $1 } / Float(slice.count)).squareRoot()
    }

    /// A solid-colour H.264 clip with a constant-amplitude AAC tone, so a
    /// test can tell whose picture and whose sound reached the export. Video
    /// and audio are written separately and muxed (the
    /// `SourceAudioOverlapParityTests` pattern): one writer with both inputs
    /// stalls waiting for interleaved samples.
    @MainActor private func makeVideo(directory: URL, name: String, size: (Int, Int), color: CGColor,
                                      seconds: Double, amplitude: Float, fps: Int32 = 30) async throws -> URL {
        let (w, h) = size
        let videoURL = directory.appendingPathComponent("\(name)-video.mp4")
        let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: w, AVVideoHeightKey: h])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h,
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
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: w, height: h, bitsPerComponent: 8,
                                                  bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                                  bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color)
            context.fill(CGRect(x: 0, y: 0, width: w, height: h))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)

        let audioURL = directory.appendingPathComponent("\(name)-audio.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
        pcm.frameLength = pcm.frameCapacity
        for index in 0..<Int(pcm.frameLength) {
            pcm.floatChannelData![0][index] = amplitude * Float(sin(Double(index) * 2 * .pi * 440 / 48_000))
        }
        do {
            let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000,
                                                                        AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000],
                                       commonFormat: .pcmFormatFloat32, interleaved: false)
            try file.write(from: pcm)
        }

        let composition = AVMutableComposition()
        for (url, kind) in [(videoURL, AVMediaType.video), (audioURL, AVMediaType.audio)] {
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
}
#endif
