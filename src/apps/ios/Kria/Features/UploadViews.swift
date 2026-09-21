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
    @ObservedObject private var uploads: BackgroundUploadCoordinator
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingPhotosPicker = false
    @State private var showingFileImporter = false
    @State private var libraryAuthorized = false
    /// How many of the chosen assets the library can still show. Fetched when the set changes, not
    /// per render: the picker's limit must count what it will actually display.
    @State private var showablePreselectedCount = 0
    @State private var selectionMessage: String?

    init(
        projectID: UUID,
        uploads: BackgroundUploadCoordinator,
        maximumClipCount: Int = 10,
        attachedClipCount: Int = 0,
        attachedMediaIDs: Set<String> = [],
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
        self.attachedMediaIDs = attachedMediaIDs
    }

    /// What this project uploads: smaller analysis copies when it renders on the phone, originals when
    /// it renders in the cloud. Fixed by the project's destination, not something the user picks per
    /// upload. Agreement to share media with Kria's AI providers is given once, at account level
    /// (`AIConsentView` gates the whole workspace), so there is no per-upload consent screen.
    private var uploadPurpose: UploadPurpose {
        destination == .phone ? .analysisProxy : .cloudRenderSource
    }

    /// Non-blocking disclosure of what is uploaded, shown where the per-upload consent screen used to be.
    private var uploadDisclosure: String? {
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
            KriaSectionLabel(title: "Add \(role.title.lowercased())")
            if role != .voiceover {
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
                libraryBacked: libraryAuthorized
            ))
            .onChange(of: photoItems) { _, items in reconcile(items) }
            }
            Button { beginImport(source: .files) } label: {
                Label("Choose from Files or iCloud", systemImage: "folder").frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(selectionCapacity.remaining == 0 || !destination.canUpload)
            .fileImporter(
                isPresented: $showingFileImporter,
                allowedContentTypes: role == .voiceover ? [.audio] : role == .visual ? visualContentTypes : [.movie],
                allowsMultipleSelection: selectionCapacity.remaining > 1,
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

    /// Called on every change of the live selection. Deliberately does nothing but diff and delegate:
    /// it must not touch presentation state, which fights the picker sheet that is still open.
    private func reconcile(_ items: [PhotosPickerItem]) {
        guard libraryAuthorized else { importUnidentified(items); return }
        // Diff against what the coordinator already knows, never against `@State`: `.id(role)`
        // resets `@State`, and a seeded selection would then read as "everything is new" and
        // upload every clip again.
        let diff = PhotoSelectionDiff(current: items.compactMap(\.itemIdentifier), known: preselectedIdentifiers)
        guard !diff.isEmpty else { return }
        let byIdentifier = Dictionary(items.compactMap { item in item.itemIdentifier.map { ($0, item) } }, uniquingKeysWith: { first, _ in first })
        for identifier in diff.added {
            guard let item = byIdentifier[identifier] else { continue }
            uploads.select(.init(assetIdentifier: identifier, projectID: projectID, role: role, purpose: uploadPurpose, itemID: itemID, limit: limit, attachedMediaIDs: attachedMediaIDs)) {
                guard let media = try await item.loadTransferable(type: ImportedMedia.self) else { throw UnreadablePhoto() }
                return media.url
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
        let purpose = uploadPurpose
        let acceptedCount = selectionCapacity.acceptedCount(requested: urls.count)
        if acceptedCount < urls.count {
            selectionMessage = "Only \(acceptedCount) more \(acceptedCount == 1 ? "clip" : "clips") can be added in this format."
        }
        let chosen = Array(urls.prefix(acceptedCount))
        let ids = chosen.map { _ in UUID() }
        for (url, id) in zip(chosen, ids) { uploads.markInFlight(id, projectID: projectID, role: role, filename: url.lastPathComponent) }
        Task {
            for (url, id) in zip(chosen, ids) {
                _ = await uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: purpose, role: role, itemID: itemID, limit: limit, recordID: id)
            }
            for id in ids { uploads.clearInFlight(id) }
        }
    }
}

private struct UnreadablePhoto: Error {}

/// Applies the library-backed picker (real checkmarks, live selection, non-nil `itemIdentifier`)
/// when Photos access was granted, and today's permission-free picker otherwise.
private struct FootagePhotosPicker: ViewModifier {
    @Binding var isPresented: Bool
    @Binding var selection: [PhotosPickerItem]
    let limit: Int
    let filter: PHPickerFilter
    let libraryBacked: Bool

    func body(content: Content) -> some View {
        if libraryBacked {
            content.photosPicker(isPresented: $isPresented, selection: $selection, maxSelectionCount: limit,
                                 selectionBehavior: .continuousAndOrdered, matching: filter, photoLibrary: .shared())
        } else {
            content.photosPicker(isPresented: $isPresented, selection: $selection, maxSelectionCount: limit, matching: filter)
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
