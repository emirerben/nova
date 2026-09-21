import XCTest
import UIKit
@testable import Kria

@MainActor final class BackgroundUploadRetryTests: XCTestCase {
    override func tearDown() { UploadRetryProtocol.handler = nil; super.tearDown() }

    func testProxyCannotEnterCloudSourceReservationContract() throws {
        XCTAssertThrowsError(try BackgroundUploadCoordinator.validateProjectUploadPurpose(.analysisProxy))
        XCTAssertNoThrow(try BackgroundUploadCoordinator.validateProjectUploadPurpose(.cloudRenderSource))
    }

    /// KRI-114 P0-4: an entry from a previous launch (a random, necessarily-
    /// stale `launchToken`) whose staged file still exists must be RESUMED —
    /// `prepare()` runs again — rather than silently discarded. The reservation
    /// stub fails with a transient (500) error, so this also proves the review
    /// follow-up: a transient failure must KEEP the staged file (`.retryLater`,
    /// fresh `launchToken`, no deletion) rather than treat it like a definitive
    /// rejection. What it also proves is that `prepare()` ran at all (the
    /// import side effect under the project's `originals/` directory), which
    /// the pre-fix behavior (unconditional delete, no resume) could never do.
    func testTransientResumeFailureKeepsStagedFileAndSchedulesRetry() async throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let projectID = UUID()
        let staged = FileManager.default.temporaryDirectory.appending(path: "staged-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: staged)
        // Backdated well past `resumeBackoffInterval` (60s): a heartbeat this
        // stale is what a genuine crash/relaunch looks like; a fresh one is
        // exercised separately below (`testRecentHeartbeatIsNotResumedYet`).
        let entry = PreparingUpload(id: UUID(), projectID: projectID, localFilePath: staged.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil, lastHeartbeatAt: Date().addingTimeInterval(-120), launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        let projectDirectory = BackgroundUploadCoordinator.projectDirectory(projectID)
        defer {
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: staged)
            try? FileManager.default.removeItem(at: projectDirectory.root)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        var reservationRequests = 0
        UploadRetryProtocol.handler = { transport in reservationRequests += 1; transport.finish(500, Data()) }
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)

        let tasks = coordinator.recoverInterruptedPreparations()
        XCTAssertEqual(tasks.count, 1)
        for task in tasks { await task.value }

        XCTAssertEqual(coordinator.lastError, "Kria couldn't resume an interrupted upload. It will retry when you're back online.")
        XCTAssertTrue(FileManager.default.fileExists(atPath: staged.path), "a TRANSIENT resume failure must never destroy the staged original")
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertEqual(remaining.count, 1, "the entry must survive a transient failure for a later launch/openWorkspace to retry")
        XCTAssertEqual(remaining.first?.state, .retryLater)
        XCTAssertNotEqual(remaining.first?.launchToken, entry.launchToken, "launchToken must be rerolled so a later recovery pass sees this as orphaned again")
        XCTAssertTrue(coordinator.records.isEmpty, "reserve failed, so this never became a real upload task")
        // The reservation is only attempted after `prepare()` succeeds, so this proves the import step
        // actually ran against the staged file (which the pre-fix unconditional delete could never do).
        XCTAssertEqual(reservationRequests, 1, "prepare() must have run, and the failure must have come from the reservation")
        // The failed attempt's import is discarded: the retry re-imports from the staged file under a
        // fresh asset id, so leaving this one would strand a full-size copy that nothing references.
        let importedFiles = (try? FileManager.default.contentsOfDirectory(at: projectDirectory.originals, includingPropertiesForKeys: nil)) ?? []
        XCTAssertTrue(importedFiles.isEmpty, "a failed attempt must not leave its imported original behind")
    }

