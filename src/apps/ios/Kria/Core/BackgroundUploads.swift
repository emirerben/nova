import Foundation
import UniformTypeIdentifiers
import Combine
import KriaMediaEngine

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
    var reservationID: UUID?
    var clientUploadID: String?
    var mediaID: String?
    var gcsPath: String?
    var contentType: String?
    var uploadCompleted: Bool?
    var retentionExpiresAt: Date?
    var taskIdentifier: Int
    var retryCount: Int
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

@MainActor final class BackgroundUploadCoordinator: NSObject, ObservableObject, URLSessionTaskDelegate, @unchecked Sendable {
    static let sessionIdentifier = "com.kria.app.media-uploads"
    @Published private(set) var records: [UploadRecoveryRecord] = []
    @Published private(set) var progress: [UUID: Double] = [:]
    @Published private(set) var lastError: String?

    private let api: KriaAPIClient
    private let defaultsKey = "kria.background-upload-recovery.v1"
    private var backgroundSession: URLSession!

    init(api: KriaAPIClient) {
        self.api = api
        super.init()
        records = Self.restoreRecords(key: defaultsKey)
        let configuration = URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        configuration.waitsForConnectivity = true
        backgroundSession = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }

    func enqueue(fileURL: URL, projectID: UUID, source: UploadSource, consentGiven: Bool, purpose: UploadPurpose) async {
        do {
            try UploadCoordinator().validate(source: source, purpose: purpose, consentGiven: consentGiven)
            let preparedURL = try await prepare(fileURL: fileURL, projectID: projectID, purpose: purpose)
            let localURL = try Self.copyIntoRecoveryDirectory(preparedURL)
            let values = try localURL.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw APIError.invalidResponse }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            guard contentType.hasPrefix("video/") else { throw APIError.invalidResponse }
            let recordID = UUID()
            let clientUploadID = "ios-\(recordID.uuidString)"
            let reservation = try await api.reserveProjectUpload(
                threadID: projectID,
                clientUploadID: clientUploadID,
                filename: fileURL.lastPathComponent,
                contentType: contentType,
                size: Int64(size)
            )
            try startTask(
                recordID: recordID,
                localURL: localURL,
                filename: fileURL.lastPathComponent,
                projectID: projectID,
                source: source,
                purpose: purpose,
                reservation: reservation,
                clientUploadID: clientUploadID,
                retryCount: 0
            )
        } catch { lastError = error.localizedDescription }
    }

    func cancel(recordID: UUID) async {
        guard let record = records.first(where: { $0.id == recordID }) else { return }
        let tasks = await backgroundSession.allTasks
        tasks.first(where: { $0.taskIdentifier == record.taskIdentifier })?.cancel()
        if let reservationID = record.reservationID {
            try? await api.cancelUpload(reservationID: reservationID)
        }
        remove(recordID, deleteLocalFile: true)
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

    private func retry(_ record: UploadRecoveryRecord) async {
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
            let reservation = try await api.reserveProjectUpload(
                threadID: record.projectID,
                clientUploadID: clientUploadID,
                filename: record.filename,
                contentType: contentType,
                size: Int64(size)
            )
            if let reservationID = record.reservationID {
                try? await api.cancelUpload(reservationID: reservationID)
            }
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
                retryCount: record.retryCount + 1
            )
        } catch { lastError = error.localizedDescription }
    }

    private func attach(_ record: UploadRecoveryRecord) async {
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
                _ = try await api.attachProjectMedia(
                    threadID: record.projectID,
                    mediaID: mediaID,
                    gcsPath: gcsPath,
                    filename: record.filename,
                    contentType: contentType,
                    expectedRevision: current.revision,
                    clientEventID: "ios-attach-\(record.id.uuidString)"
                )
                remove(record.id, deleteLocalFile: true)
                return
            } catch {
                lastAttachmentError = error
            }
        }
        lastError = lastAttachmentError?.localizedDescription ?? "The uploaded footage could not be attached to this project."
    }

    private func startTask(recordID: UUID, localURL: URL, filename: String, projectID: UUID, source: UploadSource, purpose: UploadPurpose, reservation: ProjectUploadReservation, clientUploadID: String, retryCount: Int) throws {
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
            reservationID: nil,
            clientUploadID: clientUploadID,
            mediaID: reservation.mediaID,
            gcsPath: reservation.gcsPath,
            contentType: reservation.contentType,
            uploadCompleted: false,
            retentionExpiresAt: nil,
            taskIdentifier: task.taskIdentifier,
            retryCount: retryCount
        )
        records.append(record)
        persist()
        task.resume()
    }

    private func prepare(fileURL: URL, projectID: UUID, purpose: UploadPurpose) async throws -> URL {
        let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appending(path: "KriaProjects/\(projectID.uuidString)", directoryHint: .isDirectory)
        let project = ProjectDirectory(root: root)
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: fileURL)
        let original = root.appending(path: asset.relativePath)
        _ = purpose
        return original
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
