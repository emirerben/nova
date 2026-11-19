import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
import Vision
import CoreML
@testable import KriaMediaEngine

private final class HardwareToken {}

private func clipDurationMs(_ d: Duration) -> Double { Double(d.components.seconds) * 1000 + Double(d.components.attoseconds) / 1e15 }

/// Which hardware Vision's person segmentation runs on by default, and what each choice costs alone and next to a GPU render loop.
final class TalkSaveComputeDeviceTests: XCTestCase {
    private func name(_ device: MLComputeDevice?) -> String {
        guard let device else { return "nil" }
        switch device { case .cpu: return "cpu"; case .gpu: return "gpu"; case .neuralEngine: return "ane"; @unknown default: return "other" }
    }
    func testComputeDevices() throws {
        try TalkSaveOptIn.require()
        let request = VisionPersonSegmenter.makeRequest(quality: .balanced)
        for (stage, devices) in try request.supportedComputeStageDevices {
            print("TALKSAVE vision stage \(stage.rawValue): supported \(devices.map { name($0) }), default \(name(request.computeDevice(for: stage)))")
        }
        let url = try XCTUnwrap(Bundle(for: HardwareToken.self).url(forResource: "speaker-matte-talking", withExtension: "jpg"))
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let photo = try XCTUnwrap(CIImage(contentsOf: url))
        let scale = max(canvas.width / photo.extent.width, canvas.height / photo.extent.height)
        let frame = photo.transformed(by: CGAffineTransform(scaleX: scale, y: scale)).cropped(to: canvas)
        let input = try XCTUnwrap(VisionPersonSegmenter().segmentationInput(for: frame, canvas: canvas, config: .shipping))
        let context = CIContext(options: [.cacheIntermediates: false])
        var out: CVPixelBuffer?
        CVPixelBufferCreate(nil, 1080, 1920, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &out)
        let target = try XCTUnwrap(out)
        let graph = frame.applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: 6]).applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: 1.1])
        func renderLoop(_ stop: @escaping () -> Bool) -> Int { var n = 0; while !stop() { context.render(graph, to: target, bounds: canvas, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!); n += 1 }; return n }
        func visionRun(_ device: MLComputeDevice?, threads: Int, count: Int, withGPULoad: Bool) -> (msPerMask: Double, renders: Int) {
            let flag = NSLock(); nonisolated(unsafe) var stopped = false
            nonisolated(unsafe) var renders = 0
            let loadGroup = DispatchGroup()
            if withGPULoad { loadGroup.enter(); DispatchQueue.global().async { renders = renderLoop { flag.lock(); defer { flag.unlock() }; return stopped }; loadGroup.leave() } }
            let start = ContinuousClock.now
            let group = DispatchGroup()
            for _ in 0..<threads {
                group.enter()
                DispatchQueue.global(qos: .userInitiated).async {
                    for _ in 0..<(count / threads) {
                        let r = VisionPersonSegmenter.makeRequest(quality: .balanced)
                        if let device { r.setComputeDevice(device, for: .main) }
                        try? VNImageRequestHandler(cvPixelBuffer: input, options: [:]).perform([r])
                    }
                    group.leave()
                }
            }
            group.wait()
            let elapsed = start.duration(to: .now)
            flag.lock(); stopped = true; flag.unlock(); loadGroup.wait()
            return (clipDurationMs(elapsed) / Double(count), renders)
        }
        let supported = try request.supportedComputeStageDevices[.main] ?? []
        for device in [nil] + supported.map({ Optional($0) }) {
            _ = visionRun(device, threads: 1, count: 10, withGPULoad: false)
            for threads in [1, 2] {
                let alone = visionRun(device, threads: threads, count: 80, withGPULoad: false)
                let loaded = visionRun(device, threads: threads, count: 80, withGPULoad: true)
                print(String(format: "TALKSAVE vision device=%@ threads=%d: alone %.1f ms/mask; next to a GPU render loop %.1f ms/mask (%d renders meanwhile)", name(device), threads, alone.msPerMask, loaded.msPerMask, loaded.renders))
            }
        }
    }
}

