#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Opt-in benchmark (`KRIA_PREVIEW_REBUILD_BENCH=1 swift test --filter PreviewRebuildBenchmarkTests`).
/// Replays NativeEditorSession's slider-drag pattern (cancel the previous task,
/// sleep 80 ms, full `LivePreviewComposition` rebuild, publish only the latest)
/// against a Talking-shaped recipe and prints how many builds started, how many
/// ran to the end while already stale, how many overlapped, how long the last
/// sample took to settle, and the longest main-actor stall.
final class PreviewRebuildBenchmarkTests: XCTestCase {
    @MainActor func testSliderDragRebuilds() async throws {
        guard ProcessInfo.processInfo.environment["KRIA_PREVIEW_REBUILD_BENCH"] != nil else { throw XCTSkip("opt-in benchmark") }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let seconds = 60.0
        let talk = try await mux(try await blackVideo(directory, seconds: seconds), try tone(directory, seconds: seconds), directory.appendingPathComponent("talk.mov"))
        let urls = ["talk": talk, "music": try tone(directory, name: "music", frequency: 660, seconds: seconds), "font": try fontURL()]
        let cuts = Int(ProcessInfo.processInfo.environment["KRIA_BENCH_CUTS"] ?? "") ?? 24
        let cues = Int(ProcessInfo.processInfo.environment["KRIA_BENCH_CUES"] ?? "") ?? 60
        let recipe = try talkingRecipe(urls, cuts: cuts, cues: cues)

        var single: [Double] = []
        for _ in 0..<3 {
            let start = ContinuousClock.now
            _ = try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard)
            single.append(start.duration(to: .now).milliseconds)
        }
        let build = single.sorted()[1]
        print("BENCH single-build cuts=\(cuts) cues=\(cues) ms=\(single.map { Int($0) })")

