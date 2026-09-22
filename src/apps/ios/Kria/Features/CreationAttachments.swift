import SwiftUI
import AVFoundation
import UIKit
import KriaMediaEngine

struct AttachmentSheet: View {
    let projectID: UUID
    let maximumClipCount: Int
    let attachedClipCount: Int
    let format: CreationFormat?
    let thread: CreationThread?
    let capabilities: CreationCapabilities?
    /// False until the account's capabilities have loaded; uploads wait for them.
    var capabilitiesLoaded = true
    let refresh: () async -> Void
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var role: CreationMediaRole = .clip
    @State private var pool: CreationVisuals?
    @State private var error: String?
    @State private var mutatingVisual = false
    @State private var removingMedia = false
    @State private var pendingRemoval: CreationActionIdentity?
    @State private var uploadingRecording = false
    @State private var pendingRecords: [UploadRecoveryRecord] = []
    @StateObject private var recorder = CreationVoiceRecorder()

    init(
        projectID: UUID,
        maximumClipCount: Int,
        attachedClipCount: Int,
        format: CreationFormat? = nil,
        thread: CreationThread?,
        capabilities: CreationCapabilities?,
        capabilitiesLoaded: Bool = true,
        refresh: @escaping () async -> Void
    ) {
        self.projectID = projectID
        self.maximumClipCount = maximumClipCount
        self.attachedClipCount = attachedClipCount
        self.format = format
        self.thread = thread
        self.capabilities = capabilities
        self.capabilitiesLoaded = capabilitiesLoaded
        self.refresh = refresh
        _role = State(initialValue: format?.usesVisualPool == true ? .visual : .clip)
    }

    /// Slides may only use the existing PlanItemAsset Visuals pool. Keeping
    /// this decision inside the sheet prevents a transient role-picker render
    /// from offering primary footage or voiceover before the picker appears.
    private var usesVisualPoolOnly: Bool { format?.usesVisualPool == true }

