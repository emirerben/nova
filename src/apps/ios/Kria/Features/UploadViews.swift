import SwiftUI
import Photos
import PhotosUI
import UniformTypeIdentifiers
import CoreTransferable
import KriaMediaEngine

/// Picks footage, visuals or a voiceover and hands each file to `BackgroundUploadCoordinator`.
///
/// KRI-125: with Photos access, the picker reports every tap live (`.continuousAndOrdered`), so an
/// upload starts the moment a clip is chosen instead of after the user taps Done, and clips that
/// are already chosen show as selected when it reopens. Everything stateful lives in the
/// coordinator, not here: `AttachmentSheet` applies `.id(role)` to this view, so switching
/// Footage↔Visuals destroys every `@State` and any `Task` it owned.
///
/// Without Photos access the picker cannot identify individual assets (`itemIdentifier` is nil), so
/// it falls back to committing on Done — but the batch now runs concurrently instead of one clip at
/// a time. There is no live start, no preselection and no un-choosing there; the attached list
/// below the picker is the duplicate guard.
struct FootagePickerView: View {
    let projectID: UUID
    let maximumClipCount: Int
    let attachedClipCount: Int
    /// Ids of the media currently attached for this role. Lets the picker tell an attached clip that is
    /// still there (show as chosen) from one that was removed elsewhere (don't).
    let attachedMediaIDs: Set<String>
    let role: CreationMediaRole
    let itemID: String?
    let limit: CreationMediaLimit?
    let destination: ProjectUploadDestination
    /// KRI-175: called when a pick filled the live picker and it closed itself, so the host can return
    /// to chat too (one talking-to-camera clip: pick it and you're back).
    let onPickerFilled: (() -> Void)?
    /// Called after a picker has closed with newly selected footage. This is
    /// distinct from `onPickerFilled`, which is only the live-picker cap flow.
    let onSelectionCompleted: (() -> Void)?
    /// Voiceover and song preview flow: ownership of the copied local audio file passes
    /// to this callback. It deliberately does not enqueue an upload.
    let onAudioFileSelected: ((URL) -> Void)?
    let showsHeading: Bool
    @ObservedObject private var uploads: BackgroundUploadCoordinator
    /// Upload rows say when they are waiting for a connection (KRI-294).
    @ObservedObject private var network = NetworkReachability.shared
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var libraryAuthorized = false
    /// How many of the chosen assets the library can still show. Fetched when the set changes, not
    /// per render: the picker's limit must count what it will actually display.
    @State private var showablePreselectedCount = 0
    @State private var selectionMessage: String?
    @State private var photosPickerHasNewFootage = false

    init(
        projectID: UUID,
        uploads: BackgroundUploadCoordinator,
        maximumClipCount: Int = 10,
        attachedClipCount: Int = 0,
        attachedMediaIDs: Set<String> = [],
        role: CreationMediaRole = .clip,
        itemID: String? = nil,
        limit: CreationMediaLimit? = nil,
        destination: ProjectUploadDestination = .cloud,
        onPickerFilled: (() -> Void)? = nil,
        onSelectionCompleted: (() -> Void)? = nil,
        onAudioFileSelected: ((URL) -> Void)? = nil,
        showsHeading: Bool = true
    ) {
        self.onPickerFilled = onPickerFilled
        self.onSelectionCompleted = onSelectionCompleted
        self.onAudioFileSelected = onAudioFileSelected
        self.showsHeading = showsHeading
        self.role = role
        self.itemID = itemID
        self.limit = limit
        self.destination = destination
        self.projectID = projectID
        self.uploads = uploads
        self.maximumClipCount = max(0, maximumClipCount)
        self.attachedClipCount = max(0, attachedClipCount)
        self.attachedMediaIDs = attachedMediaIDs
    }

