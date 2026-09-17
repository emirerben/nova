import Foundation
import UniformTypeIdentifiers
import Combine
import KriaMediaEngine
import AVFoundation
import UIKit

@MainActor final class BackgroundUploadLifecycle {
    static let shared = BackgroundUploadLifecycle()
    private var completionHandler: (() -> Void)?
    private init() {}
    func store(completionHandler: @escaping () -> Void) { self.completionHandler = completionHandler }
    func finish() {
        let completionHandler = self.completionHandler
        self.completionHandler = nil
        completionHandler?()
    }
}

struct UploadRecoveryRecord: Codable, Identifiable, Sendable, Equatable {
    let id: UUID
    let projectID: UUID
    let localFilePath: String
    let filename: String
    let source: UploadSource
    let purpose: UploadPurpose
    var uploadContract: ProjectMediaUploadContract? = nil
    var reservationID: UUID?
    var clientUploadID: String?
    var mediaID: String?
    var gcsPath: String?
    var contentType: String?
    var uploadCompleted: Bool?
    var retentionExpiresAt: Date?
    var taskIdentifier: Int
    var retryCount: Int
    var mediaRole: CreationMediaRole? = nil
    var itemID: String? = nil
    var visualReservationID: String? = nil
    var role: CreationMediaRole { mediaRole ?? .clip }
}

enum UploadRecoveryAction: Equatable, Sendable { case retry, keepForManualRetry, chooseFileAgain }
struct UploadRecoveryPolicy: Sendable {
    let maximumAutomaticRetries: Int
    init(maximumAutomaticRetries: Int = 1) { self.maximumAutomaticRetries = maximumAutomaticRetries }
    func action(retryCount: Int, statusCode: Int?, fileExists: Bool) -> UploadRecoveryAction {
        guard fileExists else { return .chooseFileAgain }
        guard retryCount < maximumAutomaticRetries else { return .keepForManualRetry }
        if statusCode == nil || statusCode == 408 || statusCode == 429 || (500...599).contains(statusCode ?? 0) { return .retry }
        if statusCode == 401 || statusCode == 403 { return .retry }
        return .keepForManualRetry
    }

    func uploadReachedStorage(statusCode: Int?, hasTransportError: Bool) -> Bool {
        guard !hasTransportError, let statusCode else { return false }
        return (200..<300).contains(statusCode) || statusCode == 412
    }
}

/// `PreparingUpload.state`:
/// - `.preparing` — `prepare()` (import + proxy transcode) is presumed running,
///   either in this process right now or in a process that no longer exists.
/// - `.expired` — a `UIApplication.beginBackgroundTask` assertion covering this
///   entry's `prepare()`/reservation window ran out before the work finished.
///   The staged file is deliberately kept: the app is very likely about to be
///   suspended or killed, and the next launch should resume, not discard.
/// - `.interrupted` — set transiently by `recoverInterruptedPreparations()` on
///   an entry it has decided to resume (see below), so a crash mid-resume is
///   still discoverable as belonging to a previous, now-gone process.
/// - `.retryLater` — a resume attempt ran and hit a TRANSIENT failure (offline,
///   a 5xx/429-shaped `APIError.requestFailed`/`.offline`, or any `URLError`) —
///   see `BackgroundUploadCoordinator.isTransientResumeFailure`. The staged file
///   is kept and the entry's `launchToken` is rerolled so a later
///   `recoverInterruptedPreparations()` call (same launch or a future one) sees
///   it as orphaned again and retries, subject to the `resumeBackoffInterval`
///   heartbeat gate so repeated `openWorkspace()` calls while offline don't
///   re-run the transcode every time.
enum PreparationState: String, Codable, Sendable { case preparing, expired, interrupted, retryLater }

/// A durably-staged upload not yet past `prepare()` (import + proxy transcode for
/// `.clip`) — the window before any reservation or `UploadRecoveryRecord` exists.
/// Deliberately NOT part of `BackgroundUploadCoordinator.records`: that array is
/// `@Published` and observed by retry/cancel/UI, and this placeholder describes
/// work legitimately still in flight on the calling `Task`, not a completed or
/// failed attempt those act on. Persisted under its own UserDefaults key so a
/// crash/force-quit during `prepare()` is discoverable on next launch.
///
/// `recoverInterruptedPreparations()` runs at every `openWorkspace()` and must
/// tell three situations apart: (1) `prepare()` is still genuinely running in
/// *this* process (e.g. the workspace was reopened while a just-picked file is
/// still transcoding) — never touch it; (2) the process that wrote this entry
/// is gone (crash, force-quit, or a background-task expiration that outlived
/// the app) but the staged source file survives — RESUME by re-running
/// `prepare()` from that file, silently, no user-visible error unless the
/// resume itself fails; (3) the staged file is gone too — nothing to resume,
/// so the entry is dropped and `lastError` asks the user to choose the file
/// again. Distinguishing (1) from (2) uses a per-launch token: an entry tagged
/// with the coordinator's current `launchToken` (or whose id is in
/// `activePreparationIDs`) is presumed live; anything else is presumed to
/// belong to a process that no longer exists.
struct PreparingUpload: Codable, Sendable, Equatable {
    let id: UUID
    let projectID: UUID
    let localFilePath: String
    let filename: String
    let source: UploadSource
    let purpose: UploadPurpose
    let role: CreationMediaRole
    let itemID: String?
    var state: PreparationState
    var lastHeartbeatAt: Date
    /// Identifies the process that most recently wrote/touched this entry.
    /// Compared against `BackgroundUploadCoordinator.launchToken` to tell a
    /// still-running preparation from an orphaned one (see type doc above).
    var launchToken: UUID