        // Sample spacing just past the 80 ms coalesce window, so every sample's
        // build starts and the next sample supersedes it mid-build.
        for (label, gaps) in [("mid-build", Array(repeating: 80 + build * 0.5, count: 20)),
                              ("slow-drag", (0..<20).map { [100.0, 150, 200, 120, 90][$0 % 5] })] {
            let replay = SessionReplay(urls: urls)
            let heartbeat = Heartbeat()
            heartbeat.start()
            for (index, gap) in gaps.enumerated() {
                var next = recipe
                next.audio.originalVolume = 1 - Double(index + 1) / 40
                replay.schedule(next)
                try await Task.sleep(for: .milliseconds(Int(gap)))
            }
            let last = ContinuousClock.now
            var next = recipe
            next.audio.originalVolume = 0.25
            replay.schedule(next)
            let deadline = last.advanced(by: .seconds(60))
            // Settled once the final sample's own build has published and no
            // superseded build is still running.
            while replay.publishedSequence != replay.sequence || replay.inFlight > 0, ContinuousClock.now < deadline {
                try await Task.sleep(for: .milliseconds(5))
            }
            let drained = ContinuousClock.now
            heartbeat.stop()
            XCTAssertEqual(replay.published?.recipe.audio.originalVolume, 0.25, label)
            let settle = replay.settledAt.map { last.duration(to: $0).milliseconds } ?? -1
            print("BENCH \(label) samples=\(gaps.count + 1) gapMs=\(Int(gaps[0]))"
                + " started=\(replay.started) staleCompleted=\(replay.staleCompleted) aborted=\(replay.aborted)"
                + " maxInFlight=\(replay.maxInFlight) settleMs=\(Int(settle)) finalBuildMs=\(Int(replay.finalBuild)) drainMs=\(Int(last.duration(to: drained).milliseconds))"
                + " maxStallMs=\(Int(heartbeat.maxStall)) staleBuildMs=\(replay.staleDurations.map { Int($0) })"
                + " abortedBuildMs=\(replay.abortedDurations.map { Int($0) })")
        }
    }

    @MainActor private final class SessionReplay {
        let urls: [String: URL]
        var task: Task<Void, Never>?
        var sequence = 0, publishedSequence = 0, started = 0, staleCompleted = 0, aborted = 0, inFlight = 0, maxInFlight = 0
        var staleDurations: [Double] = [], abortedDurations: [Double] = [], finalBuild = 0.0
        var settledAt: ContinuousClock.Instant?
        var published: LivePreviewComposition?
        init(urls: [String: URL]) { self.urls = urls }

        func schedule(_ recipe: EditRecipe) {
            task?.cancel()
            sequence += 1
            let sequence = sequence
            task = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(80))
                guard !Task.isCancelled else { return }
                await self?.rebuild(recipe, sequence: sequence)
            }
        }

        private func rebuild(_ recipe: EditRecipe, sequence: Int) async {
            started += 1; inFlight += 1; maxInFlight = max(maxInFlight, inFlight)
            defer { inFlight -= 1 }
            let start = ContinuousClock.now
            do {
                let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls, branding: .standard)
                guard sequence == self.sequence, !Task.isCancelled else {
                    staleCompleted += 1; staleDurations.append(start.duration(to: .now).milliseconds); return
                }
                published = preview
                publishedSequence = sequence
                settledAt = .now
                finalBuild = start.duration(to: .now).milliseconds
            } catch {
                aborted += 1; abortedDurations.append(start.duration(to: .now).milliseconds)
            }
        }
    }

    /// Longest gap between 4 ms main-actor ticks, minus the tick itself: the
    /// worst frame hitch the drag would cause on the main thread.
    @MainActor private final class Heartbeat {
        var maxStall = 0.0
        private var task: Task<Void, Never>?
        func start() {
            task = Task { [weak self] in
                var previous = ContinuousClock.now
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(4))
                    let now = ContinuousClock.now
                    if let self { self.maxStall = max(self.maxStall, previous.duration(to: now).milliseconds - 4) }
                    previous = now
                }
            }
        }
        func stop() { task?.cancel() }
    }

    /// A speech-cleanup Talking edit as the phone compiler emits it: one take cut
    /// into `cuts` segments (0.5 s removed between each), a caption cue per
    /// window, and a music lane under the voice.
    private func talkingRecipe(_ urls: [String: URL], cuts: Int, cues: Int) throws -> EditRecipe {
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let references = try assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        }
        let segment = 2.0, total = Double(cuts) * segment
        let clips = (0..<cuts).map { index in
            TimelineClip(id: "cut-\(index)", sourceAssetID: "talk", sourceStart: Double(index) * (segment + 0.5),
                         sourceDuration: segment, timelineStart: Double(index) * segment)
        }
        let window = total / Double(cues)
        let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1), black = TextInk(red: 0, green: 0, blue: 0, alpha: 1)
        let captions = (0..<cues).map { index in
            PortableTextLayer(id: "caption-\(index)", start: Double(index) * window, end: Double(index + 1) * window,
                anchorX: 540, anchorY: 1500, rotationDegrees: 0,
                runs: [PositionedTextRun(text: "caption line number \(index)", fontAssetID: "font", fontSize: 64, x: 160, baselineY: 1500,
                                         letterSpacing: 0, shaped: true, fill: white, stroke: black, strokeWidth: 4)])
        }
        return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 1080, height: 1920), assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: clips),
                     TimelineTrack(id: "music", kind: .audio, clips: [TimelineClip(id: "bed", sourceAssetID: "music", sourceDuration: total, volume: 0.25)])],
            audio: AudioMixRecipe(originalVolume: 1), assetManifest: RenderAssetManifest(assets: references), textLayers: captions)
    }

    private func fontURL() throws -> URL {
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        return URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
    }

    private func tone(_ directory: URL, name: String = "voice", frequency: Double = 431, seconds: Double) throws -> URL {
        let url = directory.appendingPathComponent("\(name).m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000))); buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) { buffer.floatChannelData![0][index] = Float(0.5 * sin(Double(index) * 2 * .pi * frequency / 48_000)) }
        let file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000], commonFormat: .pcmFormatFloat32, interleaved: false)
        try file.write(from: buffer); return url
    }

    @MainActor private func blackVideo(_ directory: URL, seconds: Double) async throws -> URL {
        let url = directory.appendingPathComponent("picture.mp4"), writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 360, AVVideoHeightKey: 640])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 360, kCVPixelBufferHeightKey as String: 640])
        writer.add(input); XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        for index in 0..<Int(seconds * 30) { while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }; var raw: CVPixelBuffer?; CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &raw); let pixel = try XCTUnwrap(raw); CVPixelBufferLockBaseAddress(pixel, []); memset(try XCTUnwrap(CVPixelBufferGetBaseAddress(pixel)), 0, CVPixelBufferGetDataSize(pixel)); CVPixelBufferUnlockBaseAddress(pixel, []); XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30))) }
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
}

private extension Duration {
    var milliseconds: Double { Double(components.seconds) * 1000 + Double(components.attoseconds) / 1e15 }
}
#endif