    /// KRI-114 P0-4 review follow-up: a DEFINITIVE rejection (here, a 409 the
    /// server didn't tag with a recognized `detail`, which `KriaAPI` surfaces as
    /// `APIError.conflict` -- not the ambiguous, retryable `.requestFailed`)
    /// must still drop the entry and its staged file with the original
    /// "choose the file again" messaging; retrying would fail identically.
    func testDefinitiveResumeFailureStillDropsEntryAndFile() async throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let projectID = UUID()
        let staged = FileManager.default.temporaryDirectory.appending(path: "staged-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: staged)
        let entry = PreparingUpload(id: UUID(), projectID: projectID, localFilePath: staged.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil, lastHeartbeatAt: Date().addingTimeInterval(-120), launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        let projectDirectory = BackgroundUploadCoordinator.projectDirectory(projectID)
        defer {
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: staged)
            try? FileManager.default.removeItem(at: projectDirectory.root)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        UploadRetryProtocol.handler = { transport in transport.finish(409, Data("{}".utf8)) }
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)

        let tasks = coordinator.recoverInterruptedPreparations()
        XCTAssertEqual(tasks.count, 1)
        for task in tasks { await task.value }

        XCTAssertEqual(coordinator.lastError, "The original upload could not be resumed. Choose the file again.")
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.path), "a definitive resume failure must clean up its staged copy")
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertTrue(remaining.isEmpty)
        XCTAssertTrue(coordinator.records.isEmpty)
    }

    /// KRI-114 P0-4 review follow-up: `recoverInterruptedPreparations()` runs on
    /// every `openWorkspace()`, so an orphaned entry with a recent heartbeat
    /// (< 60s old -- e.g. a previous resume attempt just failed transiently)
    /// must not be re-resumed immediately; that would re-run the transcode on
    /// every workspace open while offline.
    func testRecentHeartbeatIsNotResumedYet() throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let staged = FileManager.default.temporaryDirectory.appending(path: "staged-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: staged)
        let entry = PreparingUpload(id: UUID(), projectID: UUID(), localFilePath: staged.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil, state: .retryLater, lastHeartbeatAt: Date(), launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        defer {
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: staged)
        }
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)

        let tasks = coordinator.recoverInterruptedPreparations()

        XCTAssertTrue(tasks.isEmpty, "a heartbeat this fresh must not trigger another resume attempt yet")
        XCTAssertNil(coordinator.lastError)
        XCTAssertTrue(FileManager.default.fileExists(atPath: staged.path))
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertEqual(remaining.count, 1, "the entry must survive untouched until the backoff elapses")
        XCTAssertEqual(remaining.first?.state, .retryLater, "left completely untouched, including its state")
    }

    /// KRI-114 P0-4: when the staged file is already gone, there's genuinely
    /// nothing to resume from — this is the only case that still falls back to
    /// the old "choose the file again" messaging, and the orphaned entry is
    /// removed rather than retried forever.
    func testMissingStagedFileFallsBackToChooseFileAgain() throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let missing = FileManager.default.temporaryDirectory.appending(path: "missing-\(UUID().uuidString).mp4")
        let entry = PreparingUpload(id: UUID(), projectID: UUID(), localFilePath: missing.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil, launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        defer { UserDefaults.standard.removeObject(forKey: "\(key).preparing") }
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)

        let tasks = coordinator.recoverInterruptedPreparations()

        XCTAssertTrue(tasks.isEmpty, "nothing to resume from -- no prepare() should be attempted")
        XCTAssertEqual(coordinator.lastError, "An upload was interrupted before it could start. Choose the file again.")
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertTrue(remaining.isEmpty)
        XCTAssertTrue(coordinator.records.isEmpty)
    }

    /// KRI-114 P0-4 (a): recovery must never reap an entry whose `prepare()` is
    /// genuinely still running in this very process (e.g. `openWorkspace()` is
    /// re-entered while a just-picked file is mid-transcode) just because its
    /// on-disk `launchToken` happens to look stale.
    func testInFlightPreparationInSameProcessIsNotReaped() throws {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let staged = FileManager.default.temporaryDirectory.appending(path: "inflight-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: staged)
        let id = UUID()
        let entry = PreparingUpload(id: id, projectID: UUID(), localFilePath: staged.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, role: .clip, itemID: nil, launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([entry]), forKey: "\(key).preparing")
        defer {
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: staged)
        }
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)
        coordinator.test_markPreparationActive(id)
        defer { coordinator.test_clearPreparationActive(id) }

        let tasks = coordinator.recoverInterruptedPreparations()

        XCTAssertTrue(tasks.isEmpty, "an in-flight preparation must never be resumed a second time")
        XCTAssertNil(coordinator.lastError)
        XCTAssertTrue(FileManager.default.fileExists(atPath: staged.path))
        let remaining = UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []
        XCTAssertEqual(remaining.count, 1, "the untouched entry must still be there for the still-running prepare() to eventually clear")
    }

    func testNoInterruptedPreparationIsANoOp() {
        let key = "kria.test.preparing.\(UUID().uuidString)"
        let configuration = URLSessionConfiguration.ephemeral
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration)
        XCTAssertTrue(coordinator.recoverInterruptedPreparations().isEmpty)
        XCTAssertNil(coordinator.lastError)
    }

    /// KRI-114 P0-4: the `beginBackgroundTask`/`endBackgroundTask` assertion
    /// around `enqueue()`'s prepare()+reserve() window must balance on both the
    /// success exit and the thrown-error exit.
    func testBackgroundActivityAssertionBalancedOnSuccessAndError() async throws {
        try await assertBackgroundActivityBalanced(reservationStatus: 200, expectSuccess: true)
        try await assertBackgroundActivityBalanced(reservationStatus: 500, expectSuccess: false)
    }

    private func assertBackgroundActivityBalanced(reservationStatus: Int, expectSuccess: Bool) async throws {
        let key = "kria.test.enqueue.\(UUID().uuidString)"
        let projectID = UUID()
        let source = FileManager.default.temporaryDirectory.appending(path: "source-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: source)
        let projectDirectory = BackgroundUploadCoordinator.projectDirectory(projectID)
        defer {
            UserDefaults.standard.removeObject(forKey: key)
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: source)
            try? FileManager.default.removeItem(at: projectDirectory.root)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        UploadRetryProtocol.handler = { transport in
            if transport.request.httpMethod == "PUT" { transport.finish(200, Data()); return }
            let body = reservationStatus == 200
                ? Data(#"[{"media_id":"clip-1","upload_url":"https://uploads.test/put","gcs_path":"users/u/clip.mp4","content_type":"video/mp4","upload_headers":{}}]"#.utf8)
                : Data()
            transport.finish(reservationStatus, body)
        }
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let activity = RecordingBackgroundActivityAssertion()
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration, backgroundActivity: activity)

        let accepted = await coordinator.enqueue(fileURL: source, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .clip)

        XCTAssertEqual(accepted, expectSuccess)
        XCTAssertEqual(activity.beginCount, 1)
        XCTAssertEqual(activity.endCount, 1, "the assertion must end even when enqueue() throws")
    }

    /// KRI-125: a cloud clip used to be written to disk four times (Photos export, staging,
    /// `originals/`, upload file). Through a real `enqueue`, the bytes must now exist once —
    /// the same inode from the Photos export all the way to the file the URLSession reads —
    /// with exactly two names left (the project original and the upload file) and no staged
    /// leftover.
    func testEnqueueWritesACloudClipToDiskOnce() async throws {
        let key = "kria.test.enqueue.\(UUID().uuidString)"
        let projectID = UUID()
        let source = FileManager.default.temporaryDirectory.appending(path: "source-\(UUID().uuidString).mp4")
        try Data("one copy of these bytes".utf8).write(to: source)
        let exportIdentity = try XCTUnwrap(fileIdentity(source))
        let projectDirectory = BackgroundUploadCoordinator.projectDirectory(projectID)
        defer {
            UserDefaults.standard.removeObject(forKey: key)
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: source)
            try? FileManager.default.removeItem(at: projectDirectory.root)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        UploadRetryProtocol.handler = { transport in
            // Hold the PUT open so the upload stays in flight while the disk is inspected;
            // letting it finish would run the attach step against this stub.
            guard transport.request.httpMethod != "PUT" else { return }
            transport.finish(200, Data(#"[{"media_id":"clip-1","upload_url":"https://uploads.test/put","gcs_path":"users/u/clip.mp4","content_type":"video/mp4","upload_headers":{}}]"#.utf8))
        }
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration, backgroundActivity: RecordingBackgroundActivityAssertion())

        let accepted = await coordinator.enqueue(fileURL: source, projectID: projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource, role: .clip)

        XCTAssertTrue(accepted)
        let record = try XCTUnwrap(coordinator.records.first)
        let uploadFile = URL(fileURLWithPath: record.localFilePath)
        let originals = try FileManager.default.contentsOfDirectory(at: projectDirectory.originals, includingPropertiesForKeys: nil)
        XCTAssertEqual(originals.count, 1)
        let original = try XCTUnwrap(originals.first)

        XCTAssertEqual(fileIdentity(original), exportIdentity, "project original must be the exported bytes, not a copy")
        XCTAssertEqual(fileIdentity(uploadFile), exportIdentity, "upload file must be the exported bytes, not a copy")
        let names = try XCTUnwrap((try FileManager.default.attributesOfItem(atPath: uploadFile.path))[.referenceCount] as? NSNumber).intValue
        XCTAssertEqual(names, 2, "original + upload file only; the staged name must be gone")
        XCTAssertFalse(FileManager.default.fileExists(atPath: source.path), "the Photos export was renamed into staging")
        let stillStaged = (UserDefaults.standard.data(forKey: "\(key).preparing")
            .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? [])
        XCTAssertTrue(stillStaged.isEmpty)

        await coordinator.cancel(recordID: record.id)
    }

    /// KRI-114 P0-4: a background-task expiration must mark the entry `.expired`
    /// and leave the staged file alone -- never delete it -- so the next launch
    /// resumes it instead of finding nothing.
    func testExpirationMarksEntryExpiredWithoutDeletingStagedFile() async throws {
        let key = "kria.test.enqueue.\(UUID().uuidString)"
        let projectID = UUID()
        let source = FileManager.default.temporaryDirectory.appending(path: "source-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: source)
        let projectDirectory = BackgroundUploadCoordinator.projectDirectory(projectID)
        defer {
            UserDefaults.standard.removeObject(forKey: key)
            UserDefaults.standard.removeObject(forKey: "\(key).preparing")
            try? FileManager.default.removeItem(at: source)
            try? FileManager.default.removeItem(at: projectDirectory.root)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        let started = expectation(description: "Reservation in flight")
        var held: UploadRetryProtocol?
        UploadRetryProtocol.handler = { transport in
            held = transport
            started.fulfill()
        }
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        let activity = RecordingBackgroundActivityAssertion()
        let coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration, backgroundActivity: activity)

        let enqueueTask = Task { await coordinator.enqueue(fileURL: source, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .clip) }
        await fulfillment(of: [started], timeout: 3)

        XCTAssertEqual(activity.beginCount, 1)
        activity.triggerExpiration()

        // The production expiration handler re-hops onto `@MainActor` via an
        // unstructured `Task` (a real `UIApplication` may invoke it off-main),
        // so give that hop a chance to run before reading the persisted state.
        var staged: PreparingUpload?
        for _ in 0..<50 {
            staged = (UserDefaults.standard.data(forKey: "\(key).preparing")
                .flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) } ?? []).first
            if staged?.state == .expired { break }
            await Task.yield()
        }
        let unwrappedStaged = try XCTUnwrap(staged)
        XCTAssertEqual(unwrappedStaged.state, .expired)
        XCTAssertTrue(FileManager.default.fileExists(atPath: unwrappedStaged.localFilePath), "expiration must never delete the staged copy")

        try XCTUnwrap(held).finish(500, Data())
        _ = await enqueueTask.value
        XCTAssertEqual(activity.endCount, 1)
    }

    func testConcurrentRetryReservesOnlyOneReplacement() async throws {
        let fixture = try makeCoordinator()
        defer { fixture.cleanup() }
        let started = expectation(description: "Reservation suspended")
        var held: UploadRetryProtocol?
        var reservations = 0
        UploadRetryProtocol.handler = { transport in
            reservations += 1
            held = transport
            started.fulfill()
        }
        let first = Task { await fixture.coordinator.retryUpload(recordID: fixture.record.id) }
        await fulfillment(of: [started], timeout: 3)
        await fixture.coordinator.retryUpload(recordID: fixture.record.id)
        XCTAssertEqual(reservations, 1)
        try XCTUnwrap(held).finish(500, Data())
        await first.value
        XCTAssertEqual(fixture.coordinator.records.count, 1)
        XCTAssertNotNil(fixture.coordinator.lastError)
        UploadRetryProtocol.handler = { transport in
            reservations += 1
            transport.finish(500, Data())
        }
        await fixture.coordinator.retryUpload(recordID: fixture.record.id)
        XCTAssertEqual(reservations, 2) // A failed attempt releases the retry guard.
    }

    func testCancellationWhileReservationIsPendingCannotRestartUpload() async throws {
        let fixture = try makeCoordinator()
        defer { fixture.cleanup() }
        let started = expectation(description: "Reservation suspended")
        var held: UploadRetryProtocol?
        var puts = 0
        UploadRetryProtocol.handler = { transport in
            if transport.request.httpMethod == "PUT" { puts += 1; transport.finish(200, Data()); return }
            held = transport
            started.fulfill()
        }
        let retry = Task { await fixture.coordinator.retryUpload(recordID: fixture.record.id) }
        await fulfillment(of: [started], timeout: 3)
        await fixture.coordinator.cancel(recordID: fixture.record.id)
        XCTAssertTrue(fixture.coordinator.records.isEmpty)
        try XCTUnwrap(held).finish(200, Data(#"[{"media_id":"clip-1","upload_url":"https://uploads.test/put","gcs_path":"users/u/clip.mp4","content_type":"video/mp4","upload_headers":{}}]"#.utf8))
        await retry.value
        XCTAssertTrue(fixture.coordinator.records.isEmpty)
        XCTAssertEqual(puts, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.record.localFilePath))
    }

    private struct Fixture {
        let coordinator: BackgroundUploadCoordinator
        let record: UploadRecoveryRecord
        let key: String
        func cleanup() {
            UserDefaults.standard.removeObject(forKey: key)
            try? FileManager.default.removeItem(atPath: record.localFilePath)
        }
    }
    private func makeCoordinator() throws -> Fixture {
        let key = "kria.test.retry.\(UUID().uuidString)"
        let file = FileManager.default.temporaryDirectory.appending(path: "retry-\(UUID().uuidString).mp4")
        try Data([1, 2, 3]).write(to: file)
        let record = UploadRecoveryRecord(id: UUID(), projectID: UUID(), localFilePath: file.path, filename: "clip.mp4", source: .files, purpose: .cloudRenderSource, taskIdentifier: 999, retryCount: 0)
        UserDefaults.standard.set(try JSONEncoder().encode([record]), forKey: key)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        return Fixture(coordinator: BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration), record: record, key: key)
    }
}

