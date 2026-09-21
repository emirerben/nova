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
        UploadRetryProtocol.handler = { transport in transport.finish(500, Data()) }
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
        let importedFiles = (try? FileManager.default.contentsOfDirectory(at: projectDirectory.originals, includingPropertiesForKeys: nil)) ?? []
        XCTAssertEqual(importedFiles.count, 1, "prepare()'s import step must have actually run against the staged file")
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
