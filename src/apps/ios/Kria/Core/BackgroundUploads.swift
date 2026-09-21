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
    /// Photos assets the user chose, per project (keyed by `projectID.uuidString`). See `PhotoSelectionEntry`.
    @Published private(set) var photoSelections: [String: ProjectPhotoSelection] = [:]
    /// Uploads between "enqueue started" and "record published". `records` is blind to this window
    /// (a record is only appended after prepare AND reserve), so without it a clip that is being
    /// prepared doesn't count against the project's clip limit.
    @Published private(set) var inFlight: [UUID: InFlightUpload] = [:]
    /// Per-clip failures. `lastError` is one global string that any sibling's success would erase.
    @Published private(set) var failures: [UploadFailure] = []

    struct InFlightUpload: Equatable, Sendable {
        let projectID: UUID
        let role: CreationMediaRole
    }

    private let api: KriaAPIClient
    private let defaultsKey: String
    /// Bounds concurrent import/transcode work. Cloud is I/O-bound (a couple of overlapping
    /// clips saturate the disk); the phone-render proxy runs a live `AVAssetExportSession`, and
    /// more than one at once invites `AVError.operationInterrupted` on older devices.
    private let cloudGate: UploadAdmissionGate
    private let proxyGate: UploadAdmissionGate
    /// One per asset the user is currently choosing (debounce → load → enqueue), so un-choosing it
    /// can cancel the work. Keyed `"<projectID>|<assetIdentifier>"`.
    private var selectionTasks: [String: Task<Void, Never>] = [:]
    /// Assets whose `deselect` is still running. The picker's `onChange` can fire again before it
    /// finishes; a second concurrent `remove_media` would be answered "no longer attached", read as
    /// a refusal, and wrongly put the clip back.
    private var deselecting: Set<String> = []
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

    init(api: KriaAPIClient, defaultsKey: String = "kria.background-upload-recovery.v1", sessionConfiguration: URLSessionConfiguration? = nil, backgroundActivity: any BackgroundActivityAssertion = UIKitBackgroundActivityAssertion(), maxConcurrentCloudPreparations: Int = 2, maxConcurrentProxyPreparations: Int = 1) {
        self.api = api
        self.defaultsKey = defaultsKey
        self.backgroundActivity = backgroundActivity
        self.cloudGate = UploadAdmissionGate(limit: maxConcurrentCloudPreparations)
        self.proxyGate = UploadAdmissionGate(limit: maxConcurrentProxyPreparations)
        super.init()
        records = Self.restoreRecords(key: defaultsKey)
        photoSelections = Self.restorePhotoSelections(key: defaultsKey)
        pruneOrphanedClaims()
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

    /// `recordID` is caller-supplied only so a selection can find (and cancel) the work it started;
    /// every other caller lets it default.
    ///
    /// Unlike before, this does not clear `lastError` on entry: with uploads overlapping, a later
    /// clip's `enqueue` would erase an earlier clip's failure before anyone saw it. Callers that
    /// want a clean slate call `clearLastError()` once per batch.
    @discardableResult
    func enqueue(fileURL: URL, projectID: UUID, source: UploadSource, consentGiven: Bool, purpose: UploadPurpose, role: CreationMediaRole = .clip, itemID: String? = nil, limit: CreationMediaLimit? = nil, recordID: UUID = UUID()) async -> Bool {
        var recoveryCopy: URL?
        var accepted = false
        var staged = false
        var preparedToDiscard: (URL, MediaAsset?)?
        // Counts against the project's clip limit for the whole prepare/reserve window, then hands
        // over to `records` (appended by `startTask` before this returns) with no gap in between.
        inFlight[recordID] = InFlightUpload(projectID: projectID, role: role)
        defer { inFlight[recordID] = nil }
        var holdsSlot = false
        let gate = purpose == .analysisProxy ? proxyGate : cloudGate
        defer { if holdsSlot { gate.release() } }
        // On any exit before `accepted` becomes true, undo whatever this attempt
        // staged. A genuine crash/force-quit bypasses this `defer` entirely — the
        // whole point: it leaves the staging entry behind for
        // `recoverInterruptedPreparations()` to find on next launch, matching how
        // a crash after `startTask()` already leaves its (later) record behind.
        defer {
            if !accepted {
                if let recoveryCopy { try? FileManager.default.removeItem(at: recoveryCopy) }
                if staged { Self.clearPreparingUpload(id: recordID, key: defaultsKey, deleteLocalFile: true) }
                if let preparedToDiscard { Self.discardPrepared(preparedToDiscard.0, asset: preparedToDiscard.1, project: projectID) }
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
                let copy = try Self.stageIntoRecoveryDirectory(fileURL)
                stagedURL = copy
                Self.persistPreparingUpload(PreparingUpload(id: recordID, projectID: projectID, localFilePath: copy.path,
                    filename: fileURL.lastPathComponent, source: source, purpose: purpose, role: role, itemID: itemID, launchToken: launchToken), key: defaultsKey)
                staged = true
                activePreparationIDs.insert(recordID)
                backgroundTaskID = backgroundActivity.begin(name: Self.preparationBackgroundTaskName) { [weak self] in
                    Task { @MainActor in self?.markPreparationExpired(recordID: recordID) }
                }
                // Wait for a slot only AFTER staging: with a Photos pick that is now a rename, so a
                // clip queued behind others is already durable and crash-resumable rather than
                // sitting in tmp where a force-quit would lose it. Throws if cancelled while queued.
                try await gate.acquire()
                holdsSlot = true
            }
            let prepared = role == .clip ? try await prepare(fileURL: stagedURL ?? fileURL, projectID: projectID, purpose: purpose, recordID: recordID) : (fileURL, nil, nil)
            if role == .clip { preparedToDiscard = (prepared.0, prepared.1) }
            // Cancellation checkpoint 1 of 2. `prepare()` itself cannot be interrupted mid-import (a
            // synchronous copy + hash); a cancel during it only lands here, so it still costs that
            // work but never reaches the server.
            try Task.checkCancellation()
            try Self.validateProjectUploadPurpose(purpose, contract: prepared.2)
            let preparedURL = prepared.0
            let localURL = try Self.linkIntoRecoveryDirectory(preparedURL)
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
            // Cancellation checkpoint 2 of 2, and the load-bearing one: the last moment before any
            // server state (a reservation row) exists. Past this line an un-chosen clip is a real
            // upload that has to be cancelled or detached, not merely abandoned.
            try Task.checkCancellation()
            let (reservation, visualReservationID) = try await reserve(
                projectID: projectID, itemID: itemID, role: role, clientUploadID: clientUploadID,
                filename: fileURL.lastPathComponent, contentType: contentType, size: Int64(size), contract: prepared.2
            )
            preparedToDiscard = nil   // the upload owns these files from here on
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
            // An un-chosen clip is not a failure: surfacing "Swift.CancellationError error 1" would
            // tell the user something broke when they did exactly what they meant to.
            if error is CancellationError || Task.isCancelled { return false }
            lastError = error.localizedDescription
            recordFailure(id: recordID, projectID: projectID, role: role, filename: fileURL.lastPathComponent, message: error.localizedDescription)
            return false
        }
    }

    /// Wipes the global error line. Called once per batch by the picker, in place of every
    /// `enqueue` doing it (which erased a sibling clip's failure).
    func clearLastError() { lastError = nil }

    func dismissFailure(id: UUID) { failures.removeAll { $0.id == id } }

    func clearFailures(projectID: UUID, role: CreationMediaRole) {
        failures.removeAll { $0.projectID == projectID && $0.role == role }
    }

    private func recordFailure(id: UUID, projectID: UUID, role: CreationMediaRole, filename: String, message: String) {
        failures.removeAll { $0.id == id }
        failures.append(UploadFailure(id: id, projectID: projectID, role: role, filename: filename, message: message))
    }

    /// Removes files a cancelled or failed preparation left in the project tree: the imported
    /// original, and the analysis proxy on the phone-render path.
    private static func discardPrepared(_ prepared: URL, asset: MediaAsset?, project: UUID) {
        try? FileManager.default.removeItem(at: prepared)
        if let asset {
            try? FileManager.default.removeItem(at: projectDirectory(project).root.appending(path: asset.relativePath))
        }
    }

    // MARK: Selection (KRI-125)

    struct PhotoSelectionRequest {
        let assetIdentifier: String
        let projectID: UUID
        let role: CreationMediaRole
        let purpose: UploadPurpose
        let itemID: String?
        let limit: CreationMediaLimit?
        /// Ids of the media currently attached to the project, used to tell a live ledger entry from
        /// a stale one (its clip was removed elsewhere).
        let attachedMediaIDs: Set<String>
    }

    /// Starts preparing one chosen asset now, after a short debounce so a tap-then-untap costs
    /// nothing. Choosing an asset that is already chosen is a no-op, which is the dedup.
    ///
    /// The work lives here, not in the view, because `AttachmentSheet` applies `.id(role)` to the
    /// picker: switching Footage↔Visuals destroys the view and any `Task` it owned mid-upload.
    func select(_ request: PhotoSelectionRequest, debounce: Duration = .milliseconds(500), loadFile: @escaping @MainActor () async throws -> URL) {
        let key = Self.selectionKey(request.projectID, request.assetIdentifier)
        guard selectionTasks[key] == nil else { return }
        if let existing = photoSelections[request.projectID.uuidString]?.byIdentifier[request.assetIdentifier],
           existing.mediaID == nil || request.attachedMediaIDs.contains(existing.mediaID ?? "") {
            return
        }
        let recordID = UUID()
        claim(request, recordID: recordID)
        selectionTasks[key] = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.selectionTasks[key] = nil }
            var loaded: URL?
            do {
                try await Task.sleep(for: debounce)
                loaded = try await loadFile()
                guard let url = loaded else { throw CancellationError() }
                // Un-chosen while Photos was still handing the file over: don't start anything.
                try Task.checkCancellation()
                let accepted = await self.enqueue(fileURL: url, projectID: request.projectID, source: .photos, consentGiven: true, purpose: request.purpose, role: request.role, itemID: request.itemID, limit: request.limit, recordID: recordID)
                loaded = nil   // `enqueue` owns the file from here (it renames or links it)
                if !accepted { self.releaseSelection(recordID: recordID, projectID: request.projectID) }
            } catch {
                if let loaded { try? FileManager.default.removeItem(at: loaded) }
                self.releaseSelection(recordID: recordID, projectID: request.projectID)
                if !(error is CancellationError) {
                    self.recordFailure(id: recordID, projectID: request.projectID, role: request.role, filename: "Selected item", message: "This file couldn’t be read. Try Files or choose it again.")
                }
            }
        }
    }

    /// The user un-chose an asset. What that means depends on how far it got:
    /// - still choosing/preparing: cancel it; nothing exists server-side;
    /// - uploading: cancel the PUT (a single-request PUT that never completes creates no object —
    ///   see the atomicity note in `completed`), unless it already finished;
    /// - already attached (or finished uploading): detach it, deleting its file.
    func deselect(assetIdentifier: String, projectID: UUID, role: CreationMediaRole, itemID: String?) async -> DeselectOutcome {
        let pid = projectID.uuidString
        guard let entry = photoSelections[pid]?.byIdentifier[assetIdentifier] else { return .notTracked }
        let key = Self.selectionKey(projectID, assetIdentifier)
        guard deselecting.insert(key).inserted else { return .alreadyInProgress }   // the first call decides
        defer { deselecting.remove(key) }
        if let task = selectionTasks[key] {
            task.cancel()
            await task.value
        }
        // A cancelled task can still have crossed the last checkpoint (just before `reserve`) and
        // started a real upload, so look again rather than assuming it unwound.
        if let record = records.first(where: { $0.id == entry.recordID }) {
            if record.uploadCompleted != true, (progress[record.id] ?? 0) < 1 {
                await cancel(recordID: record.id)   // also releases the ledger entry
                return .cancelledUpload
            }
            // The bytes already reached storage. Let the attach that is in flight land, then treat
            // it as attached — cancelling now would strand a finished blob.
            await attachmentTasks[projectID]?.task.value
            if records.contains(where: { $0.id == entry.recordID }) {
                await cancel(recordID: entry.recordID)   // attach failed; there is nothing to detach
                return .cancelledUpload
            }
        }
        guard let current = photoSelections[pid]?.byIdentifier[assetIdentifier] else { return .discarded }
        guard let mediaID = current.mediaID else {
            releaseSelection(recordID: current.recordID, projectID: projectID)
            return .discarded
        }
        return await detach(mediaID: mediaID, role: current.role, projectID: projectID, itemID: itemID, recordID: current.recordID)
    }

    /// Detaches an attached clip (`remove_media`, which also deletes the blob server-side) or visual.
    /// Serialized with attaches so it can't race one on the thread's optimistic `expected_revision`.
    private func detach(mediaID: String, role: CreationMediaRole, projectID: UUID, itemID: String?, recordID: UUID) async -> DeselectOutcome {
        await serialized(projectID: projectID) { [self] () async -> DeselectOutcome in
            do {
                if role == .visual {
                    guard let itemID else { return .refused("Kria can’t remove this right now.") }
                    try await api.removeVisual(itemID: itemID, assetID: mediaID)
                } else {
                    let latest = try await api.project(threadID: projectID)
                    // A stable id makes a retried removal idempotent instead of a second action.
                    attachedThreads[projectID] = try await api.creationAction(
                        threadID: projectID, action: "remove_media", payload: ["media_id": .string(mediaID)],
                        expectedRevision: latest.revision, clientActionID: "ios-deselect-\(mediaID)"
                    )
                }
                releaseSelection(recordID: recordID, projectID: projectID)
                return .detached
            } catch APIError.conflict {
                // The server refuses removal while a render is running, and on a stale revision.
                return .refused("Kria can’t remove this right now. Wait for the current step to finish and try again.")
            } catch {
                return .refused("Couldn’t remove it. \(error.localizedDescription)")
            }
        }
    }

    /// Runs `body` after every attach/detach already queued for the project — the same chain
    /// `attach` uses — so nothing races the thread's optimistic `expected_revision`.
    private func serialized<T: Sendable>(projectID: UUID, _ body: @escaping @MainActor () async -> T) async -> T {
        let predecessor = attachmentTasks[projectID]?.task
        let token = UUID()
        let box = ResultBox<T>()
        let task = Task { @MainActor in
            await predecessor?.value
            box.value = await body()
        }
        attachmentTasks[projectID] = (token, task)
        await task.value
        if attachmentTasks[projectID]?.token == token { attachmentTasks.removeValue(forKey: projectID) }
        return box.value!
    }

    private final class ResultBox<T>: @unchecked Sendable { var value: T? }

    /// Identifiers the picker should show as already chosen for this project and role.
    func preselectedIdentifiers(projectID: UUID, role: CreationMediaRole, attachedMediaIDs: Set<String>) -> [String] {
        photoSelections[projectID.uuidString]?.preselected(role: role, attachedMediaIDs: attachedMediaIDs) ?? []
    }

    /// Clips that occupy a slot but aren't in `records` or attached yet: mid-prepare uploads, and
    /// chosen assets still in their debounce/load window.
    func reservedCount(projectID: UUID, role: CreationMediaRole) -> Int {
        Self.reservedCount(projectID: projectID, role: role, inFlight: inFlight, records: records, selections: photoSelections)
    }

    /// The counting rule as a pure function over values. A view that observes `$inFlight` and
    /// `$records` must call this with the values those publishers EMIT: a `@Published` sink fires
    /// before the property changes, so re-reading the coordinator inside it returns stale state.
    /// `role: nil` counts every role (what "is anything still on its way?" needs).
    nonisolated static func reservedCount(projectID: UUID, role: CreationMediaRole?, inFlight: [UUID: InFlightUpload], records: [UploadRecoveryRecord], selections: [String: ProjectPhotoSelection]) -> Int {
        func matches(_ candidate: CreationMediaRole) -> Bool { role == nil || candidate == role }
        let flying = Set(inFlight.filter { $0.value.projectID == projectID && matches($0.value.role) }.keys)
        let recorded = Set(records.filter { $0.projectID == projectID && matches($0.role) }.map(\.id))
        let entries: [PhotoSelectionEntry] = selections[projectID.uuidString].map { Array($0.byIdentifier.values) } ?? []
        let choosing = entries.filter {
            matches($0.role) && $0.mediaID == nil && !flying.contains($0.recordID) && !recorded.contains($0.recordID)
        }
        return flying.count + choosing.count
    }

    /// Reserves a slot for a clip whose file is still being fetched, so the limit is honored
    /// while it loads. Undone by `enqueue` (which owns `inFlight` from then on) or `clearInFlight`.
    func markInFlight(_ recordID: UUID, projectID: UUID, role: CreationMediaRole) {
        inFlight[recordID] = InFlightUpload(projectID: projectID, role: role)
    }

    func clearInFlight(_ recordID: UUID) { inFlight[recordID] = nil }

    func reportFailure(id: UUID = UUID(), projectID: UUID, role: CreationMediaRole, filename: String, message: String) {
        recordFailure(id: id, projectID: projectID, role: role, filename: filename, message: message)
    }

    // MARK: Ledger

    private static func selectionKey(_ projectID: UUID, _ assetIdentifier: String) -> String { "\(projectID.uuidString)|\(assetIdentifier)" }

    private func claim(_ request: PhotoSelectionRequest, recordID: UUID) {
        var selection = photoSelections[request.projectID.uuidString] ?? ProjectPhotoSelection()
        selection.byIdentifier[request.assetIdentifier] = PhotoSelectionEntry(recordID: recordID, mediaID: nil, role: request.role)
        photoSelections[request.projectID.uuidString] = selection
        persistPhotoSelections()
    }

    private func bindSelection(recordID: UUID, projectID: UUID, mediaID: String) {
        let pid = projectID.uuidString
        guard var selection = photoSelections[pid],
              let identifier = selection.byIdentifier.first(where: { $0.value.recordID == recordID })?.key else { return }
        selection.byIdentifier[identifier]?.mediaID = mediaID
        photoSelections[pid] = selection
        persistPhotoSelections()
    }

    private func releaseSelection(recordID: UUID, projectID: UUID) {
        let pid = projectID.uuidString
        guard var selection = photoSelections[pid],
              let identifier = selection.byIdentifier.first(where: { $0.value.recordID == recordID })?.key else { return }
        selection.byIdentifier[identifier] = nil
        photoSelections[pid] = selection.byIdentifier.isEmpty ? nil : selection
        persistPhotoSelections()
    }

    /// A claim with no media id whose upload is gone (a crash mid-prepare that left nothing to
    /// resume) would preselect an asset that isn't on its way anywhere and block re-choosing it.
    private func pruneOrphanedClaims() {
        let live = Set(records.map(\.id)).union(Self.restorePreparingUploads(key: defaultsKey).map(\.id))
        var changed = false
        for (pid, var selection) in photoSelections {
            for (identifier, entry) in selection.byIdentifier where entry.mediaID == nil && !live.contains(entry.recordID) {
                selection.byIdentifier[identifier] = nil
                changed = true
            }
            photoSelections[pid] = selection.byIdentifier.isEmpty ? nil : selection
        }
        if changed { persistPhotoSelections() }
    }

    private static func photoSelectionsKey(_ key: String) -> String { "\(key).photo-selection" }

    private static func restorePhotoSelections(key: String) -> [String: ProjectPhotoSelection] {
        guard let data = UserDefaults.standard.data(forKey: photoSelectionsKey(key)) else { return [:] }
        return (try? JSONDecoder().decode([String: ProjectPhotoSelection].self, from: data)) ?? [:]
    }

    private func persistPhotoSelections() {
        guard let data = try? JSONEncoder().encode(photoSelections) else { return }
        UserDefaults.standard.set(data, forKey: Self.photoSelectionsKey(defaultsKey))
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
        releaseSelection(recordID: recordID, projectID: record.projectID)
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
        inFlight[recordID] = InFlightUpload(projectID: entry.projectID, role: entry.role)
        let backgroundTaskID = backgroundActivity.begin(name: Self.preparationBackgroundTaskName) { [weak self] in
            Task { @MainActor in self?.markPreparationExpired(recordID: recordID) }
        }
        let gate = entry.purpose == .analysisProxy ? proxyGate : cloudGate
        return Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                self.activePreparationIDs.remove(recordID)
                self.inFlight[recordID] = nil
                self.backgroundActivity.end(backgroundTaskID)
            }
            // A relaunch can resume many interrupted clips at once; share the live-upload limit.
            guard (try? await gate.acquire()) != nil else { return }
            defer { gate.release() }
            await self.performResume(entry)
        }
    }

    private func performResume(_ entry: PreparingUpload) async {
        let recordID = entry.id
        let stagedURL = URL(fileURLWithPath: entry.localFilePath)
        // Tracks the fresh (post-import/transcode) file `linkIntoRecoveryDirectory`
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
            let localURL = try Self.linkIntoRecoveryDirectory(preparedURL)
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
        // No `lastError = nil` here: attaches now run concurrently with later enqueues, so a
        // success would erase a sibling clip's failure. Per-clip failures live in `failures`.
        guard records.contains(where: { $0.id == record.id }) else { return }
        if record.role == .visual {
            do {
                guard let itemID = record.itemID, let reservationID = record.visualReservationID,
                      let path = record.gcsPath, let contentType = record.contentType else { throw APIError.invalidResponse }
                _ = try await api.registerVisual(itemID: itemID, reservationID: reservationID, gcsPath: path, contentType: contentType, filename: record.filename)
                attachedThreads[record.projectID] = try await api.project(threadID: record.projectID)
                bindSelection(recordID: record.id, projectID: record.projectID, mediaID: reservationID)
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
                // Hand the asset's identity from the (about to be deleted) record to the ledger, so
                // the clip still shows as chosen the next time the picker opens.
                bindSelection(recordID: record.id, projectID: record.projectID, mediaID: mediaID)
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
        // `fileURL` is the staged file for `.clip` (ours, never edited), so the import can
        // share its bytes instead of writing them a third time. An external source stays a copy.
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: fileURL, preferLink: Self.isAppOwned(fileURL))
        updatePreparationHeartbeat(recordID: recordID) // import done
        let original = project.root.appending(path: asset.relativePath)
        if purpose == .analysisProxy {
            updatePreparationHeartbeat(recordID: recordID) // transcode started
            let proxy = project.proxies.appendingPathComponent("\(asset.id).mp4")
            let result: URL
            do {
                result = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: original, destination: proxy)
            } catch is CancellationError {
                // Un-chosen mid-transcode: the export stopped, but the import above already wrote
                // the original into the project tree and nothing else will ever reference it.
                try? FileManager.default.removeItem(at: original)
                throw CancellationError()
            }
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

    // MARK: Recovery-directory placement
    //
    // A cloud clip used to be written to disk four times (Photos export, staging, project
    // `originals/`, upload file) — ~4x the file size at peak, up to 16 GB for a 4 GB clip. Only
    // the first write is unavoidable (PhotosPicker's file is valid only inside its transfer
    // closure). The rest share those bytes when — and only when — the file is one the app owns:
    // a hardlink shares an inode, so it is safe for files we create and only ever delete, and
    // wrong for a Files/iCloud pick, where the user editing the original mid-upload must not
    // change what gets uploaded. External sources therefore always get a real copy.

    /// Application Support (project + recovery trees) and the app's own tmp.
    static func isAppOwned(_ url: URL) -> Bool {
        let fm = FileManager.default
        let roots = [fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0], fm.temporaryDirectory]
        return roots.contains { isInside(url, directory: $0) }
    }

    /// The app's tmp, whose contents are disposable by definition (the OS may purge them).
    static func isInTemporaryDirectory(_ url: URL) -> Bool {
        isInside(url, directory: FileManager.default.temporaryDirectory)
    }

    private static func isInside(_ url: URL, directory: URL) -> Bool {
        // Resolve symlinks on both sides: tmp is `/var/…` but its real path is `/private/var/…`.
        let root = directory.resolvingSymlinksInPath().standardizedFileURL.path
        return url.resolvingSymlinksInPath().standardizedFileURL.path.hasPrefix(root + "/")
    }

    /// Durable staging for crash-resume. Renames our own tmp file into place (no second write);
    /// copies anything else, including every Files/iCloud pick.
    static func stageIntoRecoveryDirectory(_ source: URL) throws -> URL {
        let destination = try recoveryDestination(for: source)
        if isInTemporaryDirectory(source) {
            // A failed rename (e.g. tmp on another volume) is not an error, only a missed saving.
            if (try? FileManager.default.moveItem(at: source, to: destination)) != nil { return destination }
        }
        return try copy(source, to: destination)
    }

    /// The file the background `URLSession` reads. Hardlinks an app-owned source; copies otherwise.
    static func linkIntoRecoveryDirectory(_ source: URL) throws -> URL {
        let destination = try recoveryDestination(for: source)
        if isAppOwned(source) {
            if (try? FileManager.default.linkItem(at: source, to: destination)) != nil { return destination }
        }
        return try copy(source, to: destination)
    }

    private static func recoveryDestination(for source: URL) throws -> URL {
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appending(path: "KriaUploads", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appending(path: "\(UUID().uuidString)-\(source.lastPathComponent)")
    }

    private static func copy(_ source: URL, to destination: URL) throws -> URL {
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
