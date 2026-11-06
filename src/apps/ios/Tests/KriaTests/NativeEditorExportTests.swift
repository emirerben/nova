import XCTest
@testable import Kria

@MainActor final class NativeEditorExportTests: XCTestCase {
    func testSaveAndShareScheduleTheActualPreviewExport() async throws {
        for sharing in [false, true] {
            let h = try ExportHarness()
            defer { h.cleanup() }
            let operation = h.start(sharing: sharing)
            await h.waitForSubmission()
            XCTAssertEqual(h.source.starts, 0)
            let systemTask = ExportSystemTask()
            h.scheduler.launch?(systemTask)
            await h.waitForExport()
            h.source.progress?(0.5)
            for _ in 0..<20 where systemTask.progress.isEmpty { await Task.yield() }
            XCTAssertEqual(systemTask.progress, [500])
            XCTAssertTrue(systemTask.completions.isEmpty)
            h.source.finish()
            await operation.value
            XCTAssertEqual(h.source.starts, 1)
            XCTAssertEqual(systemTask.completions, [true])
            if sharing {
                XCTAssertTrue(h.exporter.isSharing)
                XCTAssertEqual(h.exporter.sharedFile, h.source.output.fileURL)
            } else {
                let writes = await h.photos.writes
                XCTAssertEqual(writes, [h.source.output.fileURL])
                XCTAssertEqual(h.exporter.phase, .savedToPhotos)
                XCTAssertFalse(FileManager.default.fileExists(atPath: h.source.output.cleanupURL.path))
            }
        }
    }

    func testCancelledNativeEncoderDoesNotRetry() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let source = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: source)
        let probe = ExportCancellationProbe()
        let operation = Task {
            session.togglePlayback()
            XCTAssertTrue(session.isPlaying)
            return try await session.exportDisplayedSourcePreview(progress: { fraction in
                if fraction == 0 {
                    probe.recordStart()
                    withUnsafeCurrentTask { $0?.cancel() }
                }
            })
        }
        do {
            let output = try await operation.value
            try? FileManager.default.removeItem(at: output.cleanupURL)
            XCTFail("Cancelled encoding returned a shareable file")
        } catch {
            XCTAssertTrue(error is CancellationError)
        }
        XCTAssertEqual(probe.starts, 1, "Cancellation must not launch the session's second export attempt")
        XCTAssertFalse(session.isPlaying, "Cancelled export must not restart playback")
    }

    func testExpirationDuringCompletionCannotDeliverOutput() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        let operation = h.start(sharing: false)
        await h.waitForSubmission()
        let systemTask = ExportSystemTask()
        systemTask.expiresDuringCompletion = true
        h.scheduler.launch?(systemTask)
        await h.waitForExport()
        h.source.finish()
        await operation.value
        let writes = await h.photos.writes
        XCTAssertTrue(writes.isEmpty)
        XCTAssertEqual(systemTask.completions, [false])
        XCTAssertFalse(FileManager.default.fileExists(atPath: h.source.output.cleanupURL.path))
    }

    func testEncoderFailureNeverSavesOrShares() async throws {
        for sharing in [false, true] {
            let h = try ExportHarness()
            defer { h.cleanup() }
            h.source.fails = true
            let operation = h.start(sharing: sharing)
            await h.waitForSubmission()
            let systemTask = ExportSystemTask()
            h.scheduler.launch?(systemTask)
            await h.waitForExport()
            h.source.finish()
            await operation.value
            let writes = await h.photos.writes
            XCTAssertTrue(writes.isEmpty)
            XCTAssertFalse(h.exporter.isSharing)
            XCTAssertEqual(systemTask.completions, [false])
            guard case .failed = h.exporter.phase else { return XCTFail("Export failure was hidden") }
        }
    }

    func testUnsupportedBackgroundAndRejectedSchedulingExportOnce() async throws {
        for scenario in 0..<3 {
            let h = try ExportHarness()
            defer { h.cleanup() }
            h.scheduler.supportsGPUExport = scenario != 0
            h.foreground = scenario != 1
            h.scheduler.accept = scenario != 2
            let operation = h.start()
            await h.waitForExport()
            XCTAssertEqual(h.scheduler.submissions, scenario == 2 ? 1 : 0)
            h.source.finish()
            await operation.value
            XCTAssertEqual(h.source.starts, 1)
            XCTAssertTrue(h.exporter.isSharing)
        }
    }

    func testExpirationCancelsEncodingAndDiscardsLateOutput() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        let operation = h.start()
        await h.waitForSubmission()
        let systemTask = ExportSystemTask()
        h.scheduler.launch?(systemTask)
        await h.waitForExport()
        systemTask.expirationHandler?()
        await operation.value
        h.source.finish()
        for _ in 0..<100 where !h.source.sawCancellation || FileManager.default.fileExists(atPath: h.source.output.cleanupURL.path) { await Task.yield() }
        XCTAssertTrue(h.source.sawCancellation)
        XCTAssertFalse(h.exporter.isSharing)
        XCTAssertNil(h.exporter.sharedFile)
        XCTAssertEqual(systemTask.completions, [false])
        XCTAssertFalse(FileManager.default.fileExists(atPath: h.source.output.cleanupURL.path))
        let writes = await h.photos.writes
        XCTAssertTrue(writes.isEmpty)
    }

    func testCallerCancellationBeforeLaunchNeverStartsExport() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        let operation = h.start()
        await h.waitForSubmission()
        operation.cancel()
        await operation.value
        let late = ExportSystemTask()
        h.scheduler.launch?(late)
        XCTAssertEqual(h.source.starts, 0)
        XCTAssertEqual(late.completions, [false])
        XCTAssertFalse(h.exporter.isSharing)
    }

    func testCallerCancellationAlsoCancelsFallbackEncoding() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        h.scheduler.supportsGPUExport = false
        let operation = h.start(sharing: false)
        await h.waitForExport()
        operation.cancel()
        await operation.value
        h.source.finish()
        for _ in 0..<100 where !h.source.sawCancellation { await Task.yield() }
        XCTAssertTrue(h.source.sawCancellation)
        let writes = await h.photos.writes
        XCTAssertTrue(writes.isEmpty)
        XCTAssertNotEqual(h.exporter.phase, .savedToPhotos)
    }

    func testDuplicateTapDoesNotStartAnotherExport() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        let operation = h.start()
        await h.waitForSubmission()
        await h.start().value
        XCTAssertEqual(h.scheduler.submissions, 1)
        let systemTask = ExportSystemTask()
        h.scheduler.launch?(systemTask)
        await h.waitForExport()
        h.source.finish()
        await operation.value
        XCTAssertEqual(h.source.starts, 1)
    }

    func testReadyLocalFileDoesNotSubmitOrEncode() async throws {
        let h = try ExportHarness()
        defer { h.cleanup() }
        h.source.route = .localFile(h.source.output.fileURL)
        await h.start().value
        XCTAssertEqual(h.scheduler.submissions, 0)
        XCTAssertEqual(h.source.starts, 0)
        XCTAssertEqual(h.exporter.sharedFile, h.source.output.fileURL)
        h.exporter.removeSharedFile()
        XCTAssertTrue(FileManager.default.fileExists(atPath: h.source.output.fileURL.path))
    }
}