private final class UploadRetryProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@MainActor (UploadRetryProtocol) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let callback = Callback(transport: self)
        DispatchQueue.main.async { UploadRetryProtocol.handler?(callback.transport) }
    }
    private struct Callback: @unchecked Sendable { let transport: UploadRetryProtocol }
    override func stopLoading() {}
    @MainActor func finish(_ status: Int, _ data: Data) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}

/// Test double for `BackgroundActivityAssertion`: records begin/end balance and
/// lets a test invoke the captured expiration handler on demand, without a real
/// `UIApplication` background-task runtime.
@MainActor private final class RecordingBackgroundActivityAssertion: BackgroundActivityAssertion, @unchecked Sendable {
    private(set) var beginCount = 0
    private(set) var endCount = 0
    private var lastExpirationHandler: (@Sendable () -> Void)?
    private var nextRawValue = 1
    func begin(name: String, expirationHandler: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier {
        beginCount += 1
        lastExpirationHandler = expirationHandler
        defer { nextRawValue += 1 }
        return UIBackgroundTaskIdentifier(rawValue: nextRawValue)
    }
    func end(_ identifier: UIBackgroundTaskIdentifier) { endCount += 1 }
    func triggerExpiration() { lastExpirationHandler?() }
}

private let reservationResponse = Data(#"[{"media_id":"clip-1","upload_url":"https://uploads.test/put","gcs_path":"users/u/clip.mp4","content_type":"video/mp4","upload_headers":{}}]"#.utf8)

/// A coordinator wired to the URL-protocol stub. Reservations are answered at once, or held so a
/// test can see how many are in flight; every PUT is held so uploads stay in flight.
@MainActor private final class SelectionHarness {
    let coordinator: BackgroundUploadCoordinator
    let projectID: UUID
    private(set) var received = 0
    private(set) var pending: [UploadRetryProtocol] = []
    private let answerImmediately: Bool

    init(key: String, projectID: UUID, cloudSlots: Int, answerImmediately: Bool) {
        self.projectID = projectID
        self.answerImmediately = answerImmediately
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [UploadRetryProtocol.self]
        let api = KriaAPI(baseURL: URL(string: "https://uploads.test")!, tokenStore: NativeEditorMemoryTokenStore(), session: URLSession(configuration: configuration))
        coordinator = BackgroundUploadCoordinator(api: api, defaultsKey: key, sessionConfiguration: configuration, backgroundActivity: RecordingBackgroundActivityAssertion(), maxConcurrentCloudPreparations: cloudSlots)
        UploadRetryProtocol.handler = { [unowned self] transport in
            if transport.request.httpMethod == "PUT" { return }
            self.received += 1
            if self.answerImmediately { transport.finish(200, reservationResponse) } else { self.pending.append(transport) }
        }
    }

    func answerPending(_ count: Int) {
        let answered = pending.prefix(count)
        pending.removeFirst(answered.count)
        for transport in answered { transport.finish(200, reservationResponse) }
    }

    @discardableResult
    func select(_ identifier: String, role: CreationMediaRole = .clip, itemID: String? = nil, debounce: Duration = .zero, attached: Set<String> = []) throws -> URL {
        let file = FileManager.default.temporaryDirectory.appending(path: "pick-\(UUID().uuidString).mp4")
        try Data("clip \(identifier)".utf8).write(to: file)
        coordinator.select(.init(assetIdentifier: identifier, projectID: projectID, role: role, purpose: .cloudRenderSource, itemID: itemID, limit: nil, attachedMediaIDs: attached), debounce: debounce) { file }
        return file
    }

    func deselect(_ identifier: String, role: CreationMediaRole = .clip, itemID: String? = nil) async -> DeselectOutcome {
        await coordinator.deselect(assetIdentifier: identifier, projectID: projectID, role: role, itemID: itemID)
    }
}

/// The persisted `PreparingUpload` entries under `key`, as `recoverInterruptedPreparations` sees them.
private func persistedPreparing(_ key: String) -> [PreparingUpload] {
    (UserDefaults.standard.data(forKey: "\(key).preparing").flatMap { try? JSONDecoder().decode([PreparingUpload].self, from: $0) }) ?? []
}

/// KRI-125: uploads start while the user is still choosing, and un-choosing unwinds them.
extension BackgroundUploadRetryTests {
    private func harness(key: String = "kria.test.select.\(UUID().uuidString)", projectID: UUID = UUID(), cloudSlots: Int = 2, answerImmediately: Bool = true) -> SelectionHarness {
        let project = BackgroundUploadCoordinator.projectDirectory(projectID).root
        addTeardownBlock {
            for suffix in ["", ".preparing", ".photo-selection"] { UserDefaults.standard.removeObject(forKey: key + suffix) }
            try? FileManager.default.removeItem(at: project)
        }
        return SelectionHarness(key: key, projectID: projectID, cloudSlots: cloudSlots, answerImmediately: answerImmediately)
    }

    private func waitUntil(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }

    func testChoosingAnAssetStartsUploadingBeforeTheUserTapsDone() async throws {
        let h = harness()
        try h.select("asset-1")

        let started = await waitUntil { h.coordinator.records.count == 1 }

        XCTAssertTrue(started)
        XCTAssertEqual(h.coordinator.preselectedIdentifiers(projectID: h.projectID, role: .clip, attachedMediaIDs: []), ["asset-1"],
                       "reopening the picker shows it as already chosen while it uploads")
        XCTAssertEqual(h.coordinator.reservedCount(projectID: h.projectID, role: .clip), 0, "once recorded it counts through `records`, never twice")
    }

    func testChoosingTheSameAssetAgainUploadsItOnlyOnce() async throws {
        let h = harness()
        try h.select("asset-1")
        try h.select("asset-1")   // e.g. the picker re-fires while the first is still being prepared
        let started = await waitUntil { h.coordinator.records.count == 1 }
        XCTAssertTrue(started)

        try h.select("asset-1")   // and again once it is already on its way
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(h.received, 1, "one reservation, one upload")
        XCTAssertEqual(h.coordinator.records.count, 1)
    }

    func testChoosingAnAssetWhoseClipWasRemovedElsewhereUploadsItAgain() async throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let projectID = UUID()
        try seedLedger(key: key, projectID: projectID, PhotoSelectionEntry(assetIdentifier: "asset-1", recordID: UUID(), mediaID: "media-1", role: .clip, itemID: nil, boundAt: nil))
        let h = harness(key: key, projectID: projectID)

        try h.select("asset-1", attached: ["media-1"])   // media-1 is still attached: a genuine duplicate
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(h.received, 0, "a live duplicate must not upload")

        try h.select("asset-1", attached: [])            // media-1 was removed on the web: this is a fresh choice
        let started = await waitUntil { h.received == 1 }
        XCTAssertTrue(started, "a stale entry must never make an asset impossible to add again")
    }

    func testUnchoosingDuringTheDebounceNeverReachesTheServer() async throws {
        let h = harness()
        try h.select("asset-1", debounce: .seconds(30))

        let outcome = await h.deselect("asset-1")

        XCTAssertEqual(outcome, .discarded)
        XCTAssertEqual(h.received, 0)
        XCTAssertTrue(h.coordinator.photoSelections.isEmpty)
        XCTAssertNil(h.coordinator.lastError, "un-choosing is not an error")
        XCTAssertTrue(h.coordinator.failures.isEmpty)
    }

    func testUnchoosingWhileUploadingCancelsItAndCleansUp() async throws {
        let h = harness()
        try h.select("asset-1")
        let started = await waitUntil { h.coordinator.records.count == 1 }
        XCTAssertTrue(started)
        let uploadFile = try XCTUnwrap(h.coordinator.records.first).localFilePath

        let outcome = await h.deselect("asset-1")

        XCTAssertEqual(outcome, .cancelledUpload)
        XCTAssertTrue(h.coordinator.records.isEmpty)
        XCTAssertTrue(h.coordinator.photoSelections.isEmpty, "the asset can be chosen again")
        XCTAssertFalse(FileManager.default.fileExists(atPath: uploadFile))
        XCTAssertNil(h.coordinator.lastError)
    }

    /// The rule `ChatWorkspaceView` gates Continue on: an upload record only appears after a clip is
    /// prepared AND reserved, so counting records alone lets Continue through while chosen clips are
    /// still on their way — proceeding with fewer clips than the user picked.
    private func pendingCount(_ h: SelectionHarness) -> Int {
        let coordinator = h.coordinator
        let recorded = coordinator.records.filter { $0.projectID == h.projectID }.count
        let preparing = BackgroundUploadCoordinator.reservedCount(projectID: h.projectID, role: nil, inFlight: coordinator.inFlight, records: coordinator.records, selections: coordinator.photoSelections)
        return recorded + preparing
    }

    func testAChosenClipBlocksContinueBeforeItsUploadRecordExists() async throws {
        let h = harness(answerImmediately: false)
        try h.select("asset-1", debounce: .seconds(30))   // chosen, still in its debounce window

        XCTAssertTrue(h.coordinator.records.isEmpty)
        XCTAssertEqual(pendingCount(h), 1, "a clip that is only chosen already counts as on its way")
        XCTAssertFalse(FootageReadiness(attachedCount: 1, pendingCount: pendingCount(h)).canContinue)

        _ = await h.deselect("asset-1")
        XCTAssertEqual(pendingCount(h), 0, "un-choosing it releases the gate")
    }

    func testAClipBeingPreparedBlocksContinueAndIsNotCountedTwiceOnceRecorded() async throws {
        let h = harness(answerImmediately: false)
        try h.select("asset-1")
        let reached = await waitUntil { h.received == 1 }   // the reservation is in flight: prepared, but no record yet
        XCTAssertTrue(reached)

        XCTAssertTrue(h.coordinator.records.isEmpty)
        XCTAssertEqual(pendingCount(h), 1)
        XCTAssertFalse(FootageReadiness(attachedCount: 1, pendingCount: pendingCount(h)).canContinue)

        h.answerPending(1)
        let recorded = await waitUntil { h.coordinator.records.count == 1 }
        XCTAssertTrue(recorded)
        XCTAssertEqual(pendingCount(h), 1, "once it has a record it counts through the record, not twice")
    }

    /// The picker re-diffs after an un-choose finishes. If a second `deselect` for the same asset just
    /// reported `.discarded` while the first was still running, that re-diff would find the same removal
    /// again (the ledger has not changed yet) and re-issue it in a loop for the length of the round trip.
    func testASecondUnchooseWhileTheFirstIsRunningDefersToItInsteadOfLooping() async throws {
        let h = harness()
        try h.select("asset-1", debounce: .seconds(30))

        async let first = h.deselect("asset-1")
        async let second = h.deselect("asset-1")
        let outcomes = await [first, second]

        XCTAssertTrue(outcomes.contains(.discarded), "one call does the work")
        XCTAssertTrue(outcomes.contains(.alreadyInProgress), "the other must say it is deferring, not report a result")
        XCTAssertEqual(h.received, 0)
    }

    func testStartingANewBatchClearsThatRolesEarlierFailuresOnly() async throws {
        let h = harness()
        let bad = FileManager.default.temporaryDirectory.appending(path: "notes-\(UUID().uuidString).txt")
        try Data("not a video".utf8).write(to: bad)
        _ = await h.coordinator.enqueue(fileURL: bad, projectID: h.projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource, role: .clip)
        h.coordinator.reportFailure(projectID: h.projectID, role: .visual, filename: "card.png", message: "visual failed")
        XCTAssertEqual(h.coordinator.failures.count, 2)

        h.coordinator.clearFailures(projectID: h.projectID, role: .clip)

        XCTAssertEqual(h.coordinator.failures.map(\.role), [.visual], "a footage batch must not wipe a visual's failure")
    }

    /// B-roll of your own footage: one video chosen as a clip AND as a visual. Un-choosing one must not
    /// touch the other, and choosing the second must not be swallowed by the first.
    func testTheSameAssetChosenAsAClipAndAsAVisualAreIndependent() async throws {
        let h = harness()
        try h.select("shared", role: .clip, debounce: .seconds(30))
        try h.select("shared", role: .visual, itemID: "item-1", debounce: .seconds(30))
        XCTAssertEqual(h.coordinator.photoSelections[h.projectID.uuidString]?.entries.count, 2, "two choices, not one overwriting the other")

        let outcome = await h.deselect("shared", role: .clip)

        XCTAssertEqual(outcome, .discarded)
        XCTAssertEqual(h.coordinator.preselectedIdentifiers(projectID: h.projectID, role: .clip, attachedMediaIDs: []), [])
        XCTAssertEqual(h.coordinator.preselectedIdentifiers(projectID: h.projectID, role: .visual, itemID: "item-1", attachedMediaIDs: []), ["shared"],
                       "un-choosing the clip must leave the visual chosen")
        _ = await h.deselect("shared", role: .visual, itemID: "item-1")   // don't leave a 30 s debounce running
    }

    /// The moment between "attached" and "visible in the attached list" (a network refresh for visuals):
    /// the picker's still-ticked asset must not look newly chosen and upload a second time.
    func testAnAssetJustAttachedIsNotUploadedAgainBeforeTheAttachedListCatchesUp() async throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let projectID = UUID()
        try seedLedger(key: key, projectID: projectID, PhotoSelectionEntry(assetIdentifier: "asset-1", recordID: UUID(), mediaID: "media-1", role: .clip, itemID: nil, boundAt: Date()))
        let h = harness(key: key, projectID: projectID)

        try h.select("asset-1", attached: [])   // attached list has not caught up yet
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(h.received, 0, "a duplicate upload is exactly the bug this ticket is about")
    }