    init(id: UUID, projectID: UUID, localFilePath: String, filename: String, source: UploadSource, purpose: UploadPurpose, role: CreationMediaRole, itemID: String?, state: PreparationState = .preparing, lastHeartbeatAt: Date = Date(), launchToken: UUID) {
        self.id = id
        self.projectID = projectID
        self.localFilePath = localFilePath
        self.filename = filename
        self.source = source
        self.purpose = purpose
        self.role = role
        self.itemID = itemID
        self.state = state
        self.lastHeartbeatAt = lastHeartbeatAt
        self.launchToken = launchToken
    }

    // Custom decoding so an entry persisted by an older build (missing the
    // state/heartbeat/launchToken fields added for KRI-114 P0-4) still loads
    // instead of taking down the whole `[PreparingUpload]` array on decode
    // failure (see `restorePreparingUploads`'s `try?`). A missing launch token
    // is deliberately randomized rather than defaulted to some fixed value —
    // it can never accidentally equal a live coordinator's token, so an
    // old-format leftover is always correctly treated as belonging to a gone
    // process and becomes a resume (or choose-again) candidate.
    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        projectID = try container.decode(UUID.self, forKey: .projectID)
        localFilePath = try container.decode(String.self, forKey: .localFilePath)
        filename = try container.decode(String.self, forKey: .filename)
        source = try container.decode(UploadSource.self, forKey: .source)
        purpose = try container.decode(UploadPurpose.self, forKey: .purpose)
        role = try container.decode(CreationMediaRole.self, forKey: .role)
        itemID = try container.decodeIfPresent(String.self, forKey: .itemID)
        state = try container.decodeIfPresent(PreparationState.self, forKey: .state) ?? .interrupted
        lastHeartbeatAt = try container.decodeIfPresent(Date.self, forKey: .lastHeartbeatAt) ?? .distantPast
        launchToken = try container.decodeIfPresent(UUID.self, forKey: .launchToken) ?? UUID()
    }
}

/// Thin seam over `UIApplication.beginBackgroundTask`/`endBackgroundTask`
/// (`BackgroundUploadCoordinator` is the only production conformer) so unit
/// tests can assert the begin/end balance and trigger expiration
/// deterministically, without a real `UIApplication` background-task runtime.
@MainActor protocol BackgroundActivityAssertion: Sendable {
    func begin(name: String, expirationHandler: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier
    func end(_ identifier: UIBackgroundTaskIdentifier)
}

@MainActor struct UIKitBackgroundActivityAssertion: BackgroundActivityAssertion {
    func begin(name: String, expirationHandler: @escaping @Sendable () -> Void) -> UIBackgroundTaskIdentifier {
        UIApplication.shared.beginBackgroundTask(withName: name, expirationHandler: expirationHandler)
    }
    func end(_ identifier: UIBackgroundTaskIdentifier) {
        guard identifier != .invalid else { return }
        UIApplication.shared.endBackgroundTask(identifier)
    }
}

@MainActor final class BackgroundUploadCoordinator: NSObject, ObservableObject, URLSessionTaskDelegate, @unchecked Sendable {
    static let sessionIdentifier = "com.kria.app.media-uploads"
    @Published private(set) var records: [UploadRecoveryRecord] = []
    @Published private(set) var progress: [UUID: Double] = [:]
    @Published private(set) var lastError: String?
    @Published private(set) var attachedThreads: [UUID: CreationThread] = [:]

    private let api: KriaAPIClient
    private let defaultsKey: String
    private var backgroundSession: URLSession!
    private var retryingRecords: Set<UUID> = []
    private var cancellingRecords: Set<UUID> = []
    private var attachmentTasks: [UUID: (token: UUID, task: Task<Void, Never>)] = [:]
    private let backgroundActivity: any BackgroundActivityAssertion
    private static let preparationBackgroundTaskName = "com.kria.app.upload-preparation"
    /// Identifies this process, generated once per coordinator (i.e. once per
    /// app launch, since the coordinator is a long-lived singleton). See the
    /// `PreparingUpload` doc comment for how this disambiguates a still-running
    /// preparation from one left behind by a process that no longer exists.
    let launchToken = UUID()
    /// `PreparingUpload.id`s with a `prepare()`/reservation call currently in
    /// flight on this coordinator instance. Consulted by
    /// `recoverInterruptedPreparations()` so it never reaps work that is
    /// legitimately still running just because `openWorkspace()` was re-entered.
    private var activePreparationIDs: Set<UUID> = []

