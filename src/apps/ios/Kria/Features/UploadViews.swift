import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import CoreTransferable

struct FootagePickerView: View {
    let projectID: UUID
    let maximumClipCount: Int
    @EnvironmentObject private var model: AppModel
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var showingCloudConsent = false
    @State private var consentedSource: UploadSource = .photos
    // Snapshot the already-attached/pending count when the picker opens. New
    // selections are tracked by reservedClipCount so upload-record publishes do
    // not count the same clip twice while this sheet remains presented.
    @State private var baselineClipCount: Int
    @State private var reservedClipCount = 0
    @State private var selectionMessage: String?

    init(projectID: UUID, maximumClipCount: Int = 10, existingClipCount: Int = 0) {
        self.projectID = projectID
        self.maximumClipCount = max(0, maximumClipCount)
        _baselineClipCount = State(initialValue: max(0, existingClipCount))
    }

    private var selectionCapacity: ClipSelectionCapacity {
        ClipSelectionCapacity(
            maximum: maximumClipCount,
            existing: baselineClipCount,
            reserved: reservedClipCount
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            KriaSectionLabel(title: "Add footage")
            Button { consentedSource = .photos; showingCloudConsent = true } label: {
                Label("Choose from Photos", systemImage: "photo.on.rectangle").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(selectionCapacity.remaining == 0)
            .photosPicker(
                isPresented: $showingPhotosPicker,
                selection: $photoItems,
                maxSelectionCount: max(1, selectionCapacity.remaining),
                matching: .videos
            )
            .onChange(of: photoItems) { _, items in Task { await importPhotoItems(items) } }
            Button { consentedSource = .files; showingCloudConsent = true } label: {
                Label("Choose from Files or iCloud", systemImage: "folder").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(selectionCapacity.remaining == 0)
            .fileImporter(
                isPresented: $showingFileImporter,
                allowedContentTypes: [.movie],
                allowsMultipleSelection: selectionCapacity.remaining > 1,
                onCompletion: importFiles
            )
            .sheet(isPresented: $showingCloudConsent) {
                CloudUploadConsentView {
                    if consentedSource == .photos { showingPhotosPicker = true }
                    else { showingFileImporter = true }
                }
            }
            if selectionCapacity.remaining == 0 {
                Text("This format already has its maximum number of clips.")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
            } else if let selectionMessage {
                Text(selectionMessage)
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
            }
            ForEach(model.uploads.records.filter { $0.projectID == projectID }) { record in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(record.filename).lineLimit(1)
                        Spacer()
                        if record.uploadCompleted == true {
                            Button("Retry attach") { Task { await model.uploads.retryAttachment(recordID: record.id) } }
                        } else {
                            Button("Cancel") { Task { await model.uploads.cancel(recordID: record.id) } }
                        }
                    }
                    ProgressView(value: model.uploads.progress[record.id] ?? 0).tint(KriaColor.limeText)
                    if let deadline = record.retentionExpiresAt { Text("Temporary source removed by \(deadline.formatted(date: .abbreviated, time: .shortened)).").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc) }
                }
            }
            if let error = model.uploads.lastError { Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }
        }.accessibilityElement(children: .contain)
    }
    private func importPhotoItems(_ items: [PhotosPickerItem]) async {
        let acceptedCount = selectionCapacity.acceptedCount(requested: items.count)
        if acceptedCount < items.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        reservedClipCount += acceptedCount
        for item in items.prefix(acceptedCount) {
            guard let media = try? await item.loadTransferable(type: ImportedMedia.self) else { continue }
            await model.uploads.enqueue(fileURL: media.url, projectID: projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource)
        }
        photoItems = []
    }
    private func importFiles(_ result: Result<[URL], any Error>) {
        guard case .success(let urls) = result else { return }
        let acceptedCount = selectionCapacity.acceptedCount(requested: urls.count)
        if acceptedCount < urls.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        reservedClipCount += acceptedCount
        Task {
            for url in urls.prefix(acceptedCount) {
                await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource)
            }
        }
    }
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
