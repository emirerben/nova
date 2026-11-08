#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
import CoreVideo
@testable import KriaMediaEngine

/// Opt-in Mac microbenchmark (`KRIA_USETAKE_BENCH=1 swift test --filter UseTakeBenchmarkTests`) of the steps Use take runs
/// on a recorded take: staging the file, hashing it, and the 640x360 analysis-proxy transcode, run the way the upload does
/// them and again with the prewarm in front. The numbers are Mac numbers (hardware HEVC decode and H.264 encode on this
/// machine); they size the steps against each other and are not iPhone numbers.
final class UseTakeBenchmarkTests: XCTestCase {
    private func median(_ values: [Double]) -> Double { values.sorted()[values.count / 2] }
    private func ms(_ block: () throws -> Void) rethrows -> Double {
        let start = DispatchTime.now().uptimeNanoseconds
        try block()
        return Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
    }
    @MainActor private func msAsync(_ block: @MainActor () async throws -> Void) async rethrows -> Double {
        let start = DispatchTime.now().uptimeNanoseconds
        try await block()
        return Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
    }

    /// 1920x1080 HEVC at roughly a phone's bitrate, from noise frames so the encoder spends its whole budget, plus AAC.
    @MainActor private func makeTake(in directory: URL, seconds: Double, bitrate: Int) async throws -> URL {
        let (w, h, fps) = (1920, 1080, 30)
        let videoURL = directory.appendingPathComponent("video.mov")
        let writer = try AVAssetWriter(outputURL: videoURL, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.hevc, AVVideoWidthKey: w, AVVideoHeightKey: h,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: bitrate, AVVideoExpectedSourceFrameRateKey: fps]])
        input.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferWidthKey as String: w, kCVPixelBufferHeightKey as String: h])
        writer.add(input)
        XCTAssertTrue(writer.startWriting()); writer.startSession(atSourceTime: .zero)
        var pool: [CVPixelBuffer] = []
        for _ in 0..<45 {
            var buffer: CVPixelBuffer?
            CVPixelBufferCreate(nil, w, h, kCVPixelFormatType_32BGRA, nil, &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            arc4random_buf(CVPixelBufferGetBaseAddress(pixel), CVPixelBufferGetDataSize(pixel))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            pool.append(pixel)
        }
        for index in 0..<Int(seconds * Double(fps)) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            XCTAssertTrue(adaptor.append(pool[index % pool.count], withPresentationTime: CMTime(value: Int64(index), timescale: Int32(fps))))
        }
        input.markAsFinished(); await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        let audioURL = directory.appendingPathComponent("audio.m4a")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let pcm = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(seconds * 48_000)))
        pcm.frameLength = pcm.frameCapacity
        for index in 0..<Int(pcm.frameLength) { pcm.floatChannelData![0][index] = 0.2 * Float(sin(Double(index) * 2 * .pi * 220 / 48_000)) }
        do {
            // The file is finalized when it goes out of scope.
            let file = try AVAudioFile(forWriting: audioURL, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 128_000],
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
        let output = directory.appendingPathComponent("take.mov")
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output; exporter.outputFileType = .mov
        await exporter.export()
        XCTAssertEqual(exporter.status, .completed)
        try? FileManager.default.removeItem(at: videoURL); try? FileManager.default.removeItem(at: audioURL)
        return output
    }

    @MainActor func testUseTakeSteps() async throws {
        guard ProcessInfo.processInfo.environment["KRIA_USETAKE_BENCH"] != nil else { throw XCTSkip("opt-in benchmark") }
        let seconds = Double(ProcessInfo.processInfo.environment["KRIA_USETAKE_SECONDS"] ?? "") ?? 60
        let bitrate = Int(ProcessInfo.processInfo.environment["KRIA_USETAKE_BITRATE"] ?? "") ?? 10_000_000
        let runs = Int(ProcessInfo.processInfo.environment["KRIA_USETAKE_RUNS"] ?? "") ?? 5
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("usetake-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let made = ContinuousClock.now
        let take = try await makeTake(in: root, seconds: seconds, bitrate: bitrate)
        let size = (try FileManager.default.attributesOfItem(atPath: take.path)[.size] as? NSNumber)?.int64Value ?? 0
        let videoTracks = try await AVURLAsset(url: take).loadTracks(withMediaType: .video)
        let formats = try await videoTracks.first?.load(.formatDescriptions)
        let codec = formats?.first.map { CMFormatDescriptionGetMediaSubType($0) }
        print("BENCH fixture seconds=\(seconds) bytes=\(size) MB=\(size / 1_000_000) codec=\(codec.map { String(format: "%08x", $0) } ?? "?") built_in=\(made.duration(to: .now))")

        let staging = root.appendingPathComponent("KriaUploads", isDirectory: true)
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true)

        var copies: [Double] = [], links: [Double] = [], hashes: [Double] = [], realCopies: [Double] = []
        for run in 0..<runs {
            let copyTarget = staging.appendingPathComponent("copy-\(run).mov")
            copies.append(try ms { try FileManager.default.copyItem(at: take, to: copyTarget) })
            let linkTarget = staging.appendingPathComponent("link-\(run).mov")
            links.append(try ms { try FileManager.default.linkItem(at: take, to: linkTarget) })
            // A copy that cannot be cloned (a read-write loop), the cost on a filesystem without copy-on-write.
            let plain = staging.appendingPathComponent("plain-\(run).mov")
            realCopies.append(try ms {
                let input = try FileHandle(forReadingFrom: take)
                FileManager.default.createFile(atPath: plain.path, contents: nil)
                let output = try FileHandle(forWritingTo: plain)
                while let chunk = try input.read(upToCount: 4 * 1024 * 1024), !chunk.isEmpty { try output.write(contentsOf: chunk) }
                try input.close(); try output.close()
            })
            hashes.append(try ms { _ = try SHA256Fingerprinter().fingerprint(file: take) })
        }
        let identityMatches = try FileIdentity(of: take) == FileIdentity(of: staging.appendingPathComponent("link-0.mov"))
        let cloneIdentityMatches = try FileIdentity(of: take) == FileIdentity(of: staging.appendingPathComponent("copy-0.mov"))
        print("BENCH stage copyItem_ms_median=\(Int(median(copies))) hardlink_ms_median=\(String(format: "%.2f", median(links))) read_write_copy_ms_median=\(Int(median(realCopies))) link_identity_equal=\(identityMatches) copyItem_identity_equal=\(cloneIdentityMatches)")
        print("BENCH hash sha256_ms_median=\(Int(median(hashes))) all=\(hashes.map { Int($0) })")

        var transcodes: [Double] = []
        for run in 0..<max(3, runs / 2) {
            let destination = root.appendingPathComponent("proxy-\(run).mp4")
            transcodes.append(try await msAsync { _ = try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: take, destination: destination) })
        }
        print("BENCH proxy transcode_ms_median=\(Int(median(transcodes))) all=\(transcodes.map { Int($0) })")

        // After Use take, the part of the work that sits under the spinner: stage -> import (+hash) -> transcode.
        var before: [Double] = [], after: [Double] = [], prewarmWork: [Double] = []
        let analysis: SourcePrewarmer.ProxyMaker = { source, destination in
            try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: source, destination: destination)
        }
        for run in 0..<max(3, runs / 2) {
            let project = ProjectDirectory(root: root.appendingPathComponent("before-\(run)", isDirectory: true))
            before.append(try await msAsync {
                let staged = staging.appendingPathComponent("b-\(run).mov")
                try FileManager.default.copyItem(at: take, to: staged)
                let asset = try await AssetImportCoordinator(project: project).importAsset(from: staged, preferLink: true)
                let proxy = project.proxies.appendingPathComponent("\(asset.id).mp4")
                _ = try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: project.root.appendingPathComponent(asset.relativePath), destination: proxy)
            })

            // What the prewarm itself costs, started and claimed at once.
            let timed = SourcePrewarmer(directory: root.appendingPathComponent("prewarm-t-\(run)", isDirectory: true), makeProxy: analysis)
            prewarmWork.append(try await msAsync {
                timed.start(key: UUID(), source: take)
                let claim = try XCTUnwrap(timed.claim(source: take))
                _ = await claim.fingerprint(); _ = await claim.takeProxy(); claim.discard()
            })

            // Use take after the creator reviewed the take for longer than the prewarm needs.
            let prewarmer = SourcePrewarmer(directory: root.appendingPathComponent("prewarm-\(run)", isDirectory: true), makeProxy: analysis)
            prewarmer.start(key: UUID(), source: take)
            try await Task.sleep(for: .milliseconds(Int(median(transcodes) * 1.5)))
            let project2 = ProjectDirectory(root: root.appendingPathComponent("after-\(run)", isDirectory: true))
            after.append(try await msAsync {
                let staged = staging.appendingPathComponent("a-\(run).mov")
                try FileManager.default.linkItem(at: take, to: staged)
                let claim = try XCTUnwrap(prewarmer.claim(source: staged))
                let known = await claim.fingerprint()
                let asset = try await AssetImportCoordinator(project: project2).importAsset(from: staged, preferLink: true, reusing: known)
                let proxyOutput = await claim.takeProxy()
                let ready = try XCTUnwrap(proxyOutput)
                try FileManager.default.createDirectory(at: project2.proxies, withIntermediateDirectories: true)
                try FileManager.default.moveItem(at: ready, to: project2.proxies.appendingPathComponent("\(asset.id).mp4"))
                claim.discard()
            })
        }
        print("BENCH usetake before_ms_median=\(Int(median(before))) after_prewarm_ready_ms_median=\(Int(median(after))) prewarm_work_ms_median=\(Int(median(prewarmWork))) before_all=\(before.map { Int($0) }) after_all=\(after.map { Int($0) })")
    }
}
#endif
