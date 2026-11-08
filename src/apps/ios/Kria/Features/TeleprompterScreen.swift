import SwiftUI
import AVKit
import Combine
import PhotosUI

struct TeleprompterScreen: View {
    @StateObject private var flow: TeleprompterRecordingFlow
    let admissionMessage: (TeleprompterTakeManifest?) -> String?
    /// Set when using the take swaps an attached clip instead of adding one. Shown above Use take.
    var replacementNotice: String? = nil
    let intake: (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult
    let intakeState: (TeleprompterTakeManifest) async -> TeleprompterIntakeResult?
    /// Fires when the upload queue's records or failures change, which is what moves a queued take to added or failed.
    let intakeSignals: AnyPublisher<Void, Never>
    @State private var intakeWake = IntakeWake()
    /// The creator already wrote a script in the script sheet and chose to use it.
    let startsWithScript: Bool
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var confirmsDiscard = false
    @State private var allowsAutomaticScroll = false
    @State private var showsWindowSettings = false
    @State private var showsSpeedSheet = false
    @State private var captureControlsHeight: CGFloat = 160
    @State private var readingCanvas: CGRect = .zero

    private var manualScrolling: Bool {
        UIAccessibility.isVoiceOverRunning || (KriaMotion.isReduced(reduceMotion) && !allowsAutomaticScroll)
    }

    init(accountID: String, projectID: UUID, admissionMessage: @escaping (TeleprompterTakeManifest?) -> String?,
         replacementNotice: String? = nil,
         intake: @escaping (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult,
         intakeState: @escaping (TeleprompterTakeManifest) async -> TeleprompterIntakeResult?,
         intakeSignals: AnyPublisher<Void, Never> = Empty().eraseToAnyPublisher(),
         speakerBackgroundEnabled: Bool = false,
         backgroundIntake: BackgroundIntake? = nil,
         backgroundStatement: BackgroundStatement? = nil,
         startsWithScript: Bool = false,
         reportDiagnostic: @escaping (RecorderDiagnosticReport) -> Void = { _ in }) {
        _flow = StateObject(wrappedValue: {
            let flow = TeleprompterRecordingFlow(
                accountID: accountID, projectID: projectID, speakerBackgroundEnabled: speakerBackgroundEnabled,
                backgroundIntake: backgroundIntake, backgroundStatement: backgroundStatement,
                reportDiagnostic: reportDiagnostic)
            flow.useScript = startsWithScript
            return flow
        }())
        self.admissionMessage = admissionMessage; self.replacementNotice = replacementNotice; self.intake = intake; self.intakeState = intakeState
        self.intakeSignals = intakeSignals
        self.startsWithScript = startsWithScript
    }

    var body: some View {
        Group {
            switch flow.stage {
            case .review: takeReview
            default: cameraScreen
            }
        }
        .background(KriaColor.paper)
        .kriaPage()
        .interactiveDismissDisabled(flow.captureBusy || flow.usingTake)
        .task {
            await flow.load()
            await flow.prepareCameraIfNeeded()
            // A statement the last opening could not make is retried here.
            await flow.syncBackgroundStatement()
            while !Task.isCancelled {
                if !flow.usingTake, let take = flow.selectedTake, take.status == .attaching,
                   let state = await intakeState(take) {
                    flow.apply(state)
                    // Resumes a background upload a relaunch interrupted; a no-op once it was handed over.
                    if case .failed = state {} else { await flow.uploadBackgroundIfNeeded() }
                }
                // Woken by the upload queue the moment it changes; the timeout is the safety net for a signal that
                // never comes (the server was unreachable when the record left the queue, a relaunch).
                await intakeWake.wait(timeout: .seconds(3))
            }
        }
        .onReceive(intakeSignals) { intakeWake.signal() }
        .onReceive(flow.camera.$phase) { phase in flow.cameraChanged(phase) }
        .onChange(of: scenePhase) { _, phase in flow.scenePhaseChanged(phase) }
        .onAppear { flow.viewAppeared() }
        .onDisappear { flow.viewDisappeared() }
        .confirmationDialog("Discard this take?", isPresented: $confirmsDiscard, titleVisibility: .visible) {
            Button("Discard take", role: .destructive) { flow.discardSelected() }
            Button("Keep take", role: .cancel) { }
        } message: { Text("Your script stays saved.") }
        .sheet(isPresented: $showsWindowSettings) {
            TeleprompterWindowSettings(script: flow.script, canvas: readingCanvas, canChangeFont: !flow.captureBusy, persist: { flow.persistDraft() })
                .environment(\.dynamicTypeSize, dynamicTypeSize)
                .presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showsSpeedSheet) {
            TeleprompterSpeedSheet(script: flow.script, manual: manualScrolling, persist: { flow.persistDraft() })
                .environment(\.dynamicTypeSize, dynamicTypeSize)
                .presentationDetents(dynamicTypeSize.isAccessibilitySize ? [.large] : [.height(manualScrolling ? 300 : 260), .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $flow.showsScriptSheet) {
            TeleprompterScriptSheet(script: flow.script, save: { flow.persistDraft() },
                                    useScript: { flow.useScript = true; flow.persistDraft(); flow.showsScriptSheet = false },
                                    skip: { flow.useScript = false; flow.showsScriptSheet = false })
                .environment(\.dynamicTypeSize, dynamicTypeSize)
        }
    }

    // MARK: Camera (Paper S3.08 to S3.10)

    private var cameraScreen: some View {
        GeometryReader { geometry in
            let topInset: CGFloat = flow.useScript ? 116 : 60
            let bottomInset: CGFloat = max(120, captureControlsHeight + 16)
            let canvas = CGRect(x: 12, y: topInset, width: max(1, geometry.size.width - 24),
                                height: max(0, geometry.size.height - topInset - bottomInset))
            ZStack {
                Color.black.ignoresSafeArea()
                TeleprompterPreviewSurface(flow: flow, camera: flow.camera)
                    .frame(width: geometry.size.width, height: geometry.size.width * 16 / 9)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityHidden(true)
                if flow.useScript {
                    TeleprompterWindowOverlay(script: flow.script, canvas: canvas, persist: { flow.persistDraft() },
                                               openSettings: { showsWindowSettings = true })
                }
                VStack(spacing: 8) {
                    topRow
                    if !flow.captureBusy, showsControls, flow.useScript { textWindowRow }
                    Spacer(minLength: 8)
                    cameraBottom
                        .background(GeometryReader { proxy in
                            Color.clear.preference(key: TeleprompterCaptureControlsHeight.self, value: proxy.size.height)
                        })
                }.foregroundStyle(KriaColor.ink).padding(.top, 8).padding(.bottom, 16)
                if flow.stage == .countdown {
                    KriaCountdownDisc(value: flow.countdown) { flow.cancelCountdown() }
                }
                if let message = flow.camera.error, !flow.captureBusy, !flow.useScript {
                    // Centred when it fits; at larger text it scrolls below the close button instead of being cut off.
                    ViewThatFits(in: .vertical) {
                        accessCard(message)
                        ScrollView { accessCard(message) }.padding(.top, 60)
                    }
                }
            }
            .onAppear { readingCanvas = canvas }
            .onChange(of: geometry.size) { _, _ in readingCanvas = canvas }
            .onChange(of: captureControlsHeight) { _, _ in readingCanvas = canvas }
            .onPreferenceChange(TeleprompterCaptureControlsHeight.self) { captureControlsHeight = $0 }
        }
    }

    /// A refused camera shows the access card alone, unless a script is on: then the reading window stays.
    private var showsControls: Bool { flow.camera.error == nil || flow.useScript }

    private func accessCard(_ message: String) -> some View {
        CameraAccessCard(kind: flow.camera.errorKind, message: message,
                         openSettings: { if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) } },
                         retry: { Task { await flow.camera.prepare() } })
    }

    private var topRow: some View {
        ZStack {
            HStack {
                KriaCameraCloseButton(identifier: "teleprompter-close", label: flow.captureBusy ? "Stop and review take" : "Close camera", action: closeCapture)
                Spacer()
                if !flow.captureBusy, showsControls {
                    KriaCameraCapsule(title: "Script", symbol: "text.alignleft", identifier: "teleprompter-script",
                                      isOn: flow.useScript) { flow.editScript() }
                }
            }
            if flow.stage == .recording || flow.stage == .finalizing {
                TeleprompterTimerPill(start: flow.camera.recordingStartedAt)
            }
        }
        .padding(.horizontal, 16)
    }

    /// The reading window's own control, kept with Script at the top.
    private var textWindowRow: some View {
        KriaCameraCapsule(title: "Text window", symbol: "slider.horizontal.3", identifier: "teleprompter-window-settings") { showsWindowSettings = true }
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, alignment: .trailing)
    }

    /// Background and Cinematic: two switches on the same capsule, one row above the capture button.
    @ViewBuilder private var lookRow: some View {
        let showsCinematic = flow.cinematicAvailability == .available
        if flow.speakerBackgroundEnabled || showsCinematic {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { lookControls(showsCinematic: showsCinematic) }
                VStack(spacing: 8) { lookControls(showsCinematic: showsCinematic) }
            }
            .padding(.horizontal, 16).frame(maxWidth: .infinity)
        }
    }

