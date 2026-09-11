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

    init(api: KriaAPIClient, defaultsKey: String = "kria.background-upload-recovery.v1", sessionConfiguration: URLSessionConfiguration? = nil) {
        self.api = api
        self.defaultsKey = defaultsKey
        super.init()
        records = Self.restoreRecords(key: defaultsKey)
        let configuration = sessionConfiguration ?? URLSessionConfiguration.background(withIdentifier: Self.sessionIdentifier)
        configuration.isDiscretionary = false
        configuration.sessionSendsLaunchEvents = true
        configuration.waitsForConnectivity = true
        backgroundSession = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }

    @discardableResult
    func enqueue(fileURL: URL, projectID: UUID, source: UploadSource, consentGiven: Bool, purpose: UploadPurpose, role: CreationMediaRole = .clip, itemID: String? = nil, limit: CreationMediaLimit? = nil) async -> Bool {
        lastError = nil
        var recoveryCopy: URL?
        var accepted = false
        defer { if !accepted, let recoveryCopy { try? FileManager.default.removeItem(at: recoveryCopy) } }
        do {
            try UploadCoordinator().validate(source: source, purpose: purpose, consentGiven: consentGiven)
            let prepared = role == .clip ? try await prepare(fileURL: fileURL, projectID: projectID, purpose: purpose) : (fileURL, nil, nil)
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
            let recordID = UUID()
            let clientUploadID = "ios-\(recordID.uuidString)"
            let (reservation, visualReservationID) = try await reserve(
                projectID: projectID, itemID: itemID, role: role, clientUploadID: clientUploadID,
                filename: fileURL.lastPathComponent, contentType: contentType, size: Int64(size), contract: prepared.2
            )
            if let original = prepared.1 {
                try SourceAssetStore(project: Self.projectDirectory(projectID)).bind(mediaID: reservation.mediaID, original: original)
            }
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

    private func prepare(fileURL: URL, projectID: UUID, purpose: UploadPurpose) async throws -> (URL, MediaAsset?, ProjectMediaUploadContract?) {
        let project = Self.projectDirectory(projectID)
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: fileURL)
        let original = project.root.appending(path: asset.relativePath)
        if purpose == .analysisProxy {
            let proxy = project.proxies.appendingPathComponent("\(asset.id).mp4")
            let result = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: original, destination: proxy)
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
