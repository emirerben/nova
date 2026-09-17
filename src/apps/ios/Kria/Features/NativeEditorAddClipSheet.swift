import SwiftUI
import PhotosUI

/// Picker sheet for appending a new clip or photo to the end of the native
/// editor's timeline. Deliberately does not reuse `FootagePickerView` — that
/// component uploads into the creation-thread's project media pool
/// (`attachProjectMedia`), while an already-rendered job's timeline needs a
/// clip minted straight into `job.all_candidates["clip_paths"]` via
/// `NativeEditorSession.addClip`. The consent step is the same one
/// `FootagePickerView` shows before every cloud upload.
struct NativeEditorAddClipSheet: View {
    @ObservedObject var session: NativeEditorSession
    @Environment(\.dismiss) private var dismiss
    @State private var photoItem: PhotosPickerItem?
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var pendingConsent: PendingConsentSource?

    private struct PendingConsentSource: Identifiable {
        let id = UUID()
        let source: UploadSource
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("Adds to the end of your edit.")
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.mutedInk)

                Button { pendingConsent = PendingConsentSource(source: .photos) } label: {
                    Label("Choose from Photos", systemImage: "photo.on.rectangle")
                        .frame(maxWidth: .infinity, minHeight: 48)
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(session.isAddingClip)
                .photosPicker(isPresented: $showingPhotosPicker, selection: $photoItem, matching: .any(of: [.videos, .images]))
                .onChange(of: photoItem) { _, item in
                    guard let item else { return }
                    Task { await importPhotoItem(item) }
                }
                .accessibilityIdentifier("native-editor-add-clip-photos")

                Button { pendingConsent = PendingConsentSource(source: .files) } label: {
                    Label("Choose from Files or iCloud", systemImage: "folder")
                        .frame(maxWidth: .infinity, minHeight: 48)
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(session.isAddingClip)
                .fileImporter(isPresented: $showingFileImporter, allowedContentTypes: [.movie, .image], onCompletion: importFile)
                .accessibilityIdentifier("native-editor-add-clip-files")

                if session.isAddingClip {
                    HStack { Spacer(); ProgressView("Adding…"); Spacer() }
                }
                if let error = session.addClipError {
                    Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("native-editor-add-clip-error")
                }
                Spacer()
            }
            .padding(20)
            .sheet(item: $pendingConsent) { pending in
                CloudUploadConsentView(onConsent: {
                    switch pending.source {
                    case .photos: showingPhotosPicker = true
                    default: showingFileImporter = true
                    }
                })
            }
            .navigationTitle("Add clip or photo")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .onChange(of: session.isAddingClip) { _, isAdding in
                if !isAdding, session.addClipError == nil { dismiss() }
            }
        }
        .presentationDetents([.medium])
    }

    private func importPhotoItem(_ item: PhotosPickerItem) async {
        guard let media = try? await item.loadTransferable(type: ImportedMedia.self) else {
            session.addClipError = "This file couldn’t be read. Try Files or choose it again."
            photoItem = nil
            return
        }
        photoItem = nil
        await session.addClip(fileURL: media.url)
    }

    private func importFile(_ result: Result<URL, any Error>) {
        guard case .success(let url) = result else { return }
        Task { await session.addClip(fileURL: url) }
    }
}