    private var itemID: String? { thread?.activePlanItemID }
    private var limit: CreationMediaLimit? { capabilities?.media?[role.capabilityKey] }
    private var media: [CreationAttachedMedia] { CreationAttachedMedia.parse(thread?.state ?? [:]) }
    private var maximum: Int {
        switch role {
        case .clip: maximumClipCount
        case .voiceover: capabilities?.media?["voiceover"]?.max ?? 1
        case .visual: pool?.maxAssets ?? 0
        }
    }
    private var existing: Int {
        switch role {
        case .clip: return attachedClipCount
        case .voiceover: return media.filter { $0.kind == "audio" }.count
        case .visual:
            let ownReservations = Set(pendingRecords.filter { $0.projectID == projectID && $0.role == .visual }.compactMap(\.visualReservationID))
            let overlap = pool?.activeReservations?.filter { ownReservations.contains($0.reservationID) }.count ?? 0
            return max(pool?.assets.count ?? 0, (pool?.occupiedAssets ?? 0) - overlap)
        }
    }
    /// What is attached for the current role, so the picker can show those clips as already chosen —
    /// and stop doing so the moment one is removed (trash button, web, or un-choosing it).
    private var attachedMediaIDs: Set<String> {
        switch role {
        case .clip: Set(media.filter { $0.kind == "video" }.map(\.id))
        case .voiceover: Set(media.filter { $0.kind == "audio" }.map(\.id))
        case .visual: Set((pool?.assets ?? []).map(\.id))
        }
    }
    private var canRecord: Bool {
        existing + pendingRecords.filter { $0.projectID == projectID && $0.role == .voiceover }.count < maximum
    }
    private var uploadDestination: ProjectUploadDestination {
        ProjectUploadDestination.resolve(
            capabilities: capabilities?.phoneRendering,
            capabilitiesLoaded: capabilitiesLoaded,
            sourcePurposes: ProjectUploadDestination.sourcePurposes(media: media, records: pendingRecords, projectID: projectID),
            role: role
        )
    }
    /// Names only the kinds the Visuals pickers offer on this destination.
    private var visualsCaption: String {
        let kinds = uploadDestination.visualKinds
        return kinds == [.image] ? "Photos and screenshots."
            : kinds == [.video] ? "Short supporting videos."
            : "Photos, screenshots, or short supporting videos."
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if !usesVisualPoolOnly {
                        Picker("Attachment type", selection: $role) {
                            Text("Footage").tag(CreationMediaRole.clip)
                            if capabilities?.visualsEnabled == true, itemID != nil { Text("Visuals").tag(CreationMediaRole.visual) }
                            if thread.map({ CreationFormat(thread: $0) == .narrated }) == true || media.contains(where: { $0.kind == "audio" }) {
                                Text("Voiceover").tag(CreationMediaRole.voiceover)
                            }
                        }.pickerStyle(.segmented)
                    }
                    if role == .visual {
                        // When nothing can upload, the destination message explains why.
                        if uploadDestination.canUpload {
                            Text(usesVisualPoolOnly ? "Photos & videos" : visualsCaption)
                                .font(KriaFont.body(14))
                        }
                        if pool == nil, error == nil { ProgressView("Loading visuals…") }
                    }
                    // Until the pool loads, its limit is unknown; don't show a
                    // disabled picker claiming the limit was reached.
                    if role != .visual || pool != nil {
                        FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum, attachedClipCount: existing, attachedMediaIDs: attachedMediaIDs, role: role, itemID: itemID, limit: limit, destination: uploadDestination)
                            .id(role)
                    }
                    // KRI-132: available on `.cloud` (always) and on `.phone` once the
                    // device has verified `narrationAudio` -- `uploadDestination.resolve`
                    // only ever returns `.phone` for the voiceover role in that case.
                    if role == .voiceover && uploadDestination.canUpload {
                        if recorder.isRecording {
                            Button("Stop recording") { Task { await finishRecording() } }.buttonStyle(CanonicalPrimaryButtonStyle())
                        } else if recorder.hasRecording {
                            Button(uploadingRecording ? "Uploading recording…" : "Upload recording") { Task { await finishRecording() } }
                                .disabled(uploadingRecording || !canRecord)
                            Button("Discard recording") { recorder.discard() }.disabled(uploadingRecording)
                        } else {
                            Button("Record voiceover") { Task { await recorder.start() } }.disabled(!canRecord || uploadingRecording)
                        }
                        if let recorderError = recorder.error { Text(recorderError).font(KriaFont.body(13)) }
                    }
                    if role != .visual {
                        ForEach(media.filter { role == .voiceover ? $0.kind == "audio" : $0.kind == "video" }) { attachment in
                            HStack {
                                CreationAttachmentThumbnail(media: attachment)
                                Text(attachment.filename).lineLimit(1)
                                Spacer()
                                Button { Task { await removeAttached(attachment) } } label: { Image(systemName: "trash").frame(width: 44, height: 44) }
                                    .accessibilityLabel("Remove \(attachment.filename)")
                                    .disabled(removingMedia || !pendingRecords.filter { $0.projectID == projectID }.isEmpty)
                            }
                        }
                    }
                    if role == .visual {
                        ForEach(pool?.assets ?? []) { asset in
                            HStack(spacing: 12) {
                                AsyncImage(url: asset.previewURL ?? (asset.kind == "image" ? asset.displayURL : nil)) { image in
                                    image.resizable().scaledToFill()
                                } placeholder: { Image(systemName: asset.kind == "image" ? "photo" : "video") }
                                    .frame(width: 52, height: 64).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
                                VStack(alignment: .leading) {
                                    Text(asset.sourceFilename ?? "Visual").lineLimit(1)
                                    Text(asset.status.capitalized).font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
                                }
                                Spacer()
                                if asset.status == "failed", asset.retryable != false {
                                    Button("Retry") { Task { await retry(asset) } }.disabled(mutatingVisual)
                                }
                                Button { Task { await remove(asset) } } label: { Image(systemName: "trash").frame(width: 44, height: 44) }
                                    .accessibilityLabel("Remove \(asset.sourceFilename ?? "visual")")
                                    .disabled(mutatingVisual)
                            }
                        }
                    }
                    if let error {
                        Text(error).font(KriaFont.body(13))
                        if role == .visual { Button("Retry loading visuals") { Task { await loadVisuals() } } }
                    }
                }.padding(20)
            }
            .background(KriaColor.paper)
            .navigationTitle("Add media")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.disabled(recorder.isRecording) } }
            .interactiveDismissDisabled(recorder.isRecording)
            .task {
                guard capabilities?.visualsEnabled == true || usesVisualPoolOnly else { return }
                while !Task.isCancelled {
                    await loadVisuals()
                    do { try await Task.sleep(for: .seconds(5)) } catch { return }
                }
            }
            // Uploads wait for the account's capabilities; keep asking rather
            // than leave the sheet on "Checking…" after a failed load. A load
            // changes `capabilitiesLoaded`, which restarts this task and ends it.
            .task(id: capabilitiesLoaded) {
                guard !capabilitiesLoaded else { return }
                while !Task.isCancelled {
                    await refresh()
                    try? await Task.sleep(for: .seconds(3))
                }
            }
            .onReceive(model.uploads.$records) { pendingRecords = $0 }
            // Un-choosing a visual in the picker removes it server-side; refresh the pool now rather
            // than on the next poll, or the picker would keep showing it as chosen for a few seconds.
            .onReceive(model.uploads.$photoSelections) { _ in if role == .visual { Task { await loadVisuals() } } }
            .onDisappear { recorder.discard() }
        }
    }
    private func removeAttached(_ attachment: CreationAttachedMedia) async {
        guard !removingMedia else { return }
        removingMedia = true
        defer { removingMedia = false }
        do {
            let latest = try await model.api.project(threadID: projectID)
            let identity = CreationActionIdentity.reusing(pendingRemoval, action: "remove_media", payload: ["media_id": .string(attachment.id)], revision: latest.revision)
            pendingRemoval = identity
            _ = try await model.api.creationAction(threadID: projectID, action: identity.action, payload: identity.payload, expectedRevision: identity.revision, clientActionID: identity.id)
            pendingRemoval = nil
            await refresh()
        } catch APIError.conflict {
            pendingRemoval = nil
            await refresh()
            error = "This project changed. Review the attached files and try again."
        } catch { self.error = "Couldn’t remove the file. \(error.localizedDescription)" }
    }
    private func loadVisuals() async {
        guard let itemID else { return }
        do { pool = try await model.api.visuals(itemID: itemID); error = nil }
        catch { self.error = "Kria couldn’t load your visuals. \(error.localizedDescription)" }
    }
    private func remove(_ asset: CreationVisual) async {
        guard let itemID, !mutatingVisual else { return }
        mutatingVisual = true
        defer { mutatingVisual = false }
        do { try await model.api.removeVisual(itemID: itemID, assetID: asset.id); await loadVisuals() }
        catch { self.error = "Couldn’t remove this visual. \(error.localizedDescription)" }
    }
    private func retry(_ asset: CreationVisual) async {
        guard let itemID, !mutatingVisual else { return }
        mutatingVisual = true
        defer { mutatingVisual = false }
        do { _ = try await model.api.retryVisual(itemID: itemID, assetID: asset.id); await loadVisuals() }
        catch { self.error = "Couldn’t retry this visual. \(error.localizedDescription)" }
    }
    private func finishRecording() async {
        guard !uploadingRecording, let url = recorder.stop() else { return }
        uploadingRecording = true
        defer { uploadingRecording = false }
        let accepted = await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .voiceover, itemID: itemID, limit: capabilities?.media?["voiceover"])
        if accepted { recorder.discard() }
    }
}