    @ViewBuilder private func lookControls(showsCinematic: Bool) -> some View {
        if flow.speakerBackgroundEnabled { TeleprompterBackgroundMenu(flow: flow) }
        if showsCinematic { TeleprompterCinematicCapsule(flow: flow, camera: flow.camera) }
    }

    @ViewBuilder private var cameraBottom: some View {
        VStack(spacing: 12) {
            // With a script on, the reading window and controls stay put and the card sits above them, scrollable.
            if let message = flow.camera.error, !flow.captureBusy, flow.useScript {
                ScrollView { accessCard(message) }
                    .frame(maxHeight: dynamicTypeSize.isAccessibilitySize ? 210 : 190)
            }
            if let error = flow.error { errorText(error).padding(.horizontal, 16) }
            if let note = flow.backgroundNote {
                Text(note).kriaType(.label).foregroundStyle(Color.white)
                    .padding(.horizontal, 16).accessibilityIdentifier("teleprompter-background-note")
            }
            TeleprompterCinematicNote(flow: flow, camera: flow.camera)
            if flow.camera.micSilent {
                Label("We can’t hear you. Check that nothing is covering the microphone, then stop and record again.",
                      systemImage: "mic.slash.fill")
                    .kriaType(.label).foregroundStyle(Color.white)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading).kriaCameraGlass(RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .padding(.horizontal, 16).accessibilityIdentifier("teleprompter-mic-silent")
            }
            if showsControls {
                if flow.useScript, flow.stage != .starting, flow.stage != .finalizing { scriptStatusRow }
                if flow.stage == .starting || flow.stage == .finalizing {
                    TeleprompterSavingLabel(camera: flow.camera, starting: flow.stage == .starting)
                } else {
                    if !flow.captureBusy { lookRow }
                    captureBar
                }
            }
        }
    }

