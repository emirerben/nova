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

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("Adds to the end of your edit. Kria uploads the full-quality original.")
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.mutedInk)

                Button { showingPhotosPicker = true } label: {
                    Label("Choose from Photos", systemImage: "photo.on.rectangle")
                        .frame(maxWidth: .infinity, minHeight: 48)
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(session.isAddingClip)
                .photosPicker(isPresented: $showingPhotosPicker, selection: $photoItem, matching: .any(of: [.videos, .images]))
                .onChange(of: photoItem) { _, item in
                    guard let item else { return }
                    importPhotoItem(item)
                }
                .accessibilityIdentifier("native-editor-add-clip-photos")

                Button { showingFileImporter = true } label: {
                    Label("Choose from Files or iCloud", systemImage: "folder")
                        .frame(maxWidth: .infinity, minHeight: 48)
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(session.isAddingClip)
                .fileImporter(isPresented: $showingFileImporter, allowedContentTypes: [.movie, .image], onCompletion: importFile)
                .accessibilityIdentifier("native-editor-add-clip-files")

                // Normally invisible: the sheet closes as soon as a file is chosen. Kept for the moment
                // before it does, so the user never sees dead buttons and no progress.
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
            .navigationTitle("Add clip or photo")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.medium])
    }

    // The sheet closes the moment a file is chosen. Everything after that — fetching the file out of
    // Photos (seconds for an iCloud asset), the upload, and minting the clip — runs on the session,
    // which the editor owns and which therefore outlives this sheet. The editor shows progress and
    // any error; holding the user on a spinner here is what made adding a clip feel slow.

    /// Dismissing synchronously inside the picker's own completion stacks two dismissals — the race
    /// `FootagePickerView` documents — and can leave this sheet open. Let the picker finish closing first.
    private func dismissAfterPickerCloses() {
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(250))
            dismiss()
        }
    }

    private func importPhotoItem(_ item: PhotosPickerItem) {
        let session = session
        photoItem = nil
        dismissAfterPickerCloses()
        Task { @MainActor in
            await session.addClip(source: {
                guard let media = try await item.loadTransferable(type: ImportedMedia.self) else { throw AddClipSourceUnreadable() }
                return media.url
            }, uploadSource: .photos)
        }
    }

    private func importFile(_ result: Result<URL, any Error>) {
        guard case .success(let url) = result else { return }
        let session = session
        // The importer's URL is security-scoped. Hold access until the upload is done, since the
        // work now outlives the sheet that received it.
        let scoped = url.startAccessingSecurityScopedResource()
        dismissAfterPickerCloses()
        Task { @MainActor in
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            await session.addClip(source: { url }, uploadSource: .files)
        }
    }
}

/// Photos handed back nothing readable for the chosen item.
struct AddClipSourceUnreadable: Error {}
