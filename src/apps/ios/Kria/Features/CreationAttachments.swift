import SwiftUI
import AVFoundation
import UIKit
import PhotosUI
import KriaMediaEngine

/// What the Add media sheet starts doing the moment it opens, so a Record or Upload tile elsewhere lands straight in
/// the camera or the right picker instead of on a second screen of buttons.
enum AttachmentLaunch: Equatable {
    case record
    case recordWithScript
    case voiceover
    /// Open a later step directly (song, overlays).
    case step(AttachmentStep)
    /// The "Upload from" list over the sheet.
    case chooseSource
    case source(FootageSourceKind)
    /// A generated image a suggestion card made: the overlays step, with the review of that image already open.
    case generatedOverlays
}

/// Which surface asked Add media to open a camera or a picker, and so where that presentation lands when it ends with
/// nothing taken or chosen.
enum AttachmentOrigin: Equatable {
    /// The Add media step itself, the footage stage's tiles, or a tray row whose screen is the destination (script,
    /// voiceover, song, overlays): the creator ends up on the Add media step they came from.
    case addMedia
    /// The composer's + tray, Record or Upload: the creator ends up back in the chat.
    case composerTray
}

struct AttachmentSheet: View {
    let projectID: UUID
    let maximumClipCount: Int
    let attachedClipCount: Int
    let format: CreationFormat?
    let thread: CreationThread?
    let capabilities: CreationCapabilities?
    /// False until the account's capabilities have loaded; uploads wait for them.
    var capabilitiesLoaded = true
    /// KRI-815: opened from a script card: the sheet starts on the voiceover step (any format) and shows this
    /// script to read while recording. Nil = the sheet as before.
    var voiceoverScript: VoiceoverScript? = nil
    var launch: AttachmentLaunch? = nil
    /// Who asked for `launch`. Consumed by the first camera or picker it opens.
    var origin: AttachmentOrigin = .addMedia
    /// Re-record from the ready video: the sheet is only a host. It opens the teleprompter at once, swaps this clip for
    /// the new take, and closes with it. Nil = the sheet as before.
    var rerecordTarget: RerecordTarget? = nil
    let refresh: () async -> Void
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var role: CreationMediaRole = .clip
    @State private var step: AttachmentStep = .footage
    @State private var lastGeneratedDraft: GeneratedMediaDraft?
    @State private var generatedDraft: GeneratedMediaDraft?
    @State private var overlaySet = GeneratedOverlaySet()
    @State private var showsOverlaySet = false
    @State private var loadingNarration = false
    @State private var pool: CreationVisuals?
    @State private var error: String?
    @State private var mutatingVisual = false
    @State private var removingMedia = false
    @State private var mediaRemovalError: String?
    @State private var pendingRemoval: CreationActionIdentity?
    @State private var uploadingRecording = false
    @State private var voiceoverUploadError: String?
    @State private var confirmsDiscardingTake = false
    @State private var confirmsCancellingRecording = false
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
    /// KRI-374: local playback of a picked song before it is added. Never uploads by itself.
    @StateObject private var songPreview = CreationVoiceRecorder()
    @State private var uploadingSong = false
    @State private var songUploadError: String?
    @State private var showsTeleprompter = false
    @State private var cameraStartsWithScript = false
    @State private var showsScriptEntry = false
    @State private var openCameraAfterScript = false
    @State private var pickerLaunch: FootageSourceKind?
    @State private var voiceoverPickerLaunch: FootageSourceKind?
    @State private var launched = false
    /// `origin` until the presentation it launched ends; `.addMedia` after, so the sheet's own tiles keep their behavior.
    @State private var launchOrigin: AttachmentOrigin = .addMedia
    /// A take the camera handed to intake, so closing the camera afterwards is not a cancel.
    @State private var cameraKeptTake = false
    /// KRI-580: the "Where in the song did you film?" sheet, and whether the server said the feature is off.
    @State private var showsSongRange = false
    @State private var songRangeUnavailable = false
    @State private var pendingSongRangeEvent: SongRangeEvent?

    init(
        projectID: UUID,
        maximumClipCount: Int,
        attachedClipCount: Int,
        format: CreationFormat? = nil,
        thread: CreationThread?,
        capabilities: CreationCapabilities?,
        capabilitiesLoaded: Bool = true,
        voiceoverScript: VoiceoverScript? = nil,
        launch: AttachmentLaunch? = nil,
        origin: AttachmentOrigin = .addMedia,
        rerecordTarget: RerecordTarget? = nil,
        refresh: @escaping () async -> Void
    ) {
        self.launch = launch
        self.origin = origin
        _launchOrigin = State(initialValue: origin)
        self.rerecordTarget = rerecordTarget
        self.projectID = projectID
        self.maximumClipCount = maximumClipCount
        self.attachedClipCount = attachedClipCount
        self.format = format
        self.thread = thread
        self.capabilities = capabilities
        self.capabilitiesLoaded = capabilitiesLoaded
        self.refresh = refresh
        // A script is for a recording: never on a Visuals-only (slide) project.
        let script = format?.usesVisualPool == true ? nil : voiceoverScript
        self.voiceoverScript = script
        let songFirst = format == .lipsync && capabilities?.songUploadEnabled == true
        let initialRole: CreationMediaRole = script != nil ? .voiceover : (format?.usesVisualPool == true ? .visual : (songFirst ? .song : .clip))
        _role = State(initialValue: initialRole)
        _step = State(initialValue: script != nil ? .voiceover : (initialRole == .visual ? .overlays : (songFirst ? .song : .footage)))
    }