    /// What this project uploads: smaller analysis copies when it renders on the phone, originals when
    /// it renders in the cloud. Fixed by the project's destination, not something the user picks per
    /// upload. Agreement to share media with Kria's AI providers is given once, at account level
    /// (`AIConsentView` gates the whole workspace), so there is no per-upload consent screen.
    /// A voiceover always uploads through the existing cloud contract (KRI-132): `destination` can be
    /// `.phone` for this role once `narrationAudio` is verified, but that only unlocks the recorder/
    /// picker UI -- the bytes themselves never become a phone analysis proxy.
    private var uploadPurpose: UploadPurpose {
        // A song (KRI-374) is full bytes too: it never goes through the analysis-proxy contract.
        role.isAudio ? .cloudRenderSource : (destination == .phone ? .analysisProxy : .cloudRenderSource)
    }

    /// Non-blocking disclosure of what is uploaded, shown where the per-upload consent screen used to be.
    private var uploadDisclosure: String? {
        if role == .song { return "Only use songs you have the rights to. Kria uploads the full file to analyze and render it with your video." }
        guard role != .voiceover else { return nil }
        switch destination {
        case .phone: return "Kria uploads a smaller copy of each video to plan your edit. Full-quality originals stay on this iPhone."
        case .cloud: return "Kria uploads the full-quality originals you choose and keeps them with this project."
        // A phone-rendered project still uploads Visuals at full quality (so the AI can plan around them)
        // and downloads them again to render. That is the disclosure the per-upload screen carried for
        // this case, so it must not be lost with the screen.
        case .phoneVisuals: return "Kria uploads the full-quality visuals you choose and keeps them with this project so its AI can plan your edit. When your video renders on this iPhone, it downloads them again."
        default: return nil
        }
    }

    private var preselectedIdentifiers: [String] {
        uploads.preselectedIdentifiers(projectID: projectID, role: role, itemID: itemID, attachedMediaIDs: attachedMediaIDs)
    }

    private var selectionCapacity: ClipSelectionCapacity {
        ClipSelectionCapacity(
            maximum: maximumClipCount,
            existing: attachedClipCount + pendingUploadCount,
            reserved: uploads.reservedCount(projectID: projectID, role: role),
            preselected: libraryAuthorized ? showablePreselectedCount : 0
        )
    }