/// Thumbnail of a clip that is still being prepared or uploaded, keyed by its upload id, so it is
/// visible from the moment the clip is chosen rather than after it has attached.
struct CreationRecordThumbnail: View {
    let recordID: UUID
    /// `BackgroundUploadCoordinator.previewVersion`. Every other input here is constant, so without it
    /// SwiftUI would never re-read the file when the thumbnail appears.
    let version: Int

    var body: some View {
        let _ = version
        Group {
            if let image = UIImage(contentsOfFile: CreationMediaPreview.url(recordID: recordID).path) {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: "video").foregroundStyle(KriaColor.zinc)
            }
        }
        .frame(width: 52, height: 64).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
        .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
        .accessibilityHidden(true)
    }
}

struct CreationAttachmentThumbnail: View {
    let media: CreationAttachedMedia
    var body: some View {
        Group {
            if media.kind == "audio" { Image(systemName: "waveform") }
            else if let image = UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path) { Image(uiImage: image).resizable().scaledToFill() }
            else { AsyncImage(url: media.previewURL) { image in image.resizable().scaledToFill() } placeholder: { Image(systemName: "video") } }
        }.frame(width: 52, height: 64).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

@MainActor final class CreationVoiceRecorder: ObservableObject {
    @Published private(set) var isRecording = false
    @Published private(set) var error: String?
    @Published private(set) var hasRecording = false
    private var recorder: AVAudioRecorder?
    private var fileURL: URL?
    func start() async {
        guard !isRecording else { return }
        guard await AVAudioApplication.requestRecordPermission() else {
            error = "Allow microphone access in Settings to record narration. You can also upload an audio file."
            return
        }
        do {
            discard()
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker])
            try session.setActive(true)
            let url = FileManager.default.temporaryDirectory.appending(path: "voiceover-\(UUID().uuidString).m4a")
            fileURL = url
            recorder = try AVAudioRecorder(url: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 44100, AVNumberOfChannelsKey: 1, AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue])
            guard recorder?.record() == true else { throw APIError.invalidResponse }
            error = nil
            isRecording = true
        } catch { self.error = "Couldn’t start recording. \(error.localizedDescription)"; discard() }
    }
    func stop() -> URL? {
        recorder?.stop()
        recorder = nil
        isRecording = false
        hasRecording = fileURL != nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        return fileURL
    }
    func discard() {
        _ = stop()
        if let fileURL { try? FileManager.default.removeItem(at: fileURL) }
        fileURL = nil
        hasRecording = false
    }
}
