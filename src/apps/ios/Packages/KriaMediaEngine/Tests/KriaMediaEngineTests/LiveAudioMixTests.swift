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
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
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
}
#endif
