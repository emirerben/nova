import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
import VideoToolbox
import CryptoKit
@testable import KriaMediaEngine

private final class TalkSaveBenchToken {}

/// Samples the app's physical footprint (what the system counts against the app's memory limit) while an export runs.
private final class FootprintSampler: @unchecked Sendable {
    private let lock = NSLock()
    private var running = true
    private var peakBytes: UInt64 = 0
    private var startBytes: UInt64 = 0
    private var lowestAvailableBytes = Int.max

    static func footprint() -> UInt64 {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count) }
        }
        return result == KERN_SUCCESS ? info.phys_footprint : 0
    }

    init() {
        startBytes = Self.footprint()
        peakBytes = startBytes
        let thread = Thread { [self] in
            while true {
                lock.lock(); let go = running; lock.unlock()
                if !go { break }
                let now = Self.footprint(), available = os_proc_available_memory()
                lock.lock(); peakBytes = max(peakBytes, now); if available > 0 { lowestAvailableBytes = min(lowestAvailableBytes, available) }; lock.unlock()
                usleep(20_000)
            }
        }
        thread.start()
    }
    func finish() -> (start: UInt64, peak: UInt64, lowestAvailable: Int) {
        lock.lock(); defer { lock.unlock() }
        running = false
        return (startBytes, max(peakBytes, Self.footprint()), lowestAvailableBytes == Int.max ? 0 : lowestAvailableBytes)
    }
}

/// Device benchmark for the Talking editor Save: the real `AVFoundationLocalExporter` on a Talking recipe with a
/// speaker background, on a source shaped like a phone take (1080x1920 HEVC through a rotated 1920x1080 stream).
/// Env (prefix the variable with TEST_RUNNER_ for xcodebuild): TALKSAVE_SECONDS (30), TALKSAVE_RUNS (3),
/// TALKSAVE_ARMS (comma list of tuning names, interleaved), TALKSAVE_BRAND (standard|none), TALKSAVE_CAPTIONS (1|0),
/// TALKSAVE_BG (image|video), TALKSAVE_LABEL. Numbers print as lines starting TALKSAVE.
final class TalkSaveBenchTests: XCTestCase {
    private final class Stats: @unchecked Sendable {
        private let lock = NSLock()
        private var table: [String: (count: Int, sum: Double, max: Double)] = [:]
        func add(_ name: String, _ ms: Double?) {
            guard let ms else { lock.lock(); table[name, default: (0, 0, 0)].count += 1; lock.unlock(); return }
            lock.lock()
            var row = table[name, default: (0, 0, 0)]
            row.count += 1; row.sum += ms; row.max = Swift.max(row.max, ms)
            table[name] = row
            lock.unlock()
        }
        func reset() { lock.lock(); table = [:]; lock.unlock() }
        func snapshot() -> [String: (count: Int, sum: Double, max: Double)] { lock.lock(); defer { lock.unlock() }; return table }
    }

