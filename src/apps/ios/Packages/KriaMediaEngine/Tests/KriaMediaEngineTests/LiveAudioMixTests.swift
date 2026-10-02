#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

final class LiveAudioMixTests: XCTestCase {

    /// KRI-241: original audio is embedded in the video, not a separate SFX lane.
    /// A deterministic tone stands in for speech so level changes are measurable.
    /// Mix identity guards against resetting the live audio pipeline on visual edits;
    /// offline decoding alone cannot reproduce the reported iPhone playback failure.
    @MainActor func testVideoOriginalAudioSurvivesTextStyleGainAndTrimEdits() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let clip = try await mux(picture, try tone(directory, "voice-probe", 431), directory.appendingPathComponent("speaker.mov"))
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["speaker": clip, "font": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        })
        let canvas = Canvas(width: 96, height: 160)
        func caption(_ text: String, id: String = "caption", size: Double = 12) throws -> PortableTextLayer {
            try AuthoredTextLayout.compile(id: id, text: text, start: 0, end: 1.5,
                style: .init(fontAssetID: "font", size: size, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
                fontURL: font, canvas: canvas)
        }
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "speaker", sourceAssetID: "speaker", sourceStart: 0.5, sourceDuration: 2)])],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: manifest, textLayers: [try caption("Before")])
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let item = live.preview.playerItem, source = item.asset
        let baseline = rms(try await decode(source, item.audioMix), 0.2, 1.4)
        XCTAssertGreaterThan(baseline, 0.25)
        for mutation in ["caption", "title", "style", "original-gain", "clip-gain"] {
            let oldMix = try XCTUnwrap(item.audioMix)
            switch mutation {
            case "caption": recipe.textLayers[0] = try caption("After")
            case "title": recipe.textLayers.append(try caption("Title", id: "title"))
            case "style": recipe.textLayers[0] = try caption("After", size: 16)
            case "original-gain": recipe.audio.originalVolume = 0.5
            default: recipe.tracks[0].clips[0].volume = 0.5
            }
            try live.updateText(recipe: recipe)
            XCTAssertTrue(item === live.preview.playerItem, mutation)
            XCTAssertTrue(source === item.asset, mutation)
            if mutation == "original-gain" || mutation == "clip-gain" {
                XCTAssertFalse(oldMix === item.audioMix, mutation)
            } else {
                XCTAssertTrue(oldMix === item.audioMix, "\(mutation) must leave the running audio mix untouched")
            }
            let level = rms(try await decode(source, item.audioMix), 0.2, 1.4)
            XCTAssertEqual(level, baseline * recipe.audio.originalVolume * recipe.tracks[0].clips[0].volume, accuracy: 0.003, mutation)
        }
        // Saving the edited recipe must retain the same embedded source audio.
        let output = directory.appendingPathComponent("edited.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let exported = try await decode(AVURLAsset(url: output), nil)
        XCTAssertEqual(rms(exported, 0.2, 1.4), baseline * 0.25, accuracy: 0.003)
        // Trim is explicitly rejected by the fast path; rebuild from source instead.
        recipe.tracks[0].clips[0].sourceStart = 1
        recipe.tracks[0].clips[0].sourceDuration = 1.5
        XCTAssertThrowsError(try live.updateText(recipe: recipe))
        let rebuilt = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        XCTAssertFalse(item === rebuilt.preview.playerItem)
        let rebuiltLevel = rms(try await decode(rebuilt.preview.playerItem.asset, rebuilt.preview.playerItem.audioMix), 0.2, 1.4)
        XCTAssertEqual(rebuiltLevel, baseline * 0.25, accuracy: 0.003)
    }

    /// Music-backed previews keep their complete audio mix for visual-only
    /// edits, while audio mutations remain rejected until a full composition
    /// rebuild can safely recreate the music track.
    @MainActor func testMusicPreviewPreservesMixForTextEditsAndRejectsAudioMutations() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let music = try tone(directory, "music", 523)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["picture": picture, "music": music, "font": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        })
        let canvas = Canvas(width: 96, height: 160)
        func caption(_ text: String) throws -> PortableTextLayer {
            try AuthoredTextLayout.compile(id: "caption", text: text, start: 0, end: 1.5,
                style: .init(fontAssetID: "font", size: 12, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
                fontURL: font, canvas: canvas)
        }
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "picture", sourceAssetID: "picture", sourceDuration: 2)])],
            audio: AudioMixRecipe(musicAssetID: "music"), assetManifest: manifest, textLayers: [try caption("Before")])
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let item = live.preview.playerItem
        let source = item.asset
        let oldMix = try XCTUnwrap(item.audioMix)

        recipe.textLayers = [try caption("After")]
        try live.updateText(recipe: recipe)
        XCTAssertTrue(item === live.preview.playerItem)
        XCTAssertTrue(source === item.asset)
        XCTAssertTrue(oldMix === item.audioMix, "text-only music edits must preserve the running mix")

        recipe.audio.originalVolume = 0.5
        XCTAssertThrowsError(try live.updateText(recipe: recipe)) { error in
            XCTAssertEqual((error as? NativePreviewFeatureError)?.feature, "LivePreviewComposition-176")
        }
        XCTAssertTrue(oldMix === item.audioMix, "rejected music mutation must not publish a replacement mix")
    }

    /// The narrated E2E slows 2.95 seconds of footage into a six-second step.
    /// AVFoundation's time-pitch audio tail must not outlast the recipe video.
    @MainActor func testRetimedFootageAndNarrationExportEndsAtRecipeBoundary() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let picture = try await video(directory, "picture")
        let footage = try await mux(picture, try tone(directory, "footage", 410, sampleRate: 44_100), directory.appendingPathComponent("footage.mov"))
        let voice = try tone(directory, "voice", 1046, seconds: 10, sampleRate: 44_100)
        let urls = ["footage": footage, "voice": voice]
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160),
            assets: urls.keys.sorted().map { MediaAsset(id: $0, relativePath: $0) },
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "first", sourceAssetID: "footage", sourceDuration: 4),
                TimelineClip(id: "slow", sourceAssetID: "footage", sourceDuration: 2.95, timelineStart: 4, rate: 2.95 / 6),
            ]), TimelineTrack(id: "narration", kind: .audio, clips: [
                TimelineClip(id: "voice", sourceAssetID: "voice", sourceDuration: 10),
            ])], audio: AudioMixRecipe(originalVolume: 0.4, narrationAssetID: "voice"))
        let output = directory.appendingPathComponent("retimed.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, 10, accuracy: 1.0 / 48_000, "retiming must not append an audible audio tail")
        for track in try await asset.loadTracks(withMediaType: .audio) {
            let end = try await track.load(.timeRange).end.seconds
            XCTAssertLessThanOrEqual(end, 10 + 1.0 / 48_000)
        }
        let pcm = try await decode(asset, nil)
        XCTAssertGreaterThan(rms(pcm, 9.9, 9.97), 0.02, "keep the final valid audio before the boundary")
        XCTAssertLessThan(rms(pcm, 10.01, 10.1), 0.002, "no audio may play after the recipe ends")
    }

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
    /// RMS over both channels of the interleaved 48 kHz stereo decode.
    private func rms(_ pcm: [Float], _ from: Double, _ to: Double) -> Double {
        let start = Int((from * 48_000).rounded()) * 2, end = min(Int((to * 48_000).rounded()) * 2, pcm.count)
        guard start >= 0, end > start else { return 0 }
        var sum = 0.0
        for index in start..<end { sum += Double(pcm[index]) * Double(pcm[index]) }
        return (sum / Double(end - start)).squareRoot()
    }

    private func tone(_ directory: URL, _ name: String, _ frequency: Double, seconds: Int = 4, sampleRate: Int = 48_000) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: Double(sampleRate), channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * sampleRate))); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * frequency / Double(sampleRate))) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: sampleRate, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func video(_ directory: URL, _ name: String) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<120 { while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }; var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw); let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, []); memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, []); XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30))) }
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
        let duration = try await asset.load(.duration).seconds
        var pcm = [Float](repeating: 0, count: Int((duration * 48_000).rounded(.up)) * 2)
        while let sample = output.copyNextSampleBuffer() { let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2; let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block)); XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr); bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } } }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