    /// Speed, the place in the script and the prompter switch. Only while a script is on.
    private var scriptStatusRow: some View {
        TeleprompterScriptStatusRow(script: flow.script, manual: manualScrolling,
                                    useSteady: { allowsAutomaticScroll = true }, openSpeed: { showsSpeedSheet = true })
    }

    /// Capture in the middle, flip on the trailing side. While a take is running only the capture control stays.
    private var captureBar: some View {
        let recording = flow.stage == .recording
        let idle = flow.stage == .camera
        return HStack {
            Color.clear.frame(width: 44, height: 44).frame(maxWidth: .infinity)
            KriaCaptureButton(isRecording: recording, isEnabled: recording || (idle && flow.camera.phase == .ready)) {
                if recording { flow.stop() } else { flow.record(manual: manualScrolling) }
            }
            Group {
                if idle {
                    KriaCameraIconButton(symbol: "arrow.triangle.2.circlepath.camera", label: "Flip camera",
                                         identifier: "teleprompter-flip") { flow.flipCamera() }
                } else { Color.clear.frame(width: 44, height: 44) }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 24)
    }

    // MARK: Review (Paper S3.12)

    private var takeReview: some View {
        let take = flow.selectedTake
        let silent = take?.audioCheck?.verdict == .silent
        let admission = take.flatMap { admissionMessage($0) }
        let blocked = admission != nil && !flow.queueAccepted
        let failureShowing = silent || blocked || take?.status == .invalid || flow.error != nil
        let showsBar = !flow.attachmentFinished && take != nil
        // At accessibility sizes the whole review scrolls, so the player keeps its size and no label is cut off.
        return Group {
            if dynamicTypeSize.isAccessibilitySize {
                ScrollView {
                    VStack(spacing: 0) {
                        reviewHeader
                        reviewContent(take: take, silent: silent, admission: admission, failureShowing: failureShowing)
                        if showsBar, let take { reviewBar(take: take, silent: silent, blocked: blocked, admission: admission, failureShowing: failureShowing) }
                    }
                }
            } else {
                VStack(spacing: 0) {
                    reviewHeader
                    reviewContent(take: take, silent: silent, admission: admission, failureShowing: failureShowing)
                }
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if showsBar, let take { reviewBar(take: take, silent: silent, blocked: blocked, admission: admission, failureShowing: failureShowing) }
                }
            }
        }
    }

