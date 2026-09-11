import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import CoreTransferable

struct FootagePickerView: View {
    let projectID: UUID
    let maximumClipCount: Int
    let attachedClipCount: Int
    let role: CreationMediaRole
    let itemID: String?
    let limit: CreationMediaLimit?
    let destination: ProjectUploadDestination
    @ObservedObject private var uploads: BackgroundUploadCoordinator
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var consentSelection: UploadConsentSelection?
    @State private var consentedPurpose: UploadPurpose = .cloudRenderSource
    @State private var reservedClipCount = 0
    @State private var selectionMessage: String?

    init(
        projectID: UUID,
        uploads: BackgroundUploadCoordinator,
        maximumClipCount: Int = 10,
        attachedClipCount: Int = 0,
        role: CreationMediaRole = .clip,
        itemID: String? = nil,
        limit: CreationMediaLimit? = nil,
        destination: ProjectUploadDestination = .cloud
    ) {
        self.role = role
        self.itemID = itemID
        self.limit = limit
        self.destination = destination
        self.projectID = projectID
        self.uploads = uploads
        self.maximumClipCount = max(0, maximumClipCount)
        self.attachedClipCount = max(0, attachedClipCount)
    }

    private var selectionCapacity: ClipSelectionCapacity {
        ClipSelectionCapacity(
            maximum: maximumClipCount,
            existing: attachedClipCount + pendingUploadCount,
            reserved: reservedClipCount
        )
    }

    private var pendingUploadCount: Int {
        uploads.records.filter { $0.projectID == projectID && $0.role == role }.count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            KriaSectionLabel(title: "Add \(role.title.lowercased())")
            if role != .voiceover {
            Button { requestConsent(source: .photos) } label: {
                Label("Choose from Photos", systemImage: "photo.on.rectangle").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(selectionCapacity.remaining == 0 || !destination.canUpload || reservedClipCount > 0)
            .photosPicker(
                isPresented: $showingPhotosPicker,
                selection: $photoItems,
                maxSelectionCount: max(1, selectionCapacity.remaining),
                matching: role == .visual ? .any(of: [.videos, .images]) : .videos
            )
            .onChange(of: photoItems) { _, items in Task { await importPhotoItems(items) } }
            }
            Button { requestConsent(source: .files) } label: {
                Label("Choose from Files or iCloud", systemImage: "folder").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(selectionCapacity.remaining == 0 || !destination.canUpload || reservedClipCount > 0)
            .fileImporter(
                isPresented: $showingFileImporter,
                allowedContentTypes: role == .voiceover ? [.audio] : role == .visual ? [.movie, .image] : [.movie],
                allowsMultipleSelection: selectionCapacity.remaining > 1,
                onCompletion: importFiles
            )
            .sheet(item: $consentSelection) { selection in
                if selection.purpose == .analysisProxy {
                    AnalysisUploadConsentView { beginImport(selection) }
                } else {
                    CloudUploadConsentView { beginImport(selection) }
                }
            }
            if let message = destination.message {
                Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if selectionCapacity.remaining == 0 {
                Text("You’ve reached the limit for \(role.title.lowercased()).")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
            } else if let selectionMessage {
                Text(selectionMessage)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
            }
            ForEach(uploads.records.filter { $0.projectID == projectID && $0.role == role }) { record in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(record.filename).lineLimit(1)
                        Spacer()
                        if record.uploadCompleted == true {
                            Button("Retry attach") { Task { await uploads.retryAttachment(recordID: record.id) } }
                        } else {
                            Button("Retry") { Task { await uploads.retryUpload(recordID: record.id) } }
                            Button("Cancel") { Task { await uploads.cancel(recordID: record.id) } }
                        }
                    }
                    ProgressView(value: uploads.progress[record.id] ?? 0).tint(KriaColor.ink)
                    if let deadline = record.retentionExpiresAt { Text("Temporary source removed by \(deadline.formatted(date: .abbreviated, time: .shortened)).").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc) }
                }
            }
            if let error = uploads.lastError { Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }
        }.accessibilityElement(children: .contain)
    }
    private func requestConsent(source: UploadSource) {
        guard destination.canUpload else { return }
        consentSelection = UploadConsentSelection(source: source, purpose: destination == .phone ? .analysisProxy : .cloudRenderSource)
    }
    private func beginImport(_ selection: UploadConsentSelection) {
        consentedPurpose = selection.purpose
        if selection.source == .photos { showingPhotosPicker = true }
        else { showingFileImporter = true }
    }
    private func importPhotoItems(_ items: [PhotosPickerItem]) async {
        let purpose = consentedPurpose
        let acceptedCount = selectionCapacity.acceptedCount(requested: items.count)
        if acceptedCount < items.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        reservedClipCount += acceptedCount
        for item in items.prefix(acceptedCount) {
            guard let media = try? await item.loadTransferable(type: ImportedMedia.self) else {
                selectionMessage = "This file couldn’t be read. Try Files or choose it again."
                reservedClipCount -= 1
                continue
            }
            _ = await uploads.enqueue(fileURL: media.url, projectID: projectID, source: .photos, consentGiven: true, purpose: purpose, role: role, itemID: itemID, limit: limit)
            // enqueue publishes a live record before returning on success; on
            // failure the reservation is free for another selection.
            reservedClipCount -= 1
        }
        photoItems = []
    }
    private func importFiles(_ result: Result<[URL], any Error>) {
        guard case .success(let urls) = result else { return }
        let purpose = consentedPurpose
        let acceptedCount = selectionCapacity.acceptedCount(requested: urls.count)
        if acceptedCount < urls.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        reservedClipCount += acceptedCount
        Task {
            for url in urls.prefix(acceptedCount) {
                _ = await uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: purpose, role: role, itemID: itemID, limit: limit)
                reservedClipCount -= 1
            }
        }
    }
}

private struct UploadConsentSelection: Identifiable {
    let id = UUID()
    let source: UploadSource
    let purpose: UploadPurpose
}

struct ClipSelectionCapacity: Equatable, Sendable {
    let maximum: Int
    let existing: Int
    let reserved: Int

    var remaining: Int { max(0, maximum - existing - reserved) }

    func acceptedCount(requested: Int) -> Int {
        min(max(0, requested), remaining)
    }
}

private struct ImportedMedia: Transferable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .movie) { try imported($0) }
        FileRepresentation(importedContentType: .image) { try imported($0) }
    }
    private static func imported(_ received: ReceivedTransferredFile) throws -> ImportedMedia {
        let destination = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString)-\(received.file.lastPathComponent)")
        try FileManager.default.copyItem(at: received.file, to: destination)
        return ImportedMedia(url: destination)
    }
}