    /// The subset of `ids` the library can still resolve. A clip attached from a photo the user has
    /// since deleted has no tick the picker can show, and the picker dropping it must never be read as
    /// the user un-choosing it — that would delete the clip from the project.
    private static func resolvable(_ ids: [String]) -> [String] {
        guard !ids.isEmpty else { return [] }
        var present = Set<String>()
        PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil).enumerateObjects { asset, _, _ in
            present.insert(asset.localIdentifier)
        }
        return ids.filter(present.contains)
    }

    private func refreshShowablePreselected() {
        showablePreselectedCount = libraryAuthorized ? Self.resolvable(preselectedIdentifiers).count : 0
    }

    private var pendingUploadCount: Int {
        uploads.records.filter { $0.projectID == projectID && $0.role == role }.count
    }

    /// At the cap the user can still open the picker to see (and un-choose) what is already there.
    private var photosDisabled: Bool {
        !destination.canUpload || (selectionCapacity.remaining == 0 && selectionCapacity.preselected == 0)
    }

    private var failures: [UploadFailure] {
        uploads.failures.filter { $0.projectID == projectID && $0.role == role }
    }

    /// Chosen clips that have no upload record yet (still importing or waiting for a slot), oldest first.
    private var preparingUploads: [(id: UUID, filename: String?)] {
        let recorded = Set(uploads.records.map(\.id))
        return uploads.inFlight
            .filter { $0.value.projectID == projectID && $0.value.role == role && !recorded.contains($0.key) }
            .sorted { $0.value.startedAt < $1.value.startedAt }
            .map { (id: $0.key, filename: $0.value.filename) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if showsHeading { KriaSectionLabel(title: "Add \(role.title.lowercased())") }
            if !role.isAudio {
            Button { beginImport(source: .photos) } label: {
                Label("Choose from Photos", systemImage: "photo.on.rectangle").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(photosDisabled)
            .modifier(FootagePhotosPicker(
                isPresented: $showingPhotosPicker,
                selection: $photoItems,
                limit: selectionCapacity.pickerSelectionLimit,
                filter: role == .visual ? visualPickerFilter : .videos,
                kinds: role == .visual ? visualGalleryKinds : .videos,
                libraryBacked: libraryAuthorized,
                title: role.title,
                onFilled: onPickerFilled,
                didChooseNewFootage: $photosPickerHasNewFootage,
                onSelectionCompleted: onSelectionCompleted
            ))
            .onChange(of: photoItems) { _, items in reconcile(items) }
            }
            Button { beginImport(source: .files) } label: {
                Label(role == .song ? "Choose a song from Files" : role == .voiceover && onAudioFileSelected != nil ? "Upload an audio file" : "Choose from Files or iCloud", systemImage: "folder").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(AttachmentFileButtonStyle(isVoiceover: role.isAudio && onAudioFileSelected != nil))
            .disabled(selectionCapacity.remaining == 0 || !destination.canUpload)
            .fileImporter(
                isPresented: $showingFileImporter,
                allowedContentTypes: role.isAudio ? [.audio] : role == .visual ? visualContentTypes : [.movie],
                allowsMultipleSelection: role.isAudio && onAudioFileSelected != nil ? false : selectionCapacity.remaining > 1,
                onCompletion: importFiles
            )
            if let message = destination.message {
                Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
            if let disclosure = uploadDisclosure {
                Text(disclosure).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
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
            // Clips still being prepared: not yet an upload record, but already chosen. Shown at once, with
            // their thumbnail, so the user sees each pick land instead of waiting for it to finish uploading.
            ForEach(preparingUploads, id: \.id) { item in
                HStack(spacing: 12) {
                    CreationRecordThumbnail(recordID: item.id, version: uploads.previewVersion)
                    Text(item.filename ?? "Preparing…").lineLimit(1)
                    Spacer()
                    ProgressView()
                }
            }
            ForEach(uploads.records.filter { $0.projectID == projectID && $0.role == role }) { record in
                HStack(alignment: .top, spacing: 12) {
                    CreationRecordThumbnail(recordID: record.id, version: uploads.previewVersion)
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(BackgroundUploadCoordinator.displayFilename(record.filename)).lineLimit(1)
                            Spacer()
                            if record.uploadCompleted == true {
                                Button("Retry attach") { Task { await uploads.retryAttachment(recordID: record.id) } }
                            } else {
                                Button("Retry") { Task { await uploads.retryUpload(recordID: record.id) } }
                            }
                            // Also for a clip that finished uploading but could not attach. It used to have
                            // only "Retry attach", so a clip the server kept rejecting could never be
                            // removed: it sat in the list forever, keeping Continue disabled.
                            Button(record.uploadCompleted == true ? "Remove" : "Cancel") { Task { await uploads.cancel(recordID: record.id) } }
                        }
                        ProgressView(value: uploads.progress[record.id] ?? 0).tint(KriaColor.ink)
                        // Failed uploads explain themselves in the failure lines below.
                        if record.uploadFailed != true {
                            Text(UploadProgressCaption.text(progress: uploads.progress[record.id], completed: record.uploadCompleted == true, online: network.isOnline))
                                .font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc).monospacedDigit()
                                .accessibilityIdentifier("upload-progress-caption")
                        }
                        if let deadline = record.retentionExpiresAt { Text("Temporary source removed by \(deadline.formatted(date: .abbreviated, time: .shortened)).").font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc) }
                    }
                }
            }
            // A failed clip is reported next to the ones that worked. One shared line can't do that:
            // with uploads overlapping, a later clip's success would erase an earlier clip's failure.
            ForEach(failures) { failure in
                HStack(alignment: .firstTextBaseline) {
                    Text("\(failure.filename): \(failure.message)").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    Spacer()
                    Button("Dismiss") { uploads.dismissFailure(id: failure.id) }.font(KriaFont.body(12))
                }
            }
            if let error = uploads.lastError, failures.isEmpty { Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }
        }
        .accessibilityElement(children: .contain)
        .task {
            libraryAuthorized = PHPhotoLibrary.authorizationStatus(for: .readWrite) == .authorized
            refreshShowablePreselected()
        }
        .onChange(of: preselectedIdentifiers) { _, _ in refreshShowablePreselected() }
    }
    /// An iPhone-rendered project only takes the Visuals kinds this iPhone can draw.
    private var visualPickerFilter: PHPickerFilter {
        switch destination.visualKinds {
        case [.image]: .images
        case [.video]: .videos
        default: .any(of: [.videos, .images])
        }
    }
    private var visualGalleryKinds: LibraryMediaKinds {
        switch destination.visualKinds {
        case [.image]: .images
        case [.video]: .videos
        default: [.videos, .images]
        }
    }
    private var visualContentTypes: [UTType] {
        switch destination.visualKinds {
        case [.image]: [.image]
        case [.video]: [.movie]
        default: [.movie, .image]
        }
    }

    private func beginImport(source: UploadSource) {
        guard destination.canUpload else { return }
        uploads.clearLastError()
        // A new batch starts clean: earlier failures were already shown, and one for a clip the user is
        // now adding again would otherwise sit there forever after the retry succeeds.
        uploads.clearFailures(projectID: projectID, role: role)
        selectionMessage = nil
        if source == .photos { Task { await presentPhotosPicker() } }
        else { showingFileImporter = true }
    }

    /// The Photos permission prompt comes here, only when the user actually asks for Photos.
    private func presentPhotosPicker() async {
        let wasAuthorized = libraryAuthorized
        libraryAuthorized = await Self.photoLibraryAuthorized()
        if libraryAuthorized, ProcessInfo.processInfo.arguments.contains("-ui-testing-seed-photo-video") { await Self.seedUITestVideo() }
        refreshShowablePreselected()
        // Seed with what is already chosen, so those clips render as selected and can't be re-picked.
        // Only assets the library can still show: seeding one it can't resolve would have the picker
        // drop it, and that must not be able to look like the user un-choosing it.
        photoItems = libraryAuthorized ? Self.resolvable(preselectedIdentifiers).map { PhotosPickerItem(itemIdentifier: $0) } : []
        // The first time access is granted, the picker modifier below swaps to the library-backed
        // variant. Presenting in the same pass as that swap can silently fail to present, so give the
        // new modifier a moment to install. Happens once per permission grant, not per presentation.
        if libraryAuthorized != wasAuthorized { try? await Task.sleep(for: .milliseconds(150)) }
        showingPhotosPicker = true
    }

    /// Only `.authorized`. `.limited` is deliberately excluded: a library-backed picker under limited
    /// access doesn't reliably show the whole library, and silently hiding footage is worse than
    /// having no checkmarks.
    private static func photoLibraryAuthorized() async -> Bool {
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-no-photo-library") { return false }
        let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        let resolved = status == .notDetermined ? await PHPhotoLibrary.requestAuthorization(for: .readWrite) : status
        return resolved == .authorized
    }

    /// UI tests only: the simulator's sample library has no videos, and Footage only offers videos.
    /// `nonisolated` so the change block isn't MainActor-isolated: Photos runs it on its own queue,
    /// and the isolation check traps (SIGTRAP) there.
    private nonisolated static func seedUITestVideo() async {
        let wanted = ProcessInfo.processInfo.arguments.contains("-ui-testing-seed-photo-videos") ? 6 : 1
        let existing = PHAsset.fetchAssets(with: .video, options: nil).count
        guard existing < wanted, let url = KriaBranding.outroURL() else { return }
        try? await PHPhotoLibrary.shared().performChanges {
            for _ in existing..<wanted { _ = PHAssetCreationRequest.creationRequestForAssetFromVideo(atFileURL: url) }
        }
    }

    /// Called on every change of the live selection. Deliberately does nothing but diff and delegate:
    /// it must not touch presentation state, which fights the picker sheet that is still open.
    private func reconcile(_ items: [PhotosPickerItem]) {
        guard libraryAuthorized else { importUnidentified(items); return }
        // A failed clip stays ticked in the picker. Its error line goes when the user un-ticks it.
        uploads.pruneSelectionFailures(projectID: projectID, role: role, itemID: itemID, chosen: Set(items.compactMap(\.itemIdentifier)))
        // Diff against what the coordinator already knows, never against `@State`: `.id(role)`
        // resets `@State`, and a seeded selection would then read as "everything is new" and
        // upload every clip again.
        let diff = PhotoSelectionDiff(current: items.compactMap(\.itemIdentifier), known: preselectedIdentifiers)
        guard !diff.isEmpty else { return }
        if role == .clip, !diff.added.isEmpty { photosPickerHasNewFootage = true }
        let byIdentifier = Dictionary(items.compactMap { item in item.itemIdentifier.map { ($0, item) } }, uniquingKeysWith: { first, _ in first })
        for identifier in diff.added {
            guard let item = byIdentifier[identifier] else { continue }
            uploads.select(.init(assetIdentifier: identifier, projectID: projectID, role: role, purpose: uploadPurpose, itemID: itemID, limit: limit, attachedMediaIDs: attachedMediaIDs)) {
                // Gallery picks have no item provider (KRI-282 regression): PhotoItemFileLoader reads them from Photos.
                try await PhotoItemFileLoader().load(item: item, identifier: identifier)
            }
        }
        // Only what the library can still show can have been un-ticked. Anything else was dropped by the
        // picker (a deleted photo, an over-limit seed), which must never be treated as the user's
        // decision: it would remove the clip from the project.
        let removable = Set(Self.resolvable(diff.removed))
        for identifier in diff.removed where removable.contains(identifier) { Task { await unchoose(identifier) } }
    }

    private func unchoose(_ identifier: String) async {
        let outcome = await uploads.deselect(assetIdentifier: identifier, projectID: projectID, role: role, itemID: itemID)
        // The first call for this asset will decide, and re-diffing before it finishes would find the
        // same removal again and re-issue it in a loop.
        if outcome == .alreadyInProgress { return }
        if case .refused(let reason) = outcome {
            // Put it back: leaving it un-chosen would show a screen that disagrees with the project.
            selectionMessage = reason
            if !photoItems.contains(where: { $0.itemIdentifier == identifier }) {
                photoItems.append(PhotosPickerItem(itemIdentifier: identifier))
            }
            return
        }
        // The user may have chosen this asset again while the removal was still running (a detach is
        // a network round trip). That change was diffed against a ledger that still contained it, so
        // it looked like no change and nothing was started; the picker would show it selected while
        // the coordinator has released it. Diff again now that the ledger is up to date.
        reconcile(photoItems)
    }

    /// No-Photos-access path: today's commit-on-Done behavior, minus the one-at-a-time wait.
    private func importUnidentified(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        let purpose = uploadPurpose
        let acceptedCount = selectionCapacity.acceptedCount(requested: items.count)
        if acceptedCount < items.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        let chosen = Array(items.prefix(acceptedCount))
        if role == .clip, !chosen.isEmpty {
            photosPickerHasNewFootage = true
            // The permission-free picker only commits this binding after it
            // has closed. Yield once so its dismissal state is visible even
            // when PhotosUI delivers the binding before `isPresented` flips.
            Task { @MainActor in
                await Task.yield()
                guard !showingPhotosPicker, photosPickerHasNewFootage else { return }
                photosPickerHasNewFootage = false
                onSelectionCompleted?()
            }
        }
        photoItems = []
        let ids = chosen.map { _ in UUID() }
        // Count them against the limit while their files are still being fetched.
        for id in ids { uploads.markInFlight(id, projectID: projectID, role: role) }
        for (item, id) in zip(chosen, ids) {
            Task {
                guard let media = try? await item.loadTransferable(type: ImportedMedia.self) else {
                    uploads.clearInFlight(id)
                    uploads.reportFailure(id: id, projectID: projectID, role: role, filename: "Selected item", message: "This file couldn’t be read. Try Files or choose it again.")
                    return
                }
                _ = await uploads.enqueue(fileURL: media.url, projectID: projectID, source: .photos, consentGiven: true, purpose: purpose, role: role, itemID: itemID, limit: limit, recordID: id)
            }
        }
    }

    /// Files/iCloud stays one clip at a time: an external file is genuinely copied (never linked, so
    /// the user editing it mid-upload can't change what is sent), and overlapping N real copies
    /// would only thrash the disk.
    private func importFiles(_ result: Result<[URL], any Error>) {
        guard case .success(let urls) = result else { return }
        if role.isAudio, let onAudioFileSelected {
            importVoiceoverPreview(urls, onAudioFileSelected: onAudioFileSelected)
            return
        }
        let purpose = uploadPurpose
        let acceptedCount = selectionCapacity.acceptedCount(requested: urls.count)
        if acceptedCount < urls.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        let chosen = Array(urls.prefix(acceptedCount))
        if role == .clip, !chosen.isEmpty {
            Task { @MainActor in onSelectionCompleted?() }
        }
        let ids = chosen.map { _ in UUID() }
        for (url, id) in zip(chosen, ids) { uploads.markInFlight(id, projectID: projectID, role: role, filename: url.lastPathComponent) }
        Task {
            for (url, id) in zip(chosen, ids) {
                _ = await uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: purpose, role: role, itemID: itemID, limit: limit, recordID: id)
            }
            for id in ids { uploads.clearInFlight(id) }
        }
    }

    /// File importer URLs may be security-scoped and stop being readable as
    /// soon as this callback returns. Copy first, then hand the caller a stable
    /// temporary file for recording/import preview; the caller owns deletion or
    /// later enqueueing after the creator taps Use voiceover.
    private func importVoiceoverPreview(_ urls: [URL], onAudioFileSelected: @escaping (URL) -> Void) {
        guard destination.canUpload else {
            selectionMessage = destination.message
            return
        }
        let acceptedCount = selectionCapacity.acceptedCount(requested: urls.count)
        guard acceptedCount > 0, let source = urls.first else {
            selectionMessage = "You’ve reached the limit for \(role.title.lowercased())."
            return
        }
        let accessed = source.startAccessingSecurityScopedResource()
        defer { if accessed { source.stopAccessingSecurityScopedResource() } }
        do {
            let values = try source.resourceValues(forKeys: [.fileSizeKey, .contentTypeKey])
            guard let size = values.fileSize, size > 0 else { throw CreationUploadError.unsupportedType }
            let contentType = values.contentType?.preferredMIMEType ?? "application/octet-stream"
            guard role.accepts(contentType) else { throw CreationUploadError.unsupportedType }
            if let limit, !limit.contentTypes.contains(contentType) { throw CreationUploadError.unsupportedType }
            if let limit, let maximum = limit.byteLimit(contentType: contentType), Int64(size) > maximum { throw CreationUploadError.tooLarge }
            let destination = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString)-\(source.lastPathComponent)")
            try FileManager.default.copyItem(at: source, to: destination)
            onAudioFileSelected(destination)
        } catch {
            selectionMessage = error.localizedDescription
        }
    }
}