    private var reviewHeader: some View {
        HStack(spacing: 12) {
            Button { flow.persistDraft(); dismiss() } label: { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                .buttonStyle(.kriaIcon).accessibilityLabel("Close, keeping script and take")
                .accessibilityIdentifier("teleprompter-close").disabled(flow.usingTake)
            Text("Review take").kriaType(.title).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24).padding(.top, 12).padding(.bottom, 12)
    }

    @ViewBuilder private func reviewContent(take: TeleprompterTakeManifest?, silent: Bool, admission: String?, failureShowing: Bool) -> some View {
        if flow.attachmentFinished {
            VStack(spacing: 16) {
                KriaCheckDisc(size: 24)
                Text(replacementNotice == nil ? "Take added to your edit" : "Take replaced").kriaType(.heading).foregroundStyle(KriaColor.ink)
                Button("Done") { dismiss() }.buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
            }
            .padding(.horizontal, 24).frame(maxHeight: .infinity)
        } else if let take {
            VStack(spacing: 12) {
                if take.status != .invalid {
                    ReviewTakePlayer(url: flow.store.fileURL(for: take)).id(take.id)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    KriaNoticeCard(tone: .failure, title: "This interrupted take couldn’t be played.")
                        .frame(maxHeight: .infinity, alignment: .top)
                }
                reviewNotices(take: take, silent: silent, admission: admission)
            }
            .padding(.horizontal, 24)
        } else {
            VStack(spacing: 12) {
                if let error = flow.error { KriaNoticeCard(tone: .failure, title: error) }
                if flow.needsMetadataRetry {
                    Button("Retry saving take") { flow.retryMetadata() }.buttonStyle(KriaButtonStyle(role: failureShowing ? .ink : .primary, fullWidth: true))
                } else {
                    Button("Record again") { Task { await flow.openCamera() } }.buttonStyle(KriaButtonStyle(role: failureShowing ? .ink : .primary, fullWidth: true))
                }
            }
            .padding(.horizontal, 24).frame(maxHeight: .infinity, alignment: .top)
        }
    }

    @ViewBuilder private func reviewNotices(take: TeleprompterTakeManifest, silent: Bool, admission: String?) -> some View {
        VStack(spacing: 8) {
            if take.interrupted, take.status != .invalid {
                KriaNoticeCard(tone: .warning, title: "\(take.stopReason?.creatorMessage ?? "Recording was interrupted.") Review this take before using it.")
            }
            if silent {
                KriaNoticeCard(tone: .failure, title: "This take has no sound.",
                               message: "Your microphone didn’t record anything. Record again with the mic uncovered.")
                    .accessibilityIdentifier("teleprompter-silent-take")
            } else if take.audioCheck?.verdict == .quiet {
                KriaNoticeCard(tone: .warning, title: TeleprompterRecordingFlow.quietTakeMessage)
                    .accessibilityIdentifier("teleprompter-quiet-take")
            }
            if let cinematic = take.cinematic, take.status != .invalid {
                reviewNote(cinematic.creatorMessage, id: "teleprompter-review-cinematic")
            }
            if take.speakerBackground != nil {
                reviewNote("Your background is added with this take.", id: "teleprompter-review-background")
            }
            if let note = flow.backgroundNote { reviewNote(note, id: "teleprompter-background-note") }
            if let note = flow.photosNote { reviewNote(note, id: "teleprompter-photos-note") }
            if flow.queueAccepted || flow.usingTake {
                HStack(spacing: 8) { KriaSpinner(size: 16); Text(replacementNotice == nil ? "Adding take to your edit…" : "Swapping in your new take…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let admission, !flow.queueAccepted {
                KriaNoticeCard(tone: .failure, title: admission).accessibilityIdentifier("teleprompter-admission")
            }
            if let notice = replacementNotice, !flow.queueAccepted {
                reviewNote(notice, id: "teleprompter-replacement-notice")
            }
            if let error = flow.error, !silent, take.status != .invalid {
                KriaNoticeCard(tone: .failure, title: error).accessibilityIdentifier("teleprompter-review-error")
            }
        }
    }

    private func reviewNote(_ text: String, id: String) -> some View {
        Text(text).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier(id)
    }

    /// The primary is butter, except while a failure notice is on screen: butter never sits beside red, so it is ink then.
    private func reviewBar(take: TeleprompterTakeManifest, silent: Bool, blocked: Bool, admission: String?, failureShowing: Bool) -> some View {
        let primary: KriaButtonStyle.Role = failureShowing ? .ink : .primary
        let busy = flow.usingTake || flow.queueAccepted
        let canUse = take.status != .invalid && !silent
        let limitHit = blocked && (admission?.hasPrefix("This video already has") ?? false)
        return VStack(spacing: 8) {
            if canUse, !blocked {
                Button(take.audioCheck?.verdict == .quiet ? "Use anyway" : replacementNotice == nil ? "Use take" : "Replace take") { Task { await flow.useTake(intake) } }
                    .buttonStyle(KriaButtonStyle(role: primary, fullWidth: true, isLoading: flow.usingTake))
                    .disabled(busy).accessibilityIdentifier("teleprompter-use-take")
            } else if canUse, blocked {
                Button(limitHit ? "Manage clips" : "Close") { flow.persistDraft(); dismiss() }
                    .buttonStyle(KriaButtonStyle(role: primary, fullWidth: true)).accessibilityIdentifier("teleprompter-manage-clips")
            } else {
                Button { Task { await flow.retake() } } label: { Label("Record again", systemImage: KriaSourceOption.recordSymbol) }
                    .buttonStyle(KriaButtonStyle(role: primary, fullWidth: true)).disabled(busy)
                    .accessibilityIdentifier("teleprompter-retake")
            }
            if canUse {
                Button { Task { await flow.retake() } } label: { Label("Record again", systemImage: KriaSourceOption.recordSymbol) }
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true)).disabled(busy)
                    .accessibilityIdentifier("teleprompter-retake")
            }
            if let previous = flow.pendingTake, previous.id != take.id {
                Button("Go back to previous take") { flow.previousTake() }
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true)).disabled(busy)
                    .accessibilityIdentifier("teleprompter-previous-take")
            }
            Button("Discard take") { confirmsDiscard = true }
                .buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium, fullWidth: true)).disabled(busy)
                .accessibilityIdentifier("teleprompter-discard")
        }
        .padding(.horizontal, 24).padding(.top, 12).padding(.bottom, 8)
        .background(KriaColor.paper)
    }

    private func closeCapture() {
        if flow.stage == .countdown { flow.cancelCountdown() }
        else if flow.captureBusy { flow.stop() }
        else { flow.persistDraft(); dismiss() }
    }
    private func errorText(_ text: String) -> some View {
        Text(text).kriaType(.label).foregroundStyle(Color.white).frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The camera view: the live matted preview while a background is active and frames are flowing, otherwise the plain
/// camera preview. Observes the camera itself, which the screen's own flow object does not republish.
private struct TeleprompterPreviewSurface: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    @ObservedObject var camera: TeleprompterCamera

    var body: some View {
        #if DEBUG
        if let scene = TeleprompterSceneFixture.gradient {
            scene.accessibilityIdentifier("teleprompter-scene-fixture")
        } else { live }
        #else
        live
        #endif
    }

    @ViewBuilder private var live: some View {
        if flow.background != nil, let preview = flow.livePreview, camera.liveFramesAttached {
            LiveBackgroundPreviewView(preview: preview).accessibilityIdentifier("teleprompter-live-background")
        } else {
            TeleprompterCameraPreview(camera: camera)
        }
    }
}