/// Does a smaller input for Vision change the mask, and what does it save? Compared against Vision's own repeat noise.
final class TalkSaveInputSizeTests: XCTestCase {
    private func masks(_ vision: VisionPersonSegmenter, _ frames: [CIImage], canvas: CGRect, side: Int) throws -> (masks: [[UInt8]], inputMs: Double, visionMs: Double) {
        var config = SpeakerMatteConfig.shipping
        config.inputMaxSide = side
        var out: [[UInt8]] = []
        var inputMs = 0.0, visionMs = 0.0
        for frame in frames {
            let t0 = ContinuousClock.now
            let input = try XCTUnwrap(vision.segmentationInput(for: frame, canvas: canvas, config: config))
            let t1 = ContinuousClock.now
            let mask = try XCTUnwrap(vision.segmentationMask(forInput: input, config: config))
            let t2 = ContinuousClock.now
            inputMs += clipDurationMs(t0.duration(to: t1)); visionMs += clipDurationMs(t1.duration(to: t2))
            CVPixelBufferLockBaseAddress(mask, .readOnly)
            let w = CVPixelBufferGetWidth(mask), h = CVPixelBufferGetHeight(mask), stride = CVPixelBufferGetBytesPerRow(mask)
            let base = CVPixelBufferGetBaseAddress(mask)!.assumingMemoryBound(to: UInt8.self)
            var bytes = [UInt8](); bytes.reserveCapacity(w * h)
            for y in 0..<h { for x in 0..<w { bytes.append(base[y * stride + x]) } }
            CVPixelBufferUnlockBaseAddress(mask, .readOnly)
            out.append(bytes)
        }
        return (out, inputMs / Double(frames.count), visionMs / Double(frames.count))
    }
    private func diff(_ a: [[UInt8]], _ b: [[UInt8]]) -> (mean: Double, max: Int, over8: Double) {
        var total = 0, count = 0, worst = 0, over = 0
        for (x, y) in zip(a, b) { for (p, q) in zip(x, y) { let d = abs(Int(p) - Int(q)); total += d; count += 1; worst = max(worst, d); if d > 8 { over += 1 } } }
        return (Double(total) / Double(count), worst, Double(over) / Double(count))
    }
    func testInputSide() throws {
        try TalkSaveOptIn.require()
        let posterURL = try XCTUnwrap(Bundle(for: HardwareToken.self).url(forResource: "talking", withExtension: "mp4"))
        let asset = AVURLAsset(url: posterURL)
        let track = try XCTUnwrap(asset.tracks(withMediaType: .video).first)
        let reader = try AVAssetReader(asset: asset)
        let out = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(out); reader.startReading()
        var frames: [CIImage] = []
        var index = 0
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        while let sample = out.copyNextSampleBuffer(), let buffer = CMSampleBufferGetImageBuffer(sample) {
            if index % 15 == 0 { frames.append(CIImage(cvPixelBuffer: buffer).transformed(by: CGAffineTransform(scaleX: 1080 / 416, y: 1920 / 740))) }
            index += 1
        }
        let vision = VisionPersonSegmenter()
        _ = try masks(vision, frames, canvas: canvas, side: 960)
        let reference = try masks(vision, frames, canvas: canvas, side: 960)
        let repeatNoise = diff(reference.masks, try masks(vision, frames, canvas: canvas, side: 960).masks)
        print(String(format: "TALKSAVE inputside repeat 960 vs 960 (Vision on identical input): mean abs %.4f, max %d, fraction over 8 levels %.5f", repeatNoise.mean, repeatNoise.max, repeatNoise.over8))
        for side in [960, 768, 640, 512, 384] {
            let r = try masks(vision, frames, canvas: canvas, side: side)
            let d = diff(reference.masks, r.masks)
            print(String(format: "TALKSAVE inputside %d: render input %.2f ms, Vision %.2f ms; vs 960: mean abs %.4f, max %d, fraction over 8 levels %.5f", side, r.inputMs, r.visionMs, d.mean, d.max, d.over8))
        }
    }
}

