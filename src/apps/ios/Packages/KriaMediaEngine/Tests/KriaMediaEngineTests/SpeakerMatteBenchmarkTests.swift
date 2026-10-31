#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// Opt-in benchmark. Run it optimized, as the app ships:
/// `KRIA_SPEAKER_MATTE_BENCH=1 swift test -c release -Xswiftc -enable-testing --filter SpeakerMatteBenchmarkTests`
/// (an unoptimized build inflates the coverage and fragment check about 30x). Exports the app's real
/// talking-head poster clip on a 1080x1920 canvas with and without a background, under each config the device
/// may ship, with the real Vision segmenter, and prints the added cost per frame next to the segmenter's own
/// time per call (the expected limiter). These are Mac numbers, not device ones.
final class SpeakerMatteBenchmarkTests: XCTestCase {
    @MainActor func testPerFrameCost() async throws {
        guard ProcessInfo.processInfo.environment["KRIA_SPEAKER_MATTE_BENCH"] != nil else { throw XCTSkip("opt-in benchmark") }
        let directory = try speakerTestDirectory(self)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let clip = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/web/public/plan/type-posters/talking.mp4")
        let seconds = try await AVURLAsset(url: clip).load(.duration).seconds.rounded(.down)
        let canvas = Canvas(width: 1080, height: 1920)
        let background = try solidSpeakerTestImage(directory: directory, size: (1080, 1920))
        let plain = try speakerRecipe(canvas: canvas, speaker: clip, duration: seconds, background: nil)
        let scene = try speakerRecipe(canvas: canvas, speaker: clip, duration: seconds, background: SpeakerTestBackground(url: background))
        let frames = seconds * 30
        let runs = 3

        func median(_ values: [Double]) -> Double { values.sorted()[values.count / 2] }
        func exportSeconds(_ recipe: EditRecipe, urls: [String: URL], name: String) async throws -> Double {
            let start = ContinuousClock.now
            _ = try await exportSpeakerRecipe(recipe, urls: urls, directory: directory, name: name)
            let elapsed = start.duration(to: .now)
            return Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18
        }

        var plainTimes: [Double] = []
        for run in 0..<runs { plainTimes.append(try await exportSeconds(plain.recipe, urls: plain.urls, name: "plain-\(run)")) }
        print("BENCH mac plain export: \(Int(frames)) frames, runs s=\(plainTimes.map { String(format: "%.2f", $0) })")

        let frame = try await previewFrameImage(plain.recipe, urls: plain.urls, at: seconds / 2)
        for (name, config) in speakerMatteConfigsUnderTest {
            useSpeakerConfig(self, config)
            var times: [Double] = []
            for run in 0..<runs { times.append(try await exportSeconds(scene.recipe, urls: scene.urls, name: "\(name.count)-\(run)")) }
            let added = (median(times) - median(plainTimes)) / frames * 1000

            let segmenter = VisionPersonSegmenter()
            let rect = CGRect(x: 0, y: 0, width: 1080, height: 1920)
            var vision: [Double] = []
            for _ in 0..<20 {
                let start = ContinuousClock.now
                _ = try segmenter.personMask(for: frame, canvas: rect, config: config)
                let elapsed = start.duration(to: .now)
                vision.append((Double(elapsed.components.seconds) + Double(elapsed.components.attoseconds) / 1e18) * 1000)
            }
            let segmentationsPerFrame = config.maskRate.map { $0 / 30 } ?? 1
            print(String(format: "BENCH mac %@: export runs s=%@, added per output frame %.1f ms; segmenter p50 %.1f ms p90 %.1f ms per call, %.2f calls per frame",
                         name, times.map { String(format: "%.2f", $0) }.joined(separator: ","), added,
                         median(vision), vision.sorted()[Int(Double(vision.count) * 0.9)], segmentationsPerFrame))
        }
    }

    @MainActor private func previewFrameImage(_ recipe: EditRecipe, urls: [String: URL], at seconds: Double) async throws -> CIImage {
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        return CIImage(cgImage: try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image)
    }
}
#endif