/// Applies the library-backed picker (real checkmarks, live selection, non-nil `itemIdentifier`)
/// when Photos access was granted, and today's permission-free picker otherwise.
///
/// KRI-175: continuous selection drops the system picker's Add/Cancel, and `.photosPicker` can't be
/// given toolbar items, so the library-backed picker is hosted inline in our own sheet with a Done
/// button. Without it the only way back was an undiscoverable swipe-down.
private struct FootagePhotosPicker: ViewModifier {
    @Binding var isPresented: Bool
    @Binding var selection: [PhotosPickerItem]
    let limit: Int
    let filter: PHPickerFilter
    let kinds: LibraryMediaKinds
    let libraryBacked: Bool
    let title: String
    /// Runs once the sheet has closed itself because a pick filled it, so the host can go further back.
    let onFilled: (() -> Void)?
    @Binding var didChooseNewFootage: Bool
    let onSelectionCompleted: (() -> Void)?
    @State private var closedByFilling = false

    private func completeSelectionIfNeeded() {
        guard didChooseNewFootage else { return }
        didChooseNewFootage = false
        onSelectionCompleted?()
    }

    func body(content: Content) -> some View {
        if libraryBacked {
            content.sheet(isPresented: $isPresented, onDismiss: {
                // After the dismissal, not with it: the host closing its own sheet while this one is
                // still on screen would tear both down mid-animation.
                if closedByFilling { closedByFilling = false; onFilled?() }
                completeSelectionIfNeeded()
            }) {
                LibraryPhotosPickerSheet(title: title, selection: $selection, limit: limit, filter: filter, kinds: kinds) { filled in
                    closedByFilling = filled
                    isPresented = false
                }
            }
        } else {
            content.photosPicker(isPresented: $isPresented, selection: $selection, maxSelectionCount: limit, matching: filter)
                .onChange(of: isPresented) { _, presented in
                    if !presented { completeSelectionIfNeeded() }
                }
        }
    }
}