/// The "Background" capsule: pick a photo or video from the library, or clear it. Nothing uploads from here.
private struct TeleprompterBackgroundMenu: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    @State private var showsPicker = false
    @State private var item: PhotosPickerItem?
    @State private var loading = false

    private var stateLabel: String {
        switch flow.background?.reference.kind { case .image: "Photo"; case .video: "Video"; case nil: "None" }
    }

    var body: some View {
        Menu {
            Button { choose() } label: { Label("Choose photo or video", systemImage: "photo.on.rectangle") }
                .accessibilityIdentifier("teleprompter-background-choose")
            if flow.background != nil {
                Button(role: .destructive) { flow.clearBackground() } label: { Label("None", systemImage: "xmark.circle") }
                    .accessibilityIdentifier("teleprompter-background-clear")
            }
        } label: {
            KriaCameraCapsule(title: "Background", symbol: "photo.on.rectangle", isOn: flow.background != nil, isSwitch: true, isBusy: loading)
        }
        .disabled(flow.captureBusy || loading)
        .accessibilityLabel("Background").accessibilityValue(stateLabel)
        .accessibilityIdentifier("teleprompter-background")
        .photosPicker(isPresented: $showsPicker, selection: $item, matching: .any(of: [.images, .videos]))
        .onChange(of: item) { _, picked in
            guard let picked else { return }
            item = nil
            let isVideo = picked.supportedContentTypes.contains { $0.conforms(to: .movie) }
            loading = true
            Task {
                defer { loading = false }
                guard let url = try? await picked.loadTransferable(type: ImportedMedia.self)?.url else {
                    flow.backgroundNote = "Couldn’t read that file. Choose another photo or video."
                    return
                }
                flow.chooseBackground(from: url, kind: isVideo ? .video : .image)
                try? FileManager.default.removeItem(at: url)
            }
        }
    }

    private func choose() {
        #if DEBUG
        // Simulator UI tests cannot drive the system picker: a generated photo stands in for the library.
        if TeleprompterBackgroundFixture.isRequested, let url = TeleprompterBackgroundFixture.makePhoto() {
            flow.chooseBackground(from: url, kind: .image)
            try? FileManager.default.removeItem(at: url)
            return
        }
        #endif
        showsPicker = true
    }
}