    init(api: KriaAPIClient, defaultsKey: String = "kria.background-upload-recovery.v1", sessionConfiguration: URLSessionConfiguration? = nil, backgroundActivity: any BackgroundActivityAssertion = UIKitBackgroundActivityAssertion()) {
        self.api = api
        self.defaultsKey = defaultsKey
        self.backgroundActivity = backgroundActivity
        super.init()
        records = Self.restoreRecords(key: defaultsKey)
        let configuration = sessionConfiguration ?? URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        configuration.waitsForConnectivity = true
        backgroundSession = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }

    #if DEBUG
    /// Test-only seam: lets a unit test simulate "`prepare()` is genuinely
    /// still running in this process" without racing a real transcode.
    func test_markPreparationActive(_ id: UUID) { activePreparationIDs.insert(id) }
    func test_clearPreparationActive(_ id: UUID) { activePreparationIDs.remove(id) }
    #endif

    @discardableResult
    func enqueue(fileURL: URL, projectID: UUID, source: UploadSource, consentGiven: Bool, purpose: UploadPurpose, role: CreationMediaRole = .clip, itemID: String? = nil, limit: CreationMediaLimit? = nil) async -> Bool {
        lastError = nil
        var recoveryCopy: URL?
        var accepted = false
        let recordID = UUID()
        var staged = false
        // On any exit before `accepted` becomes true, undo whatever this attempt
        // staged. A genuine crash/force-quit bypasses this `defer` entirely — the
        // whole point: it leaves the staging entry behind for
        // `recoverInterruptedPreparations()` to find on next launch, matching how
        // a crash after `startTask()` already leaves its (later) record behind.
        defer {
            if !accepted {
                if let recoveryCopy { try? FileManager.default.removeItem(at: recoveryCopy) }
                if staged { Self.clearPreparingUpload(id: recordID, key: defaultsKey, deleteLocalFile: true) }
            }
        }
        // Keeps the `prepare()`/reservation window below alive if the app is
        // backgrounded mid-transcode, so iOS doesn't suspend the process before
        // `startTask()` hands off to the (self-sufficient) background
        // `URLSessionTask`. Balanced on every exit path via `defer`, including
        // thrown errors. `role == .clip` only: that's the only path with a slow
        // AVFoundation import/transcode worth protecting.
        var backgroundTaskID: UIBackgroundTaskIdentifier?
        defer {
            if let backgroundTaskID {
                activePreparationIDs.remove(recordID)
                backgroundActivity.end(backgroundTaskID)
            }
        }
        do {
            try UploadCoordinator().validate(source: source, purpose: purpose, consentGiven: consentGiven)
            // `fileURL` (from PhotosPicker/fileImporter) is transient and may not
            // survive an app relaunch; `prepare()` below (import + proxy transcode
            // for `.clip`) can run for many seconds on a large clip. Stage a durable
            // copy and record it (outside `records` — see `PreparingUpload`) BEFORE
            // that slow step, so a crash mid-transcode is discoverable on next
            // launch instead of silently vanishing with no trace at all.
            var stagedURL: URL?
            if role == .clip {
                let copy = try Self.copyIntoRecoveryDirectory(fileURL)
                stagedURL = copy
                Self.persistPreparingUpload(PreparingUpload(id: recordID, projectID: projectID, localFilePath: copy.path,
                    filename: fileURL.lastPathComponent, source: source, purpose: purpose, role: role, itemID: itemID, launchToken: launchToken), key: defaultsKey)
                staged = true
                activePreparationIDs.insert(recordID)
                backgroundTaskID = backgroundActivity.begin(name: Self.preparationBackgroundTaskName) { [weak self] in
                    Task { @MainActor in self?.markPreparationExpired(recordID: recordID) }
                }
            }
            let prepared = role == .clip ? try await prepare(fileURL: stagedURL ?? fileURL, projectID: projectID, purpose: purpose, recordID: recordID) : (fileURL, nil, nil)
            try Self.validateProjectUploadPurpose(purpose, contract: prepared.2)
            let preparedURL = prepared.0
            let localURL = try Self.copyIntoRecoveryDirectory(preparedURL)
            recoveryCopy = localURL
            let values = try localURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            guard role.accepts(contentType) else { throw CreationUploadError.unsupportedType }
            if let limit {
                guard limit.contentTypes.contains(contentType) else { throw CreationUploadError.unsupportedType }
                if let maximum = limit.byteLimit(contentType: contentType), Int64(size) > maximum { throw CreationUploadError.tooLarge }
            }
            let clientUploadID = "ios-\(recordID.uuidString)"
            let (reservation, visualReservationID) = try await reserve(
                projectID: projectID, itemID: itemID, role: role, clientUploadID: clientUploadID,
                filename: fileURL.lastPathComponent, contentType: contentType, size: Int64(size), contract: prepared.2
            )
            if let original = prepared.1 {
                try SourceAssetStore(project: Self.projectDirectory(projectID)).bind(mediaID: reservation.mediaID, original: original)
            }
            // The staging entry (if any) is superseded by the real task record
            // `startTask` persists below under the same `recordID` — clear it and
            // its now-redundant staged copy first so nothing double-tracks this attempt.
            if staged { Self.clearPreparingUpload(id: recordID, key: defaultsKey, deleteLocalFile: true); staged = false }
            try startTask(
                recordID: recordID,
                localURL: localURL,
                filename: fileURL.lastPathComponent,
                projectID: projectID,
                source: source,
                purpose: purpose,
                reservation: reservation,
                clientUploadID: clientUploadID,
                retryCount: 0, role: role, itemID: itemID, visualReservationID: visualReservationID, uploadContract: prepared.2
            )
            accepted = true
            return true
        } catch {
            lastError = error.localizedDescription
            return false
        }
    }