/// Test-only switch for the levers under measurement; `apply` is called per arm.
enum TalkSaveTuning {
    /// "base" draws one frame at a time (the behaviour before the overlap), "p<N>" keeps N frames in flight, "lufs" is "p3" with
    /// a loudness target (a Talking recipe has none; this prices the pre-pass), "nm<N>" removes Vision (an upper bound, not a candidate).
    static func apply(_ arm: String) {
        ExportFrameOverlap.framesInFlight = 1
        SpeakerMatte.segmenterFactory = { VisionPersonSegmenter() }
        if arm.hasPrefix("p"), let depth = Int(arm.dropFirst()) { ExportFrameOverlap.framesInFlight = depth }
        if arm == "lufs" { ExportFrameOverlap.framesInFlight = 3 }
        if arm.hasPrefix("nm") { SpeakerMatte.segmenterFactory = { nil }; ExportFrameOverlap.framesInFlight = Int(arm.dropFirst(2)) ?? 1 }
    }
}

import CryptoKit

/// Records what Vision answers for each distinct input, so two exports can be compared mask for mask (the exported picture
/// also carries encoder and decoder differences; this does not).
final class RecordingVision: PixelSegmenting {
    private let base = VisionPersonSegmenter()
    private let lock = NSLock()
    private(set) var table: [String: [String]] = [:]
    var supportsConcurrentRequests: Bool { true }
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? { try base.confidence(for: frame, canvas: canvas) }
    func personMask(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
        guard let input = segmentationInput(for: frame, canvas: canvas, config: config) else { return nil }
        return try segmentationMask(forInput: input, config: config)
    }
    func segmentationInput(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) -> CVPixelBuffer? { base.segmentationInput(for: frame, canvas: canvas, config: config) }
    func segmentationMask(forInput input: CVPixelBuffer, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
        let key = SpeakerMaskKey(input: input, config: config, environment: .current)?.hex ?? "?"
        let mask = try base.segmentationMask(forInput: input, config: config)
        var digest = "nil"
        if let mask {
            CVPixelBufferLockBaseAddress(mask, .readOnly); defer { CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
            let w = CVPixelBufferGetWidth(mask), h = CVPixelBufferGetHeight(mask), s = CVPixelBufferGetBytesPerRow(mask)
            var hasher = SHA256()
            let b = CVPixelBufferGetBaseAddress(mask)!
            for y in 0..<h { hasher.update(bufferPointer: UnsafeRawBufferPointer(start: b + y * s, count: w)) }
            digest = hasher.finalize().prefix(6).map { String(format: "%02x", $0) }.joined()
        }
        lock.lock(); table[key, default: []].append(digest); lock.unlock()
        return mask
    }
}

/// On the phone: is Vision's answer for one and the same input always the same, serial or overlapped? Settles where the
/// run-to-run differences between two exports come from.
final class TalkSaveMaskDeterminismTests: XCTestCase {
    @MainActor func testMaskPerInput() async throws {
        try TalkSaveOptIn.require()
        let directory = try speakerTestDirectory(self)
        let clip = try XCTUnwrap(Bundle(for: HardwareToken.self).url(forResource: "talking", withExtension: "mp4"))
        let background = try solidSpeakerTestImage(directory: directory, size: (1080, 1920))
        let scene = try speakerRecipe(canvas: Canvas(width: 1080, height: 1920), speaker: clip, duration: 5, background: SpeakerTestBackground(url: background))
        addTeardownBlock { ExportFrameOverlap.framesInFlight = ExportFrameOverlap.defaultFramesInFlight }
        func run(depth: Int) async throws -> [String: [String]] {
            ExportFrameOverlap.framesInFlight = depth
            let recorder = RecordingVision()
            let previous = SpeakerMatte.segmenterFactory
            SpeakerMatte.segmenterFactory = { recorder }
            defer { SpeakerMatte.segmenterFactory = previous }
            _ = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "rec-\(depth)-\(UUID().uuidString.prefix(4))")
            return recorder.table
        }
        func compare(_ a: [String: [String]], _ b: [String: [String]]) -> String {
            var different = 0, missing = 0
            for (key, masks) in b { if let expected = a[key] { if Set(masks) != Set(expected) { different += 1 } } else { missing += 1 } }
            return "\(b.count) inputs: \(different) with a different mask, \(missing) not in the reference"
        }
        let serial1 = try await run(depth: 1)
        let serial2 = try await run(depth: 1)
        print("TALKSAVE determinism: serial run 2 vs serial run 1: \(compare(serial1, serial2))")
        for attempt in 0..<3 {
            let overlapped = try await run(depth: 3)
            print("TALKSAVE determinism: overlapped run \(attempt) vs serial run 1: \(compare(serial1, overlapped))")
        }
    }
}
