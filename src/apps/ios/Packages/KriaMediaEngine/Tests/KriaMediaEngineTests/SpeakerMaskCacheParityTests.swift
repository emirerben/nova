#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
@testable import KriaMediaEngine

/// End to end, on the Mac with the real Vision segmenter and the app's real talking-head poster clip: a Talking edit
/// with a speaker background is exported uncached twice (the noise floor of Vision plus the encoder), then with an
/// empty cache (every frame a miss) and again with the warm cache (every frame a hit). Mac numbers, not iPhone ones.
final class SpeakerMaskCacheParityTests: XCTestCase {
    private final class EventCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var counts: [String: Int] = [:]
        func add(_ name: String) { lock.lock(); counts[name, default: 0] += 1; lock.unlock() }
        var failedFrames: Int { lock.lock(); defer { lock.unlock() }; return (counts["talk.matte.held"] ?? 0) + (counts["talk.matte.original"] ?? 0) }
    }

    private func seconds(_ duration: Duration) -> Double { Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18 }

    @MainActor func testCachedRenderMatchesUncachedFramesAndIsServedFromTheCache() async throws {
        let directory = try speakerTestDirectory(self)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let clip = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/web/public/plan/type-posters/talking.mp4")
        let length = try await AVURLAsset(url: clip).load(.duration).seconds.rounded(.down)
        let background = try solidSpeakerTestImage(directory: directory, size: (1080, 1920))
        let scene = try speakerRecipe(canvas: Canvas(width: 1080, height: 1920), speaker: clip, duration: length,
                                      background: SpeakerTestBackground(url: background))
        let times = stride(from: 0.2, to: length - 0.2, by: (length - 0.4) / 7).map { $0 }

        // Vision can fail on a loaded Mac; the matte then holds or drops the mask, which changes pixels with or without a
        // cache. Counting those events tells a failed frame from a cache difference.
        let events = EventCounter()
        EngineSignpost.observer = { name, _, _ in events.add(name) }
        addTeardownBlock { EngineSignpost.observer = nil }
        func render(_ name: String) async throws -> (frames: [SpeakerTestFrame], seconds: Double, failedFrames: Int) {
            let before = events.failedFrames
            let start = ContinuousClock.now
            let file = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: name)
            let elapsed = seconds(start.duration(to: .now))
            var frames: [SpeakerTestFrame] = []
            for time in times { frames.append(try await fileFrame(file, at: time)) }
            return (frames, elapsed, events.failedFrames - before)
        }
        func worst(_ a: [SpeakerTestFrame], _ b: [SpeakerTestFrame]) -> (maxDelta: Int, meanDelta: Double, differing: Double) {
            let all = zip(a, b).map { $0.difference(from: $1) }
            return (all.map(\.maxDelta).max()!, all.map(\.meanDelta).max()!, all.map(\.differingFraction).max()!)
        }

        // KRIA_MASK_CACHE_RUNS repeats the timed renders; a loaded Mac makes a single timing meaningless.
        let runs = max(1, Int(ProcessInfo.processInfo.environment["KRIA_MASK_CACHE_RUNS"] ?? "") ?? 1)

        SpeakerMaskStore.installed = nil
        var uncached: [(frames: [SpeakerTestFrame], seconds: Double, failedFrames: Int)] = []
        for run in 0..<max(2, runs) { uncached.append(try await render("uncached-\(run)")) }
        let first = uncached[0], second = uncached[1]
        let noise = worst(first.frames, second.frames)
        print("MASKCACHE mac noise floor (uncached vs uncached, \(times.count) exported frames): max channel delta \(noise.maxDelta), worst mean delta \(String(format: "%.5f", noise.meanDelta)), worst differing-pixel fraction \(String(format: "%.5f", noise.differing))")

        let store = try XCTUnwrap(SpeakerMaskStore(root: directory.appendingPathComponent("masks")))
        SpeakerMaskStore.installed = store
        addTeardownBlock { SpeakerMaskStore.installed = nil }
        let miss = try await render("cache-miss")
        store.flush()
        let afterMiss = store.statistics
        var warm: [(frames: [SpeakerTestFrame], seconds: Double, failedFrames: Int)] = []
        for run in 0..<runs { warm.append(try await render("cache-hit-\(run)")) }
        let afterHit = store.statistics

        let hits = afterHit.hits - afterMiss.hits, misses = afterHit.misses - afterMiss.misses
        let frameCount = Int(length * 30)
        print("MASKCACHE mac vision failures (held or original frames) per render: uncached \(uncached.map(\.failedFrames)), miss \(miss.failedFrames), hit \(warm.map(\.failedFrames))")
        func summary(_ values: [Double]) -> String { "min \(String(format: "%.2f", values.min()!)) s, median \(String(format: "%.2f", values.sorted()[values.count / 2])) s of \(values.count)" }
        print("MASKCACHE mac speed (\(frameCount) frames): uncached \(summary(uncached.map(\.seconds))); cache miss \(String(format: "%.2f", miss.seconds)) s; cache hit \(summary(warm.map(\.seconds)))")
        print("MASKCACHE mac cache: miss run stored \(afterMiss.stored) masks, \(store.diskBytes()) bytes (\(store.diskBytes() / Int64(max(1, afterMiss.stored))) per mask, \(Double(store.diskBytes()) / length * 60 / 1_048_576) MB per minute of this clip); \(hits) hits and \(misses) misses over \(runs) warm run(s)")

        XCTAssertEqual(misses, 0, "the warm renders asked Vision for nothing")
        XCTAssertEqual(hits, frameCount * runs, "every frame of every warm render was served from the cache")
        XCTAssertEqual(afterMiss.droppedWrites, 0)
        XCTAssertEqual(afterMiss.corruptEntries + afterHit.corruptEntries, 0)

        // The cache property, exact: a warm render replays the very masks the cold render stored, so the two are identical
        // whatever Vision does from run to run. The export itself is not deterministic on a heavily loaded Mac (two
        // uncached renders then differ by tens of levels, which the noise-floor line shows), so a warm render that differs is
        // rendered again. A real cache fault differs on every replay; load noise does not.
        var hit = warm[0]
        var hitAgainstMiss = worst(hit.frames, miss.frames)
        var replayDeltas = [hitAgainstMiss.maxDelta]
        while hitAgainstMiss.maxDelta != 0, replayDeltas.count < 3 {
            hit = try await render("cache-hit-replay-\(replayDeltas.count)")
            hitAgainstMiss = worst(hit.frames, miss.frames)
            replayDeltas.append(hitAgainstMiss.maxDelta)
        }
        print("MASKCACHE mac replay: cache-hit vs cache-miss render max delta per warm render \(replayDeltas), worst differing fraction of the last \(String(format: "%.5f", hitAgainstMiss.differing))")
        XCTAssertEqual(hitAgainstMiss.maxDelta, 0, "a hit replays the stored mask byte for byte (deltas per warm render: \(replayDeltas))")
        XCTAssertEqual(hit.failedFrames, miss.failedFrames, "a hit holds or drops exactly the frames the cold render did")

        // The cache against no cache is Vision's own run-to-run noise plus nothing: reported, not asserted. One earlier run
        // on a heavily loaded Mac (load average above 40) saw both cached renders differ from the uncached pair by up to
        // 42 levels while the uncached pair matched each other. It was the export, not the cache: at load average 25 to 40 a later
        // run had the two uncached renders differ by 46 levels with no store installed.
        let missAgainstUncached = worst(miss.frames, first.frames), hitAgainstUncached = worst(hit.frames, first.frames)
        print("MASKCACHE mac parity: cache-miss vs uncached max delta \(missAgainstUncached.maxDelta), cache-hit vs uncached max delta \(hitAgainstUncached.maxDelta), worst mean \(String(format: "%.5f", hitAgainstUncached.meanDelta)), worst differing fraction \(String(format: "%.5f", hitAgainstUncached.differing)), uncached reference failures \(first.failedFrames)/\(second.failedFrames)")
    }

    /// Where a cache hit's time goes, per frame, with the real segmenter on a real talking-head frame (Mac, 1080x1920 canvas).
    func testPerFrameCostBreakdown() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "speaker-matte-talking", withExtension: "jpg", subdirectory: "Fixtures"))
        let canvas = CGRect(x: 0, y: 0, width: 1080, height: 1920)
        let photo = try XCTUnwrap(CIImage(contentsOf: url))
        let scale = max(canvas.width / photo.extent.width, canvas.height / photo.extent.height)
        let frame = photo.transformed(by: CGAffineTransform(scaleX: scale, y: scale)).cropped(to: canvas)
        let config = SpeakerMatteConfig.shipping
        let vision = VisionPersonSegmenter()
        let store = try XCTUnwrap(SpeakerMaskStore(root: try speakerTestDirectory(self).appendingPathComponent("masks")))
        let pack = try XCTUnwrap(store.pack(sources: ["bench"]))
        let cached = CachingPersonSegmenter(base: vision, pack: pack, environment: store.environment)

        func p50<T>(_ count: Int = 25, _ body: () throws -> T) rethrows -> (ms: Double, last: T) {
            var times: [Double] = [], last: T?
            for _ in 0..<count {
                let start = ContinuousClock.now
                last = try body()
                times.append(seconds(start.duration(to: .now)) * 1000)
            }
            return (times.sorted()[times.count / 2], last!)
        }
        let input = p50 { vision.segmentationInput(for: frame, canvas: canvas, config: config)! }
        let key = p50 { SpeakerMaskKey(input: input.last, config: config, environment: store.environment)! }
        let segment = try p50 { try XCTUnwrap(vision.segmentationMask(forInput: input.last, config: config)) }
        _ = try cached.personMask(for: frame, canvas: canvas, config: config)
        store.flush()
        let lookup = p50 { pack.lookup(key.last)! }
        let hitCall = try p50 { try XCTUnwrap(cached.personMask(for: frame, canvas: canvas, config: config)) }
        let missBytes = try XCTUnwrap(SpeakerMaskPixels(segment.last)).bytes, hitBytes = try XCTUnwrap(SpeakerMaskPixels(hitCall.last)).bytes
        XCTAssertEqual(missBytes, hitBytes, "a hit is Vision's mask for these pixels, byte for byte")
        print(String(format: "MASKCACHE mac per-frame p50: render Vision input %.1f ms, hash it %.1f ms, Vision %.1f ms, cache read+decode %.1f ms; whole personMask on a hit %.1f ms (uncached = input + Vision = %.1f ms); entry %d bytes",
                     input.ms, key.ms, segment.ms, lookup.ms, hitCall.ms, input.ms + segment.ms, SpeakerMaskPixels(segment.last)!.encoded()!.count))
    }
}
#endif