/// "Starting recording…" / "Saving take…", and KRI-1160's longer "Applying Cinematic look…" while a take is baked.
private struct TeleprompterSavingLabel: View {
    @ObservedObject var camera: TeleprompterCamera
    let starting: Bool
    var body: some View {
        HStack(spacing: 10) {
            KriaSpinner()
            Text(starting ? "Starting recording…" : camera.applyingCinematic ? "Applying Cinematic look…" : "Saving take…")
                .font(KriaFont.body(15).weight(.semibold))
        }.padding(16).kriaGlassPanel(cornerRadius: 24)
    }
}

/// KRI-1160: the Cinematic capsule shows what the camera is really recording, not what was asked for. It reads
/// the camera itself, which the screen's own flow object does not republish.
private struct TeleprompterCinematicCapsule: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    @ObservedObject var camera: TeleprompterCamera

    var body: some View {
        // While the graph is rebuilt for a front-camera request the capsule follows the request, then the real state.
        let settling = flow.cinematicEnabled && camera.position == .front && camera.phase == .preparing
        KriaCameraCapsule(title: "Cinematic", symbol: "camera.aperture", identifier: "teleprompter-cinematic",
                          isOn: camera.cinematicActive || settling, isSwitch: true) { flow.setCinematic(!flow.cinematicEnabled) }
    }
}

/// Says so when Cinematic is asked for but the take will be standard: the rear camera cannot record it, and the
/// front camera may fall back.
private struct TeleprompterCinematicNote: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    @ObservedObject var camera: TeleprompterCamera

    private var text: String? {
        guard flow.cinematicEnabled, camera.error == nil, camera.phase != .preparing else { return nil }
        if camera.position == .back { return "Cinematic records with the front camera, so this take is standard." }
        return camera.cinematicNote
    }

    var body: some View {
        if let text {
            Text(text).kriaType(.label).foregroundStyle(Color.white)
                .fixedSize(horizontal: false, vertical: true)
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .kriaCameraGlass(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .padding(.horizontal, 16).accessibilityIdentifier("teleprompter-cinematic-note")
        }
    }
}

private struct TeleprompterCaptureControlsHeight: PreferenceKey {
    static let defaultValue: CGFloat = 220
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// Keeps high-frequency text playback and window preference updates out of the whole camera view.
private struct TeleprompterWindowOverlay: View {
    @ObservedObject var script: TeleprompterScript
    let canvas: CGRect
    let persist: () -> Void
    let openSettings: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let minimumHeight = TeleprompterScriptWidget.minimumHeight(fontSize: script.fontSize, dynamicTypeSize: dynamicTypeSize)
        let frame = TeleprompterWindowGeometry.frame(preference: script.windowPreference, in: canvas, minimumHeight: minimumHeight)
        return TeleprompterScriptWidget(model: script, preference: script.windowPreference, canvas: canvas,
                                        setPreference: { script.setWindowPreference($0) }, persist: persist, openSettings: openSettings)
            .frame(width: frame.width, height: frame.height)
            .position(x: frame.midX, y: frame.midY)
    }
}