private struct LibraryPhotosPickerSheet: View {
    let title: String
    @Binding var selection: [PhotosPickerItem]
    let limit: Int
    let filter: PHPickerFilter
    let kinds: LibraryMediaKinds
    let close: (_ filled: Bool) -> Void
    /// The in-app gallery (slide-to-select) is the default under full access; Apple's inline picker stays one tap away.
    @State private var usesApplePicker = ProcessInfo.processInfo.arguments.contains("-ui-testing-apple-photo-picker")

    var body: some View {
        NavigationStack {
            Group {
                if usesApplePicker {
                    PhotosPicker(selection: $selection, maxSelectionCount: limit, selectionBehavior: .continuousAndOrdered,
                                 matching: filter, photoLibrary: .shared()) { EmptyView() }
                        .photosPickerStyle(.inline)
                        .ignoresSafeArea(edges: .bottom)
                } else {
                    LibraryGalleryGrid(selection: $selection, limit: limit, kinds: kinds)
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(usesApplePicker ? "Kria gallery" : "Apple's picker") { usesApplePicker.toggle() }
                        .accessibilityIdentifier("gallery-toggle-picker")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { close(false) }.accessibilityIdentifier("photos-picker-done")
                }
            }
        }
        .onChange(of: selection) { previous, current in
            let ids = current.compactMap(\.itemIdentifier)
            guard PhotoPickerAutoClose.shouldClose(previous: previous.compactMap(\.itemIdentifier), current: ids, limit: limit) else { return }
            // Long enough to see the checkmark land; the uploads were already started by the host's
            // own `onChange`, which doesn't depend on this sheet staying open.
            Task {
                try? await Task.sleep(for: .milliseconds(350))
                if selection.compactMap(\.itemIdentifier) == ids { close(true) }
            }
        }
    }
}

