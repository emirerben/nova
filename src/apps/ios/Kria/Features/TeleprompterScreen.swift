import SwiftUI
import AVKit
import PhotosUI

struct TeleprompterScreen: View {
    @StateObject private var flow: TeleprompterRecordingFlow
    let admissionMessage: (TeleprompterTakeManifest?) -> String?
    let intake: (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult
    let intakeState: (TeleprompterTakeManifest) async -> TeleprompterIntakeResult?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var confirmsDiscard = false
    @State private var allowsAutomaticScroll = false
    @State private var showsWindowSettings = false
    @State private var captureControlsHeight: CGFloat = 220
    @State private var readingCanvas: CGRect = .zero

    private var manualScrolling: Bool {
        UIAccessibility.isVoiceOverRunning || (KriaMotion.isReduced(reduceMotion) && !allowsAutomaticScroll)
    }

    init(accountID: String, projectID: UUID, admissionMessage: @escaping (TeleprompterTakeManifest?) -> String?,
         intake: @escaping (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult,
         intakeState: @escaping (TeleprompterTakeManifest) async -> TeleprompterIntakeResult?,
         speakerBackgroundEnabled: Bool = false,
         backgroundIntake: BackgroundIntake? = nil,
         backgroundStatement: BackgroundStatement? = nil,
         reportDiagnostic: @escaping (RecorderDiagnosticReport) -> Void = { _ in }) {
        _flow = StateObject(wrappedValue: TeleprompterRecordingFlow(
            accountID: accountID, projectID: projectID, speakerBackgroundEnabled: speakerBackgroundEnabled,
            backgroundIntake: backgroundIntake, backgroundStatement: backgroundStatement,
            reportDiagnostic: reportDiagnostic))
        self.admissionMessage = admissionMessage; self.intake = intake; self.intakeState = intakeState
    }

    var body: some View {
        Group {
            switch flow.stage {
            case .script: scriptEntry
            case .review: takeReview
            default: cameraScreen
            }
        }
        .background(KriaColor.paper)
        .kriaPage()
        .interactiveDismissDisabled(flow.captureBusy || flow.usingTake)
        .task {
            await flow.load()
            // A statement the last opening could not make is retried here.
            await flow.syncBackgroundStatement()
            while !Task.isCancelled {
                if !flow.usingTake, let take = flow.selectedTake, take.status == .attaching,
                   let state = await intakeState(take) {
                    flow.apply(state)
                    // Resumes a background upload a relaunch interrupted; a no-op once it was handed over.
                    if case .failed = state {} else { await flow.uploadBackgroundIfNeeded() }
                }
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
        .onReceive(flow.camera.$phase) { phase in flow.cameraChanged(phase) }
        .onChange(of: scenePhase) { _, phase in flow.scenePhaseChanged(phase) }
        .onAppear { flow.viewAppeared() }
        .onDisappear { flow.viewDisappeared() }
        .confirmationDialog("Discard this take?", isPresented: $confirmsDiscard, titleVisibility: .visible) {
            Button("Discard take", role: .destructive) { flow.discardSelected() }
        } message: { Text("Your script and any previous take stay saved.") }
        .sheet(isPresented: $showsWindowSettings) {
            TeleprompterWindowSettings(script: flow.script, canvas: readingCanvas, canChangeFont: !flow.captureBusy, persist: { flow.persistDraft() })
                .environment(\.dynamicTypeSize, dynamicTypeSize)
                .presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
        }
    }

    private var scriptEntry: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    AttachmentHeading(title: "Your script", subtitle: "Read your script while filming yourself.")
                    TeleprompterScriptEditor(script: flow.script) { flow.persistDraft() }
                    TeleprompterReadEstimate(script: flow.script)
                    Text("Your script is saved with this edit on this iPhone.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    if let error = flow.error { errorText(error) }
                }.padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .toolbar(.hidden, for: .navigationBar)
            .safeAreaInset(edge: .top) { WorkspaceTopRow(title: "Record with script", leading: { Color.clear.frame(width: 44, height: 44) }, trailing: { closeButton }).background(KriaColor.paper) }
            .safeAreaInset(edge: .bottom) {
                TeleprompterOpenCameraButton(script: flow.script) { Task { await flow.openCamera() } }
                    .padding(.horizontal, 20).padding(.vertical, 12).background(KriaColor.paper)
            }
        }
    }

    private var cameraScreen: some View {
        GeometryReader { geometry in
            let topInset: CGFloat = 60
            let bottomInset: CGFloat = max(120, captureControlsHeight + 16)
            let canvas = CGRect(x: 12, y: topInset, width: max(1, geometry.size.width - 24),
                                height: max(0, geometry.size.height - topInset - bottomInset))
            ZStack {
                Color.black.ignoresSafeArea()
                TeleprompterPreviewSurface(flow: flow, camera: flow.camera)
                    .frame(width: geometry.size.width, height: geometry.size.width * 16 / 9)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityHidden(true)
                TeleprompterWindowOverlay(script: flow.script, canvas: canvas, persist: { flow.persistDraft() },
                                           openSettings: { showsWindowSettings = true })
                VStack(spacing: 8) {
                    HStack {
                        KriaGlassCloseButton(identifier: "teleprompter-close", label: flow.captureBusy ? "Stop and review take" : "Close camera", action: closeCapture)
                        Spacer()
                        if flow.speakerBackgroundEnabled {
                            TeleprompterBackgroundMenu(flow: flow, accessibilityLayout: dynamicTypeSize.isAccessibilitySize)
                        }
                        Button { showsWindowSettings = true } label: {
                            HStack(spacing: 6) { KriaIcon(.settings); if !dynamicTypeSize.isAccessibilitySize { Text("Text window") } }
                                .font(KriaFont.body(13).weight(.semibold)).padding(.horizontal, 12).frame(minHeight: 44)
                        }
                        .buttonStyle(.plain).kriaFloatingSurface(Capsule()).accessibilityLabel("Text window")
                        .accessibilityIdentifier("teleprompter-window-settings")
                    }.padding(.horizontal, 12)
                    Spacer(minLength: 8)
                    cameraBottom
                        .background(GeometryReader { proxy in
                            Color.clear.preference(key: TeleprompterCaptureControlsHeight.self, value: proxy.size.height)
                        })
                }.foregroundStyle(KriaColor.ink).padding(.top, 8).padding(.bottom, 16)
            }
            .onAppear { readingCanvas = canvas }
            .onChange(of: geometry.size) { _, _ in readingCanvas = canvas }
            .onChange(of: captureControlsHeight) { _, _ in readingCanvas = canvas }
            .onPreferenceChange(TeleprompterCaptureControlsHeight.self) { captureControlsHeight = $0 }
        }
    }

    @ViewBuilder private var cameraBottom: some View {
        VStack(spacing: 8) {
            if let message = flow.camera.error {
                TeleprompterCameraErrorCard(message: message, accessibilityLayout: dynamicTypeSize.isAccessibilitySize,
                    editScript: { flow.editScript() }, retry: { Task { await flow.camera.prepare() } },
                    openSettings: { if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) } })
            }
            if let error = flow.error { errorText(error).padding(.horizontal, 16) }
            if let note = flow.backgroundNote {
                Text(note).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .padding(.horizontal, 16).accessibilityIdentifier("teleprompter-background-note")
            }
            if flow.background != nil, flow.stage == .camera || flow.stage == .recording {
                TeleprompterBackgroundPreviewNotice(camera: flow.camera)
            }
            if flow.camera.micSilent {
                Label("We can’t hear you. Check that nothing is covering the microphone, then stop and record again.",
                      systemImage: "mic.slash.fill")
                    .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading).kriaGlassPanel(cornerRadius: 16)
                    .padding(.horizontal, 16).accessibilityIdentifier("teleprompter-mic-silent")
            }
            if flow.stage == .countdown {
                VStack(spacing: 12) {
                    Text("\(flow.countdown)").font(KriaFont.display(48).weight(.bold))
                        .accessibilityLabel("Recording in \(flow.countdown)")
                    Button("Cancel countdown") { flow.cancelCountdown() }.buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                        .accessibilityIdentifier("teleprompter-cancel-countdown")
                }.padding(16).kriaGlassPanel(cornerRadius: 24)
            } else if flow.stage == .starting || flow.stage == .finalizing {
                HStack(spacing: 10) {
                    KriaSpinner()
                    Text(flow.stage == .starting ? "Starting recording…" : "Saving take…").font(KriaFont.body(15).weight(.semibold))
                }.padding(16).kriaGlassPanel(cornerRadius: 24)
            } else {
                TeleprompterCaptureControls(script: flow.script, camera: flow.camera,
                    recording: flow.stage == .recording,
                    manual: manualScrolling,
                    enableAutomaticScroll: UIAccessibility.isVoiceOverRunning ? nil : { allowsAutomaticScroll = true },
                    record: { flow.record(manual: manualScrolling) },
                    stop: { flow.stop() })
            }
        }
    }

    private var takeReview: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if flow.attachmentFinished {
                        Label("Take added to your edit", systemImage: "checkmark.circle.fill")
                            .font(KriaFont.body(20).weight(.semibold)).foregroundStyle(KriaColor.success)
                        Button("Done") { dismiss() }.buttonStyle(KriaPrimaryButtonStyle())
                    } else if let take = flow.selectedTake {
                        if take.status != .invalid {
                            TeleprompterTakePlayer(url: flow.store.fileURL(for: take))
                                .id(take.id)
                                .frame(height: 340).clipShape(RoundedRectangle(cornerRadius: 20))
                        } else {
                            Label("This interrupted take couldn’t be played.", systemImage: "exclamationmark.triangle")
                                .font(KriaFont.body(18)).padding(24)
                        }
                        if take.interrupted, take.status != .invalid {
                            Text("Recording was interrupted. Review this take before using it.").font(KriaFont.body(14))
                        }
                        if take.audioCheck?.verdict == .quiet {
                            Text(TeleprompterRecordingFlow.quietTakeMessage).font(KriaFont.body(14))
                                .foregroundStyle(KriaColor.mutedInk).accessibilityIdentifier("teleprompter-quiet-take")
                        }
                        if take.speakerBackground != nil {
                            Text("Your background is added with this take. The recording itself stays unedited.")
                                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                                .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("teleprompter-review-background")
                        }
                        if let note = flow.backgroundNote {
                            Text(note).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                                .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("teleprompter-background-note")
                        }
                        if let note = flow.photosNote {
                            Text(note).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                                .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("teleprompter-photos-note")
                        }
                        if flow.queueAccepted || flow.usingTake { ProgressView("Adding take to your edit…") }
                        if let message = admissionMessage(take), !flow.queueAccepted { Text(message).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk) }
                        if let error = flow.error { errorText(error).accessibilityIdentifier("teleprompter-review-error") }
                        if take.status != .invalid, take.audioCheck?.verdict != .silent {
                            Button(take.audioCheck?.verdict == .quiet ? "Use anyway" : "Use take") { Task { await flow.useTake(intake) } }
                                .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                                .disabled(admissionMessage(take) != nil || flow.usingTake || flow.queueAccepted)
                                .accessibilityIdentifier("teleprompter-use-take")
                        }
                        Button(take.audioCheck?.verdict == .silent ? "Record again" : "Retake") { Task { await flow.retake() } }.buttonStyle(KriaSecondaryButtonStyle())
                            .disabled(flow.usingTake || flow.queueAccepted).accessibilityIdentifier("teleprompter-retake")
                        if let previous = flow.pendingTake, previous.id != take.id {
                            Button("Previous take") { flow.previousTake() }.buttonStyle(KriaSecondaryButtonStyle())
                                .disabled(flow.usingTake || flow.queueAccepted)
                            Text("Returning to the previous take discards this candidate.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        }
                        Button("Discard take", role: .destructive) { confirmsDiscard = true }.buttonStyle(KriaSecondaryButtonStyle())
                            .disabled(flow.usingTake || flow.queueAccepted)
                    } else if flow.needsMetadataRetry {
                        if let error = flow.error { errorText(error) }
                        Button("Retry saving take") { flow.retryMetadata() }.buttonStyle(KriaPrimaryButtonStyle())
                    } else {
                        if let error = flow.error { errorText(error) }
                        Button("Edit script") { flow.editScript() }.buttonStyle(KriaPrimaryButtonStyle())
                    }
                }.padding(20)
            }.toolbar(.hidden, for: .navigationBar)
                .safeAreaInset(edge: .top) { WorkspaceTopRow(title: "Review take", leading: { Color.clear.frame(width: 44, height: 44) }, trailing: { closeButton.disabled(flow.usingTake) }).background(KriaColor.paper) }
        }
    }

    private var closeButton: some View {
        KriaGlassCloseButton(identifier: "teleprompter-close", label: "Close, keeping script and take") { flow.persistDraft(); dismiss() }
    }
    private func closeCapture() {
        if flow.stage == .countdown { flow.cancelCountdown(); flow.editScript() }
        else if flow.captureBusy { flow.stop(interrupted: true) }
        else { flow.editScript() }
    }
    private func errorText(_ text: String) -> some View {
        Text(text).font(KriaFont.body(14)).foregroundStyle(KriaColor.failureText).frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The camera view: the live matted preview while a background is active and frames are flowing, otherwise the plain
/// camera preview. Observes the camera itself, which the screen's own flow object does not republish.
private struct TeleprompterPreviewSurface: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    @ObservedObject var camera: TeleprompterCamera

    var body: some View {
        if flow.background != nil, let preview = flow.livePreview, camera.liveFramesAttached {
            LiveBackgroundPreviewView(preview: preview).accessibilityIdentifier("teleprompter-live-background")
        } else {
            TeleprompterCameraPreview(camera: camera)
        }
    }
}