    /// A claim whose upload can no longer resume (its staged file is gone) must be released, or it
    /// counts as pending forever and keeps Continue disabled with no way out.
    func testARecoveredUploadWhoseStagedFileIsGoneReleasesItsClaim() throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let projectID = UUID()
        let recordID = UUID()
        try seedLedger(key: key, projectID: projectID, PhotoSelectionEntry(assetIdentifier: "asset-1", recordID: recordID, mediaID: nil, role: .clip, itemID: nil, boundAt: nil))
        let staged = PreparingUpload(id: recordID, projectID: projectID, localFilePath: "/nonexistent/\(UUID().uuidString).mp4", filename: "x.mp4", source: .photos, purpose: .cloudRenderSource, role: .clip, itemID: nil, launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([staged]), forKey: "\(key).preparing")
        let h = harness(key: key, projectID: projectID)
        XCTAssertEqual(h.coordinator.reservedCount(projectID: projectID, role: .clip), 1, "kept at launch: its staged entry was still there")

        _ = h.coordinator.recoverInterruptedPreparations()

        XCTAssertEqual(h.coordinator.reservedCount(projectID: projectID, role: .clip), 0, "nothing left to resume, so nothing is pending")
        XCTAssertTrue(h.coordinator.photoSelections.isEmpty)
    }

    /// After a relaunch there is no selection task to cancel, only a staged upload waiting to be resumed.
    /// Un-choosing must remove it, or it would be resumed later and attach a clip the user un-ticked.
    func testUnchoosingAfterARelaunchRemovesTheStagedUploadSoItIsNeverResumed() async throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let projectID = UUID()
        let recordID = UUID()
        let stagedFile = FileManager.default.temporaryDirectory.appending(path: "staged-\(UUID().uuidString).mp4")
        try Data("staged".utf8).write(to: stagedFile)
        try seedLedger(key: key, projectID: projectID, PhotoSelectionEntry(assetIdentifier: "asset-1", recordID: recordID, mediaID: nil, role: .clip, itemID: nil, boundAt: nil))
        let staged = PreparingUpload(id: recordID, projectID: projectID, localFilePath: stagedFile.path, filename: "x.mp4", source: .photos, purpose: .cloudRenderSource, role: .clip, itemID: nil, launchToken: UUID())
        UserDefaults.standard.set(try JSONEncoder().encode([staged]), forKey: "\(key).preparing")
        let h = harness(key: key, projectID: projectID)

        let outcome = await h.deselect("asset-1")

        XCTAssertEqual(outcome, .discarded)
        XCTAssertTrue(persistedPreparing(key).isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: stagedFile.path))
        XCTAssertTrue(h.coordinator.recoverInterruptedPreparations().isEmpty, "nothing left to resume")
        XCTAssertEqual(h.received, 0)
    }

    /// A clip queued behind the gate is already staged (durable) but not yet running; un-choosing it must
    /// unwind that, leaving the running clip alone.
    func testUnchoosingAClipQueuedBehindTheGateRemovesItsStagedUpload() async throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let h = harness(key: key, cloudSlots: 1, answerImmediately: false)
        try h.select("running")
        let running = await waitUntil { h.received == 1 }   // holds the only slot, waiting on its reservation
        XCTAssertTrue(running)
        try h.select("queued")
        let queued = await waitUntil { persistedPreparing(key).count == 2 }   // staged, then waiting for the slot
        XCTAssertTrue(queued)

        let outcome = await h.deselect("queued")

        XCTAssertEqual(outcome, .discarded)
        XCTAssertEqual(persistedPreparing(key).count, 1, "only the running clip stays staged")
        XCTAssertEqual(h.coordinator.inFlight.count, 1)
        XCTAssertEqual(h.received, 1, "the queued clip never reached the server")
        h.answerPending(1)   // let the running one finish so nothing lingers past the test
    }

    func testUnchoosingAnAssetTheCoordinatorNeverHeardOfIsHarmless() async {
        let h = harness()
        let outcome = await h.deselect("never-chosen")
        XCTAssertEqual(outcome, .notTracked)
    }

    func testOnlyTheConfiguredNumberOfClipsPrepareAtOnce() async throws {
        let h = harness(cloudSlots: 2, answerImmediately: false)
        for id in ["a", "b", "c", "d"] { try h.select(id) }

        var reached = await waitUntil { h.received == 2 }
        XCTAssertTrue(reached)
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertEqual(h.received, 2, "the other two wait for a slot instead of piling on")
        XCTAssertEqual(h.coordinator.reservedCount(projectID: h.projectID, role: .clip), 4, "but all four already count against the clip limit")

        h.answerPending(2)
        reached = await waitUntil { h.received == 4 }
        XCTAssertTrue(reached, "finishing two frees their slots for the queued clips")
        h.answerPending(2)   // don't leave the last two reservations hanging past the test
    }

    func testAClipsFailureSurvivesALaterClipSucceeding() async throws {
        let h = harness()
        let bad = FileManager.default.temporaryDirectory.appending(path: "notes-\(UUID().uuidString).txt")
        try Data("not a video".utf8).write(to: bad)
        let good = FileManager.default.temporaryDirectory.appending(path: "good-\(UUID().uuidString).mp4")
        try Data("video".utf8).write(to: good)

        let first = await h.coordinator.enqueue(fileURL: bad, projectID: h.projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource, role: .clip)
        let second = await h.coordinator.enqueue(fileURL: good, projectID: h.projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource, role: .clip)

        XCTAssertFalse(first)
        XCTAssertTrue(second)
        XCTAssertEqual(h.coordinator.failures.count, 1, "the failed clip is reported in place")
        XCTAssertEqual(h.coordinator.failures.first?.filename, bad.lastPathComponent)
        XCTAssertNotNil(h.coordinator.lastError, "a later clip's enqueue used to erase this before anyone saw it")
    }

    func testStaleClaimsAreDroppedOnRelaunchButAttachedOnesSurvive() throws {
        let key = "kria.test.select.\(UUID().uuidString)"
        let projectID = UUID()
        try seedLedger(key: key, projectID: projectID,
                       PhotoSelectionEntry(assetIdentifier: "ghost", recordID: UUID(), mediaID: nil, role: .clip, itemID: nil, boundAt: nil),   // its upload died with the process
                       PhotoSelectionEntry(assetIdentifier: "attached", recordID: UUID(), mediaID: "media-1", role: .clip, itemID: nil, boundAt: nil))

        let h = harness(key: key, projectID: projectID)

        let restored = h.coordinator.photoSelections[projectID.uuidString]?.entries.values.map(\.assetIdentifier) ?? []
        XCTAssertFalse(restored.contains("ghost"), "a claim with no upload behind it would block re-choosing that asset")
        XCTAssertTrue(restored.contains("attached"), "an attached clip must still show as chosen after a relaunch")
    }

    private func seedLedger(key: String, projectID: UUID, _ entries: PhotoSelectionEntry...) throws {
        var selection = ProjectPhotoSelection()
        for entry in entries {
            selection.entries[ProjectPhotoSelection.key(role: entry.role, itemID: entry.itemID, assetIdentifier: entry.assetIdentifier)] = entry
        }
        UserDefaults.standard.set(try JSONEncoder().encode([projectID.uuidString: selection]), forKey: "\(key).photo-selection")
    }
}
