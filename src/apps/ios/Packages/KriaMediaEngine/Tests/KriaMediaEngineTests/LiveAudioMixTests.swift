#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

final class LiveAudioMixTests: XCTestCase {
    @MainActor func testGainEditPreservesPlayerAndMatchesExportedTrimAndPlacement() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let audio = directory.appendingPathComponent("tone.caf")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48_000))
        buffer.frameLength = 48_000
        for index in 0..<48_000 { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * 440 / 48_000)) }
        do {
            let file = try AVAudioFile(forWriting: audio, settings: format.settings)
            try file.write(from: buffer)
        }
        let urls = ["photo": photo, "tone": audio]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) }
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "photo", sourceDuration: 2)]),
                     TimelineTrack(id: "sfx", kind: .audio, clips: [TimelineClip(id: "effect", sourceAssetID: "tone", sourceStart: 0.25, sourceDuration: 0.5, timelineStart: 0.5)])],
            assetManifest: RenderAssetManifest(assets: references))
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let item = preview.preview.playerItem, source = preview.preview.playerItem.asset
        recipe.tracks[1].clips[0].volume = 0.25
        try preview.updateText(recipe: recipe)
        XCTAssertTrue(item === preview.preview.playerItem)
        XCTAssertTrue(source === item.asset)
        let output = directory.appendingPathComponent("mix.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        var levels: [Double] = []
        for (asset, mix) in [(source, item.audioMix), (AVURLAsset(url: output) as AVAsset, nil)] {
            let reader = try AVAssetReader(asset: asset)
            let tracks = try await asset.loadTracks(withMediaType: .audio)
            let decoded = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [
                AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2,
                AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false])
            decoded.audioMix = mix
            reader.add(decoded)
            XCTAssertTrue(reader.startReading())
            var energy = [Double](repeating: 0, count: 3), counts = [Int](repeating: 0, count: 3)
            while let sample = decoded.copyNextSampleBuffer() {
                let timestamp = CMSampleBufferGetPresentationTimeStamp(sample).seconds
                let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample))
                var bytes = Data(count: CMBlockBufferGetDataLength(block))
                let status = bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }
                XCTAssertEqual(status, noErr)
                bytes.withUnsafeBytes { raw in
                    for (index, value) in raw.bindMemory(to: Float.self).enumerated() {
                        let time = timestamp + Double(index / 2) / 48_000
                        let window = time < 0.4 ? 0 : (time >= 0.6 && time < 0.9 ? 1 : (time >= 1.1 ? 2 : -1))
                        if window >= 0 { energy[window] += Double(value * value); counts[window] += 1 }
                    }
                }
            }
            XCTAssertEqual(reader.status, .completed)
            XCTAssertGreaterThan(counts[1], 10_000)
            for index in [0, 2] where counts[index] > 0 { XCTAssertLessThan(sqrt(energy[index] / Double(counts[index])), 0.002) }
            let rms = sqrt(energy[1] / Double(counts[1]))
            XCTAssertGreaterThan(rms, 0.07)
            XCTAssertLessThan(rms, 0.10)
            levels.append(rms)
        }
        XCTAssertEqual(levels[0], levels[1], accuracy: 0.003)
    }

    /// KRI-241. A caption edit on a Talking timeline (two cuts of one clip,
    /// the voice is the clip's own audio) must leave the live item's mix alone:
    /// re-assigning even an identical mix to the item AVPlayer is rendering is
    /// what the silent iPhone preview traced to. A volume edit still rebuilds it.
    @MainActor func testCaptionEditKeepsTheVoiceMixAndAVolumeEditRebuildsIt() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let talk = try await mux(try await blackVideo(directory), try tone(directory), directory.appendingPathComponent("talk.mov"))
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["talk": talk, "font": font]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        func caption(_ text: String) -> PortableTextLayer {
            let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1), black = TextInk(red: 0, green: 0, blue: 0, alpha: 1)
            return PortableTextLayer(id: "caption-cue", start: 0.25, end: 1.75, anchorX: 48, anchorY: 80, rotationDegrees: 0,
                runs: [PositionedTextRun(text: text, fontAssetID: "font", fontSize: 18, x: 10, baselineY: 80, letterSpacing: 0,
                    shaped: true, fill: white, stroke: black, strokeWidth: 0)])
        }
        // A speech-cleanup cut: the second clip skips 0.5 s of the same source.
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [
                TimelineClip(id: "a", sourceAssetID: "talk", sourceStart: 0, sourceDuration: 1, timelineStart: 0),
                TimelineClip(id: "b", sourceAssetID: "talk", sourceStart: 1.5, sourceDuration: 1, timelineStart: 1)])],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: RenderAssetManifest(assets: references), textLayers: [caption("Hello")])
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let item = preview.preview.playerItem
        let original = try XCTUnwrap(item.audioMix)
        let before = try await decode(item.asset, original)
        let voice = [rms(before, 0.4, 0.6), rms(before, 1.4, 1.6)]
        XCTAssertGreaterThan(voice.min()!, 0.25, "the voice plays on both sides of the cut")

        recipe.textLayers = [caption("Hello there")]
        try preview.updateText(recipe: recipe)
        XCTAssertTrue(item === preview.preview.playerItem)
        XCTAssertTrue(item.audioMix === original, "a caption edit must not replace the live item's audio mix")
        let edited = try await decode(item.asset, item.audioMix)
        XCTAssertEqual(rms(edited, 0.4, 0.6), voice[0], accuracy: 0.01)
        XCTAssertEqual(rms(edited, 1.4, 1.6), voice[1], accuracy: 0.01)

        recipe.audio.originalVolume = 0.5
        try preview.updateText(recipe: recipe)
        XCTAssertTrue(item === preview.preview.playerItem)
        XCTAssertFalse(item.audioMix === original, "a volume edit rebuilds the mix")
        let quieter = try await decode(item.asset, item.audioMix)
        XCTAssertEqual(rms(quieter, 0.4, 0.6), voice[0] / 2, accuracy: 0.01)
        XCTAssertEqual(rms(quieter, 1.4, 1.6), voice[1] / 2, accuracy: 0.01)
    }

    /// RMS over both channels of the interleaved 48 kHz stereo decode.
    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    private func tone(_ directory: URL) throws -> URL {
        let url = directory.appendingPathComponent("voice.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 3 * 48_000)); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * 431 / 48_000)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func blackVideo(_ directory: URL) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<90 { while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }; var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw); let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, []); memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, []); XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30))) }
        input.markAsFinished(); await writer.finishWriting(); XCTAssertEqual(writer.status, .completed); return url
    }

    @MainActor private func mux(_ video: URL, _ audio: URL, _ output: URL) async throws -> URL {
        let composition = AVMutableComposition()
        for (url, kind) in [(video, AVMediaType.video), (audio, AVMediaType.audio)] {
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: kind)
            let track = try XCTUnwrap(tracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await track.load(.timeRange), of: track, at: .zero)
        }
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough)); exporter.outputURL = output; exporter.outputFileType = .mov; await exporter.export(); XCTAssertEqual(exporter.status, .completed, "\(String(describing: exporter.error))"); return output
    }

    @MainActor private func decode(_ asset: AVAsset, _ mix: AVAudioMix?) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false]); output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: 3 * 48_000 * 2)
        while let sample = output.copyNextSampleBuffer() { let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2; let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block)); XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr); bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } } }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