    private func env(_ name: String) -> String? { ProcessInfo.processInfo.environment[name] }
    private func thermal() -> String {
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: "nominal"; case .fair: "fair"; case .serious: "serious"; case .critical: "critical"; @unknown default: "unknown"
        }
    }
    private func median(_ values: [Double]) -> Double { values.sorted()[values.count / 2] }

    private var work: URL {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("talksave", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base
    }

    // MARK: source

    /// 1920x1080 HEVC with a 90 degree transform, like the camera writes, the talking-head poster clip looped to `seconds`
    /// and scaled up, plus a mono AAC voice-like track. Cached between runs.
    @MainActor private func phoneTake(seconds: Double) async throws -> URL {
        let codec = env("TALKSAVE_CODEC") == "h264" ? AVVideoCodecType.h264 : AVVideoCodecType.hevc
        let final = work.appendingPathComponent("take-\(Int(seconds))-\(codec.rawValue).mov")
        if FileManager.default.fileExists(atPath: final.path) { return final }
        let posterURL = try XCTUnwrap(Bundle(for: TalkSaveBenchToken.self).url(forResource: "talking", withExtension: "mp4"))
        let poster = AVURLAsset(url: posterURL)
        let posterTracks = try await poster.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(posterTracks.first)
        let reader = try AVAssetReader(asset: poster)
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(out)
        XCTAssertTrue(reader.startReading())
        var stills: [CIImage] = []
        while let sample = out.copyNextSampleBuffer(), let buffer = CMSampleBufferGetImageBuffer(sample) { stills.append(CIImage(cvPixelBuffer: buffer)) }
        let context = CIContext()
        let rendered: [CGImage] = stills.compactMap { still in
            let portrait = still.transformed(by: CGAffineTransform(scaleX: 1080 / still.extent.width, y: 1920 / still.extent.height))
            // Stored landscape = display portrait turned 90 degrees counter-clockwise.
            let landscape = portrait.transformed(by: CGAffineTransform(rotationAngle: .pi / 2)).transformed(by: CGAffineTransform(translationX: 1920, y: 0))
            return context.createCGImage(landscape, from: CGRect(x: 0, y: 0, width: 1920, height: 1080))
        }
        XCTAssertEqual(rendered.count, 150)

        let videoURL = work.appendingPathComponent("take-video-\(Int(seconds))-\(codec.rawValue).mov")
        try? FileManager.default.removeItem(at: videoURL)
        let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: codec, AVVideoWidthKey: 1920, AVVideoHeightKey: 1080,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 16_000_000],
        ])
        input.transform = CGAffineTransform(a: 0, b: 1, c: -1, d: 0, tx: 1080, ty: 0)
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferWidthKey as String: 1920, kCVPixelBufferHeightKey as String: 1080,
        ])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        let frames = Int(seconds * 30)
        for index in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let cg = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 1920, height: 1080, bitsPerComponent: 8,
                                             bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                             bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue))
            cg.draw(rendered[index % rendered.count], in: CGRect(x: 0, y: 0, width: 1920, height: 1080))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)

        let audioURL = work.appendingPathComponent("take-audio-\(Int(seconds))-\(codec.rawValue).m4a")
        try? FileManager.default.removeItem(at: audioURL)
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
        pcm.frameLength = pcm.frameCapacity
        for index in 0..<Int(pcm.frameLength) {
            let t = Double(index) / 48_000
            let syllables = 0.5 + 0.5 * sin(2 * .pi * 3.2 * t)
            pcm.floatChannelData![0][index] = Float(0.25 * syllables * (sin(2 * .pi * 180 * t) + 0.5 * sin(2 * .pi * 540 * t)))
        }
        do {
            let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 96_000],
                                       commonFormat: .pcmFormatFloat32, interleaved: false)
            try file.write(from: pcm)
        }
        let composition = AVMutableComposition()
        let videoAsset = AVURLAsset(url: videoURL), audioAsset = AVURLAsset(url: audioURL)
        for (asset, kind) in [(videoAsset, AVMediaType.video), (audioAsset, AVMediaType.audio)] {
            let sourceTracks = try await asset.loadTracks(withMediaType: kind)
            let source = try XCTUnwrap(sourceTracks.first)
            let destination = try XCTUnwrap(composition.addMutableTrack(withMediaType: kind, preferredTrackID: kCMPersistentTrackID_Invalid))
            try destination.insertTimeRange(try await source.load(.timeRange), of: source, at: .zero)
            if kind == .video { destination.preferredTransform = try await source.load(.preferredTransform) }
        }
        let export = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        export.outputURL = final
        export.outputFileType = .mov
        await export.export()
        XCTAssertEqual(export.status, .completed, "\(String(describing: export.error))")
        try? FileManager.default.removeItem(at: videoURL); try? FileManager.default.removeItem(at: audioURL)
        return final
    }

    // MARK: recipe

    @MainActor private func scene(seconds: Double) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let take = try await phoneTake(seconds: seconds)
        let bundle = Bundle(for: TalkSaveBenchToken.self)
        let backgroundPhoto = try XCTUnwrap(bundle.url(forResource: "speaker-matte-talking", withExtension: "jpg"))
        var background = SpeakerTestBackground(url: backgroundPhoto)
        if env("TALKSAVE_BG") == "video" {
            background = SpeakerTestBackground(url: try XCTUnwrap(bundle.url(forResource: "talking", withExtension: "mp4")), kind: .video)
        }
        var scene = try speakerRecipe(canvas: Canvas(width: 1080, height: 1920), speaker: take, duration: seconds, background: background)
        if env("TALKSAVE_CAPTIONS") != "0" {
            let font = try XCTUnwrap(bundle.url(forResource: "Inter-Regular", withExtension: "ttf") ?? bundle.url(forResource: "Inter-Regular", withExtension: "ttf", subdirectory: "fonts")
                ?? Bundle.main.url(forResource: "Inter-Regular", withExtension: "ttf") ?? Bundle.main.url(forResource: "Inter-Regular", withExtension: "ttf", subdirectory: "fonts"))
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: font)
            scene.recipe.assets.append(MediaAsset(id: "font", relativePath: "font", fingerprint: fingerprint))
            var references = scene.recipe.assetManifest?.assets ?? []
            references.append(RenderAssetReference(id: "font", fingerprint: try RenderFingerprint(fingerprint), source: .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: fingerprint.hex)))
            scene.recipe.assetManifest = RenderAssetManifest(assets: references)
            scene.urls["font"] = font
            let white = TextInk(red: 1, green: 1, blue: 1, alpha: 1), black = TextInk(red: 0, green: 0, blue: 0, alpha: 1)
            let cues = Int(seconds / 1.5)
            scene.recipe.textLayers = (0..<cues).map { index in
                PortableTextLayer(id: "caption-\(index)", start: Double(index) * 1.5, end: Double(index + 1) * 1.5, anchorX: 540, anchorY: 1500, rotationDegrees: 0,
                                  runs: [PositionedTextRun(text: "caption line number \(index)", fontAssetID: "font", fontSize: 64, x: 160, baselineY: 1500,
                                                           letterSpacing: 0, shaped: true, fill: white, stroke: black, strokeWidth: 4)])
            }
        }
        try scene.recipe.validate()
        return scene
    }

    // MARK: runs

    private struct Run {
        var arm: String
        var seconds: Double
        var thermalStart: String
        var thermalEnd: String
        var stages: [String: (count: Int, sum: Double, max: Double)]
        var frames: [SpeakerTestFrame]
        var composed: [String]
        var file: URL
        var peakFootprint: UInt64 = 0
        var startFootprint: UInt64 = 0
        var lowestAvailable = 0
    }

    /// SHA-256 of every composed frame as the compositor hands it to the encoder: exact, and free of encoder noise.
    private final class FrameTap: @unchecked Sendable {
        private let lock = NSLock()
        private(set) var hashes: [String] = []
        func install() {
            RecipeWriter.frameTap = { [self] buffer in
                CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
                let w = CVPixelBufferGetWidth(buffer), h = CVPixelBufferGetHeight(buffer), stride = CVPixelBufferGetBytesPerRow(buffer)
                var hasher = SHA256(); let base = CVPixelBufferGetBaseAddress(buffer)!
                for y in 0..<h { hasher.update(bufferPointer: UnsafeRawBufferPointer(start: base + y * stride, count: w * 4)) }
                let hex = hasher.finalize().prefix(8).map { String(format: "%02x", $0) }.joined()
                lock.lock(); hashes.append(hex); lock.unlock()
            }
        }
    }

    @MainActor private func exportOnce(_ recipe: EditRecipe, urls: [String: URL], arm: String, label: String, stats: Stats, times: [Double]) async throws -> Run {
        let output = work.appendingPathComponent("export-\(label)-\(arm).mp4")
        try? FileManager.default.removeItem(at: output)
        let state = work.appendingPathComponent("state-\(UUID().uuidString)")
        let branding: KriaBranding.Options = env("TALKSAVE_BRAND") == "none" ? .none : .standard
        stats.reset()
        let tap = FrameTap()
        if env("TALKSAVE_TAP") == "1" { tap.install() }
        defer { RecipeWriter.frameTap = nil }
        let thermalStart = thermal()
        let sampler = FootprintSampler()
        let start = ContinuousClock.now
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: state), branding: branding)
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        let elapsed = start.duration(to: .now)
        let memory = sampler.finish()
        let wall = Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18
        XCTAssertEqual(checkpoint.status, .completed)
        let thermalEnd = thermal()
        var frames: [SpeakerTestFrame] = []
        for time in times { frames.append(try await fileFrame(output, at: time)) }
        try? FileManager.default.removeItem(at: state)
        if env("TALKSAVE_POST") == "1" {
            let hashStart = ContinuousClock.now
            let fingerprint = try SHA256Fingerprinter().fingerprint(file: output)
            let hashed = hashStart.duration(to: .now)
            let validateStart = ContinuousClock.now
            let length = try await AVURLAsset(url: output).load(.duration).seconds
            let validated = validateStart.duration(to: .now)
            print(String(format: "TALKSAVE post-export arm=%@: SHA-256 of %.1f MB %.0f ms (twice in a Save), duration check %.0f ms (%.2f s)", arm, Double(fingerprint.byteCount) / 1_048_576, Double(hashed.components.attoseconds) / 1e15 + Double(hashed.components.seconds) * 1000, Double(validated.components.attoseconds) / 1e15 + Double(validated.components.seconds) * 1000, length))
        }
        return Run(arm: arm, seconds: wall, thermalStart: thermalStart, thermalEnd: thermalEnd, stages: stats.snapshot(), frames: frames, composed: tap.hashes, file: output,
                   peakFootprint: memory.peak, startFootprint: memory.start, lowestAvailable: memory.lowestAvailable)
    }

    @MainActor func testTalkSaveBench() async throws {
        try TalkSaveOptIn.require()
        let free = try URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage ?? 0
        print("TALKSAVE device free bytes \(free)")
        XCTAssertGreaterThan(free, 3_000_000_000)
        let seconds = Double(env("TALKSAVE_SECONDS") ?? "") ?? 30
        let runs = Int(env("TALKSAVE_RUNS") ?? "") ?? 3
        let arms = (env("TALKSAVE_ARMS") ?? "base").split(separator: ",").map(String.init)
        let label = env("TALKSAVE_LABEL") ?? "x"
        let prepStart = ContinuousClock.now
        let (recipe, urls) = try await scene(seconds: seconds)
        print("TALKSAVE scene ready in \(prepStart.duration(to: .now)) (take \(seconds) s, brand \(env("TALKSAVE_BRAND") ?? "standard"), captions \(env("TALKSAVE_CAPTIONS") ?? "1"), bg \(env("TALKSAVE_BG") ?? "image"), note \(env("TALKSAVE_NOTE") ?? "-"))")

        let stats = Stats()
        EngineSignpost.observer = { name, ms, _ in stats.add(name, ms) }
        addTeardownBlock { EngineSignpost.observer = nil }
        let times = stride(from: 0.5, to: seconds - 0.5, by: (seconds - 1) / 6).map { $0 }

        // A warm-up export is not reported: first use of Metal pipelines, Vision models and the hardware encoder.
        _ = try await exportOnce(recipe, urls: urls, arm: "warmup", label: label, stats: stats, times: [])

        var all: [Run] = []
        for round in 0..<runs {
            for arm in arms {
                TalkSaveTuning.apply(arm)
                defer { TalkSaveTuning.apply("base") }
                var armRecipe = recipe
                if arm == "lufs" { armRecipe.audio.targetLUFS = -14 }
                let run = try await exportOnce(armRecipe, urls: urls, arm: arm, label: "\(label)-\(round)", stats: stats, times: times)
                all.append(run)
                report(run, round: round, seconds: seconds)
            }
        }
        summarize(all, seconds: seconds)
        fidelity(all)
    }

    private func ms(_ run: Run, _ name: String) -> (count: Int, sum: Double, max: Double) { run.stages[name] ?? (0, 0, 0) }

    private func report(_ run: Run, round: Int, seconds: Double) {
        let frames = Double(ms(run, "talk.stage.frame").count)
        var line = String(format: "TALKSAVE run round=%d arm=%@ wall=%.2fs frames=%d (%.1f ms/frame wall) thermal %@->%@ file %.2f MB", round, run.arm, run.seconds, Int(frames), run.seconds * 1000 / max(1, frames), run.thermalStart, run.thermalEnd, Double((try? FileManager.default.attributesOfItem(atPath: run.file.path)[.size] as? Int) ?? 0) / 1_048_576)
        for name in run.stages.keys.sorted() {
            let row = run.stages[name]!
            line += String(format: "\nTALKSAVE   %@ n=%d sum=%.0fms mean=%.2fms max=%.1fms", name, row.count, row.sum, row.count > 0 ? row.sum / Double(row.count) : 0, row.max)
        }
        print(line)
        print(String(format: "TALKSAVE memory round=%d arm=%@ footprint start %.1f MB peak %.1f MB (growth %.1f MB), lowest os_proc_available_memory %.1f MB", round, run.arm,
                     Double(run.startFootprint) / 1_048_576, Double(run.peakFootprint) / 1_048_576, Double(run.peakFootprint &- run.startFootprint) / 1_048_576, Double(run.lowestAvailable) / 1_048_576))
    }

    private func summarize(_ all: [Run], seconds: Double) {
        for arm in Array(Set(all.map(\.arm))).sorted() {
            let rows = all.filter { $0.arm == arm }
            let walls = rows.map(\.seconds)
            let peaks = rows.map { Double($0.peakFootprint) / 1_048_576 }
            print(String(format: "TALKSAVE summary memory arm=%@ peak footprint median %.1f MB max %.1f MB (%@)", arm, median(peaks), peaks.max()!, peaks.map { String(format: "%.1f", $0) }.joined(separator: ", ")))
            print(String(format: "TALKSAVE summary arm=%@ runs=%d wall median %.2fs min %.2fs max %.2fs (%@)", arm, rows.count, median(walls), walls.min()!, walls.max()!, walls.map { String(format: "%.2f", $0) }.joined(separator: ", ")))
        }
    }

    private func worst(_ a: [SpeakerTestFrame], _ b: [SpeakerTestFrame]) -> (maxDelta: Int, meanDelta: Double, differing: Double) {
        let all = zip(a, b).map { $0.difference(from: $1) }
        return (all.map(\.maxDelta).max() ?? 0, all.map(\.meanDelta).max() ?? 0, all.map(\.differingFraction).max() ?? 0)
    }

    /// Every run against the first base run, and each arm against its own first run (repeatability of the arm itself).
    private func fidelity(_ all: [Run]) {
        for arm in Array(Set(all.map(\.arm))).sorted() {
            let own = all.filter { $0.arm == arm }
            guard let first = own.first else { continue }
            for run in own.dropFirst() {
                let delta = worst(run.frames, first.frames)
                print(String(format: "TALKSAVE repeat arm=%@ vs its first run: max channel delta %d, worst mean delta %.5f, worst differing-pixel fraction %.5f", arm, delta.maxDelta, delta.meanDelta, delta.differing))
            }
        }
        if let reference = all.first(where: { $0.arm == "base" && !$0.composed.isEmpty }) {
            for run in all where run.file != reference.file && !run.composed.isEmpty {
                let differing = zip(run.composed, reference.composed).filter { $0 != $1 }.count
                print("TALKSAVE composed arm=\(run.arm) vs first base run: \(differing) of \(reference.composed.count) composed frames differ (frame counts \(run.composed.count) and \(reference.composed.count))")
            }
        }
        guard let reference = all.first(where: { $0.arm == "base" }) else { return }
        for arm in Array(Set(all.map(\.arm))).sorted() where arm != "base" {
            let own = all.filter { $0.arm == arm }
            let deltas = own.map { worst($0.frames, reference.frames) }
            print(String(format: "TALKSAVE fidelity arm=%@ vs first base (%d runs): max channel delta %d, worst mean delta %.5f, worst differing-pixel fraction %.5f", arm, own.count, deltas.map(\.maxDelta).max() ?? 0, deltas.map(\.meanDelta).max() ?? 0, deltas.map(\.differing).max() ?? 0))
        }
    }
}

