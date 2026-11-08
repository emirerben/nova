#if canImport(AVFoundation)
import Foundation
import XCTest
import CoreImage
import CryptoKit
@testable import KriaMediaEngine

/// The cache's failure modes (`docs/reviews/speaker-mask-cache-failure-modes.md`, rows M1 to M15), driven through the
/// segmenter seam with a stand-in for Vision whose answers the test controls. The real-Vision end-to-end check is
/// `SpeakerMaskCacheParityTests`.
@MainActor final class SpeakerMaskCacheTests: XCTestCase {
    private let canvas = CGRect(x: 0, y: 0, width: 120, height: 200)
    private var frame: CIImage { CIImage(color: .white).cropped(to: canvas) }

    /// Vision's stand-in: the input is a 64x64 BGRA buffer filled with `fill`; the answer is `answer(callNumber)`.
    private final class Stub: PixelSegmenting, @unchecked Sendable {
        var fill: UInt8 = 10
        var answer: (Int) throws -> CVPixelBuffer? = { call in SpeakerMaskCacheTests.makeMask(seed: call) }
        private(set) var calls = 0
        private(set) var lastAnswer: CVPixelBuffer?
        func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? { nil }
        func segmentationInput(for frame: CIImage, canvas: CGRect, config: SpeakerMatteConfig) -> CVPixelBuffer? {
            var buffer: CVPixelBuffer?
            CVPixelBufferCreate(nil, 64, 64, kCVPixelFormatType_32BGRA, nil, &buffer)
            CVPixelBufferLockBaseAddress(buffer!, [])
            memset(CVPixelBufferGetBaseAddress(buffer!), Int32(fill), CVPixelBufferGetBytesPerRow(buffer!) * 64)
            CVPixelBufferUnlockBaseAddress(buffer!, [])
            return buffer
        }
        func segmentationMask(forInput input: CVPixelBuffer, config: SpeakerMatteConfig) throws -> CVPixelBuffer? {
            calls += 1
            lastAnswer = try answer(calls)
            return lastAnswer
        }
    }

    private struct VisionFailure: Error {}