private struct TeleprompterWindowSettings: View {
    @ObservedObject var script: TeleprompterScript
    let canvas: CGRect
    let canChangeFont: Bool
    let persist: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(spacing: 0) {
            WorkspaceTopRow(title: "Text window", leading: { Color.clear.frame(width: 44, height: 44) }, trailing: {
                KriaGlassCloseButton(identifier: "teleprompter-window-settings-close", label: "Close text window settings") { dismiss() }
            })
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    KriaSectionLabel(title: "Background")
                    backgroundChoices
                    KriaSectionLabel(title: "Size")
                    sliderControls
                    KriaSectionLabel(title: "Position")
                    moveControls
                    KriaSectionLabel(title: "Reading")
                    Button("Start of script") { script.reset() }.buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity)
                    Stepper("Text size \(Int(script.fontSize))", value: Binding(get: { Double(script.fontSize) }, set: { script.setFontSize(CGFloat($0)); persist() }), in: 18...32, step: 2)
                        .font(KriaFont.body(15)).disabled(!canChangeFont).accessibilityIdentifier("teleprompter-text-size")
                    Button("Reset window") { apply(.init(appearance: preference.appearance)) }
                        .buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity).accessibilityIdentifier("teleprompter-window-reset")
                }.padding(20)
            }
            .accessibilityIdentifier("teleprompter-window-options")
        }
        .kriaPage().background(KriaColor.paper)
    }

    private var sliderControls: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Width \(Int(preference.widthFraction * 100))%").font(KriaFont.body(14).weight(.semibold))
            Slider(value: Binding(get: { Double(preference.widthFraction) }, set: { apply(.init(appearance: preference.appearance, normalizedX: preference.normalizedX, normalizedY: preference.normalizedY, widthFraction: CGFloat($0), preferredHeight: preference.preferredHeight)) }), in: 0.35...1)
                .tint(KriaColor.sky).accessibilityLabel("Text window width").accessibilityValue("\(Int(preference.widthFraction * 100)) percent").accessibilityIdentifier("teleprompter-window-width")
            Text("Height \(Int(preference.preferredHeight)) points").font(KriaFont.body(14).weight(.semibold))
            Slider(value: Binding(get: { Double(preference.preferredHeight) }, set: { apply(.init(appearance: preference.appearance, normalizedX: preference.normalizedX, normalizedY: preference.normalizedY, widthFraction: preference.widthFraction, preferredHeight: CGFloat($0))) }), in: 96...320)
                .tint(KriaColor.sky).accessibilityLabel("Text window height").accessibilityValue("\(Int(preference.preferredHeight)) points").accessibilityIdentifier("teleprompter-window-height")
        }
    }

    @ViewBuilder private var backgroundChoices: some View {
        if dynamicTypeSize.isAccessibilitySize {
            VStack(spacing: 8) {
                appearanceButton(.transparent, title: "Transparent")
                appearanceButton(.glass, title: "Glass")
                appearanceButton(.solid, title: "Solid")
            }
        } else {
            HStack(spacing: 8) {
                appearanceButton(.transparent, title: "Transparent")
                appearanceButton(.glass, title: "Glass")
                appearanceButton(.solid, title: "Solid")
            }
        }
    }

    private var moveControls: some View {
        Group {
            if dynamicTypeSize.isAccessibilitySize {
                VStack(spacing: 8) { Button("Move up") { move(.init(width: 0, height: -30)) }; Button("Move down") { move(.init(width: 0, height: 30)) }; Button("Move left") { move(.init(width: -30, height: 0)) }; Button("Move right") { move(.init(width: 30, height: 0)) } }
            } else {
                VStack(spacing: 8) {
                    HStack(spacing: 8) { Button("Move up") { move(.init(width: 0, height: -30)) }; Button("Move down") { move(.init(width: 0, height: 30)) } }
                    HStack(spacing: 8) { Button("Move left") { move(.init(width: -30, height: 0)) }; Button("Move right") { move(.init(width: 30, height: 0)) } }
                }
            }
        }
        .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
    }

    @ViewBuilder private func appearanceButton(_ appearance: TeleprompterWindowAppearance, title: String) -> some View {
        if preference.appearance == appearance {
            Button(title) { setAppearance(appearance) }.buttonStyle(KriaPrimaryButtonStyle(fill: KriaColor.sky))
                .accessibilityIdentifier("teleprompter-background-\(appearance.rawValue)").accessibilityAddTraits(.isSelected)
        } else {
            Button(title) { setAppearance(appearance) }.buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("teleprompter-background-\(appearance.rawValue)")
        }
    }
    private func move(_ offset: CGSize) {
        let minimumHeight = TeleprompterScriptWidget.minimumHeight(fontSize: script.fontSize, dynamicTypeSize: dynamicTypeSize)
        apply(TeleprompterWindowGeometry.moving(preference, in: canvas, by: offset, minimumHeight: minimumHeight))
    }
    private var preference: TeleprompterWindowPreference { script.windowPreference }
    private func setAppearance(_ appearance: TeleprompterWindowAppearance) {
        apply(.init(appearance: appearance, normalizedX: preference.normalizedX, normalizedY: preference.normalizedY,
                    widthFraction: preference.widthFraction, preferredHeight: preference.preferredHeight))
    }
    private func apply(_ value: TeleprompterWindowPreference) { script.setWindowPreference(value); persist() }
}