/// Vision person segmentation on its own at the shipping input, with 1, 2 and 3 requests in flight: does the hardware overlap them?
final class TalkSaveVisionTests: XCTestCase {
    private final class Token {}
    func testVisionConcurrency() throws {
        try TalkSaveOptIn.require()
        let url = try XCTUnwrap(Bundle(for: Token.self).url(forResource: "speaker-matte-talking", withExtension: "jpg"))
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let photo = try XCTUnwrap(CIImage(contentsOf: url))
        let scale = max(canvas.width / photo.extent.width, canvas.height / photo.extent.height)
        let frame = photo.transformed(by: CGAffineTransform(scaleX: scale, y: scale)).cropped(to: canvas)
        let qualities: [(String, SpeakerMatteConfig.Quality)] = [("balanced", .balanced), ("fast", .fast)]
        for (name, quality) in qualities {
            var config = SpeakerMatteConfig.shipping
            config.quality = quality
            let vision = VisionPersonSegmenter()
            let input = try XCTUnwrap(vision.segmentationInput(for: frame, canvas: canvas, config: config))
            for _ in 0..<20 { _ = try vision.segmentationMask(forInput: input, config: config) }
            for threads in [1, 2, 3] {
                let perThread = 120 / threads
                let start = ContinuousClock.now
                let group = DispatchGroup()
                for _ in 0..<threads {
                    group.enter()
                    DispatchQueue.global(qos: .userInitiated).async {
                        let segmenter = VisionPersonSegmenter()
                        for _ in 0..<perThread { _ = try? segmenter.segmentationMask(forInput: input, config: config) }
                        group.leave()
                    }
                }
                group.wait()
                let elapsed = start.duration(to: .now)
                let seconds = Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18
                print(String(format: "TALKSAVE vision %@ threads=%d: %.2f ms per mask throughput (%d masks in %.2f s), thermal %d", name, threads, seconds * 1000 / Double(perThread * threads), perThread * threads, seconds, ProcessInfo.processInfo.thermalState.rawValue))
            }
        }
    }
}
