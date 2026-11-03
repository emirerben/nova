import SwiftUI
import PhotosUI

/// What the Add clip sheet starts doing when it opens (the empty player's Record and Upload buttons skip the choice).
enum AddClipLaunch: Hashable, Identifiable {
    case choose, record, upload
    var id: Self { self }
}

/// Sheet for appending a new clip or photo to the end of the native editor's timeline (Paper S3.16b): Record | Upload,
/// then "Upload from" once Upload is chosen. Deliberately does not reuse `FootagePickerView` — that
/// component uploads into the creation-thread's project media pool (`attachProjectMedia`), while an
/// already-rendered job's timeline needs a clip minted straight into `job.all_candidates["clip_paths"]` via
/// `NativeEditorSession.addClip`.
struct NativeEditorAddClipSheet: View {
    @ObservedObject var session: NativeEditorSession
    var launch: AddClipLaunch = .choose
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var photoItem: PhotosPickerItem?
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var showsCamera = false
    @State private var choosingSource = false
    @State private var addedByRecording = false
    @State private var uploadAfterCamera = false
    @State private var launched = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                if choosingSource {
                    Button { choosingSource = false } label: { Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)) }
                        .buttonStyle(.kriaIcon).accessibilityLabel("Back")
                }
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                    .buttonStyle(.kriaIcon).accessibilityLabel("Close").accessibilityIdentifier("native-editor-add-clip-close")
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(choosingSource ? "Upload from" : "Add a clip or photo").kriaType(.heading).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                if !choosingSource {
                    Text("Adds to the end of your video.").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
            }
            if choosingSource {
                sourceRows
            } else {
                KriaSourceRow(options: [
                    model.localAccountID == nil ? nil : .record(caption: "Film now", identifier: "native-editor-add-clip-record", isEnabled: !session.isAddingClip) { showsCamera = true },
                    .upload(caption: "Use existing video", identifier: "native-editor-add-clip-upload", isEnabled: !session.isAddingClip) { choosingSource = true }
                ].compactMap { $0 })
            }
            // Normally invisible: the sheet closes as soon as a file is chosen. Kept for the moment
            // before it does, so the user never sees dead buttons and no progress.
            if session.isAddingClip {
                HStack(spacing: 8) { KriaSpinner(size: 16); Text("Adding…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
            }
            if let error = session.addClipError {
                Text(error).kriaType(.label).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("native-editor-add-clip-error")
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24).padding(.top, 12)
        .background(KriaColor.paper)
        .photosPicker(isPresented: $showingPhotosPicker, selection: $photoItem, matching: .any(of: [.videos, .images]))
        .onChange(of: photoItem) { _, item in
            guard let item else { return }
            importPhotoItem(item)
        }
        .fileImporter(isPresented: $showingFileImporter, allowedContentTypes: [.movie, .image], onCompletion: importFile)
        .fullScreenCover(isPresented: $showsCamera, onDismiss: {
            if addedByRecording { dismiss() }
            else if uploadAfterCamera { uploadAfterCamera = false; choosingSource = true }
        }) {
            if let accountID = model.localAccountID {
                TeleprompterScreen(accountID: accountID, projectID: session.creationThreadID ?? UUID(),
                                   admissionMessage: { _ in nil },
                                   intake: { take, url in
                                       // The camera stays open on its "adding" state until the clip is in the timeline.
                                       await session.addClip(fileURL: url)
                                       if let message = session.addClipError { return .failed(message) }
                                       addedByRecording = true
                                       return .attached
                                   },
                                   intakeState: { _ in nil },
                                   onUpload: { uploadAfterCamera = true })
            }
        }
        .task {
            guard !launched else { return }
            launched = true
            switch launch {
            case .record: if model.localAccountID != nil { showsCamera = true }
            case .upload: choosingSource = true
            case .choose: break
            }
        }
        .presentationDetents([.medium])
        .presentationDragIndicator(.hidden)
    }

    private var sourceRows: some View {
        VStack(spacing: 4) {
            sourceRow("photo.on.rectangle.angled", "Photos", "Pick photos and videos from your library", id: "native-editor-add-clip-photos") { showingPhotosPicker = true }
            sourceRow("folder", "Files", "From Files or iCloud", id: "native-editor-add-clip-files") { showingFileImporter = true }
        }
        .disabled(session.isAddingClip)
    }

    private func sourceRow(_ symbol: String, _ title: String, _ detail: String, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: symbol).font(.system(size: 18)).foregroundStyle(KriaColor.ink)
                    .frame(width: 40, height: 40).background(KriaColor.fill, in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).kriaType(.button).foregroundStyle(KriaColor.ink)
                    Text(detail).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 52).contentShape(Rectangle())
        }
        .buttonStyle(.plain).accessibilityLabel("\(title), \(detail)").accessibilityIdentifier(id)
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
