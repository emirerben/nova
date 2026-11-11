import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage

final class SplitScreenMediaParityTests: XCTestCase {
    @MainActor func testOriginalPhotoSupportingCardRendersAndOriginalVideoIsRejected() async throws {
        let directory = try artifacts()
        let photo = directory.appendingPathComponent("original-photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 64, height: 128)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let clips = [[TimelineClip(id: "card", sourceAssetID: "original", sourceDuration: 2, stillLayout: .supportingCard,
            splitScreen: .init(sectionID: "s", panelIndex: 0))]]
        var recipe = try makeRecipe(["original": photo], clips, end: 2)
        recipe.splitScreenSections[0].backgroundColor = "#FFFFFF"
        try recipe.validate()
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["original": photo])
        let output = try await export(recipe, ["original": photo], directory, "original-photo-card")
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(frames, time: 0.5, x: 48, y: 135, expected: [255, 0, 0])
            try await assertPixel(frames, time: 0.5, x: 15, y: 135, expected: [0, 0, 0])
            try await assertPixel(frames, time: 0.5, x: 48, y: 80, expected: [255, 255, 255])
        }
        let video = try await SolidVideoFixture.make(directory: directory, name: "not-photo", color: color(1, 0, 0), seconds: 2)
        let invalid = try makeRecipe(["original": video], clips, end: 2)
        do {
            _ = try await AVPlayerPreviewComposer().makePreview(recipe: invalid, assetURLs: ["original": video])
            XCTFail("A video must never be interpreted as a supporting-card photo")
        } catch is NativePreviewFeatureError {}
    }

    @MainActor func testThreeVideosKeepMarkedCropsClocksHoldAndIndependentAudio() async throws {
        let directory = try artifacts()
        let frequencies = [431.0, 719, 997]
        var urls: [String: URL] = [:]
        for index in 0..<3 {
            urls["v\(index)"] = try await SolidVideoFixture.make(directory: directory, name: "marked-\(index)",
                color: CGColor(gray: 0, alpha: 1), seconds: 4, audioFrequency: frequencies[index], framePaint: { context, frame in
                    let colors: [CGColor] = [frame < 30 ? self.color(1, 0, 0) : self.color(0, 1, 0),
                        frame < 30 ? self.color(0, 0, 1) : self.color(1, 1, 0), self.color(1, 0, 1), self.color(0, 1, 1)]
                    for (i, color) in colors.enumerated() {
                        context.setFillColor(color)
                        context.fill(CGRect(x: i % 2 * 48, y: i < 2 ? 80 : 0, width: 48, height: 80))
                    }
                })
        }
        let topLeft = NormalizedSourceRect(x: 0, y: 0, width: 0.5, height: 0.5)
        let clips = [
            TimelineClip(id: "top", sourceAssetID: "v0", sourceDuration: 2, volume: 0.5, sourceCrop: topLeft, splitScreen: .init(sectionID: "s", panelIndex: 0)),
            TimelineClip(id: "middle", sourceAssetID: "v1", sourceStart: 1, sourceDuration: 2, volume: 0.8,
                sourceCrop: .init(x: 0.5, y: 0, width: 0.5, height: 0.5), splitScreen: .init(sectionID: "s", panelIndex: 1)),
            TimelineClip(id: "bottom", sourceAssetID: "v2", sourceStart: 0.5, sourceDuration: 1, rate: 2, volume: 0,
                holdDuration: 1.5, sourceCrop: topLeft, splitScreen: .init(sectionID: "s", panelIndex: 2))
        ]
        let recipe = try makeRecipe(urls, clips.map { [$0] }, end: 2)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "marked-crops-audio")
        let live = generator(preview)
        for frames in [live, AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            try await assertPixel(frames, time: 0.25, y: 135, expected: [255, 0, 0])
            try await assertPixel(frames, time: 1.25, y: 135, expected: [0, 255, 0])
            try await assertPixel(frames, time: 0.25, y: 80, expected: [255, 255, 0])
            try await assertPixel(frames, time: 1.25, y: 25, expected: [0, 255, 0])
        }
        let sourcePCM = try await decode(AVURLAsset(url: urls["v0"]!), nil)
        let secondPCM = try await decode(AVURLAsset(url: urls["v1"]!), nil)
        for (asset, mix) in [(preview.playerItem.asset, preview.playerItem.audioMix), (AVURLAsset(url: output) as AVAsset, nil)] {
            let pcm = try await decode(asset, mix)
            for time in [0.25, 1.25] {
                for channel in 0..<2 {
                    XCTAssertEqual(try amplitude(pcm, time, 431, channel: channel), try amplitude(sourcePCM, 0.25, 431, channel: channel) * 0.2, accuracy: 0.012)
                    XCTAssertEqual(try amplitude(pcm, time, 719, channel: channel), try amplitude(secondPCM, 1.25, 719, channel: channel) * 0.32, accuracy: 0.012)
                    XCTAssertLessThan(try amplitude(pcm, time, 997, channel: channel), 0.006, "Muted panel leaks audio")
                }
            }
        }
    }

    @MainActor func testPanelTransitionBackdropAndGradedHoldStayLocal() async throws {
        let directory = try artifacts()
        var urls: [String: URL] = [:]
        for (id, color) in [("red", color(1, 0, 0)), ("blue", color(0, 0, 1)), ("green", color(0, 1, 0)), ("gray", color(0.45, 0.35, 0.25))] {
            urls[id] = try await SolidVideoFixture.make(directory: directory, name: id, color: color, seconds: 3)
        }
        let tracks = [
            [TimelineClip(id: "a", sourceAssetID: "red", sourceDuration: 2, splitScreen: .init(sectionID: "s", panelIndex: 0)),
             TimelineClip(id: "b", sourceAssetID: "blue", sourceDuration: 2, timelineStart: 1,
                transition: .init(kind: .crossfade, duration: 1), splitScreen: .init(sectionID: "s", panelIndex: 0))],
            [TimelineClip(id: "c", sourceAssetID: "green", sourceDuration: 3, transform: .init(scale: 0.2, rotationDegrees: 25),
                backdrop: .color("#FFFF00"), splitScreen: .init(sectionID: "s", panelIndex: 1))],
            [TimelineClip(id: "d", sourceAssetID: "gray", sourceDuration: 1, look: .goldenHour, holdDuration: 2,
                splitScreen: .init(sectionID: "s", panelIndex: 2))]
        ]
        let recipe = try makeRecipe(urls, tracks, end: 3)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let output = try await export(recipe, urls, directory, "transition-backdrop-look-hold")
        var reference = try makeRecipe(urls, [[TimelineClip(id: "ref", sourceAssetID: "gray", sourceDuration: 1, look: .goldenHour, holdDuration: 2)]], end: 3)
        reference.splitScreenSections = []
        let referencePreview = try await AVPlayerPreviewComposer().makePreview(recipe: reference, assetURLs: urls)
        let expected = try await pixel(generator(referencePreview), time: 2.5, x: 48, y: 80)
        for frames in [generator(preview), AVAssetImageGenerator(asset: AVURLAsset(url: output))] {
            let blend = try await pixel(frames, time: 1.5, x: 48, y: 135)
            XCTAssertTrue(blend[0] > 100 && blend[2] > 100 && blend[1] < 70, "Same-panel crossfade missing: \(blend)")
            try await assertPixel(frames, time: 1.5, x: 5, y: 80, expected: [255, 255, 0])
            try await assertPixel(frames, time: 1.5, x: 48, y: 80, expected: [0, 255, 0])
            let held = try await pixel(frames, time: 2.5, x: 48, y: 25)
            for channel in 0..<3 { XCTAssertEqual(Int(held[channel]), Int(expected[channel]), accuracy: 15, "Graded held panel must match ordinary rendering") }
        }
    }

    func artifacts() throws -> URL {
        let root = ProcessInfo.processInfo.environment["KRIA_SPLIT_SCREEN_ARTIFACTS"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.temporaryDirectory
        let directory = root.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        print("Split-screen expanded artifacts: \(directory.path)")
        return directory
    }
    func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: 1) }
    func makeRecipe(_ urls: [String: URL], _ clips: [[TimelineClip]], end: Double) throws -> EditRecipe {
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: clips.enumerated().map { TimelineTrack(id: "panel-\($0.offset)", kind: .video, clips: $0.element) },
            audio: .init(originalVolume: 0.4), assetManifest: try RenderAssetManifest(assets: assets.map {
                RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id))
            }), splitScreenSections: [.init(id: "s", start: 0, end: end, panelWeights: [1, 1, 1])])
    }
    @MainActor func export(_ recipe: EditRecipe, _ urls: [String: URL], _ directory: URL, _ name: String) async throws -> URL {
        let output = directory.appendingPathComponent("\(name).mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none).export(recipe: recipe, assetURLs: urls, outputURL: output)
        return output
    }
    func generator(_ preview: PreviewComposition) -> AVAssetImageGenerator {
        let result = AVAssetImageGenerator(asset: preview.playerItem.asset)
        result.videoComposition = preview.playerItem.videoComposition
        return result
    }
    @MainActor func pixel(_ generator: AVAssetImageGenerator, time: Double, x: Int, y: Int) async throws -> [UInt8] {
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
        var bytes = [UInt8](repeating: 0, count: 4)
        CIContext().render(CIImage(cgImage: image), toBitmap: &bytes, rowBytes: 4, bounds: CGRect(x: x, y: y, width: 1, height: 1), format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        return bytes
    }
    @MainActor func assertPixel(_ generator: AVAssetImageGenerator, time: Double, x: Int = 48, y: Int, expected: [Int]) async throws {
        let value = try await pixel(generator, time: time, x: x, y: y)
        for channel in 0..<3 { XCTAssertEqual(Int(value[channel]), expected[channel], accuracy: 55, "time \(time), point \(x),\(y)") }
    }
    func amplitude(_ pcm: [Float], _ time: Double, _ frequency: Double, channel: Int = 0) throws -> Double {
        let start = Int(time * 48_000) * 2, count = 7_200
        guard channel >= 0, channel < 2, start >= 0, start + count * 2 <= pcm.count else { throw MediaEngineError.exportFailed }
        var cosine = 0.0, sine = 0.0
        for index in 0..<count {
            let phase = 2 * Double.pi * frequency * Double(index) / 48_000
            let sample = Double(pcm[start + index * 2 + channel])
            cosine += sample * cos(phase); sine += sample * sin(phase)
        }
        return 2 * hypot(cosine, sine) / Double(count)
    }

    @MainActor func decode(_ asset: AVAsset, _ mix: AVAudioMix?) async throws -> [Float] {
        let reader = try AVAssetReader(asset: asset), tracks = try await asset.loadTracks(withMediaType: .audio)
        let output = AVAssetReaderAudioMixOutput(audioTracks: tracks, audioSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsNonInterleaved: false]); output.audioMix = mix; reader.add(output); XCTAssertTrue(reader.startReading())
        var pcm = [Float](repeating: 0, count: 4 * 48_000 * 2)
        while let sample = output.copyNextSampleBuffer() { let offset = Int((CMSampleBufferGetPresentationTimeStamp(sample).seconds * 48_000).rounded()) * 2; let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample)); var bytes = Data(count: CMBlockBufferGetDataLength(block)); XCTAssertEqual(bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }, noErr); bytes.withUnsafeBytes { raw in for (index, value) in raw.bindMemory(to: Float.self).enumerated() where pcm.indices.contains(offset + index) { pcm[offset + index] = value } } }
        XCTAssertEqual(reader.status, .completed, "\(String(describing: reader.error))"); return pcm
    }
}
#endif
