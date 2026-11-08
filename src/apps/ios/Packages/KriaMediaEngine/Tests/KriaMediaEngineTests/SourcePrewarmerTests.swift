import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// What can go wrong when a take is hashed and transcoded before Use take, and each is pinned here:
/// the take is replaced or edited after the work started (a stale proxy or digest would be uploaded as the new take's);
/// Retake lands while the work is still running (it must stop and leave no file); the staged copy is a clone rather than
/// a link (the identity differs, so the work must not be reused); the work is claimed twice or started twice (duplicate
/// transcodes); the transcode fails or the process was killed (the upload must still do the work itself); the
/// importer is handed a digest for bytes it did not import. The export limit adds: two prewarms exporting at once, a
/// cancelled queued prewarm that keeps or leaks the slot, a claimant that holds the slot a queued prewarm waits for
/// (it would wait forever), and a failed export that never gives its slot back.
final class SourcePrewarmerTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("prewarm-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: root)
    }

    /// Stands in for the transcode: writes a file, can be held open, records starts and cancellation.
    private final class Probe: @unchecked Sendable {
        private let lock = NSLock()
        private var _started = 0, _cancelled = 0, _release = false
        var started: Int { lock.lock(); defer { lock.unlock() }; return _started }
        var cancelled: Int { lock.lock(); defer { lock.unlock() }; return _cancelled }
        func release() { lock.lock(); _release = true; lock.unlock() }
        private var released: Bool { lock.lock(); defer { lock.unlock() }; return _release }
        var holds = false
        var failure: Error?
        var editWhileRunning: URL?

        func make(source: URL, destination: URL) async throws -> URL {
            lock.withLock { _started += 1 }
            if let failure { throw failure }
            while holds && !released {
                if Task.isCancelled { lock.withLock { _cancelled += 1 }; throw CancellationError() }
                try? await Task.sleep(for: .milliseconds(5))
            }
            if let editWhileRunning { try Data("edited".utf8).write(to: editWhileRunning) }
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("proxy of \(source.lastPathComponent)".utf8).write(to: destination)
            return destination
        }
    }

    private func take(_ text: String = "take bytes", name: String = "take.mov") throws -> URL {
        let url = root.appendingPathComponent(name)
        try Data(text.utf8).write(to: url)
        return url
    }

    private func prewarmer(_ probe: Probe) -> SourcePrewarmer {
        SourcePrewarmer(directory: root.appendingPathComponent("prewarm", isDirectory: true)) { try await probe.make(source: $0, destination: $1) }
    }

    /// The app's one-at-a-time export limit, polling so a cancelled waiter leaves at once.
    private final class Slots: @unchecked Sendable {
        private let lock = NSLock()
        private var taken = 0
        func tryTake() -> Bool { lock.withLock { if taken == 0 { taken = 1; return true }; return false } }
        var isFree: Bool { lock.withLock { taken == 0 } }
        var admission: SourcePrewarmer.Admission {
            .init(acquire: { while !self.tryTake() { try Task.checkCancellation(); try await Task.sleep(for: .milliseconds(2)) } },
                  release: { self.lock.withLock { self.taken -= 1 } })
        }
    }

    private func gatedPrewarmer(_ probe: Probe, _ slots: Slots) -> SourcePrewarmer {
        SourcePrewarmer(directory: root.appendingPathComponent("prewarm", isDirectory: true), admission: slots.admission) { try await probe.make(source: $0, destination: $1) }
    }

    private func files(in directory: URL) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []).sorted()
    }

    // MARK: Identity

    func testAHardlinkIsTheSameFileAndACopyIsNot() throws {
        let original = try take()
        let link = root.appendingPathComponent("linked.mov"), copy = root.appendingPathComponent("copied.mov")
        try FileManager.default.linkItem(at: original, to: link)
        try FileManager.default.copyItem(at: original, to: copy)
        XCTAssertEqual(try FileIdentity(of: link), try FileIdentity(of: original))
        XCTAssertNotEqual(try FileIdentity(of: copy), try FileIdentity(of: original), "a clone has its own inode: its bytes are not vouched for")
    }

    func testAnEditChangesTheIdentityEvenWhenTheSizeStaysTheSame() throws {
        let url = try take("aaaa")
        let before = try FileIdentity(of: url)
        usleep(20_000)
        let handle = try FileHandle(forWritingTo: url)
        try handle.write(contentsOf: Data("bbbb".utf8)); try handle.close()
        XCTAssertEqual(try FileIdentity(of: url).size, before.size)
        XCTAssertNotEqual(try FileIdentity(of: url), before)
    }

    // MARK: Claiming

    func testAClaimThroughAHardlinkedNameGetsTheHashAndTheProxy() async throws {
        let probe = Probe(), warm = prewarmer(probe)
        let original = try take("the creator's take")
        warm.start(key: UUID(), source: original)
        let staged = root.appendingPathComponent("KriaUploads-staged.mov")
        try FileManager.default.linkItem(at: original, to: staged)

        let claim = try XCTUnwrap(warm.claim(source: staged))
        let known = await claim.fingerprint()
        let proxy = await claim.takeProxy()
        XCTAssertEqual(known?.fingerprint, try SHA256Fingerprinter().fingerprint(file: original))
        XCTAssertEqual(known?.identity, try FileIdentity(of: staged))
        XCTAssertEqual(try String(contentsOf: try XCTUnwrap(proxy), encoding: .utf8), "proxy of take.mov")
    }

    func testACloneOfTheTakeMissesSoItsBytesAreHashedAgain() throws {
        let warm = prewarmer(Probe())
        let original = try take()
        warm.start(key: UUID(), source: original)
        let clone = root.appendingPathComponent("clone.mov")
        try FileManager.default.copyItem(at: original, to: clone)
        XCTAssertNil(warm.claim(source: clone))
        XCTAssertEqual(warm.heldCount, 1, "a miss leaves the real job alone")
    }

    func testATakeReplacedAtTheSamePathMissesAndTheOldWorkIsCancelled() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let key = UUID()
        let url = try take("first take")
        warm.start(key: key, source: url)
        try await waitFor { probe.started == 1 }

        try FileManager.default.removeItem(at: url)
        try Data("second take, longer".utf8).write(to: url)
        XCTAssertNil(warm.claim(source: url), "the file at this path is not the one that was prewarmed")

        warm.start(key: key, source: url)
        try await waitFor { probe.cancelled == 1 }
        XCTAssertEqual(warm.heldCount, 1, "one job per take id")
        probe.release()
    }

    func testStartingTwiceForTheSameFileDoesNotTranscodeTwice() async throws {
        let probe = Probe(), warm = prewarmer(probe)
        let url = try take(), key = UUID()
        warm.start(key: key, source: url)
        warm.start(key: key, source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        _ = await claim.takeProxy()
        XCTAssertEqual(probe.started, 1)
    }

    func testAClaimHandsTheWorkToOneCallerOnly() throws {
        let warm = prewarmer(Probe())
        let url = try take()
        warm.start(key: UUID(), source: url)
        XCTAssertNotNil(warm.claim(source: url))
        XCTAssertNil(warm.claim(source: url), "a retry after a failure starts clean instead of sharing a consumed job")
        XCTAssertEqual(warm.heldCount, 0)
    }

    func testAClaimWaitsForWorkThatIsStillRunningInsteadOfStartingItAgain() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let url = try take()
        warm.start(key: UUID(), source: url)
        try await waitFor { probe.started == 1 }
        let claim = try XCTUnwrap(warm.claim(source: url))
        let waiting = Task { await claim.takeProxy() }
        try await Task.sleep(for: .milliseconds(30))
        probe.release()
        let proxy = await waiting.value
        XCTAssertNotNil(proxy)
        XCTAssertEqual(probe.started, 1)
    }

    // MARK: Retake, discard, giving up

    func testRetakeWhileTheTranscodeRunsStopsItAndLeavesNoFile() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let url = try take(), key = UUID()
        warm.start(key: key, source: url)
        try await waitFor { probe.started == 1 }

        warm.cancel(key: key)

        try await waitFor { probe.cancelled == 1 }
        XCTAssertNil(warm.claim(source: url))
        XCTAssertEqual(files(in: root.appendingPathComponent("prewarm")), [])
    }

    func testACallerThatIsCancelledWhileWaitingStopsTheWork() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let url = try take()
        warm.start(key: UUID(), source: url)
        try await waitFor { probe.started == 1 }
        let claim = try XCTUnwrap(warm.claim(source: url))
        let waiting = Task { await claim.takeProxy() }
        try await Task.sleep(for: .milliseconds(20))
        waiting.cancel()
        let proxy = await waiting.value
        XCTAssertNil(proxy)
        try await waitFor { probe.cancelled == 1 }
    }

    func testDiscardingAnUntakenClaimDeletesItsProxy() async throws {
        let warm = prewarmer(Probe())
        let url = try take()
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        _ = await claim.fingerprint()
        try await waitFor { !self.files(in: self.root.appendingPathComponent("prewarm")).isEmpty }
        claim.discard()
        XCTAssertEqual(files(in: root.appendingPathComponent("prewarm")), [])
    }

    func testATakenProxySurvivesDiscard() async throws {
        let warm = prewarmer(Probe())
        let url = try take()
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        let taken = await claim.takeProxy()
        let proxy = try XCTUnwrap(taken)
        claim.discard()
        XCTAssertTrue(FileManager.default.fileExists(atPath: proxy.path), "the upload moves it into the project; discard must not undo that")
    }

    // MARK: A file that changes or a transcode that fails

    func testAFileEditedWhileTheTranscodeRanYieldsNoProxy() async throws {
        let probe = Probe()
        let url = try take("original bytes")
        probe.editWhileRunning = url
        let warm = prewarmer(probe)
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        let proxy = await claim.takeProxy()
        XCTAssertNil(proxy, "a proxy of bytes that no longer exist must never be uploaded as the take")
        XCTAssertEqual(files(in: root.appendingPathComponent("prewarm")), [])
    }

    func testAFailedTranscodeStillGivesTheHashAndNoProxy() async throws {
        let probe = Probe(); probe.failure = CocoaError(.fileWriteOutOfSpace)
        let warm = prewarmer(probe)
        let url = try take()
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        let known = await claim.fingerprint()
        let proxy = await claim.takeProxy()
        XCTAssertNotNil(known)
        XCTAssertNil(proxy, "the upload transcodes for itself and reports the real error")
    }

    func testAFileDeletedWhileTheTranscodeRanYieldsNoProxy() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let url = try take()
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        try FileManager.default.removeItem(at: url)
        probe.release()
        let proxy = await claim.takeProxy()
        XCTAssertNil(proxy, "the file is gone by the time the transcode finishes, so what it read cannot be vouched for")
    }

    func testARelaunchStartsEmptyAndClearsAProxyTheLastProcessLeft() throws {
        let directory = root.appendingPathComponent("prewarm", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("left behind by a killed app".utf8).write(to: directory.appendingPathComponent("old.mp4"))
        let fresh = SourcePrewarmer(directory: directory) { _, destination in destination }
        XCTAssertEqual(fresh.heldCount, 0)
        XCTAssertEqual(files(in: directory), [])
        XCTAssertNil(fresh.claim(source: try take()))
    }

    // MARK: Importing with a digest someone already computed

    func testImportReusesADigestForALinkedOriginalAndRehashesACopy() async throws {
        let source = try take("a recorded take")
        let real = try SHA256Fingerprinter().fingerprint(file: source)
        let marker = AssetFingerprint(hex: String(repeating: "0", count: 64), byteCount: real.byteCount)
        let known = KnownFingerprint(identity: try FileIdentity(of: source), fingerprint: marker)
        let project = ProjectDirectory(root: root.appendingPathComponent("project", isDirectory: true))

        let linked = try await AssetImportCoordinator(project: project).importAsset(from: source, preferLink: true, reusing: known)
        XCTAssertEqual(linked.fingerprint, marker, "same inode: the digest is trusted (the marker proves the hash was skipped)")

        let copied = try await AssetImportCoordinator(project: project).importAsset(from: source, preferLink: false, reusing: known)
        XCTAssertEqual(copied.fingerprint, real, "a copy is a different file: it is hashed, whatever the caller claims")
    }

    func testImportRehashesWhenTheFileChangedAfterTheDigestWasTaken() async throws {
        let source = try take("version one")
        let stale = KnownFingerprint(identity: try FileIdentity(of: source), fingerprint: try SHA256Fingerprinter().fingerprint(file: source))
        usleep(20_000)
        try Data("version two, different".utf8).write(to: source)
        let project = ProjectDirectory(root: root.appendingPathComponent("project", isDirectory: true))
        let imported = try await AssetImportCoordinator(project: project).importAsset(from: source, preferLink: true, reusing: stale)
        XCTAssertEqual(imported.fingerprint, try SHA256Fingerprinter().fingerprint(file: source))
        XCTAssertNotEqual(imported.fingerprint, stale.fingerprint)
    }

    // MARK: End to end on a real clip

    @MainActor func testPrewarmedProxyIsTheProxyTheUploadWouldHaveBuilt() async throws {
        let clip = try await SolidVideoFixture.make(directory: root, name: "real-take", color: CGColor(red: 0.2, green: 0.5, blue: 0.7, alpha: 1), seconds: 2, size: (640, 360))
        let warm = SourcePrewarmer(directory: root.appendingPathComponent("prewarm", isDirectory: true)) { source, destination in
            try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: source, destination: destination)
        }
        warm.start(key: UUID(), source: clip)
        let claim = try XCTUnwrap(warm.claim(source: clip))
        let known = await claim.fingerprint()
        let readyProxy = await claim.takeProxy()
        let ready = try XCTUnwrap(readyProxy)
        let inline = try await AVFoundationProxyGenerator(preset: .analysis).makeProxy(for: clip, destination: root.appendingPathComponent("inline.mp4"))

        XCTAssertEqual(known?.fingerprint, try SHA256Fingerprinter().fingerprint(file: clip))
        let (a, b) = (AVURLAsset(url: ready), AVURLAsset(url: inline))
        let sizes = try await (a.loadTracks(withMediaType: .video).first!.load(.naturalSize), b.loadTracks(withMediaType: .video).first!.load(.naturalSize))
        XCTAssertEqual(sizes.0, sizes.1)
        let durations = try await (a.load(.duration).seconds, b.load(.duration).seconds)
        XCTAssertEqual(durations.0, durations.1, accuracy: 0.05)
    }

    private func waitFor(timeout: Duration = .seconds(5), _ condition: @escaping () -> Bool) async throws {
        let deadline = ContinuousClock.now + timeout
        while !condition() {
            if ContinuousClock.now > deadline { return XCTFail("timed out waiting for condition") }
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    // MARK: Export limit

    func testTwoPrewarmsNeverExportAtTheSameTime() async throws {
        let probe = Probe(); probe.holds = true
        let slots = Slots(), warm = gatedPrewarmer(probe, slots)
        warm.start(key: UUID(), source: try take("first", name: "a.mov"))
        warm.start(key: UUID(), source: try take("second", name: "b.mov"))
        try await waitFor { probe.started == 1 }
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(probe.started, 1, "the second waits for the first's slot")
        probe.release()
        try await waitFor { probe.started == 2 }
    }

    func testAnotherExportHoldingTheSlotKeepsAPrewarmWaiting() async throws {
        let probe = Probe(), slots = Slots(), warm = gatedPrewarmer(probe, slots)
        XCTAssertTrue(slots.tryTake(), "a Photos clip's proxy export")
        warm.start(key: UUID(), source: try take())
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(probe.started, 0)
        slots.admission.release()
        try await waitFor { probe.started == 1 }
    }

    func testACancelledQueuedPrewarmLeavesTheQueueAndTheSlotStaysUsable() async throws {
        let probe = Probe(); probe.holds = true
        let slots = Slots(), warm = gatedPrewarmer(probe, slots)
        let queued = UUID()
        warm.start(key: UUID(), source: try take("first", name: "a.mov"))
        try await waitFor { probe.started == 1 }
        warm.start(key: queued, source: try take("second", name: "b.mov"))
        warm.cancel(key: queued)
        probe.release()
        try await waitFor { slots.isFree }
        XCTAssertEqual(probe.started, 1, "the cancelled prewarm never exported")
        XCTAssertEqual(files(in: root.appendingPathComponent("prewarm")).filter { $0.hasSuffix(".mp4") }.count, 1)
    }

    func testAClaimantThatHoldsTheSlotIsNotMadeToWaitForAQueuedPrewarm() async throws {
        let probe = Probe(), slots = Slots(), warm = gatedPrewarmer(probe, slots)
        XCTAssertTrue(slots.tryTake(), "the upload's own slot")
        let url = try take("the creator's take")
        warm.start(key: UUID(), source: url)
        let claim = try XCTUnwrap(warm.claim(source: url))
        let outcome = expectation(description: "claim answered")
        Task {
            let known = await claim.fingerprint()
            let proxy = await claim.takeProxy()
            XCTAssertNotNil(known, "the hash does not need the slot")
            XCTAssertNil(proxy, "the claimant transcodes in its own slot")
            outcome.fulfill()
        }
        await fulfillment(of: [outcome], timeout: 5)
        XCTAssertEqual(probe.started, 0)
        slots.admission.release()
        XCTAssertTrue(slots.isFree)
    }

    func testAFailedExportGivesItsSlotBack() async throws {
        let probe = Probe(); probe.failure = CocoaError(.fileWriteUnknown)
        let slots = Slots(), warm = gatedPrewarmer(probe, slots)
        warm.start(key: UUID(), source: try take())
        try await waitFor { probe.started == 1 }
        try await waitFor { slots.isFree }
    }

    func testCancellingBySourceStopsAnOrphanedExport() async throws {
        let probe = Probe(); probe.holds = true
        let warm = prewarmer(probe)
        let url = try take()
        warm.start(key: UUID(), source: url)
        try await waitFor { probe.started == 1 }
        let clone = root.appendingPathComponent("clone.mov")
        try FileManager.default.copyItem(at: url, to: clone)
        warm.cancel(source: clone)
        XCTAssertEqual(warm.heldCount, 1, "a different file's cancel leaves it")
        warm.cancel(source: url)
        try await waitFor { probe.cancelled == 1 }
        XCTAssertEqual(warm.heldCount, 0)
    }
}
