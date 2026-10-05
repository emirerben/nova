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
    @Environment(\.scenePhase) private var scenePhase
    @State private var role: CreationMediaRole = .clip
    @State private var step: AttachmentStep = .footage
    @State private var pool: CreationVisuals?
    @State private var error: String?
    @State private var mutatingVisual = false
    @State private var removingMedia = false
    @State private var mediaRemovalError: String?
    @State private var pendingRemoval: CreationActionIdentity?
    @State private var uploadingRecording = false
    @State private var voiceoverUploadError: String?
    @State private var confirmsDiscardingTake = false
    @State private var pendingRecords: [UploadRecoveryRecord] = []
    @State private var inFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [:]
    /// Per-sheet budget for retrying transient analysis failures on the creator's behalf.
    @State private var autoRetry = VisualAutoRetryScheduler()
    /// How long each Visual has been preparing, so a slow wait says so (KRI-294).
    @State private var preparation = VisualPreparationClock()
    /// Lets the batch summary say when uploads wait for a connection.
    @ObservedObject private var network = NetworkReachability.shared
    @State private var visualPollRunning = false
    /// The extra poll started when the app returns to the foreground. Kept so
    /// only one runs at a time and it is cancelled with the sheet; a bare
    /// `Task` would outlive the sheet and keep acting on its snapshot.
    @State private var foregroundPoll: Task<Void, Never>?
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
        let initialRole: CreationMediaRole = format?.usesVisualPool == true ? .visual : .clip
        _role = State(initialValue: initialRole)
        _step = State(initialValue: initialRole == .visual ? .overlays : .footage)
    }

    /// Slides may only use the existing PlanItemAsset Visuals pool. Keeping
    /// this decision inside the sheet prevents a transient role-picker render
    /// from offering primary footage or voiceover before the picker appears.
    private var usesVisualPoolOnly: Bool { format?.usesVisualPool == true }
    private var narrated: Bool { (format ?? thread.map { CreationFormat(thread: $0) }) == .narrated }
    private var hasVoiceoverStep: Bool { narrated || media.contains(where: { $0.kind == "audio" }) }
    private var hasOverlaysStep: Bool { usesVisualPoolOnly || (capabilities?.visualsEnabled == true && itemID != nil) }
    private var steps: [AttachmentStep] {
        var result: [AttachmentStep] = usesVisualPoolOnly ? [.overlays] : [.footage]
        if hasVoiceoverStep { result.append(.voiceover) }
        if hasOverlaysStep && !usesVisualPoolOnly { result.append(.overlays) }
        return result
    }
    private var stepIndex: Int { steps.firstIndex(of: step) ?? 0 }

    private var itemID: String? { thread?.activePlanItemID }
    private var pollsVisuals: Bool { capabilities?.visualsEnabled == true || usesVisualPoolOnly }
    private var limit: CreationMediaLimit? { capabilities?.media?[role.capabilityKey] }
    private var media: [CreationAttachedMedia] { CreationAttachedMedia.parse(thread?.state ?? [:]) }
    private func maximum(for mediaRole: CreationMediaRole) -> Int {
        switch mediaRole {
        case .clip: maximumClipCount
        case .voiceover: capabilities?.media?["voiceover"]?.max ?? 1
        case .visual: pool?.maxAssets ?? 0
        }
    }
    private func existing(for mediaRole: CreationMediaRole) -> Int {
        switch mediaRole {
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
    private func attachedMediaIDs(for mediaRole: CreationMediaRole) -> Set<String> {
        switch mediaRole {
        case .clip: Set(media.filter { $0.kind == "video" }.map(\.id))
        case .voiceover: Set(media.filter { $0.kind == "audio" }.map(\.id))
        case .visual: Set((pool?.assets ?? []).map(\.id))
        }
    }
    private var canRecord: Bool {
        let queued = pendingRecords.filter { $0.projectID == projectID && $0.role == .voiceover }
        let queuedIDs = Set(queued.map(\.id))
        let preparing = inFlight.filter {
            $0.value.projectID == projectID && $0.value.role == .voiceover && !queuedIDs.contains($0.key)
        }.count
        return !recorder.isImporting && existing(for: .voiceover) + queued.count + preparing < maximum(for: .voiceover)
    }
    private func uploadDestination(for mediaRole: CreationMediaRole) -> ProjectUploadDestination {
        ProjectUploadDestination.resolve(
            capabilities: capabilities?.phoneRendering,
            capabilitiesLoaded: capabilitiesLoaded,
            sourcePurposes: ProjectUploadDestination.sourcePurposes(media: media, records: pendingRecords, projectID: projectID),
            role: mediaRole
        )
    }
    /// Chosen Visuals not in the pool yet, for the batch progress (KRI-294). Uses the values the
    /// sheet copied from the coordinator's publishers, like the other counts here.
    private var uploadingVisualCount: Int {
        BackgroundUploadCoordinator.activeUploadCount(projectID: projectID, role: .visual, inFlight: inFlight, records: pendingRecords, selections: model.uploads.photoSelections)
    }
    /// Names only the kinds the Visuals pickers offer on this destination.
    private var visualsCaption: String {
        let kinds = uploadDestination(for: .visual).visualKinds
        return kinds == [.image] ? "Photos and screenshots."
            : kinds == [.video] ? "Short supporting videos."
            : "Photos, screenshots, or short supporting videos."
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                attachmentNavigation
                AttachmentStepProgress(steps: steps, current: step, overlaysTitle: usesVisualPoolOnly ? SlideMediaCopy.poolTitle : nil)
                TabView(selection: $step) {
                    ForEach(steps) { aStep in
                        ScrollView {
                            stepContent(aStep)
                                .padding(.horizontal, 24)
                                .padding(.top, 20)
                                .padding(.bottom, 18)
                        }
                        .tag(aStep)
                        .accessibilityIdentifier(aStep.accessibilityID)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: step)
                .onChange(of: step) { _, newStep in
                    if recorder.isRecording, newStep != .voiceover {
                        recorder.stopForInterruption()
                        step = .voiceover
                        role = .voiceover
                        return
                    }
                    role = newStep.role
                    if newStep != .voiceover { recorder.pausePlayback() }
                }
                attachmentFooter
            }
            .background(KriaColor.paper)
            .interactiveDismissDisabled(recorder.isRecording || recorder.hasRecording)
            .confirmationDialog("Discard this voiceover?", isPresented: $confirmsDiscardingTake, titleVisibility: .visible) {
                Button("Discard recording", role: .destructive) { recorder.discard(); dismiss() }
                Button("Keep recording", role: .cancel) { }
            } message: { Text("Your recording has not been added yet.") }
            .task {
                guard pollsVisuals else { return }
                while !Task.isCancelled {
                    await pollVisuals()
                    do { try await Task.sleep(for: .seconds(5)) } catch { return }
                }
            }
            // Back in the foreground, poll now: an automatic retry that came due
            // while the app was suspended fires at once instead of up to 5 s later.
            .onChange(of: scenePhase) { _, phase in
                guard phase == .active, pollsVisuals, foregroundPoll == nil else { return }
                foregroundPoll = Task {
                    await pollVisuals()
                    foregroundPoll = nil
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
            #if DEBUG
            .task {
                if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat"),
                   ProcessInfo.processInfo.environment["KRIA_VOICEOVER_PREPARING_FIXTURE"] == "1" {
                    model.uploads.markInFlight(UUID(), projectID: projectID, role: .voiceover, filename: "Preparing voiceover.m4a")
                }
                guard ProcessInfo.processInfo.environment["KRIA_VOICEOVER_REVIEW_FIXTURE"] == "1",
                      ProcessInfo.processInfo.arguments.contains("-ui-testing-chat"),
                      !recorder.hasRecording,
                      let url = VoiceoverReviewFixture.make() else { return }
                recorder.importFile(url)
            }
            #endif
            .onReceive(model.uploads.$records) { records in
                // A Visual that just attached leaves `records` before the next poll lists it in the
                // pool; load now so it doesn't drop out of the batch progress for a few seconds.
                let visualLeft = pendingRecords.contains { old in
                    old.projectID == projectID && old.role == .visual && !records.contains { $0.id == old.id }
                }
                pendingRecords = records
                if visualLeft, pollsVisuals { Task { await loadVisuals() } }
            }
            .onReceive(model.uploads.$inFlight) { inFlight = $0 }
            // Un-choosing a visual in the picker removes it server-side; refresh the pool now rather
            // than on the next poll, or the picker would keep showing it as chosen for a few seconds.
            .onReceive(model.uploads.$photoSelections) { _ in if role == .visual { Task { await loadVisuals() } } }
            .onChange(of: scenePhase) { _, phase in
                if phase != .active { recorder.stopForInterruption() }
                if phase == .background { recorder.cancelPendingWorkAndStop() }
            }
            .onDisappear {
                recorder.cancelPendingWorkAndStop()
                foregroundPoll?.cancel()
                foregroundPoll = nil
            }
        }
    }
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var attachmentNavigation: some View {
        HStack {
            Button { goBack() } label: {
                Image(systemName: "chevron.left").frame(width: 44, height: 44)
            }.opacity(stepIndex == 0 ? 0 : 1).disabled(stepIndex == 0 || recorder.isRecording)
            Spacer()
            Text("Add media").font(KriaFont.body(16).weight(.semibold))
            Spacer()
            Button { requestDismiss() } label: {
                Image(systemName: "xmark").frame(width: 44, height: 44).background(KriaColor.softZinc, in: Circle())
            }.disabled(recorder.isRecording).accessibilityIdentifier("attachment-close").accessibilityLabel("Close")
        }
        .padding(.horizontal, 16).frame(height: 52)
    }

    @ViewBuilder private func stepContent(_ aStep: AttachmentStep) -> some View {
        switch aStep {
        case .footage: footageStep
        case .voiceover: voiceoverStep
        case .overlays: overlaysStep
        }
    }

    private var footageStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: "Add footage", subtitle: "Choose the clips you want Kria to work with.")
            FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximumClipCount, attachedClipCount: existing(for: .clip), attachedMediaIDs: attachedMediaIDs(for: .clip), role: .clip, itemID: itemID, limit: capabilities?.media?["clips"], destination: uploadDestination(for: .clip), onPickerFilled: nil, onSelectionCompleted: advanceAfterFootageSelection, showsHeading: false)
                .id(AttachmentStep.footage)
            attachedList(role: .clip)
        }
    }

    private var voiceoverStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: recorder.hasRecording ? (recorder.isImported ? "Review voiceover" : "Review recording") : (recorder.isRecording ? "Recording voiceover" : "Add voiceover"), subtitle: recorder.hasRecording ? "Listen before adding this recording to your video." : "Record here or upload a voiceover you’ve already made.")
            if !recorder.hasRecording {
                FootageDurationCard(summary: footageDurationSummary)
                    .accessibilityIdentifier("footage-duration")
            }
            if recorder.isRecording {
                RecordingVoiceoverView(recorder: recorder) { recorder.stop() }
            } else if recorder.hasRecording {
                VoiceoverReviewView(recorder: recorder)
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Voiceover").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        Text(DurationFormatter.clock(recorder.duration)).font(KriaFont.body(24).weight(.semibold))
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 6) {
                        Text("Total footage").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        Text(footageDurationSummary.value).font(KriaFont.body(24).weight(.semibold))
                            .accessibilityIdentifier("footage-duration")
                    }
                }
            } else {
                VoiceoverStartView(canRecord: canRecord && uploadDestination(for: .voiceover).canUpload, start: { Task { await recorder.start() } })
            }
            if let recorderError = recorder.error { Text(recorderError).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
            if (recorder.hasRecording || recorder.isRecording), let warning = voiceoverWarning {
                VoiceoverDurationWarning(message: warning, addFootage: { step = .footage })
                    .accessibilityIdentifier("voiceover-duration-warning")
            }
            if recorder.hasRecording {
                VoiceoverReviewActions(recorder: recorder, onUse: { Task { await useRecording() } }, onRecordAgain: discardVoiceover, onChooseAnother: discardVoiceover, canUse: canRecord && uploadDestination(for: .voiceover).canUpload, isUsing: uploadingRecording)
            }
            attachedList(role: .voiceover)
            if let voiceoverUploadError { Text(voiceoverUploadError).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("voiceover-upload-error") }
        }
    }

    private var overlaysStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: usesVisualPoolOnly ? SlideMediaCopy.addHeading : "Add overlays", subtitle: usesVisualPoolOnly ? SlideMediaCopy.subtitle : "Optional photos and supporting clips can add context to your video.")
            if uploadDestination(for: .visual).canUpload, !usesVisualPoolOnly { Text(visualsCaption).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc) }
            if pool == nil, error == nil { ProgressView(usesVisualPoolOnly ? SlideMediaCopy.loading : "Loading overlays…") }
            if pool != nil {
                FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum(for: .visual), attachedClipCount: existing(for: .visual), attachedMediaIDs: attachedMediaIDs(for: .visual), role: .visual, itemID: itemID, limit: capabilities?.media?["visuals"], destination: uploadDestination(for: .visual), onPickerFilled: nil, onSelectionCompleted: nil, showsHeading: false, titleOverride: usesVisualPoolOnly ? SlideMediaCopy.poolTitle : nil)
                    .id(AttachmentStep.overlays)
                if let summary = VisualPreparationSummary(assets: pool?.assets ?? [], uploading: uploadingVisualCount, online: network.isOnline, slowIDs: preparation.slowIDs, surface: .addMediaSheet, nounOverride: usesVisualPoolOnly ? SlideMediaCopy.preparingNoun : nil) {
                    VisualPreparationSummaryView(summary: summary)
                }
                visualList
            }
            if let error {
                Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                Button(usesVisualPoolOnly ? SlideMediaCopy.retryLoading : "Retry loading overlays") { Task { await loadVisuals() } }
            }
        }
    }

    @ViewBuilder private var attachmentFooter: some View {
        if recorder.isRecording { EmptyView() }
        else if step == .voiceover {
            if !recorder.hasRecording {
                VStack(spacing: 8) {
                    if recorder.isImporting {
                        ProgressView("Opening audio…")
                    } else {
                        FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum(for: .voiceover), attachedClipCount: existing(for: .voiceover), attachedMediaIDs: attachedMediaIDs(for: .voiceover), role: .voiceover, itemID: itemID, limit: capabilities?.media?["voiceover"], destination: uploadDestination(for: .voiceover), onAudioFileSelected: recorder.importFile, showsHeading: false)
                    }
                    if existing(for: .voiceover) > 0 || pendingRecords.contains(where: { $0.projectID == projectID && $0.role == .voiceover }) || inFlight.values.contains(where: { $0.projectID == projectID && $0.role == .voiceover }) {
                        Button("Continue with voiceover") { advance() }.frame(minHeight: 44)
                    } else {
                        Button("Use footage audio instead") { advance() }
                            .font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.zinc)
                            .frame(minHeight: 44).accessibilityIdentifier("voiceover-skip")
                            .disabled(recorder.isImporting)
                    }
                }.padding(.horizontal, 24).padding(.vertical, 12)
            }
        }
        else {
            VStack(spacing: 8) {
                Button(stepIndex == steps.count - 1 ? "Done" : "Next") { advance() }
                    .buttonStyle(AttachmentPrimaryButtonStyle())
                    .accessibilityIdentifier(stepIndex == steps.count - 1 ? "attachment-done" : "attachment-next")
            }.padding(.horizontal, 24).padding(.vertical, 12)
        }
    }

    private func attachedList(role: CreationMediaRole) -> some View {
        VStack(spacing: 10) {
            ForEach(media.filter { role == .voiceover ? $0.kind == "audio" : $0.kind == "video" }) { attachment in
                HStack(spacing: 12) {
                    CreationAttachmentThumbnail(media: attachment)
                    Text(attachment.filename).font(KriaFont.body(14)).lineLimit(1)
                    Spacer()
                    Button { Task { await removeAttached(attachment) } } label: { Image(systemName: "trash").frame(width: 44, height: 44) }
                        .accessibilityLabel("Remove \(attachment.filename)")
                        .disabled(removingMedia || !pendingRecords.filter { $0.projectID == projectID }.isEmpty)
                }
            }
            if let mediaRemovalError {
                Text(mediaRemovalError).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("attachment-removal-error")
            }
        }
    }

    private var visualList: some View {
        VStack(spacing: 12) {
            ForEach(pool?.assets ?? []) { asset in
                HStack(spacing: 12) {
                    AsyncImage(url: asset.previewURL ?? (asset.kind == "image" ? asset.displayURL : nil)) { image in image.resizable().scaledToFill() } placeholder: { Image(systemName: asset.kind == "image" ? "photo" : "video") }
                        .frame(width: 52, height: 64).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
                    VStack(alignment: .leading) {
                        Text(asset.sourceFilename ?? (usesVisualPoolOnly ? (asset.kind == "video" ? "Video" : "Photo") : "Visual")).font(KriaFont.body(14)).lineLimit(1)
                        VisualStatusLine(text: asset.statusCaption(retryingAutomatically: autoRetry.isRetryPending(asset.id)), preparing: asset.preparationStage != nil, color: asset.status == "failed" ? KriaColor.failureText : KriaColor.zinc)
                            .font(KriaFont.body(11))
                    }
                    Spacer()
                    if asset.status == "failed", asset.retryable != false { Button("Retry") { Task { await retry(asset) } }.disabled(mutatingVisual || !autoRetry.canRetryManually(asset.id)) }
                    Button { Task { await remove(asset) } } label: { Image(systemName: "trash").frame(width: 44, height: 44) }
                        .accessibilityLabel("Remove \(asset.sourceFilename ?? (usesVisualPoolOnly ? SlideMediaCopy.removeFallback : "visual"))").disabled(mutatingVisual)
                }
            }
        }
    }

    private var footageDurationSummary: FootageDurationSummary {
        let attached = media.filter { $0.kind == "video" }
        let attachedIDs = Set(attached.map(\.id))
        let records = pendingRecords.filter { $0.projectID == projectID && $0.role == .clip && !attachedIDs.contains($0.mediaID ?? "") }
        let recordIDs = Set(records.map(\.id))
        let inflight = inFlight.filter { $0.value.projectID == projectID && $0.value.role == .clip && !recordIDs.contains($0.key) }.map(\.value)
        let durations = attached.compactMap(\.durationS) + records.compactMap(\.durationS) + inflight.compactMap(\.durationS)
        let pendingMetadata = records.contains { $0.durationS == nil } || inflight.contains { $0.durationS == nil }
        let unavailableDuration = attached.contains { $0.durationS == nil }
        return FootageDurationSummary(count: attached.count + records.count + inflight.count, duration: durations.reduce(0, +), hasPendingMetadata: pendingMetadata, hasUnavailableDuration: unavailableDuration)
    }

    private var voiceoverWarning: String? {
        guard let footage = footageDurationSummary.exactDuration, recorder.duration > footage else { return nil }
        return "Your voiceover is \(DurationFormatter.seconds(recorder.duration - footage)) longer than your footage. Add more footage, or use it anyway."
    }
    private func advanceAfterFootageSelection() { guard step == .footage else { return }; advance() }
    private func advance() { guard stepIndex + 1 < steps.count else { requestDismiss(); return }; let next = steps[stepIndex + 1]; step = next; role = next.role }
    private func goBack() { guard !recorder.isRecording, stepIndex > 0 else { return }; let previous = steps[stepIndex - 1]; step = previous; role = previous.role }
    private func requestDismiss() {
        guard !uploadingRecording else { return }
        recorder.hasRecording ? (confirmsDiscardingTake = true) : dismiss()
    }
    private func discardVoiceover() {
        voiceoverUploadError = nil
        recorder.discard()
    }
    private func useRecording() async {
        guard !uploadingRecording, let url = recorder.recordingURL, canRecord else { return }
        uploadingRecording = true
        voiceoverUploadError = nil
        let accepted = await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .voiceover, itemID: itemID, limit: capabilities?.media?["voiceover"])
        uploadingRecording = false
        if accepted { voiceoverUploadError = nil; recorder.discard(); advance() }
        else { voiceoverUploadError = "This voiceover couldn’t be added. Your recording is still here—try again." }
    }
    private func removeAttached(_ attachment: CreationAttachedMedia) async {
        guard !removingMedia else { return }
        removingMedia = true
        mediaRemovalError = nil
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
            mediaRemovalError = "This project changed. Review the attached files and try again."
        } catch { mediaRemovalError = "Couldn’t remove the file. \(error.localizedDescription)" }
    }
    @discardableResult
    private func loadVisuals() async -> Bool {
        guard let itemID else { return false }
        do {
            let loaded = try await model.api.visuals(itemID: itemID)
            pool = loaded
            preparation.observe(loaded.assets, now: Date())
            error = nil
            return true
        } catch { self.error = "Kria couldn’t load your visuals. \(error.localizedDescription)"; return false }
    }
    /// One poll of the pool followed by any automatic reanalyze that is due.
    /// Serialized so a foreground pass never acts on a snapshot older than a
    /// reanalyze the poll loop already has in flight.
    private func pollVisuals() async {
        guard !visualPollRunning else { return }
        visualPollRunning = true
        defer { visualPollRunning = false }
        guard await loadVisuals() else { return }
        await retryFailedVisualsAutomatically()
    }
    /// Transient analysis failures (`analysis_temporarily_unavailable`) retry on
    /// their own: at most three times per asset per sheet, 10/20/40 s apart.
    /// The server re-runs analysis on the object it already holds; nothing
    /// re-uploads, and non-retryable failures are never touched.
    private func retryFailedVisualsAutomatically() async {
        guard let itemID, !mutatingVisual, let assets = pool?.assets else { return }
        let due = autoRetry.observe(assets, now: Date())
        guard !due.isEmpty else { return }
        for assetID in due {
            let result = try? await model.api.retryVisual(itemID: itemID, assetID: assetID)
            autoRetry.recordAttempt(assetID: assetID, result: result, now: Date())
        }
        await loadVisuals()
    }
    private func remove(_ asset: CreationVisual) async {
        guard let itemID, !mutatingVisual else { return }
        mutatingVisual = true
        defer { mutatingVisual = false }
        do { try await model.api.removeVisual(itemID: itemID, assetID: asset.id); await loadVisuals() }
        catch { self.error = "Couldn’t remove this visual. \(error.localizedDescription)" }
    }
    private func retry(_ asset: CreationVisual) async {
        guard let itemID, !mutatingVisual, autoRetry.beginManualRetry(asset.id) else { return }
        mutatingVisual = true
        defer { mutatingVisual = false }
        do {
            let result = try await model.api.retryVisual(itemID: itemID, assetID: asset.id)
            autoRetry.recordAttempt(assetID: asset.id, result: result, now: Date())
            await loadVisuals()
        } catch {
            autoRetry.recordAttempt(assetID: asset.id, result: nil, now: Date())
            self.error = "Couldn’t retry this visual. \(error.localizedDescription)"
        }
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