/// Red dot and the running time, top centre while a take is on (Paper S3.09b).
private struct TeleprompterTimerPill: View {
    let start: TimeInterval?
    var body: some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { _ in
            let elapsed = max(0, ProcessInfo.processInfo.systemUptime - (start ?? ProcessInfo.processInfo.systemUptime))
            HStack(spacing: 8) {
                Circle().fill(KriaCaptureColor.red).frame(width: 8, height: 8)
                Text(DurationFormatter.clock(elapsed)).kriaType(.button).monospacedDigit()
            }
            .foregroundStyle(Color.white).padding(.horizontal, 16).frame(minHeight: 44)
            .kriaCameraGlass(Capsule())
        }
        .accessibilityElement(children: .ignore).accessibilityLabel("Recording")
    }
}

/// "Line 6 of 15 · 38%": where the prompter is. Reads the script itself, so a tick redraws only this chip.
private struct TeleprompterProgressChip: View {
    @ObservedObject var script: TeleprompterScript

    var body: some View {
        if !script.visualLines.isEmpty, script.travelDistance > 0 {
            let total = script.visualLines.count
            let current = (script.visualLines.lastIndex { $0.anchor <= script.scrollPosition + 1 } ?? 0) + 1
            let percent = Int((min(1, script.scrollPosition / script.travelDistance) * 100).rounded())
            KriaCameraCapsule(title: "Line \(current) of \(total) · \(percent)%", identifier: "teleprompter-progress")
        }
    }
}

/// Reading pace and the line controls, in a short sheet over the camera. It scrolls, so larger text never cuts it off.
private struct TeleprompterSpeedSheet: View {
    @ObservedObject var script: TeleprompterScript
    let manual: Bool
    let persist: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Speed").kriaType(.heading).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
                VStack(spacing: 4) {
                    Text("\(script.wordsPerMinute) words/min").kriaType(.button).frame(maxWidth: .infinity, alignment: .leading)
                    Slider(value: Binding(get: { Double(script.wordsPerMinute) }, set: { script.setWordsPerMinute(Int($0)); persist() }), in: 60...240, step: 5)
                        .tint(KriaColor.sky).accessibilityLabel("Approximate reading pace")
                        .accessibilityValue("\(script.wordsPerMinute) words per minute")
                        .accessibilityAdjustableAction { direction in
                            switch direction {
                            case .increment: script.setWordsPerMinute(script.wordsPerMinute + 5)
                            case .decrement: script.setWordsPerMinute(script.wordsPerMinute - 5)
                            @unknown default: break
                            }
                        }
                        .accessibilityIdentifier("teleprompter-pace")
                    HStack(alignment: .top) { Text("Slower"); Spacer(minLength: 8); Text("Faster") }
                        .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { lineButtons }
                    VStack(spacing: 8) { lineButtons }
                }
                if script.hasEnded { Text("End of script").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
            }
            .padding(24).frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(KriaColor.paper)
    }

    @ViewBuilder private var lineButtons: some View {
        Button("Back a line") { script.backLine() }.buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
        Button("Start of script") { script.reset() }.buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
    }
}


/// Reads the script itself, so the pace and the prompter switch redraw as it changes (the flow does not republish it).
private struct TeleprompterScriptStatusRow: View {
    @ViewBuilder private var chips: some View {
        KriaCameraCapsule(title: "Speed · \(script.wordsPerMinute) wpm", identifier: "teleprompter-speed", action: openSpeed)
        TeleprompterProgressChip(script: script)
        if !manual {
            KriaCameraCapsule(title: script.isPlaying ? "Stop prompter" : "Start prompter", identifier: "teleprompter-pause-text") {
                if script.isPlaying { script.pause() } else { script.resume() }
            }
        }
    }

    @ObservedObject var script: TeleprompterScript
    let manual: Bool
    let useSteady: () -> Void
    let openSpeed: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(spacing: 8) {
            if manual {
                HStack(spacing: 8) {
                    KriaCameraCapsule(title: "Previous line", identifier: "teleprompter-action-previous line") { script.backLine() }
                    KriaCameraCapsule(title: "Next line", identifier: "teleprompter-action-next line") { script.nextLine() }
                }
                if !UIAccessibility.isVoiceOverRunning {
                    KriaCameraCapsule(title: "Use steady scrolling", identifier: "teleprompter-steady", action: useSteady)
                }
            }
            Group {
                if dynamicTypeSize.isAccessibilitySize {
                    VStack(spacing: 8) { chips }
                } else {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) { chips }.padding(.horizontal, 16)
                    }
                    .accessibilityIdentifier("teleprompter-capture-options")
                }
            }
        }
    }
}