/// S24: the live composite is a convenience, not the render. Said plainly, never as a promise of the same pixels.
private struct TeleprompterBackgroundPreviewNotice: View {
    @ObservedObject var camera: TeleprompterCamera
    var body: some View {
        Label(camera.liveFramesAttached ? "Preview. Your recording stays unedited." : "Your background shows once the camera is ready.",
              systemImage: "eye")
            .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 12).padding(.vertical, 8).frame(maxWidth: .infinity, alignment: .leading)
            .kriaGlassPanel(cornerRadius: 16).padding(.horizontal, 16)
            .accessibilityIdentifier("teleprompter-background-preview-notice")
    }
}

/// The "Background" capsule: pick a photo or video from the library, or clear it. Nothing uploads from here.
private struct TeleprompterBackgroundMenu: View {
    @ObservedObject var flow: TeleprompterRecordingFlow
    let accessibilityLayout: Bool
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
            HStack(spacing: 6) {
                if loading { ProgressView() } else { KriaIcon(.gallery) }
                if !accessibilityLayout { Text("Background") }
            }
            .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 12).frame(minHeight: 44).kriaFloatingSurface(Capsule())
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

private struct TeleprompterReadEstimate: View {
    @ObservedObject var script: TeleprompterScript
    var body: some View { Text("About \(Int(ceil(script.estimatedReadTime))) seconds at \(script.wordsPerMinute) words/min")
        .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk) }
}
private struct TeleprompterScriptEditor: View {
    @ObservedObject var script: TeleprompterScript
    let save: () -> Void
    var body: some View {
        TextEditor(text: Binding(get: { script.text }, set: { script.updateText($0); save() }))
            .font(KriaFont.body(20)).foregroundStyle(KriaColor.ink)
            .frame(minHeight: 240)
            .padding(12).background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(KriaColor.line))
            .accessibilityLabel("Script").accessibilityIdentifier("teleprompter-script-editor")
    }
}
private struct TeleprompterOpenCameraButton: View {
    @ObservedObject var script: TeleprompterScript
    let open: () -> Void
    var body: some View {
        Button("Open camera", action: open).buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
            .disabled(script.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityIdentifier("teleprompter-open-camera")
    }
}
private struct TeleprompterCameraErrorCard: View {
    let message: String
    let accessibilityLayout: Bool
    let editScript: () -> Void
    let retry: () -> Void
    let openSettings: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(message).font(KriaFont.body(15)).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("teleprompter-camera-error")
                if accessibilityLayout {
                    VStack(spacing: 8) { actions }
                } else {
                    HStack(spacing: 8) { actions }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxHeight: accessibilityLayout ? 210 : 170)
        .padding(16).kriaGlassPanel(cornerRadius: 20).padding(.horizontal, 16)
    }