    /// A person-shaped mask of an odd width, so rows are padded in memory, with `seed` moving the person.
    private nonisolated static func makeMask(seed: Int, width: Int = 100, height: Int = 80, format: OSType = kCVPixelFormatType_OneComponent8) -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        CVPixelBufferCreate(nil, width, height, format, [kCVPixelBufferIOSurfacePropertiesKey as String: [String: String]()] as CFDictionary, &buffer)
        CVPixelBufferLockBaseAddress(buffer!, [])
        let base = CVPixelBufferGetBaseAddress(buffer!)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(buffer!)
        memset(base, 0xEE, stride * height)
        if format == kCVPixelFormatType_OneComponent8 {
            for y in 0..<height { for x in 0..<width { base[y * stride + x] = (x + seed * 7) % width < width / 2 ? 255 : UInt8((x * y) % 40) } }
        }
        CVPixelBufferUnlockBaseAddress(buffer!, [])
        return buffer!
    }

    private func makeStore(_ name: String = "masks", limits: SpeakerMaskStore.Limits = .init(), root: URL? = nil,
                           environment: SpeakerMaskEnvironment = .current) throws -> (store: SpeakerMaskStore, root: URL) {
        let directory = try root ?? speakerTestDirectory(self).appendingPathComponent(name)
        return (try XCTUnwrap(SpeakerMaskStore(root: directory, limits: limits, environment: environment)), directory)
    }

    private func caching(_ stub: Stub, _ store: SpeakerMaskStore, sources: [String] = ["source-a"]) throws -> CachingPersonSegmenter {
        CachingPersonSegmenter(base: stub, pack: try XCTUnwrap(store.pack(sources: sources)), environment: store.environment)
    }

    private func bytes(_ buffer: CVPixelBuffer?) throws -> Data { try XCTUnwrap(SpeakerMaskPixels(try XCTUnwrap(buffer))).bytes }

    private func files(under root: URL, suffix: String) -> [URL] {
        (FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? []).filter { $0.lastPathComponent.hasSuffix(suffix) }
    }

    /// M8: a hit is the bytes Vision answered (compression is lossless, padding is irrelevant), a miss is Vision's own buffer.
    func testAHitReturnsTheBytesVisionAnswered() throws {
        let (store, _) = try makeStore()
        let stub = Stub()
        let segmenter = try caching(stub, store)
        let answered = try XCTUnwrap(segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
        XCTAssertTrue(answered === stub.lastAnswer, "a miss hands the matte Vision's buffer itself")
        store.flush()
        let hit = try XCTUnwrap(segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
        XCTAssertEqual(stub.calls, 1, "the second frame never reached Vision")
        XCTAssertEqual(try bytes(hit), try bytes(answered))
        XCTAssertEqual(CVPixelBufferGetPixelFormatType(hit), kCVPixelFormatType_OneComponent8)
        XCTAssertEqual([CVPixelBufferGetWidth(hit), CVPixelBufferGetHeight(hit)], [100, 80])
    }

    /// M1: the key is the pixels, never a name, a time or a clip.
    func testDifferentPixelsNeverHitAnEarlierEntry() throws {
        let (store, _) = try makeStore()
        let stub = Stub()
        let segmenter = try caching(stub, store)
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        store.flush()
        stub.fill = 11
        let other = try XCTUnwrap(segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
        XCTAssertEqual(stub.calls, 2, "one changed pixel value is a different frame")
        stub.fill = 10
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        XCTAssertEqual(stub.calls, 2, "the original pixels still hit")
        var accurate = SpeakerMatteConfig.shipping
        accurate.quality = .accurate
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: accurate)
        XCTAssertEqual(stub.calls, 3, "another quality is another answer for the same pixels")
        XCTAssertNotNil(other)
    }

    /// M2: only a real Vision answer is stored; a failure reaches the matte exactly as it always did.
    func testAFailedSegmentationIsNeverStored() throws {
        let (store, root) = try makeStore()
        let stub = Stub()
        let segmenter = try caching(stub, store)
        stub.answer = { _ in nil }
        XCTAssertNil(try segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
        stub.answer = { _ in throw VisionFailure() }
        XCTAssertThrowsError(try segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
        stub.answer = { _ in Self.makeMask(seed: 1, format: kCVPixelFormatType_32BGRA) }
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        stub.answer = { _ in Self.makeMask(seed: 1, width: 9000, height: 2) }
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        store.flush()
        XCTAssertEqual(files(under: root, suffix: ".mask").count, 0, "nothing but a normal mask is written")
        stub.answer = { _ in Self.makeMask(seed: 2) }
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        store.flush()
        XCTAssertEqual(stub.calls, 5, "every failed frame asked Vision again; none was remembered")
        XCTAssertEqual(files(under: root, suffix: ".mask").count, 1)
    }

    /// M3: export and preview write the same pack at once.
    func testConcurrentWritersOfOneKeyLeaveOneValidEntry() throws {
        let (store, root) = try makeStore()
        let stubs = (0..<4).map { _ in Stub() }
        for stub in stubs { stub.answer = { _ in Self.makeMask(seed: 3) } }
        let segmenters = try stubs.map { try caching($0, store) }
        let frame = self.frame, canvas = self.canvas
        DispatchQueue.concurrentPerform(iterations: 64) { index in
            _ = try? segmenters[index % segmenters.count].personMask(for: frame, canvas: canvas, config: .shipping)
        }
        store.flush()
        XCTAssertEqual(files(under: root, suffix: ".partial").count, 0)
        let entries = files(under: root, suffix: ".mask")
        XCTAssertEqual(entries.count, 1)
        XCTAssertNotNil(SpeakerMaskPixels.decode(try Data(contentsOf: entries[0])), "the surviving file is whole")
        let reader = Stub()
        reader.answer = { _ in XCTFail("a warm key must not reach Vision"); return nil }
        XCTAssertEqual(try bytes(try caching(reader, store).personMask(for: frame, canvas: canvas, config: .shipping)), try bytes(Self.makeMask(seed: 3)))
    }

    /// M4: a file the OS left cut short, or damaged, is a miss that repairs itself.
    func testACorruptOrTruncatedEntryIsAMissAndIsRepaired() throws {
        let (store, root) = try makeStore()
        let stub = Stub()
        let segmenter = try caching(stub, store)
        _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
        store.flush()
        let entry = try XCTUnwrap(files(under: root, suffix: ".mask").first)
        let whole = try Data(contentsOf: entry)
        var flipped = whole
        flipped[flipped.count - 3] ^= 0xFF
        // A whole, decodable payload of another mask under this mask's header: only the content digest can tell.
        let swapped = whole.prefix(28) + (try XCTUnwrap(SpeakerMaskPixels(Self.makeMask(seed: 9))?.encoded())).dropFirst(28)
        let damages: [Data] = [Data(), whole.prefix(whole.count / 2), flipped, Data("not a mask".utf8), Data(swapped)]
        for damage in damages {
            try Data(damage).write(to: entry)
            let before = stub.calls
            XCTAssertNotNil(try segmenter.personMask(for: frame, canvas: canvas, config: .shipping))
            XCTAssertEqual(stub.calls, before + 1, "damage of \(damage.count) bytes was not served")
            store.flush()
            XCTAssertNotNil(SpeakerMaskPixels.decode(try Data(contentsOf: entry)), "the entry was rewritten whole")
        }
        XCTAssertEqual(store.statistics.corruptEntries, damages.count)
    }

    /// M4: an app kill mid-write leaves a `.partial`; launch removes the old ones and leaves a recent one (another writer's).
    func testStalePartialFilesAreSweptAtLaunch() throws {
        let (first, root) = try makeStore()
        let pack = try XCTUnwrap(first.pack(sources: ["source-a"]))
        let old = pack.directory.appendingPathComponent("old.partial"), fresh = pack.directory.appendingPathComponent("fresh.partial")
        for url in [old, fresh] { try Data("half".utf8).write(to: url) }
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: -7200)], ofItemAtPath: old.path)
        let (second, _) = try makeStore(root: root)
        second.flush()
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: fresh.path))
    }

    /// M5: the store stays under its cap by dropping the least recently used pack, not the newest.
    func testEvictionKeepsTheStoreUnderItsCapAndTheNewestPack() throws {
        var limits = SpeakerMaskStore.Limits()
        limits.protectedSeconds = 0
        limits.capBytes = 1
        let (probe, _) = try makeStore("probe")
        let stub = Stub()
        _ = try caching(stub, probe, sources: ["probe"]).personMask(for: frame, canvas: canvas, config: .shipping)
        probe.flush()
        let entrySize = probe.diskBytes()
        XCTAssertGreaterThan(entrySize, 0)

        limits.capBytes = entrySize * 2 + entrySize / 2
        let (store, root) = try makeStore(limits: limits)
        var packs: [URL] = []
        for (index, name) in ["oldest", "middle", "newest"].enumerated() {
            let pack = try XCTUnwrap(store.pack(sources: [name]))
            packs.append(pack.directory)
            let segmenter = CachingPersonSegmenter(base: stub, pack: pack, environment: store.environment)
            stub.fill = UInt8(20 + index)
            stub.answer = { _ in Self.makeMask(seed: index) }
            _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
            store.flush()
            try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: Double(index - 10) * 100)], ofItemAtPath: pack.directory.path)
        }
        // A fourth write crosses the cap and triggers the sweep.
        var tight = limits
        tight.trimEveryBytes = 1
        let (relaunched, _) = try makeStore(limits: tight, root: root)
        relaunched.flush()
        XCTAssertLessThanOrEqual(relaunched.diskBytes(), limits.capBytes)
        XCTAssertFalse(FileManager.default.fileExists(atPath: packs[0].path), "the least recently used pack went first")
        XCTAssertTrue(FileManager.default.fileExists(atPath: packs[2].path), "the newest pack stayed")
    }

    /// M5: when even the newest pack is over the cap, its oldest entries go, and the cap still holds.
    func testOneOversizedPackLosesItsOldestEntries() throws {
        var limits = SpeakerMaskStore.Limits()
        limits.protectedSeconds = 3600
        limits.trimEveryBytes = 1
        let (probe, _) = try makeStore("probe")
        let stub = Stub()
        _ = try caching(stub, probe).personMask(for: frame, canvas: canvas, config: .shipping)
        probe.flush()
        limits.capBytes = probe.diskBytes() * 3
        let (store, root) = try makeStore(limits: limits)
        let segmenter = try caching(stub, store)
        for index in 0..<10 {
            stub.fill = UInt8(40 + index)
            stub.answer = { _ in Self.makeMask(seed: index) }
            _ = try segmenter.personMask(for: frame, canvas: canvas, config: .shipping)
            store.flush()
        }
        store.flush()
        XCTAssertLessThanOrEqual(store.diskBytes(), limits.capBytes)
        XCTAssertGreaterThan(files(under: root, suffix: ".mask").count, 0)
    }

    /// M6: another OS build or Vision revision shares no entries and its old ones are removed.
    func testAnotherEnvironmentNeverHitsAndIsRemoved() throws {
        let earlier = SpeakerMaskEnvironment(osBuild: "23A1", hardware: "iPhone14,2", requestRevision: 1)
        let later = SpeakerMaskEnvironment(osBuild: "23B2", hardware: "iPhone14,2", requestRevision: 1)
        XCTAssertNotEqual(earlier.digest, later.digest)
        let (old, root) = try makeStore(environment: earlier)
        let stub = Stub()
        _ = try caching(stub, old).personMask(for: frame, canvas: canvas, config: .shipping)
        old.flush()
        XCTAssertEqual(files(under: root, suffix: ".mask").count, 1)

        let (updated, _) = try makeStore(root: root, environment: later)
        _ = try caching(stub, updated).personMask(for: frame, canvas: canvas, config: .shipping)
        XCTAssertEqual(stub.calls, 2, "the entry from the older OS build was not served")
        updated.flush()
        XCTAssertEqual(files(under: root, suffix: ".mask").map { $0.deletingLastPathComponent().deletingLastPathComponent().lastPathComponent }, [later.digest],
                       "only the current environment's directory is left")
    }

    /// M14: a root that cannot be created leaves Vision in charge; a pack removed under a writer costs only that write.
    func testAnUnwritableRootFallsBackToVision() throws {
        let directory = try speakerTestDirectory(self)
        let blocker = directory.appendingPathComponent("file")
        try Data("x".utf8).write(to: blocker)
        XCTAssertNil(SpeakerMaskStore(root: blocker.appendingPathComponent("masks"), limits: .init(), environment: .current))

        let (store, root) = try makeStore()
        let stub = Stub()
        let segmenter = try caching(stub, store)
        try FileManager.default.removeItem(at: root)
        XCTAssertNotNil(try segmenter.personMask(for: frame, canvas: canvas, config: .shipping), "Vision still answers")
        store.flush()
        XCTAssertEqual(stub.calls, 1)
        XCTAssertEqual(files(under: root, suffix: ".partial").count, 0)
    }

    /// M15 and the off path: no store, an unsplit segmenter or unknown footage is the same object as before.
    func testOffAndUnknownFootageKeepTheOldSegmenter() throws {
        let (store, _) = try makeStore()
        let stub = Stub()
        XCTAssertTrue(SpeakerMaskCaching.wrap(stub, store: nil, sources: ["a"]) as AnyObject === stub, "no store installed")
        XCTAssertTrue(SpeakerMaskCaching.wrap(stub, store: store, sources: nil) as AnyObject === stub, "footage with no fingerprint")
        XCTAssertTrue(SpeakerMaskCaching.wrap(stub, store: store, sources: []) as AnyObject === stub)
        let scripted = ScriptedSegmenter(map: nil)
        XCTAssertTrue(SpeakerMaskCaching.wrap(scripted, store: store, sources: ["a"]) as AnyObject === scripted, "a test double is never cached")
        XCTAssertNil(SpeakerMaskCaching.wrap(nil, store: store, sources: ["a"]))
        XCTAssertTrue(SpeakerMaskCaching.wrap(stub, store: store, sources: ["a"]) is CachingPersonSegmenter)
    }

    /// M15: the sources come from the recipe's manifest, and any speaker clip without one means no cache.
    func testARecipeWithoutAFingerprintGetsNoCache() throws {
        let directory = try speakerTestDirectory(self)
        let speaker = try solidSpeakerTestImage(directory: directory, name: "speaker", color: SpeakerColor.person)
        let background = try solidSpeakerTestImage(directory: directory)
        var recipe = try speakerRecipe(canvas: Canvas(width: 120, height: 200), speaker: speaker, duration: 1, background: SpeakerTestBackground(url: background)).recipe
        let ids = AVPlayerPreviewComposer.speakerClipIDs(in: recipe)
        XCTAssertEqual(ids, ["clip-0"])
        XCTAssertEqual(SpeakerMaskCaching.speakerSources(in: recipe, speakerClipIDs: ids), [try SHA256Fingerprinter().fingerprint(file: speaker).hex])
        recipe.assetManifest = nil
        XCTAssertNil(SpeakerMaskCaching.speakerSources(in: recipe, speakerClipIDs: ids))
        XCTAssertNil(SpeakerMaskCaching.speakerSources(in: recipe, speakerClipIDs: []))
    }

    /// M7: the cache sits below the matte, so a failed frame is held, counted and cleared the same on a cold and a warm run.
    func testTheMatteSeesNoDifferenceBetweenColdAndWarm() throws {
        let (store, _) = try makeStore()
        let stub = Stub()
        stub.answer = { _ in
            if stub.fill == 200 { throw VisionFailure() }
            return Self.makeMask(seed: Int(stub.fill))
        }
        func run() throws -> (summary: String, answers: [String]) {
            let matte = SpeakerMatte(segmenter: try caching(stub, store), config: .shipping)
            var answers: [String] = []
            // good, good, a Vision failure inside the 0.5 s hold, good, a failure after the hold window, a seek.
            for (index, step) in [(10, 0.0), (11, 0.04), (200, 0.08), (12, 0.12), (200, 0.9), (13, 5.0)].enumerated() {
                stub.fill = UInt8(step.0)
                switch matte.answer(for: frame, at: step.1, canvas: canvas, clipKey: "clip-0") {
                case .cutout: answers.append("\(index):cutout")
                case .backgroundOnly: answers.append("\(index):background")
                case .original: answers.append("\(index):original")
                }
            }
            return (matte.trace.snapshot().summary, answers)
        }
        let cold = try run()
        store.flush()
        let callsAfterCold = stub.calls
        let warm = try run()
        XCTAssertEqual(warm.answers, cold.answers)
        XCTAssertEqual(warm.summary.replacingOccurrences(of: #"@[0-9.]+s"#, with: "", options: .regularExpression),
                       cold.summary.replacingOccurrences(of: #"@[0-9.]+s"#, with: "", options: .regularExpression))
        XCTAssertEqual(stub.calls - callsAfterCold, 2, "only the two failed frames went back to Vision; a failure is never remembered")
        XCTAssertTrue(cold.answers.contains("2:cutout") && cold.answers.contains("4:original"), "the cold run exercised the hold and its end: \(cold.answers)")
    }
}
#endif