struct ClipSelectionCapacity: Equatable, Sendable {
    let maximum: Int
    let existing: Int
    let reserved: Int
    /// Clips the library-backed picker will show pre-selected. They are already counted in
    /// `existing`/`reserved`, and they count against the picker's own selection limit too.
    var preselected = 0

    var remaining: Int { max(0, maximum - existing - reserved) }

    /// What to pass as `maxSelectionCount`. Preselected items occupy picker slots, but clips added
    /// via Files or a voiceover occupy project slots the picker can never show — so this is the
    /// project cap minus everything that is invisible to the picker as a selection.
    /// `max(1, …)` because PhotosUI treats 0 as "unlimited".
    var pickerSelectionLimit: Int {
        // Never below what is already seeded: a limit under the seed would make the picker drop items,
        // which reads as un-choosing them. Reachable when the cap drops under what is attached (a
        // format switch, or capabilities falling back), and then there is simply no room to add more.
        max(1, preselected, min(maximum, maximum - max(0, existing + reserved - preselected)))
    }

    func acceptedCount(requested: Int) -> Int {
        min(max(0, requested), remaining)
    }
}

struct ImportedMedia: Transferable {
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

/// The guided voiceover importer is the page's primary action.
private struct AttachmentFileButtonStyle: ButtonStyle {
    let isVoiceover: Bool
    @ViewBuilder func makeBody(configuration: Configuration) -> some View {
        if isVoiceover {
            AttachmentPrimaryButtonStyle().makeBody(configuration: configuration)
        } else {
            KriaSecondaryButtonStyle().makeBody(configuration: configuration)
        }
    }
}