    @ViewBuilder private var actions: some View {
        Button("Edit script", action: editScript).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44)).fixedSize(horizontal: false, vertical: true)
            .accessibilityIdentifier("teleprompter-edit-script")
        Button("Try again", action: retry).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44)).fixedSize(horizontal: false, vertical: true)
        Button("Settings", action: openSettings).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44)).fixedSize(horizontal: false, vertical: true)
    }
}
private struct TeleprompterCaptureControls: View {
    @ObservedObject var script: TeleprompterScript
    @ObservedObject var camera: TeleprompterCamera
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let recording: Bool
    let manual: Bool
    let enableAutomaticScroll: (() -> Void)?
    let record: () -> Void
    let stop: () -> Void

    private var usesAccessibilityLayout: Bool { dynamicTypeSize.isAccessibilitySize }
    private var optionsHeight: CGFloat { usesAccessibilityLayout ? 200 : 156 }

    var body: some View {
        VStack(spacing: 12) {
            ScrollView {
                options
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: optionsHeight)
            .accessibilityIdentifier("teleprompter-capture-options")
            manualReadingControls
            captureActions
        }.padding(16).kriaGlassPanel(cornerRadius: 24).padding(.horizontal, 16)
    }

    private var options: some View {
        VStack(spacing: 12) {
            VStack(spacing: 4) {
                HStack {
                    Text("\(script.wordsPerMinute) words/min").font(KriaFont.body(14).weight(.semibold))
                    Spacer()
                    if recording { TeleprompterElapsed(start: camera.recordingStartedAt) }
                }
                Slider(value: Binding(get: { Double(script.wordsPerMinute) }, set: { script.setWordsPerMinute(Int($0)) }), in: 60...240, step: 5)
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
                HStack { Text("Slower"); Spacer(); Text("Faster") }.font(KriaFont.body(12))
            }
            if !manual {
                Button("Back a line", action: script.backLine).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .frame(maxWidth: .infinity)
            }
            if script.hasEnded { Text("End of script").font(KriaFont.body(13)) }
        }
    }