@MainActor private final class ExportHarness {
    let scheduler = ExportScheduler()
    let photos = ExportPhotos()
    let source: ExportSource
    var foreground = true
    lazy var exporter = NativeEditorExporter(photos: photos, scheduler: scheduler, isForeground: { [weak self] in self?.foreground == true })

    init() throws { source = try ExportSource() }
    func cleanup() { try? FileManager.default.removeItem(at: source.output.cleanupURL) }
    func start(sharing: Bool = true) -> Task<Void, Never> {
        Task {
            if sharing {
                await exporter.share(from: source, api: ChatUITestTransport.api(), deviceLocalFile: nil)
            } else {
                await exporter.saveToPhotos(from: source, api: ChatUITestTransport.api(), deviceLocalFile: nil)
            }
        }
    }
    func waitForSubmission() async {
        for _ in 0..<100 where scheduler.submissions == 0 { await Task.yield() }
        XCTAssertEqual(scheduler.submissions, 1)
    }
    func waitForExport() async {
        for _ in 0..<100 where source.starts == 0 { await Task.yield() }
        XCTAssertEqual(source.starts, 1)
    }
}

@MainActor private final class ExportSource: NativeEditorExportSource {
    var route = NativeEditorVideoDownloadRoute.sourcePreview
    let output: NativeEditorTemporaryVideo
    var starts = 0
    var sawCancellation = false
    var fails = false
    var progress: (@Sendable (Double) -> Void)?
    private var continuation: CheckedContinuation<Void, Never>?
    init() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("preview.mp4")
        try Data("current-preview".utf8).write(to: file)
        output = NativeEditorTemporaryVideo(fileURL: file, cleanupURL: directory)
    }
    func videoDownloadRoute(deviceLocalFile: URL?) throws -> NativeEditorVideoDownloadRoute { route }
    func exportDisplayedSourcePreview(progress: (@Sendable (Double) -> Void)?) async throws -> NativeEditorTemporaryVideo {
        starts += 1
        self.progress = progress
        await withCheckedContinuation { continuation = $0 }
        sawCancellation = Task.isCancelled
        if fails { throw NativeEditorVideoDownloadError.unavailable }
        return output
    }
    func finish() { continuation?.resume(); continuation = nil }
}

private actor ExportPhotos: PhotoLibrarySaving {
    var writes: [URL] = []
    func saveVideo(at url: URL) async throws { writes.append(url) }
}

@MainActor private final class ExportScheduler: ContinuedProcessingExportScheduling {
    var supportsGPUExport = true
    var accept = true
    var submissions = 0
    var launch: (@MainActor (any ContinuedProcessingExportTask) -> Void)?
    func registerAndSubmit(identifier: String, title: String, subtitle: String,
                           launch: @escaping @MainActor (any ContinuedProcessingExportTask) -> Void) -> Bool {
        submissions += 1
        self.launch = launch
        return accept
    }
    func cancel(identifier: String) {}
}

@MainActor private final class ExportSystemTask: ContinuedProcessingExportTask {
    var expirationHandler: (@MainActor () -> Void)?
    var isExpired = false
    var expiresDuringCompletion = false
    var completions: [Bool] = []
    var progress: [Int64] = []
    func reportProgress(completed: Int64, total: Int64) { progress.append(completed) }
    func complete(success: Bool) -> Bool {
        let result = success && !isExpired && !expiresDuringCompletion
        completions.append(result)
        return result
    }
}

private final class ExportCancellationProbe: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    var starts: Int { lock.withLock { count } }
    func recordStart() { lock.withLock { count += 1 } }
}