    func cancel(recordID: UUID) async {
        guard let record = records.first(where: { $0.id == recordID }), cancellingRecords.insert(recordID).inserted else { return }
        defer { cancellingRecords.remove(recordID) }
        let tasks = await backgroundSession.allTasks
        tasks.first(where: { $0.taskIdentifier == record.taskIdentifier })?.cancel()
        if record.role == .visual, let itemID = record.itemID, let reservationID = record.visualReservationID {
            try? await api.removeVisual(itemID: itemID, assetID: reservationID)
        }
        if let reservationID = record.reservationID {
            try? await api.cancelUpload(reservationID: reservationID)
        }
        remove(recordID, deleteLocalFile: true)
    }

    /// Surfaces any `PreparingUpload` still on disk: `enqueue` normally clears its
    /// entry before returning (success or graceful failure), so anything left
    /// belongs either to a preparation genuinely still running in this process,
    /// or to one whose process is gone (crash, force-quit, or a background-task
    /// expiration that outlived the app). Entries in the first bucket — this
    /// launch's own `launchToken`, or an id in `activePreparationIDs` — are left
    /// completely untouched. Entries in the second bucket are RESUMED from their
    /// staged file (silently — `prepare()` runs again, no user-visible error
    /// unless the resume itself fails) when that file still exists; only when
    /// it's gone too does this fall back to dropping the entry and asking the
    /// user to choose the file again. Call once at launch, before
    /// `restorePendingTasks()`. Returns the spawned resume tasks so tests can
    /// await them deterministically; production callers can ignore the result.
    /// Minimum time since `lastHeartbeatAt` before an orphaned entry is resumed
    /// again. `recoverInterruptedPreparations()` runs on every `openWorkspace()`,
    /// so without this a device that's offline would re-run the full
    /// import/transcode on every workspace open. Not applied to the
    /// active-in-this-process / same-launch-token bucket (those are never
    /// touched at all) or to the missing-staged-file bucket (nothing to retry).
    private static let resumeBackoffInterval: TimeInterval = 60

    @discardableResult
    func recoverInterruptedPreparations() -> [Task<Void, Never>] {
        let leftover = Self.restorePreparingUploads(key: defaultsKey)
        guard !leftover.isEmpty else { return [] }
        var toKeep: [PreparingUpload] = []
        var toResume: [PreparingUpload] = []
        var missingStagedFile = false
        let now = Date()
        for var entry in leftover {
            guard !activePreparationIDs.contains(entry.id), entry.launchToken != launchToken else {
                // Still legitimately running in this process (or written earlier
                // this same launch) -- e.g. `openWorkspace()` re-entered while
                // `prepare()` is still in flight for a just-picked file. Never reap.
                toKeep.append(entry)
                continue
            }
            guard FileManager.default.fileExists(atPath: entry.localFilePath) else {
                missingStagedFile = true
                continue
            }
            guard now.timeIntervalSince(entry.lastHeartbeatAt) >= Self.resumeBackoffInterval else {
                // Too soon since the last attempt/heartbeat -- leave it exactly as
                // it is; it'll be reconsidered once the backoff elapses.
                toKeep.append(entry)
                continue
            }
            entry.state = .interrupted
            entry.launchToken = launchToken
            toKeep.append(entry)
            toResume.append(entry)
        }
        Self.persistPreparingUploads(toKeep, key: defaultsKey)
        if missingStagedFile {
            lastError = "An upload was interrupted before it could start. Choose the file again."
        }
        return toResume.map { resumePreparation($0) }
    }