    @ViewBuilder private var manualReadingControls: some View {
        if manual {
            pairedActions(first: "Previous line", firstAction: script.backLine, second: "Next line", secondAction: script.nextLine)
            if let enableAutomaticScroll {
                Button("Use steady scrolling", action: enableAutomaticScroll).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .frame(maxWidth: .infinity)
            }
        }
    }

    @ViewBuilder private func pairedActions(first: String, firstAction: @escaping () -> Void, firstIdentifier: String? = nil,
                                            second: String, secondAction: @escaping () -> Void) -> some View {
        let actions = Group {
            Button(first, action: firstAction).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                .accessibilityIdentifier(firstIdentifier ?? "teleprompter-action-\(first.lowercased())")
            Button(second, action: secondAction).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
        }
        if usesAccessibilityLayout { VStack(spacing: 8) { actions } }
        else { HStack(spacing: 8) { actions } }
    }

    @ViewBuilder private var captureActions: some View {
        if usesAccessibilityLayout {
            VStack(spacing: 8) { actionButtons }
        } else {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { actionButtons }
                VStack(spacing: 8) { actionButtons }
            }
        }
    }

    @ViewBuilder private var actionButtons: some View {
        Button(action: recording ? stop : record) {
            Group {
                if usesAccessibilityLayout {
                    Text(recording ? "Stop recording" : "Record")
                } else {
                    Label(recording ? "Stop recording" : "Record", systemImage: recording ? "stop.fill" : "record.circle")
                }
            }.fixedSize(horizontal: false, vertical: true)
        }
        .buttonStyle(KriaInkCTAButtonStyle(height: 52))
        .frame(maxWidth: .infinity)
        .disabled(!recording && camera.phase != .ready)
        .accessibilityIdentifier(recording ? "teleprompter-stop-recording" : "teleprompter-record")

        if !manual {
            Button {
                if script.isPlaying { script.pause() } else { script.resume() }
            } label: {
                Text(script.isPlaying ? "Stop prompter" : "Start prompter")
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, usesAccessibilityLayout ? 8 : 0)
            }
            .buttonStyle(KriaSecondaryButtonStyle(minHeight: 52))
            .frame(maxWidth: .infinity)
            .accessibilityIdentifier("teleprompter-pause-text")
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
private struct TeleprompterElapsed: View {
    let start: TimeInterval?
    var body: some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { _ in
            let elapsed = max(0, ProcessInfo.processInfo.systemUptime - (start ?? ProcessInfo.processInfo.systemUptime))
            Label(DurationFormatter.clock(elapsed), systemImage: "circle.fill")
                .font(KriaFont.body(14).monospacedDigit()).foregroundStyle(KriaColor.ink)
        }.accessibilityLabel("Recording")
    }
}
private struct TeleprompterTakePlayer: View {
    let url: URL
    @State private var player: AVPlayer?
    var body: some View {
        VideoPlayer(player: player).onAppear { player = AVPlayer(url: url) }
            .onDisappear { player?.pause(); player = nil }
    }
}