    /// Slides may only use the existing PlanItemAsset Visuals pool. Keeping
    /// this decision inside the sheet prevents a transient role-picker render
    /// from offering primary footage or voiceover before the picker appears.
    private var usesVisualPoolOnly: Bool { format?.usesVisualPool == true }
    private var narrated: Bool { (format ?? thread.map { CreationFormat(thread: $0) }) == .narrated }
    private var hasVoiceoverStep: Bool { narrated || voiceoverScript != nil || media.contains(where: \.isVoiceover) }
    /// "Add your song" appears only when the server advertises `media.song` (feature on for this account and
    /// protocol), and only for montage-style projects: a song replaces the soundtrack of cut footage.
    private var resolvedFormat: CreationFormat? { format ?? thread.flatMap { CreationFormat(thread: $0) } }
    private var hasSongStep: Bool {
        guard capabilities?.songUploadEnabled == true, !usesVisualPoolOnly else { return false }
        guard let resolved = resolvedFormat else { return thread == nil }
        return resolved.runsAsMontage
    }
    /// A lip-sync starts from the song, so its song step leads the sheet.
    private var songLeads: Bool { hasSongStep && resolvedFormat == .lipsync }
    private var hasOverlaysStep: Bool { usesVisualPoolOnly || (capabilities?.visualsEnabled == true && itemID != nil) }
    private var steps: [AttachmentStep] {
        var result: [AttachmentStep] = usesVisualPoolOnly ? [.overlays] : (songLeads ? [.song, .footage] : [.footage])
        if hasVoiceoverStep { result.append(.voiceover) }
        if hasSongStep && !songLeads { result.append(.song) }
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
        case .song: capabilities?.songLimit?.max ?? 1
        case .visual: pool?.maxAssets ?? 0
        }
    }
    private func existing(for mediaRole: CreationMediaRole) -> Int {
        switch mediaRole {
        case .clip: return attachedClipCount
        case .voiceover: return media.filter(\.isVoiceover).count
        case .song: return CreationAttachedMedia.song(thread?.state ?? [:]) == nil ? 0 : 1
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
        case .voiceover: Set(media.filter(\.isVoiceover).map(\.id))
        case .song: Set(CreationAttachedMedia.song(thread?.state ?? [:]).map { [$0.id] } ?? [])
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
    /// A song picked, uploading, or already attached counts against the limit of one.
    private var canAddSong: Bool {
        let queued = pendingRecords.filter { $0.projectID == projectID && $0.role == .song }
        let queuedIDs = Set(queued.map(\.id))
        let preparing = inFlight.filter { $0.value.projectID == projectID && $0.value.role == .song && !queuedIDs.contains($0.key) }.count
        return !songPreview.isImporting && existing(for: .song) + queued.count + preparing < maximum(for: .song)
    }
    private var hasSongOrPendingSong: Bool {
        existing(for: .song) > 0
            || pendingRecords.contains { $0.projectID == projectID && $0.role == .song }
            || inFlight.values.contains { $0.projectID == projectID && $0.role == .song }
    }
    private func uploadDestination(for mediaRole: CreationMediaRole) -> ProjectUploadDestination {
        ProjectUploadDestination.resolve(
            capabilities: capabilities?.phoneRendering,
            creationMode: capabilities?.creationMode,
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
                if let lastGeneratedDraft, lastGeneratedDraft.recovery != nil,
                   capabilitiesLoaded, generatedMediaEnabled(lastGeneratedDraft.capability) {
                    KriaNoticeCard(tone: .info, title: "Your preview is still here",
                                   action: KriaNoticeAction(title: "Open") { generatedDraft = lastGeneratedDraft }, style: .banner)
                        .padding(.horizontal, 24).accessibilityIdentifier("generated-media-resume")
                }
                KriaStepRail(steps: steps.map { $0 == .overlays ? (usesVisualPoolOnly ? SlideMediaCopy.poolTitle : $0.title) : $0.title },
                             current: stepIndex)
                    .padding(.horizontal, 24).padding(.bottom, 8)
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
                    if newStep != .song { songPreview.pausePlayback() }
                }
                attachmentFooter
            }
            .background(KriaColor.paper)
            .sheet(item: $generatedDraft) { draft in
                if let itemID {
                    GeneratedMediaSheet(itemID: itemID, draft: draft) {
                        await loadVisuals()
                        await refresh()
                        lastGeneratedDraft = nil
                    }
                }
            }
            .sheet(isPresented: $showsOverlaySet) {
                if let itemID {
                    GeneratedOverlaysSheet(itemID: itemID, overlays: overlaySet, ideaTitle: thread?.title ?? "") {
                        await loadVisuals()
                        await refresh()
                    }
                }
            }
            // The camera is a full-screen cover, which also makes this content disappear. Only a real dismissal
            // stops the overlay requests; coming back picks up any that a stop did cancel.
            .onDisappear { if !showsTeleprompter { overlaySet.stopWork() } }
            .onAppear { overlaySet.resumeWork() }
            .sheet(isPresented: $showsScriptEntry, onDismiss: {
                // The camera waits for the script sheet to finish closing.
                if openCameraAfterScript { openCameraAfterScript = false; showsTeleprompter = true }
            }) {
                if let accountID = auth.localStorageIdentity {
                    TeleprompterScriptEntrySheet(accountID: accountID, projectID: projectID) { useScript in
                        cameraStartsWithScript = useScript
                        openCameraAfterScript = true
                        showsScriptEntry = false
                    }
                }
            }
            .fullScreenCover(isPresented: $showsTeleprompter, onDismiss: {
                Task { await refresh() }
                let backToChat = launchOrigin == .composerTray && !cameraKeptTake
                launchOrigin = .addMedia
                cameraKeptTake = false
                if rerecordTarget != nil || backToChat { dismiss() }
            }) {
                if let accountID = auth.localStorageIdentity {
                    TeleprompterScreen(accountID: accountID, projectID: projectID,
                        admissionMessage: teleprompterAdmission,
                        replacementNotice: replacementNotice,
                        intake: { take, url in noteKept(await useCameraTake(take, url)) },
                        intakeState: { take in await cameraTakeState(take).map(noteKept) },
                        speakerBackgroundEnabled: speakerBackgroundAvailable,
                        backgroundIntake: useCameraBackground,
                        backgroundStatement: stateSpeakerBackground,
                        startsWithScript: cameraStartsWithScript,
                        reportDiagnostic: { report in Task { try? await model.api.reportRecorderDiagnostic(report) } })
                        .environment(\.dynamicTypeSize, dynamicTypeSize)
                }
            }
            .opacity(rerecordTarget == nil ? 1 : 0)
            .accessibilityHidden(rerecordTarget != nil)
            .onAppear { if rerecordTarget != nil { showsTeleprompter = true } }
            .interactiveDismissDisabled(recorder.isRecording || recorder.hasRecording)
            .confirmationDialog("Stop and discard this recording?", isPresented: $confirmsCancellingRecording, titleVisibility: .visible) {
                Button("Discard recording", role: .destructive) { recorder.discard() }
                Button("Keep recording", role: .cancel) { }
            } message: { Text("What you have recorded so far will be deleted.") }
            .confirmationDialog("Discard this voiceover?", isPresented: $confirmsDiscardingTake, titleVisibility: .visible) {
                Button("Discard recording", role: .destructive) { recorder.discard(); dismiss() }
                Button("Keep recording", role: .cancel) { }
            } message: { Text("Your recording has not been added yet.") }
            .sheet(isPresented: $showsSongRange) {
                let song = CreationAttachedMedia.song(thread?.state ?? [:])
                SongRangeSheet(duration: song?.durationS, existing: song?.filmingRange, submit: submitSongRange)
            }
            .task {
                guard !launched else { return }
                launched = true
                switch launch {
                case .record: openCamera(withScript: false)
                case .recordWithScript: showsScriptEntry = true
                case .voiceover: step = .voiceover; role = .voiceover
                case .step(let target): if steps.contains(target) { step = target; role = target.role }
                case .chooseSource: pickerLaunch = .sources
                case .source(let kind): pickerLaunch = kind
                case .generatedOverlays:
                    if steps.contains(.overlays) { step = .overlays; role = .visual }
                    openOverlaySet()
                case nil: break
                }
            }
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
            .task(id: itemID) { configureOverlaySet() }
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
            .onReceive(model.uploads.$attachedThreads) { threads in
                if threads[projectID] != nil, pollsVisuals { Task { await loadVisuals() } }
            }
            // Un-choosing a visual in the picker removes it server-side; refresh the pool now rather
            // than on the next poll, or the picker would keep showing it as chosen for a few seconds.
            .onReceive(model.uploads.$photoSelections) { _ in if role == .visual { Task { await loadVisuals() } } }
            .onChange(of: scenePhase) { _, phase in
                if phase != .active { recorder.stopForInterruption(); songPreview.pausePlayback() }
                if phase == .background { recorder.cancelPendingWorkAndStop(); songPreview.cancelPendingWorkAndStop() }
            }
            .onDisappear {
                recorder.cancelPendingWorkAndStop()
                // A picked song is only a local copy until "Use this song"; closing the sheet drops it (the
                // creator can pick it again), so no temp file outlives the sheet.
                songPreview.discard()
                foregroundPoll?.cancel()
                foregroundPoll = nil
            }
        }
    }
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var attachmentNavigation: some View {
        HStack {
            Button { goBack() } label: { Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)) }
                .buttonStyle(.kriaIcon).accessibilityLabel("Back")
                .opacity(stepIndex == 0 ? 0 : 1).disabled(stepIndex == 0 || recorder.isRecording)
            Spacer()
            Button { requestDismiss() } label: { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                .buttonStyle(.kriaIcon).disabled(recorder.isRecording)
                .accessibilityIdentifier("attachment-close").accessibilityLabel("Close")
        }
        .padding(.horizontal, 12).frame(height: 56)
    }

    @ViewBuilder private func stepContent(_ aStep: AttachmentStep) -> some View {
        switch aStep {
        case .footage: footageStep
        case .voiceover: voiceoverStep
        case .song: songStep
        case .overlays: overlaysStep
        }
    }

    private var footageStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: "Add footage", subtitle: FootageStageCopy.attachmentSubtitle(talking: isTalkingToCamera, maximumClipCount: maximumClipCount))
            FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximumClipCount, attachedClipCount: existing(for: .clip), attachedMediaIDs: attachedMediaIDs(for: .clip), role: .clip, itemID: itemID, limit: capabilities?.media?["clips"], destination: uploadDestination(for: .clip), onPickerFilled: nil, onSelectionCompleted: advanceAfterFootageSelection, showsHeading: false, libraryEnabled: capabilities?.mediaLibrary == true,
                              controls: .tiles(record: auth.localStorageIdentity == nil ? nil : { openCamera(withScript: false) }),
                              launchSource: $pickerLaunch, onLaunchedSourceCancelled: launchedPickerCancelled)
                .id(AttachmentStep.footage)
            if isTalkingToCamera, auth.localStorageIdentity != nil {
                Button { showsScriptEntry = true } label: {
                    HStack(spacing: 12) {
                        Image(systemName: "text.alignleft").font(.system(size: 18)).foregroundStyle(KriaColor.ink)
                            .frame(width: 40, height: 40).background(KriaColor.paper, in: Circle())
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Script").kriaType(.button).foregroundStyle(KriaColor.ink)
                            Text("Optional · shows while you record").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.system(size: 14, weight: .semibold)).foregroundStyle(KriaColor.mutedInk)
                    }
                    .padding(.horizontal, 12).frame(minHeight: 64)
                    .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                }
                .buttonStyle(.plain).accessibilityIdentifier("teleprompter-open")
            }
            attachedList(role: .clip)
        }
    }

    private func openCamera(withScript: Bool) {
        guard auth.localStorageIdentity != nil else { return }
        cameraStartsWithScript = withScript
        // The cover reads `cameraStartsWithScript` when it is built, so let that change land first.
        Task { @MainActor in showsTeleprompter = true }
    }

    /// The clip a take recorded here replaces: the one the creator chose from the ready video, or, when the edit is
    /// full and the server offers replace, the only clip it has. Nil = the take is added as a clip.
    private var replaceTarget: RerecordTarget? {
        if let rerecordTarget { return rerecordTarget }
        guard isTalkingToCamera, case let .one(target) = RerecordOffer.resolve(thread: thread),
              existing(for: .clip) + model.uploads.reservedCount(projectID: projectID, role: .clip) >= maximumClipCount else { return nil }
        return target
    }

    /// Said before the creator commits. The take, script, background and other clips are kept; what is not carried
    /// over is named, because it is the one thing a re-record loses.
    private var replacementNotice: String? {
        guard let target = replaceTarget else { return nil }
        let video = thread?.job != nil
            ? " Kria makes the video again from the new take. Text and caption changes you made to the current video won’t carry over. Your current video stays until the new one is ready."
            : ""
        return "This replaces \(target.displayName). Your script, background and other clips stay.\(video)"
    }

    private func teleprompterAdmission(_ take: TeleprompterTakeManifest?) -> String? {
        guard uploadDestination(for: .clip) == .phone else {
            return uploadDestination(for: .clip).message ?? "This edit can’t accept an on-device recording yet. Your take is saved on this iPhone."
        }
        let ownID = take?.intakeID
        if let ownID, model.uploads.containsIntake(recordID: ownID) { return nil }
        if replaceTarget != nil {
            return replacementUnderway ? "Your new take is still being swapped in. Wait for it to finish before recording another." : nil
        }
        // KRI-719: a Talking edit can hold several clips, so a take is only
        // refused once the edit is full, not whenever any clip is attached.
        let occupied = existing(for: .clip) + model.uploads.reservedCount(projectID: projectID, role: .clip)
        guard occupied < maximumClipCount else {
            return "This video already has \(FootageStageCopy.count(maximumClipCount, "clip")). Your take is saved: remove a clip in Add media, then reopen it."
        }
        return nil
    }

    private var replacementUnderway: Bool {
        BackgroundUploadCoordinator.replacementUnderway(projectID: projectID, inFlight: inFlight, records: pendingRecords)
    }

    private var isTalkingToCamera: Bool {
        (format ?? thread.map { CreationFormat(thread: $0) }) == .talkingToCamera
    }

    private func noteKept(_ result: TeleprompterIntakeResult) -> TeleprompterIntakeResult {
        if result == .queued || result == .attached { cameraKeptTake = true }
        return result
    }

    private func useCameraTake(_ take: TeleprompterTakeManifest, _ url: URL) async -> TeleprompterIntakeResult {
        // Reconcile first, including a receipt-write failure after attachment.
        if take.intakeAttempted == true, let existing = await cameraTakeState(take) {
            switch existing {
            case .attached, .queued: return existing
            case .failed: break // A confirmed failure may retry its stable ID.
            }
        }
        if model.uploads.containsIntake(recordID: take.intakeID) {
            await model.uploads.retryUpload(recordID: take.intakeID)
            if let failure = model.uploads.failures.first(where: { $0.id == take.intakeID }) { return .failed(failure.message) }
            return .queued
        }
        if let message = teleprompterAdmission(take) { return .failed(message) }
        model.uploads.dismissFailure(id: take.intakeID)
        let accepted = await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files,
            consentGiven: true, purpose: .analysisProxy, role: .clip, itemID: itemID,
            limit: capabilities?.media?["clips"], recordID: take.intakeID, replacesMediaID: replaceTarget?.mediaID)
        return accepted ? .queued : .failed(model.uploads.failures.first(where: { $0.id == take.intakeID })?.message ?? "Couldn’t add this take. Your recording is saved; try again.")
    }

    /// The live background picker needs the server to offer it. UI tests force it on (the simulator has no server flag).
    private var speakerBackgroundAvailable: Bool {
        if capabilities?.speakerBackgroundEnabled == true { return true }
        let process = ProcessInfo.processInfo
        return process.arguments.contains("-ui-testing-chat") && process.arguments.contains("-ui-testing-speaker-background")
    }

    /// Uploads the background the creator filmed in front of, as a visual, after the take itself was handed to intake.
    /// The recording stays raw; the server decides what the picture is for. A failure never un-attaches the take.
    private func useCameraBackground(_ take: TeleprompterTakeManifest, _ reference: SpeakerBackgroundReference, _ url: URL) async -> BackgroundIntakeResult {
        guard let itemID else { return .failed("This edit isn’t ready for a background yet.") }
        let selection = CameraBackgroundSelection(projectID: projectID, itemID: itemID, recordID: reference.id,
                                                  kind: reference.kind, fileName: reference.fileName)
        if model.uploads.containsIntake(recordID: reference.id) {
            if let failure = model.uploads.failures.first(where: { $0.id == reference.id }) { return .failed(failure.message) }
            return .queued(selection)
        }
        model.uploads.dismissFailure(id: reference.id)
        let accepted = await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true,
            purpose: uploadDestination(for: .visual) == .phone ? .analysisProxy : .cloudRenderSource,
            role: .visual, itemID: itemID, limit: capabilities?.media?["visuals"], recordID: reference.id)
        guard accepted else {
            return .failed(model.uploads.failures.first(where: { $0.id == reference.id })?.message ?? "Add it again from your media.")
        }
        return .queued(selection)
    }

    /// Names (or clears) the background on the server with `PUT creation-threads/{id}/speaker-background`, then shows the
    /// updated thread. Called at enqueue time: the reserved upload row may still be preparing, which the server accepts.
    private func stateSpeakerBackground(_ intent: SpeakerBackgroundIntent) async -> SpeakerBackgroundOutcome {
        switch await SpeakerBackgroundSubmission.submit(client: model.api, threadID: projectID, intent: intent) {
        case .saved(let updated):
            model.uploads.publishUpdatedThread(updated, projectID: projectID)
            return .saved
        case .unavailable: return .unavailable
        case .failed: return .failed
        }
    }

    private func cameraTakeState(_ take: TeleprompterTakeManifest) async -> TeleprompterIntakeResult? {
        if let failure = model.uploads.failures.first(where: { $0.id == take.intakeID }) { return .failed(failure.message) }
        if model.uploads.containsIntake(recordID: take.intakeID) { return .queued }
        guard take.status == .attaching || take.intakeAttempted == true else { return nil }
        do {
            let latest = try await model.api.project(threadID: projectID)
            let attached = CreationAttachedMedia.parse(latest.state ?? [:]).contains { $0.filename == take.fileName }
            if attached { await refresh(); return .attached }
            return .failed("This take hasn’t been added yet. Use take to retry; your recording is saved.")
        } catch {
            // Unknown server status is never permission to reserve another
            // logical take. Keep checking the stable identity on reconnect.
            return .queued
        }
    }

    private var voiceoverStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: recorder.hasRecording ? (recorder.isImported ? "Review voiceover" : "Review recording") : "Add voiceover",
                              subtitle: recorder.hasRecording ? "Listen before adding this recording to your video."
                                  : recorder.isRecording ? (voiceoverScript == nil ? "Say your voiceover. I’ll keep recording until you stop." : "Read your script out loud. I’ll keep recording until you stop.")
                                  : "Record it here, upload one you’ve made, or use an AI voice.")
            if let voiceoverScript, !recorder.hasRecording {
                VoiceoverScriptReadAlong(script: voiceoverScript)
                    .id("\(voiceoverScript.scriptID)-\(voiceoverScript.revision)")
            }
            if recorder.isRecording {
                RecordingVoiceoverView(recorder: recorder) { recorder.stop() }
            } else if recorder.hasRecording {
                VoiceoverReviewView(recorder: recorder)
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Voiceover").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        Text(DurationFormatter.clock(recorder.duration)).kriaType(.heading)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 6) {
                        Text("Total footage").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        Text(footageDurationSummary.value).kriaType(.heading)
                            .accessibilityIdentifier("footage-duration")
                    }
                }
            } else if recorder.isImporting {
                HStack(spacing: 8) { KriaSpinner(size: 16); Text("Opening audio…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
            } else {
                let canUploadAudio = uploadDestination(for: .voiceover).canUpload
                let aiVoice: KriaSourceOption? = {
                    guard capabilitiesLoaded, capabilities?.generatedNarration == true, canRecord, itemID != nil, let voiceoverScript else { return nil }
                    return KriaSourceOption(id: "ai-voice", symbol: "waveform", title: "AI voice", caption: "Read by AI",
                                            identifier: "generate-narration", isEnabled: !loadingNarration) { generateNarration(for: voiceoverScript) }
                }()
                KriaSourceRow(options: [
                    .record(symbol: "mic.fill", caption: "Use your mic", identifier: "voiceover-record", isEnabled: canRecord && canUploadAudio) { Task { await recorder.start() } },
                    .upload(caption: "Use an audio file", identifier: "voiceover-upload", isEnabled: canRecord && canUploadAudio) { voiceoverPickerLaunch = capabilities?.mediaLibrary == true ? .sources : .files },
                    aiVoice
                ].compactMap { $0 })
                if footageDurationSummary.count > 0 {
                    HStack(spacing: 8) {
                        KriaCheckDisc(size: 20)
                        Text("\(footageDurationSummary.label) · \(footageDurationSummary.value)").kriaType(.label).foregroundStyle(KriaColor.ink)
                    }
                    .accessibilityElement(children: .combine).accessibilityIdentifier("footage-duration")
                }
            }
            // The audio importer behind the Upload tile. It draws no controls, but it does show what went wrong
            // (an unsupported or oversized file, a failed attach) right here.
            FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum(for: .voiceover), attachedClipCount: existing(for: .voiceover), attachedMediaIDs: attachedMediaIDs(for: .voiceover), role: .voiceover, itemID: itemID, limit: capabilities?.media?["voiceover"], destination: uploadDestination(for: .voiceover), onAudioFileSelected: recorder.importFile, showsHeading: false, libraryEnabled: capabilities?.mediaLibrary == true, controls: .hidden, launchSource: $voiceoverPickerLaunch)
            if let recorderError = recorder.error { Text(recorderError).kriaType(.label).foregroundStyle(KriaColor.failureText) }
            if (recorder.hasRecording || recorder.isRecording), let warning = voiceoverWarning {
                VoiceoverDurationWarning(message: warning, addFootage: { step = .footage })
                    .accessibilityIdentifier("voiceover-duration-warning")
            }
            if recorder.hasRecording {
                VoiceoverReviewActions(recorder: recorder, onUse: { Task { await useRecording() } }, onRecordAgain: discardVoiceover, onChooseAnother: discardVoiceover, canUse: canRecord && uploadDestination(for: .voiceover).canUpload, isUsing: uploadingRecording)
            }
            attachedList(role: .voiceover)
            if let voiceoverUploadError { Text(voiceoverUploadError).kriaType(.label).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("voiceover-upload-error") }
        }
    }

    private func generateNarration(for voiceoverScript: VoiceoverScript) {
        guard let itemID, !loadingNarration else { return }
        loadingNarration = true
        Task {
            defer { loadingNarration = false }
            do {
                let approved = try await model.api.generatedNarrationScript(itemID: itemID)
                guard approved.matches(voiceoverScript) else {
                    voiceoverUploadError = "The script changed. Open its latest card before generating narration."
                    return
                }
                openGeneratedMedia(GeneratedMediaDraft(capability: .narration, script: approved))
            } catch { voiceoverUploadError = "Couldn’t load the current script. Try again when connected." }
        }
    }

    private var songStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: songPreview.hasRecording ? "Review your song" : "Add your song",
                              subtitle: songPreview.hasRecording ? "Listen before adding this song to your video."
                                  : resolvedFormat == .lipsync ? "Add the song you’re lip-syncing to."
                                  : "Cut your clips to a song, or lip-sync to it. Tell Kria which in your message.")
            if songPreview.hasRecording {
                SongReviewView(player: songPreview)
                SongReviewActions(onUse: { Task { await useSong() } }, onChooseAnother: { songPreview.discard(); songUploadError = nil },
                                  canUse: canAddSong, isUsing: uploadingSong)
            }
            Text("Only use songs you have the rights to.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier("song-rights-note")
            if let songError = songPreview.error { Text(songError).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
            if let songUploadError { Text(songUploadError).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("song-upload-error") }
            attachedList(role: .song)
        }
    }

    private func generatedMediaEnabled(_ capability: GeneratedMediaCapability) -> Bool {
        switch capability {
        case .image: capabilities?.generatedImages == true
        case .imageEdit: capabilities?.generatedImageEditing == true
        case .narration: capabilities?.generatedNarration == true
        }
    }

    private func openGeneratedMedia(_ draft: GeneratedMediaDraft) {
        guard let accountID = auth.localStorageIdentity, let itemID else { return }
        draft.configureRecovery(accountID: accountID, itemID: itemID)
        if let lastGeneratedDraft, lastGeneratedDraft.recovery != nil,
           lastGeneratedDraft.capability == draft.capability, lastGeneratedDraft.source?.id == draft.source?.id,
           generatedMediaEnabled(lastGeneratedDraft.capability) {
            lastGeneratedDraft.source = draft.source
            lastGeneratedDraft.script = draft.script
            generatedDraft = lastGeneratedDraft
        } else {
            lastGeneratedDraft = draft
            generatedDraft = draft
        }
    }

    private func configureOverlaySet() {
        guard let accountID = auth.localStorageIdentity, let itemID else { return }
        overlaySet.configure(api: model.api, itemID: itemID, accountID: accountID)
    }

    private func openOverlaySet() {
        configureOverlaySet()
        overlaySet.resumeScreen()
        showsOverlaySet = true
    }

    private var overlaysStep: some View {
        VStack(alignment: .leading, spacing: 20) {
            AttachmentHeading(title: usesVisualPoolOnly ? SlideMediaCopy.addHeading : "Add overlays", subtitle: usesVisualPoolOnly ? SlideMediaCopy.subtitle : "Optional photos and supporting clips can add context to your video.")
            if uploadDestination(for: .visual).canUpload, !usesVisualPoolOnly { Text(visualsCaption).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc) }
            if capabilitiesLoaded, capabilities?.generatedImages == true, itemID != nil {
                OverlaySetNotice(overlays: overlaySet) { openOverlaySet() }
            }
            if pool == nil, error == nil { HStack(spacing: 8) { KriaSpinner(size: 16); Text(usesVisualPoolOnly ? SlideMediaCopy.loading : "Loading overlays…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) } }
            if pool != nil {
                FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum(for: .visual), attachedClipCount: existing(for: .visual), attachedMediaIDs: attachedMediaIDs(for: .visual), role: .visual, itemID: itemID, limit: capabilities?.media?["visuals"], destination: uploadDestination(for: .visual), onPickerFilled: nil, onSelectionCompleted: nil, showsHeading: false, titleOverride: usesVisualPoolOnly ? SlideMediaCopy.poolTitle : nil, libraryEnabled: capabilities?.mediaLibrary == true,
                                  controls: .tiles(record: nil, third: capabilitiesLoaded && capabilities?.generatedImages == true && itemID != nil
                                    ? KriaSourceOption(id: "generate", symbol: "sparkles", title: "Generate", caption: "Make with AI", identifier: "generate-image") {
                                        openOverlaySet()
                                    } : nil),
                                  launchSource: $pickerLaunch, onLaunchedSourceCancelled: launchedPickerCancelled)
                    .id(AttachmentStep.overlays)
                if let summary = VisualPreparationSummary(assets: pool?.assets ?? [], uploading: uploadingVisualCount, online: network.isOnline, slowIDs: preparation.slowIDs, surface: .addMediaSheet, nounOverride: usesVisualPoolOnly ? SlideMediaCopy.preparingNoun : nil) {
                    VisualPreparationSummaryView(summary: summary)
                }
                visualList
            }
            if let error {
                Text(error).kriaType(.label).foregroundStyle(KriaColor.failureText)
                Button(usesVisualPoolOnly ? SlideMediaCopy.retryLoading : "Retry loading overlays") { Task { await loadVisuals() } }
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
            }
        }
    }

    @ViewBuilder private var attachmentFooter: some View {
        if recorder.isRecording {
            Button("Cancel recording") { confirmsCancellingRecording = true }
                .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium)).accessibilityIdentifier("voiceover-cancel")
                .padding(.horizontal, 24).padding(.vertical, 12)
        }
        else if step == .voiceover {
            if !recorder.hasRecording, !recorder.isImporting {
                VStack(spacing: 8) {
                    if existing(for: .voiceover) > 0 || pendingRecords.contains(where: { $0.projectID == projectID && $0.role == .voiceover }) || inFlight.values.contains(where: { $0.projectID == projectID && $0.role == .voiceover }) {
                        Button("Continue with voiceover") { advance() }
                            .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true)).accessibilityIdentifier("voiceover-continue")
                    } else {
                        Button("Use footage audio instead") { advance() }
                            .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium)).accessibilityIdentifier("voiceover-skip")
                    }
                }.padding(.horizontal, 24).padding(.vertical, 12)
            }
        }
        else if step == .song {
            if !songPreview.hasRecording {
                VStack(spacing: 8) {
                    if songPreview.isImporting {
                        ProgressView("Opening song…")
                    } else {
                        FootagePickerView(projectID: projectID, uploads: model.uploads, maximumClipCount: maximum(for: .song), attachedClipCount: existing(for: .song), attachedMediaIDs: attachedMediaIDs(for: .song), role: .song, itemID: itemID, limit: capabilities?.songLimit, destination: uploadDestination(for: .song), onAudioFileSelected: songPreview.importFile, showsHeading: false, libraryEnabled: capabilities?.mediaLibrary == true)
                    }
                    Button(hasSongOrPendingSong ? "Continue" : "Skip, no song") { advance() }
                        .buttonStyle(KriaButtonStyle(role: hasSongOrPendingSong ? .primary : .tertiary, size: hasSongOrPendingSong ? .large : .medium, fullWidth: hasSongOrPendingSong))
                        .accessibilityIdentifier(hasSongOrPendingSong ? "song-continue" : "song-skip")
                        .disabled(songPreview.isImporting)
                }.padding(.horizontal, 24).padding(.vertical, 12)
            }
        }
        else {
            VStack(spacing: 8) {
                Button(stepIndex == steps.count - 1 ? "Done" : "Next") { advance() }
                    .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                    .accessibilityIdentifier(stepIndex == steps.count - 1 ? "attachment-done" : "attachment-next")
            }.padding(.horizontal, 24).padding(.vertical, 12)
        }
    }

    private func attachedMedia(for role: CreationMediaRole) -> [CreationAttachedMedia] {
        switch role {
        case .voiceover: media.filter(\.isVoiceover)
        case .song: CreationAttachedMedia.song(thread?.state ?? [:]).map { [$0] } ?? []
        default: media.filter { $0.kind == "video" }
        }
    }
    /// The range row shows only when the server offers it, a song is attached and its length is known and long
    /// enough to hold a range. It never appears for footage or voiceover.
    private func offersSongRange(for attachment: CreationAttachedMedia) -> Bool {
        capabilities?.songFilmingRangeEnabled == true && !songRangeUnavailable && attachment.isSong
            && SongRangeMath.pickableSeconds(duration: attachment.durationS) != nil
    }
    private func submitSongRange(_ range: SongFilmingRange?) async -> SongRangeSubmission {
        let event = SongRangeEvent.reusing(pendingSongRangeEvent, range: range)
        pendingSongRangeEvent = event
        let result = await SongRangeSubmission.submit(client: model.api, threadID: projectID, range: range, clientEventID: event.id)
        switch result {
        case .saved(let updated):
            pendingSongRangeEvent = nil
            model.uploads.publishUpdatedThread(updated, projectID: projectID)
        case .unavailable:
            pendingSongRangeEvent = nil
            songRangeUnavailable = true
        case .failed: break
        }
        return result
    }
    private func attachedList(role: CreationMediaRole) -> some View {
        let locked = removingMedia || !pendingRecords.filter { $0.projectID == projectID }.isEmpty || replacementUnderway
        return VStack(spacing: 8) {
            ForEach(attachedMedia(for: role)) { attachment in
                VStack(spacing: 8) {
                    KriaMediaRow(name: attachment.displayName,
                                 status: .ready(role == .voiceover ? "Voiceover · Ready" : role == .song ? "Song · Ready"
                                    : "Video · Ready"),
                                 trailing: .remove(label: "Remove \(attachment.displayName)") { Task { await removeAttached(attachment) } },
                                 onFill: !offersSongRange(for: attachment)) {
                        CreationAttachmentThumbnail(media: attachment)
                    }
                    .disabled(locked)
                    if offersSongRange(for: attachment) {
                        SongRangeRow(range: attachment.filmingRange, isEnabled: !locked) { showsSongRange = true }
                    }
                }
                .padding(offersSongRange(for: attachment) ? 4 : 0)
                .background(offersSongRange(for: attachment) ? KriaColor.fill : Color.clear, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            }
            if let mediaRemovalError {
                Text(mediaRemovalError).kriaType(.label).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("attachment-removal-error")
            }
        }
    }

    private var visualList: some View {
        VStack(spacing: 12) {
            ForEach(pool?.assets ?? []) { asset in
                visualRow(asset)
                    .padding(4).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            }
        }
    }

    /// One attached overlay or photo (Paper S1.01): a `KriaMediaRow` with its status, one trailing control and,
    /// when it failed, a visible Retry. Edit image lives in the menu; a row with nothing to edit shows a trash disc.
    private func visualRow(_ asset: CreationVisual) -> some View {
        let name = asset.sourceFilename ?? (usesVisualPoolOnly ? (asset.kind == "video" ? "Video" : "Photo") : "Visual")
        let kindLabel = asset.kind == "video" ? "Video" : "Photo"
        let status: KriaMediaRow<AnyView>.Status
        if asset.status == "failed" {
            status = .failed(asset.statusCaption(retryingAutomatically: autoRetry.isRetryPending(asset.id)))
        } else if asset.preparationStage != nil {
            status = .preparing
        } else if asset.status == "ready" {
            status = .ready("\(kindLabel) · Ready")
        } else {
            status = .ready(asset.statusCaption(retryingAutomatically: false))
        }
        let canEditImage = capabilitiesLoaded && capabilities?.generatedImageEditing == true
            && asset.kind == "image" && asset.status == "ready" && asset.sourceGeneration != nil
        let removeAction = KriaMenuAction(title: "Remove", systemImage: "trash", isDestructive: true) { Task { await remove(asset) } }
        let trailing: KriaMediaRow<AnyView>.Trailing = canEditImage
            ? .menu(label: "Actions for \(name)", [
                KriaMenuAction(title: "Edit image", systemImage: "wand.and.stars") { openGeneratedMedia(GeneratedMediaDraft(capability: .imageEdit, source: asset)) },
                removeAction,
            ])
            : .remove(label: "Remove \(asset.sourceFilename ?? (usesVisualPoolOnly ? SlideMediaCopy.removeFallback : "visual"))") { Task { await remove(asset) } }
        let canRetry = asset.status == "failed" && asset.retryable != false && !mutatingVisual && autoRetry.canRetryManually(asset.id)
        return KriaMediaRow(name: name, status: status, trailing: trailing,
                            onRetry: canRetry ? { Task { await retry(asset) } } : nil) {
            AnyView(AsyncImage(url: asset.previewURL ?? (asset.kind == "image" ? asset.displayURL : nil)) { image in
                image.resizable().scaledToFill()
            } placeholder: {
                KriaThumbnailPlaceholder(systemImage: asset.kind == "image" ? "photo" : "video")
            })
        }
        .accessibilityIdentifier("visual-row-\(asset.id)")
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
    /// A pick the composer's + started ends back in the chat; from the sheet's own picker it moves on to the next step.
    private func advanceAfterFootageSelection() {
        guard step == .footage else { return }
        if launchOrigin == .composerTray { launchOrigin = .addMedia; requestDismiss() } else { advance() }
    }
    /// A picker the composer's + started was cancelled: back to the chat, not the Add media step behind it.
    private func launchedPickerCancelled() {
        guard launchOrigin == .composerTray else { return }
        launchOrigin = .addMedia
        requestDismiss()
    }
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
    private func useSong() async {
        guard !uploadingSong, let url = songPreview.recordingURL, canAddSong else { return }
        uploadingSong = true
        songUploadError = nil
        // The song always uploads in full (never an analysis proxy): the server needs the bytes.
        let accepted = await model.uploads.enqueue(fileURL: url, projectID: projectID, source: .files, consentGiven: true, purpose: .cloudRenderSource, role: .song, itemID: itemID, limit: capabilities?.songLimit)
        uploadingSong = false
        if accepted { songUploadError = nil; songPreview.discard(); advance() }
        else { songUploadError = "This song couldn’t be added. It’s still here, so try again." }
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
            if media.kind == "audio" { Image(systemName: media.isSong ? "music.note" : "waveform") }
            else if let image = UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path) { Image(uiImage: image).resizable().scaledToFill() }
            else { AsyncImage(url: media.previewURL) { image in image.resizable().scaledToFill() } placeholder: { Image(systemName: "video") } }
        }.frame(width: 52, height: 64).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
