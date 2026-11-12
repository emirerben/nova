#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
import CryptoKit
@testable import KriaMediaEngine

/// An export keeps a few frames in flight (`RecipeFramePipeline`). Failure modes, from the design:
///  P1 the matte's hold and clip-cut decisions follow frame order, so a frame whose mask fails holds the PREVIOUS frame's answer;
///  P2 text, drawings and motion stores follow frame order;
///  P3 a frame that bails out early must not stall the ones behind it;
///  P4 a frame that draws more than one matte (split panels) draws alone;
///  P5 a segmenter that cannot take overlapping calls is never called concurrently;
///  P6 one frame at a time (`ExportFrameOverlap.framesInFlight == 1`) is the old behaviour, frame for frame, and the exact old path;
///  P7 a failed or cancelled export ends promptly with frames in flight and leaves the next export working.
final class ExportFramePipelineTests: XCTestCase {
    /// P1, P2, P3: with random thread timing every gate passes frames in number order, frames that skip a stage still let
    /// the later ones through, and an exclusive frame runs with nothing else in flight.
    func testGatesPassFramesInOrderAndNeverStall() async throws {
        let pipeline = RecipeFramePipeline(depth: 3)
        let log = NSLock()
        nonisolated(unsafe) var matteOrder: [Int] = [], restOrder: [Int] = []
        nonisolated(unsafe) var running = 0, runningDuringExclusive = 0
        let frames = 80
        let issued = (0..<frames).map { pipeline.issue(exclusive: $0 % 17 == 5) }
        let workers = DispatchQueue(label: "pipeline-test", attributes: .concurrent)
        let done = expectation(description: "all frames finished")
        done.expectedFulfillmentCount = frames
        for frame in issued {
            workers.async {
                frame.admit()
                log.lock(); running += 1
                if frame.number % 17 == 5 { runningDuringExclusive = max(runningDuringExclusive, running) }
                log.unlock()
                usleep(UInt32.random(in: 0...3000))
                if frame.number % 4 != 0 {
                    frame.ordered { log.lock(); matteOrder.append(frame.number); log.unlock() }
                }
                usleep(UInt32.random(in: 0...3000))
                if frame.number % 9 != 1 {
                    frame.beginRest()
                    log.lock(); restOrder.append(frame.number); log.unlock()
                }
                log.lock(); running -= 1; log.unlock()
                frame.finish()
                done.fulfill()
            }
        }
        await fulfillment(of: [done], timeout: 20)
        XCTAssertEqual(matteOrder, matteOrder.sorted(), "matte stages ran in frame order")
        XCTAssertEqual(restOrder, restOrder.sorted(), "rest stages ran in frame order")
        XCTAssertEqual(matteOrder.count, (0..<frames).filter { $0 % 4 != 0 }.count)
        XCTAssertEqual(restOrder.count, (0..<frames).filter { $0 % 9 != 1 }.count)
        XCTAssertEqual(runningDuringExclusive, 1, "an exclusive frame ran alone")
    }

    // MARK: export parity

    /// Puts every process-wide export setting back when the test ends.
    private func restoreExportSettings(_ test: XCTestCase) {
        test.addTeardownBlock {
            EngineSignpost.observer = nil
            ExportFrameOverlap.framesInFlight = ExportFrameOverlap.defaultFramesInFlight
            RecipeWriter.frameTap = nil
            RecipeVideoCompositor.frameFault = nil
        }
    }

    private func observeEvents(_ test: XCTestCase) -> EngineEventCounts {
        restoreExportSettings(test)
        let events = EngineEventCounts()
        EngineSignpost.observer = { name, _, _ in events.add(name) }
        return events
    }

    private struct Rendered { let frames: [String]; let events: [String: Int]; let highWater: Int }

