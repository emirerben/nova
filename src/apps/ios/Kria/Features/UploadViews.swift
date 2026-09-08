import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import CoreTransferable

struct FootagePickerView: View {
    let projectID: UUID
    @EnvironmentObject private var model: AppModel
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var showingCloudConsent = false
    @State private var consentedSource: UploadSource = .photos
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            KriaSectionLabel(title: "Add footage")
            Button { consentedSource = .photos; showingCloudConsent = true } label: {
                Label("Choose from Photos", systemImage: "photo.on.rectangle").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .photosPicker(isPresented: $showingPhotosPicker, selection: $photoItems, maxSelectionCount: 10, matching: .videos)
            .onChange(of: photoItems) { _, items in Task { await importPhotoItems(items) } }
            Button { consentedSource = .files; showingCloudConsent = true } label: {
                Label("Choose from Files or iCloud", systemImage: "folder").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .fileImporter(isPresented: $showingFileImporter, allowedContentTypes: [.movie], allowsMultipleSelection: true, onCompletion: importFiles)
            .sheet(isPresented: $showingCloudConsent) {
                CloudUploadConsentView {
                    if consentedSource == .photos { showingPhotosPicker = true }
                    else { showingFileImporter = true }
                }
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
        for item in items {
            guard let media = try? await item.loadTransferable(type: ImportedMedia.self) else { continue }
            await model.uploads.enqueue(fileURL: media.url, projectID: projectID, source: .photos, consentGiven: true, purpose: .cloudRenderSource)
        }
    }
    private func importFiles(_ result: Result<[URL], any Error>) {
        guard case .success(let urls) = result else { return }
        Task { for url in urls { await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource) } }
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