    /// Re-runs `prepare()`/reservation for an entry `recoverInterruptedPreparations()`
    /// decided belongs to a gone process, from its still-present staged file.
    /// Mirrors the corresponding window in `enqueue()`: same background-task
    /// assertion + `activePreparationIDs` tracking, same "fall back to choose-file-
    /// again" behavior, just entered from a persisted `PreparingUpload` instead of
    /// a fresh caller. Returns the spawned `Task` for test observability.
    @discardableResult
    private func resumePreparation(_ entry: PreparingUpload) -> Task<Void, Never> {
        let recordID = entry.id
        activePreparationIDs.insert(recordID)
        let backgroundTaskID = backgroundActivity.begin(name: Self.preparationBackgroundTaskName) { [weak self] in
            Task { @MainActor in self?.markPreparationExpired(recordID: recordID) }
        }
        return Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                self.activePreparationIDs.remove(recordID)
                self.backgroundActivity.end(backgroundTaskID)
            }
            await self.performResume(entry)
        }
    }

    private func performResume(_ entry: PreparingUpload) async {
        let recordID = entry.id
        let stagedURL = URL(fileURLWithPath: entry.localFilePath)
        // Tracks the fresh (post-import/transcode) copy `copyIntoRecoveryDirectory`
        // produces below, distinct from `stagedURL`/`entry.localFilePath` -- the
        // ORIGINAL staged source, which is the thing worth protecting from data
        // loss. On any failure this fresh copy is discarded either way (a
        // transient failure will re-run `prepare()` from `stagedURL` again next
        // attempt, a definitive one drops everything), it just shouldn't leak.
        var recoveryCopy: URL?
        do {
            let prepared = try await prepare(fileURL: stagedURL, projectID: entry.projectID, purpose: entry.purpose, recordID: recordID)
            try Self.validateProjectUploadPurpose(entry.purpose, contract: prepared.2)
            let preparedURL = prepared.0
            let localURL = try Self.copyIntoRecoveryDirectory(preparedURL)
            recoveryCopy = localURL
            let values = try localURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            guard entry.role.accepts(contentType) else { throw CreationUploadError.unsupportedType }
            let clientUploadID = "ios-\(recordID.uuidString)"
            let (reservation, visualReservationID) = try await reserve(
                projectID: entry.projectID, itemID: entry.itemID, role: entry.role, clientUploadID: clientUploadID,
                filename: entry.filename, contentType: contentType, size: Int64(size), contract: prepared.2
            )
            if let original = prepared.1 {
                try SourceAssetStore(project: Self.projectDirectory(entry.projectID)).bind(mediaID: reservation.mediaID, original: original)
            }
            Self.clearPreparingUpload(id: recordID, key: defaultsKey, deleteLocalFile: true)
            try startTask(
                recordID: recordID,
                localURL: localURL,
                filename: entry.filename,
                projectID: entry.projectID,
                source: entry.source,
                purpose: entry.purpose,
                reservation: reservation,
                clientUploadID: clientUploadID,
                retryCount: 0, role: entry.role, itemID: entry.itemID, visualReservationID: visualReservationID, uploadContract: prepared.2
            )
        } catch {
            if let recoveryCopy { try? FileManager.default.removeItem(at: recoveryCopy) }
            // A TRANSIENT failure (offline, a 5xx/429-shaped server error, or any
            // transport/timeout) must never destroy the staged original -- that's
            // exactly the data loss this lane exists to prevent. Only a
            // DEFINITIVE, non-network rejection (bad file content, consent,
            // `prepare()`'s own decode/validation guards, a real 4xx like
            // `.conflict`/`.sessionExpired`) drops the entry for good.
            if Self.isTransientResumeFailure(error) {
                markPreparationRetryLater(recordID: recordID)
                lastError = "Kria couldn't resume an interrupted upload. It will retry when you're back online."
            } else {
                lastError = "The original upload could not be resumed. Choose the file again."
                Self.clearPreparingUpload(id: recordID, key: defaultsKey, deleteLocalFile: true)
            }
        }
    }

    /// `APIError.requestFailed(status:)` carries the real HTTP status: 5xx and
    /// 429 are transient (server hiccups / rate limiting), any other 4xx is a
    /// definitive rejection of this upload.
    private static func isTransientResumeFailure(_ error: Error) -> Bool {
        if error is URLError { return true }
        if let apiError = error as? APIError {
            switch apiError {
            case .offline: return true
            case .requestFailed(let status): return status >= 500 || status == 429
            case .invalidResponse, .sessionExpired, .conflict, .unsupported, .contentPlanUnavailable, .editorNotReady: return false
            }
        }
        // `CreationUploadError`, `UploadConsentError`, `SourceAssetError`, and any
        // AVFoundation/file-system error from `prepare()` itself are all
        // definitive: retrying with the same staged bytes would fail identically.
        return false
    }

    /// Keeps the entry and its staged file, rolling the `launchToken` so a later
    /// `recoverInterruptedPreparations()` call (this launch or a future one)
    /// treats it as orphaned again and retries -- gated by `resumeBackoffInterval`
    /// via the fresh `lastHeartbeatAt` stamped here.
    private func markPreparationRetryLater(recordID: UUID) {
        var uploads = Self.restorePreparingUploads(key: defaultsKey)
        guard let index = uploads.firstIndex(where: { $0.id == recordID }) else { return }
        uploads[index].state = .retryLater
        uploads[index].lastHeartbeatAt = Date()
        uploads[index].launchToken = UUID()
        Self.persistPreparingUploads(uploads, key: defaultsKey)
    }

    /// Background-task expiration handler: the app is very likely about to be
    /// suspended or killed, so mark the entry `.expired` and leave the staged
    /// file in place — never delete it here — so the NEXT launch's
    /// `recoverInterruptedPreparations()` resumes it instead of finding nothing.
    private func markPreparationExpired(recordID: UUID) {
        var uploads = Self.restorePreparingUploads(key: defaultsKey)
        guard let index = uploads.firstIndex(where: { $0.id == recordID }) else { return }
        uploads[index].state = .expired
        uploads[index].lastHeartbeatAt = Date()
        Self.persistPreparingUploads(uploads, key: defaultsKey)
    }

    /// Updates `lastHeartbeatAt` at `prepare()`'s phase boundaries. Best-effort
    /// diagnostic signal (does not itself drive any recovery decision); no-ops
    /// if the entry was already cleared.
    private func updatePreparationHeartbeat(recordID: UUID) {
        var uploads = Self.restorePreparingUploads(key: defaultsKey)
        guard let index = uploads.firstIndex(where: { $0.id == recordID }) else { return }
        uploads[index].lastHeartbeatAt = Date()
        Self.persistPreparingUploads(uploads, key: defaultsKey)
    }

    func restorePendingTasks() async {
        let tasks = await backgroundSession.allTasks
        let active = Set(tasks.map(\.taskIdentifier))
        for record in records where !active.contains(record.taskIdentifier) {
            if record.uploadCompleted == true {
                await attach(record)
                continue
            }
            let fileExists = FileManager.default.fileExists(atPath: record.localFilePath)
            switch UploadRecoveryPolicy().action(
                retryCount: record.retryCount,
                statusCode: nil,
                fileExists: fileExists
            ) {
            case .retry:
                await retry(record)
            case .chooseFileAgain:
                lastError = "The original file is no longer available. Choose it again."
                remove(record.id, deleteLocalFile: false)
            case .keepForManualRetry:
                continue
            }
        }
    }

    func retryUpload(recordID: UUID) async {
        guard let record = records.first(where: { $0.id == recordID }) else { return }
        if record.uploadCompleted == true { await attach(record) } else { await retry(record) }
    }

    func retryAttachment(recordID: UUID) async {
        guard let record = records.first(where: { $0.id == recordID }), record.uploadCompleted == true else { return }
        await attach(record)
    }

    nonisolated func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard totalBytesExpectedToSend > 0 else { return }
        Task { @MainActor [weak self] in
            guard let self, let record = self.records.first(where: { $0.taskIdentifier == task.taskIdentifier }) else { return }
            self.progress[record.id] = Double(totalBytesSent) / Double(totalBytesExpectedToSend)
        }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        let status = (task.response as? HTTPURLResponse)?.statusCode
        Task { @MainActor [weak self] in await self?.completed(taskIdentifier: task.taskIdentifier, status: status, error: error) }
    }

    nonisolated func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        Task { @MainActor in BackgroundUploadLifecycle.shared.finish() }
    }

    private func completed(taskIdentifier: Int, status: Int?, error: (any Error)?) async {
        guard let record = records.first(where: { $0.taskIdentifier == taskIdentifier }) else { return }
        // Creation upload keys are unique to the persisted client upload id and
        // GCS writes are atomic. A retry after an app crash can therefore see
        // 412 from `if-generation-match: 0` only when our prior PUT already
        // completed; resume at the idempotent attachment step.
        if UploadRecoveryPolicy().uploadReachedStorage(statusCode: status, hasTransportError: error != nil) {
            progress[record.id] = 1
            if let index = records.firstIndex(where: { $0.id == record.id }) {
                records[index].uploadCompleted = true
                persist()
                await attach(records[index])
            }
        } else if UploadRecoveryPolicy().action(retryCount: record.retryCount, statusCode: status, fileExists: true) == .retry {
            await retry(record)
        } else {
            lastError = error?.localizedDescription ?? "The upload could not be completed."
        }
    }

    static func validateProjectUploadPurpose(_ purpose: UploadPurpose, contract: ProjectMediaUploadContract? = nil) throws {
        if purpose == .analysisProxy {
            guard contract?.purpose == .analysisProxy, contract?.proxy != nil else { throw CreationUploadError.proxyContractUnavailable }
        } else if contract != nil { throw APIError.invalidResponse }
    }

    private func retry(_ record: UploadRecoveryRecord) async {
        do { try Self.validateProjectUploadPurpose(record.purpose, contract: record.uploadContract) }
        catch { lastError = error.localizedDescription; return }

        guard records.contains(where: { $0.id == record.id }), !cancellingRecords.contains(record.id), retryingRecords.insert(record.id).inserted else { return }
        defer { retryingRecords.remove(record.id) }
        let active = await backgroundSession.allTasks
        guard !active.contains(where: { $0.taskIdentifier == record.taskIdentifier }),
              !cancellingRecords.contains(record.id), records.contains(where: { $0.id == record.id }) else { return }
        lastError = nil
        let localURL = URL(fileURLWithPath: record.localFilePath)
        guard FileManager.default.fileExists(atPath: localURL.path) else {
            lastError = "The original file is no longer available. Choose it again."
            remove(record.id, deleteLocalFile: false)
            return
        }
        do {
            let values = try localURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            let clientUploadID = record.clientUploadID ?? "ios-\(record.id.uuidString)"
            let (reservation, visualReservationID) = try await reserve(
                projectID: record.projectID, itemID: record.itemID, role: record.role,
                clientUploadID: clientUploadID, filename: record.filename, contentType: contentType, size: Int64(size), contract: record.uploadContract
            )
            guard !cancellingRecords.contains(record.id), records.contains(where: { $0.id == record.id }) else { return }
            if let reservationID = record.reservationID {
                try? await api.cancelUpload(reservationID: reservationID)
            }
            guard !cancellingRecords.contains(record.id), records.contains(where: { $0.id == record.id }) else { return }
            remove(record.id, deleteLocalFile: false)
            try startTask(
                recordID: record.id,
                localURL: localURL,
                filename: record.filename,
                projectID: record.projectID,
                source: record.source,
                purpose: record.purpose,
                reservation: reservation,
                clientUploadID: clientUploadID,
                retryCount: record.retryCount + 1, role: record.role, itemID: record.itemID, visualReservationID: visualReservationID, uploadContract: record.uploadContract
            )
        } catch { lastError = error.localizedDescription }
    }

    private func attach(_ record: UploadRecoveryRecord) async {
        do { try Self.validateProjectUploadPurpose(record.purpose, contract: record.uploadContract) }
        catch { lastError = error.localizedDescription; return }

        let projectID = record.projectID
        let predecessor = attachmentTasks[projectID]?.task
        let token = UUID()
        let task = Task { @MainActor [weak self] in
            await predecessor?.value
            guard let self else { return }
            await self.performAttachment(record)
        }
        attachmentTasks[projectID] = (token, task)
        await task.value
        if attachmentTasks[projectID]?.token == token {
            attachmentTasks.removeValue(forKey: projectID)
        }
    }

    private func performAttachment(_ record: UploadRecoveryRecord) async {
        lastError = nil
        guard records.contains(where: { $0.id == record.id }) else { return }
        if record.role == .visual {
            do {
                guard let itemID = record.itemID, let reservationID = record.visualReservationID,
                      let path = record.gcsPath, let contentType = record.contentType else { throw APIError.invalidResponse }
                _ = try await api.registerVisual(itemID: itemID, reservationID: reservationID, gcsPath: path, contentType: contentType, filename: record.filename)
                attachedThreads[record.projectID] = try await api.project(threadID: record.projectID)
                remove(record.id, deleteLocalFile: true)
            } catch { lastError = error.localizedDescription }
            return
        }
        guard
            let mediaID = record.mediaID,
            let gcsPath = record.gcsPath,
            let contentType = record.contentType
        else {
            lastError = "This upload was created by an older build. Choose the file again."
            return
        }
        var lastAttachmentError: (any Error)?
        for _ in 0..<2 {
            do {
                let current = try await api.project(threadID: record.projectID)
                let attachedThread = try await api.attachProjectMedia(
                    threadID: record.projectID,
                    mediaID: mediaID,
                    gcsPath: gcsPath,
                    filename: record.filename,
                    contentType: contentType,
                    expectedRevision: current.revision,
                    clientEventID: "ios-attach-\(record.id.uuidString)"
                )
                // Publish the authoritative media_count before removing the
                // pending record so clip capacity never briefly reopens.
                if record.role == .clip {
                    await CreationMediaPreview.save(localURL: URL(fileURLWithPath: record.localFilePath), mediaID: mediaID)
                }
                attachedThreads[record.projectID] = attachedThread
                remove(record.id, deleteLocalFile: true)
                return
            } catch {
                lastAttachmentError = error
            }
        }
        lastError = lastAttachmentError?.localizedDescription ?? "The uploaded footage could not be attached to this project."
    }

    private func startTask(recordID: UUID, localURL: URL, filename: String, projectID: UUID, source: UploadSource, purpose: UploadPurpose, reservation: ProjectUploadReservation, clientUploadID: String, retryCount: Int, role: CreationMediaRole, itemID: String?, visualReservationID: String?, uploadContract: ProjectMediaUploadContract? = nil) throws {
        var request = URLRequest(url: reservation.uploadURL)
        request.httpMethod = "PUT"
        request.setValue(reservation.contentType, forHTTPHeaderField: "Content-Type")
        for (name, value) in reservation.uploadHeaders { request.setValue(value, forHTTPHeaderField: name) }
        let task = backgroundSession.uploadTask(with: request, fromFile: localURL)
        let record = UploadRecoveryRecord(
            id: recordID,
            projectID: projectID,
            localFilePath: localURL.path,
            filename: filename,
            source: source,
            purpose: purpose,
            uploadContract: uploadContract,
            reservationID: nil,
            clientUploadID: clientUploadID,
            mediaID: reservation.mediaID,
            gcsPath: reservation.gcsPath,
            contentType: reservation.contentType,
            uploadCompleted: false,
            retentionExpiresAt: nil,
            taskIdentifier: task.taskIdentifier,
            retryCount: retryCount, mediaRole: role, itemID: itemID, visualReservationID: visualReservationID
        )
        records.append(record)
        persist()
        task.resume()
    }

    private func reserve(projectID: UUID, itemID: String?, role: CreationMediaRole, clientUploadID: String, filename: String, contentType: String, size: Int64, contract: ProjectMediaUploadContract? = nil) async throws -> (ProjectUploadReservation, String?) {
        if let contract {
            guard role == .clip else { throw APIError.invalidResponse }
            let target = try await api.reserveProjectProxyUpload(threadID: projectID, clientUploadID: clientUploadID, filename: filename, size: size, contract: contract)
            return (target, nil)
        }
        if role == .visual {
            guard let itemID else { throw APIError.invalidResponse }
            let target = try await api.reserveVisualUpload(itemID: itemID, clientUploadID: clientUploadID, filename: filename, contentType: contentType, size: size)
            return (ProjectUploadReservation(mediaID: target.reservationID, uploadURL: target.uploadURL, gcsPath: target.gcsPath, contentType: contentType, uploadHeaders: target.uploadHeaders), target.reservationID)
        }
        return (try await api.reserveProjectUpload(threadID: projectID, clientUploadID: clientUploadID, filename: filename, contentType: contentType, size: size), nil)
    }

    static func projectDirectory(_ projectID: UUID) -> ProjectDirectory {
        let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "KriaProjects/\(projectID.uuidString)", directoryHint: .isDirectory)
        return ProjectDirectory(root: root)
    }

    private func prepare(fileURL: URL, projectID: UUID, purpose: UploadPurpose, recordID: UUID) async throws -> (URL, MediaAsset?, ProjectMediaUploadContract?) {
        let project = Self.projectDirectory(projectID)
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: fileURL)
        updatePreparationHeartbeat(recordID: recordID) // import done
        let original = project.root.appending(path: asset.relativePath)
        if purpose == .analysisProxy {
            updatePreparationHeartbeat(recordID: recordID) // transcode started
            let proxy = project.proxies.appendingPathComponent("\(asset.id).mp4")
            let result = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: original, destination: proxy)
            updatePreparationHeartbeat(recordID: recordID) // transcode done
            guard let fingerprint = asset.fingerprint else { throw APIError.invalidResponse }
            let contract = try await ProjectMediaUploadContract.analysisProxy(original: original, proxy: result, fingerprint: fingerprint)
            return (result, asset, contract)
        }
        return (original, asset, nil)
    }

    private func remove(_ id: UUID, deleteLocalFile: Bool) {
        guard let record = records.first(where: { $0.id == id }) else { return }
        records.removeAll { $0.id == id }
        progress[id] = nil
        persist()
        if deleteLocalFile { try? FileManager.default.removeItem(atPath: record.localFilePath) }
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(records) else { return }
        UserDefaults.standard.set(data, forKey: defaultsKey)
    }

    private static func restoreRecords(key: String) -> [UploadRecoveryRecord] {
        guard let data = UserDefaults.standard.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([UploadRecoveryRecord].self, from: data)) ?? []
    }

    private static func preparingUploadsKey(_ key: String) -> String { "\(key).preparing" }

    private static func restorePreparingUploads(key: String) -> [PreparingUpload] {
        guard let data = UserDefaults.standard.data(forKey: preparingUploadsKey(key)) else { return [] }
        return (try? JSONDecoder().decode([PreparingUpload].self, from: data)) ?? []
    }

    private static func persistPreparingUploads(_ uploads: [PreparingUpload], key: String) {
        guard let data = try? JSONEncoder().encode(uploads) else { return }
        UserDefaults.standard.set(data, forKey: preparingUploadsKey(key))
    }

    private static func persistPreparingUpload(_ upload: PreparingUpload, key: String) {
        var uploads = restorePreparingUploads(key: key)
        uploads.append(upload)
        persistPreparingUploads(uploads, key: key)
    }

    private static func clearPreparingUpload(id: UUID, key: String, deleteLocalFile: Bool) {
        var uploads = restorePreparingUploads(key: key)
        guard let index = uploads.firstIndex(where: { $0.id == id }) else { return }
        let entry = uploads.remove(at: index)
        persistPreparingUploads(uploads, key: key)
        if deleteLocalFile { try? FileManager.default.removeItem(atPath: entry.localFilePath) }
    }

    private static func copyIntoRecoveryDirectory(_ source: URL) throws -> URL {
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appending(path: "KriaUploads", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let destination = directory.appending(path: "\(UUID().uuidString)-\(source.lastPathComponent)")
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        try FileManager.default.copyItem(at: source, to: destination)
        return destination
    }
}

enum CreationUploadError: LocalizedError {
    case unsupportedType, tooLarge, proxyContractUnavailable
    var errorDescription: String? {
        switch self {
        case .unsupportedType: "This file type is not supported here. Choose a different file."
        case .proxyContractUnavailable: "On-device creation is not available yet. Your original stays on this device."
        case .tooLarge: "This file exceeds the upload limit. Choose a smaller file."
        }
    }
}

@MainActor enum CreationMediaPreview {
    static func url(mediaID: String) -> URL {
        let safeID = Data(mediaID.utf8).base64EncodedString().replacingOccurrences(of: "/", with: "_")
        return FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appending(path: "KriaMediaPreviews/\(safeID).jpg")
    }
    static func save(localURL: URL, mediaID: String) async {
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: localURL))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 320, height: 320)
        guard let frame = try? await generator.image(at: .zero), let data = UIImage(cgImage: frame.image).jpegData(compressionQuality: 0.8) else { return }
        let destination = url(mediaID: mediaID)
        try? FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: destination, options: .atomic)
    }
}