    @MainActor private func render(_ scene: (recipe: EditRecipe, urls: [String: URL]), directory: URL, events: EngineEventCounts,
                                   depth: Int, overlapping: Bool = true, delayMicroseconds: UInt32 = 6000, name: String) async throws -> Rendered {
        ExportFrameOverlap.framesInFlight = depth
        let segmenter = MarkerSegmenter(overlapping: overlapping, delayMicroseconds: delayMicroseconds)
        let previous = SpeakerMatte.segmenterFactory
        SpeakerMatte.segmenterFactory = { segmenter }
        defer { SpeakerMatte.segmenterFactory = previous }
        let before = events.snapshot
        let composed = ComposedFrames()
        composed.install()
        defer { RecipeWriter.frameTap = nil }
        _ = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: name)
        let delta = events.snapshot.reduce(into: [String: Int]()) { $0[$1.key] = $1.value - (before[$1.key] ?? 0) }
        return Rendered(frames: composed.hashes, events: delta.filter { $0.key.hasPrefix("talk.matte.") && $0.value != 0 }, highWater: segmenter.highWater)
    }

    /// P6: depth 1 hands the export the player's composition itself, and deeper depths a copy that overlaps frames while the
    /// player keeps the serial compositor.
    @MainActor func testDepthOneIsTheUntouchedPlayerComposition() async throws {
        let directory = try speakerTestDirectory(self)
        let speaker = try await writeMarkedSpeakerClip(directory: directory, size: (144, 256), seconds: 1)
        let scene = try speakerRecipe(canvas: Canvas(width: 144, height: 256), speaker: speaker, duration: 1,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        let preview = try await AVPlayerPreviewComposer(branding: .standard).makePreview(recipe: scene.recipe, assetURLs: scene.urls)
        let player = try XCTUnwrap(preview.playerItem.videoComposition)
        restoreExportSettings(self)

        ExportFrameOverlap.framesInFlight = 1
        XCTAssertTrue(RecipeWriter.exportComposition(player) === player, "depth 1 is the old path: no copy, no class swap")
        for depth in 2...4 {
            ExportFrameOverlap.framesInFlight = depth
            let export = try XCTUnwrap(RecipeWriter.exportComposition(player))
            XCTAssertTrue(export.customVideoCompositorClass == RecipeExportVideoCompositor.self)
            XCTAssertEqual(export.frameDuration, player.frameDuration)
            XCTAssertEqual(export.renderSize, player.renderSize)
            XCTAssertEqual(export.instructions.count, player.instructions.count)
            XCTAssertTrue(zip(export.instructions, player.instructions).allSatisfy { ($0 as AnyObject) === ($1 as AnyObject) })
        }
        XCTAssertTrue(player.customVideoCompositorClass == RecipeVideoCompositor.self, "the player keeps the serial compositor")

        ExportFrameOverlap.framesInFlight = 99
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, ExportFrameOverlap.range.upperBound)
        ExportFrameOverlap.framesInFlight = -3
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 1)
        XCTAssertEqual(ExportFrameOverlap.defaultFramesInFlight, 2)
    }

    /// P1 and P6, end to end: a speaker export with failing, unreadable and long-failing stretches composes every frame the
    /// same at the default depth and at a deeper one as with one frame at a time, and takes the same held and dropped decisions.
    @MainActor func testOverlappedExportMatchesOneFrameAtATime() async throws {
        let directory = try speakerTestDirectory(self)
        let size = (144, 256)
        let seconds = 3.0
        // Frames 60..<80 outlast the half second a mask is held, so the matte goes back to the plain picture mid-stretch.
        let speaker = try await writeMarkedSpeakerClip(directory: directory, size: size, seconds: seconds,
                                                       failing: Set(20..<24).union(60..<80), unreadable: Set(31..<33))
        let scene = try speakerRecipe(canvas: Canvas(width: size.0, height: size.1), speaker: speaker, duration: seconds,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
        let events = observeEvents(self)

        let single = try await render(scene, directory: directory, events: events, depth: 1, name: "depth-1")
        XCTAssertEqual(single.frames.count, Int(seconds * 30))
        XCTAssertEqual(single.highWater, 1, "one frame at a time never overlaps Vision")
        XCTAssertGreaterThan(single.events["talk.matte.held", default: 0], 0, "the clip has failing frames the matte holds")
        XCTAssertGreaterThan(single.events["talk.matte.original", default: 0], 0, "and one that outlasts the hold")

        let overlapped = try await render(scene, directory: directory, events: events, depth: ExportFrameOverlap.defaultFramesInFlight, name: "default")
        XCTAssertGreaterThanOrEqual(overlapped.highWater, 2, "the pipelined export did run two Vision calls together")
        let deeper = try await render(scene, directory: directory, events: events, depth: 4, name: "depth-4")
        let serialSegmenter = try await render(scene, directory: directory, events: events, depth: 3, overlapping: false, name: "no-overlap")
        XCTAssertEqual(serialSegmenter.highWater, 1, "P5: a segmenter that does not allow overlap is never called concurrently")
        for (name, candidate) in [("default depth", overlapped), ("depth 4", deeper), ("non-overlapping segmenter", serialSegmenter)] {
            XCTAssertEqual(candidate.events, single.events, "\(name): the same frames were held or dropped")
            XCTAssertEqual(candidate.frames, single.frames, "\(name): every composed frame is the one-at-a-time frame")
        }
    }

    /// Two speaker clips with a crossfade cut, failing and unreadable frames on both sides of the cut, a full-screen window
    /// and clip text: the matte's clip-cut and hold decisions and the text stores follow frame order at every depth.
    @MainActor func testCutTransitionFullScreenWindowAndTextMatchOneFrameAtATime() async throws {
        let directory = try speakerTestDirectory(self)
        let size = (144, 256)
        let speaker = try await writeMarkedSpeakerClip(directory: directory, size: size, seconds: 4,
                                                       failing: Set(50..<58).union(62..<66).union(100..<104), unreadable: Set(70..<72))
        var scene = try speakerRecipe(canvas: Canvas(width: size.0, height: size.1), speaker: speaker, duration: 2,
                                      background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)),
                                      fullScreenWindows: [SpeakerFullScreenWindow(start: 2.9, end: 3.4)])
        scene.recipe.tracks[0].clips = [
            TimelineClip(id: "clip-0", sourceAssetID: "speaker", sourceStart: 0, sourceDuration: 2, timelineStart: 0,
                         text: TextTreatment(text: "hello there", fontSize: 18)),
            TimelineClip(id: "clip-1", sourceAssetID: "speaker", sourceStart: 2, sourceDuration: 2, timelineStart: 1.8,
                         transition: Transition(kind: .crossfade, duration: 0.2), text: TextTreatment(text: "second", fontSize: 18, animation: .fade)),
        ]
        try scene.recipe.validate()
        let events = observeEvents(self)

        let single = try await render(scene, directory: directory, events: events, depth: 1, name: "cut-1")
        XCTAssertEqual(single.frames.count, 114)
        XCTAssertGreaterThan(single.events["talk.matte.held", default: 0], 0)
        for (attempt, depth) in [ExportFrameOverlap.defaultFramesInFlight, ExportFrameOverlap.defaultFramesInFlight, 3].enumerated() {
            // Random Vision latency changes which frame finishes first; the output must not notice.
            let candidate = try await render(scene, directory: directory, events: events, depth: depth,
                                             delayMicroseconds: UInt32.random(in: 2000...9000), name: "cut-\(attempt)-\(depth)")
            let differing = zip(candidate.frames, single.frames).enumerated().filter { $0.element.0 != $0.element.1 }.map(\.offset)
            XCTAssertEqual(candidate.frames.count, single.frames.count, "depth \(depth) attempt \(attempt)")
            XCTAssertEqual(differing, [], "depth \(depth) attempt \(attempt): composed frames that differ from the one-at-a-time export")
            XCTAssertEqual(candidate.events, single.events, "depth \(depth) attempt \(attempt)")
        }
    }

    private struct Exported: Error { let error: Error; let seconds: Double }

    @MainActor private func exportMeasuring(_ scene: (recipe: EditRecipe, urls: [String: URL]), directory: URL, name: String,
                                            cancelAfter: Duration? = nil, watchdog: Duration = .seconds(30)) async -> Result<Double, Exported> {
        let start = ContinuousClock.now
        let task = Task { @MainActor in
            try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-\(name)")), branding: .none)
                .export(recipe: scene.recipe, assetURLs: scene.urls, outputURL: directory.appendingPathComponent("\(name).mp4"))
        }
        let timer = Task { try await Task.sleep(for: watchdog); task.cancel() }
        var cancelledAt: ContinuousClock.Instant?
        if let cancelAfter {
            try? await Task.sleep(for: cancelAfter)
            cancelledAt = .now
            task.cancel()
        }
        defer { timer.cancel() }
        func seconds(_ d: Duration) -> Double { Double(d.components.seconds) + Double(d.components.attoseconds) / 1e18 }
        do { _ = try await task.value; return .success(seconds(start.duration(to: .now))) }
        catch { return .failure(Exported(error: error, seconds: seconds((cancelledAt ?? start).duration(to: .now)))) }
    }

    @MainActor private func plainSpeakerScene(directory: URL, seconds: Double) async throws -> (recipe: EditRecipe, urls: [String: URL]) {
        let speaker = try await writeMarkedSpeakerClip(directory: directory, name: "plain", size: (144, 256), seconds: seconds)
        return try speakerRecipe(canvas: Canvas(width: 144, height: 256), speaker: speaker, duration: seconds,
                                 background: SpeakerTestBackground(url: try solidSpeakerTestImage(directory: directory)))
    }

    /// P3 and P7 end to end: a frame that fails mid-export, wherever it falls in the pipeline, fails the export promptly with
    /// that frame's error (the watchdog would cancel a hang and the test would see CancellationError instead), and the next
    /// export is unharmed.
    @MainActor func testFailedFrameMidExportFailsPromptly() async throws {
        let directory = try speakerTestDirectory(self)
        let scene = try await plainSpeakerScene(directory: directory, seconds: 4)
        restoreExportSettings(self)
        let segmenter = MarkerSegmenter(overlapping: true, delayMicroseconds: 8000)
        useSpeakerSegmenter(self, segmenter)
        struct Injected: Error {}
        for depth in [ExportFrameOverlap.defaultFramesInFlight, 4] {
            ExportFrameOverlap.framesInFlight = depth
            for failAt in [0, 1, 37, 80, 119] {
                RecipeVideoCompositor.frameFault = { time in Int((time * 30).rounded()) == failAt ? Injected() : nil }
                switch await exportMeasuring(scene, directory: directory, name: "fail-\(depth)-\(failAt)") {
                case .success: XCTFail("depth \(depth), frame \(failAt): the export completed past a failed frame")
                case .failure(let failure):
                    XCTAssertFalse(failure.error is CancellationError, "depth \(depth), frame \(failAt): hung until the watchdog")
                    XCTAssertLessThan(failure.seconds, 15, "depth \(depth), frame \(failAt)")
                }
            }
        }
        RecipeVideoCompositor.frameFault = nil
        _ = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "after-failures")
    }

    /// P7: cancelling an overlapped export while frames are in flight returns CancellationError promptly (the cancel barrier
    /// waits for every in-flight frame), and the next export succeeds.
    @MainActor func testCancelMidExportReturnsPromptlyAndNextExportSucceeds() async throws {
        let directory = try speakerTestDirectory(self)
        let scene = try await plainSpeakerScene(directory: directory, seconds: 6)
        restoreExportSettings(self)
        useSpeakerSegmenter(self, MarkerSegmenter(overlapping: true, delayMicroseconds: 30_000))
        ExportFrameOverlap.framesInFlight = ExportFrameOverlap.defaultFramesInFlight
        for round in 0..<4 {
            switch await exportMeasuring(scene, directory: directory, name: "cancel-\(round)", cancelAfter: .milliseconds(300 + 137 * round)) {
            case .success: XCTFail("round \(round): expected cancellation")
            case .failure(let failure):
                XCTAssertTrue(failure.error is CancellationError, "round \(round): threw \(failure.error)")
                XCTAssertLessThan(failure.seconds, 10, "round \(round): seconds from cancel to return")
            }
        }
        let after = try await exportSpeakerRecipe(scene.recipe, urls: scene.urls, directory: directory, name: "after-cancel")
        XCTAssertTrue(FileManager.default.fileExists(atPath: after.path))
    }
}
#endif
