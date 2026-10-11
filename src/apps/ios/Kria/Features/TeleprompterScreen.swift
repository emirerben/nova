import SwiftUI
import AVKit

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

    private var manualScrolling: Bool {
        UIAccessibility.isVoiceOverRunning || (KriaMotion.isReduced(reduceMotion) && !allowsAutomaticScroll)
    }

    init(accountID: String, projectID: UUID, admissionMessage: @escaping (TeleprompterTakeManifest?) -> String?,
         intake: @escaping (TeleprompterTakeManifest, URL) async -> TeleprompterIntakeResult,
         intakeState: @escaping (TeleprompterTakeManifest) async -> TeleprompterIntakeResult?) {
        _flow = StateObject(wrappedValue: TeleprompterRecordingFlow(accountID: accountID, projectID: projectID))
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
        .interactiveDismissDisabled(flow.captureBusy || flow.usingTake)
        .task {
            await flow.load()
            while !Task.isCancelled {
                if !flow.usingTake, let take = flow.selectedTake, take.status == .attaching,
                   let state = await intakeState(take) { flow.apply(state) }
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
        .onReceive(flow.camera.$phase) { phase in flow.cameraChanged(phase) }
        .onChange(of: scenePhase) { _, phase in
            // Privacy prompts briefly make the scene inactive. They must not
            // cancel the prepare operation whose grant they are requesting.
            if phase == .background || (phase == .inactive && flow.captureBusy) { flow.leaveCapture() }
            else if phase == .inactive { flow.script.pause(); flow.persistDraft() }
            else if flow.stage == .camera, flow.camera.phase != .preparing { Task { await flow.camera.prepare() } }
        }
        .onDisappear { flow.leaveCapture() }
        .confirmationDialog("Discard this take?", isPresented: $confirmsDiscard, titleVisibility: .visible) {
            Button("Discard take", role: .destructive) { flow.discardSelected() }
        } message: { Text("Your script and any previous take stay saved.") }
    }

    private var scriptEntry: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Read your script while filming yourself.")
                        .font(KriaFont.body(18)).foregroundStyle(KriaColor.ink)
                    Text("Script").font(KriaFont.body(14).weight(.semibold))
                    TeleprompterScriptEditor(script: flow.script) { flow.persistDraft() }
                    TeleprompterReadEstimate(script: flow.script)
                    Text("Your script is saved with this edit on this iPhone.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    if let error = flow.error { errorText(error) }
                }.padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("Record with script").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { closeButton } }
            .safeAreaInset(edge: .bottom) {
                TeleprompterOpenCameraButton(script: flow.script) { Task { await flow.openCamera() } }
                    .padding(.horizontal, 20).padding(.vertical, 12).background(KriaColor.paper)
            }
        }
    }

    private var cameraScreen: some View {
        GeometryReader { geometry in
            ZStack {
                Color.black.ignoresSafeArea()
                TeleprompterCameraPreview(camera: flow.camera)
                    .frame(width: geometry.size.width, height: geometry.size.width * 16 / 9)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityHidden(true)
                VStack(spacing: 8) {
                    TeleprompterScriptWidget(model: flow.script).padding(.horizontal, 12)
                    HStack {
                        Button { closeCapture() } label: {
                            Image(systemName: "xmark").font(.system(size: 18, weight: .semibold)).frame(width: 44, height: 44)
                                .background(Color.black.opacity(0.7), in: Circle())
                        }.accessibilityLabel(flow.captureBusy ? "Stop and review take" : "Close camera")
                        Spacer()
                    }.padding(.horizontal, 12)
                    Spacer(minLength: 8)
                    if let message = flow.camera.error {
                        TeleprompterCameraErrorCard(message: message, accessibilityLayout: dynamicTypeSize.isAccessibilitySize,
                            editScript: { flow.editScript() }, retry: { Task { await flow.camera.prepare() } },
                            openSettings: { if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) } })
                    }
                    if let error = flow.error { errorText(error).padding(.horizontal, 16) }
                    if flow.stage == .countdown {
                        Text("\(flow.countdown)").font(KriaFont.body(48).weight(.bold))
                            .accessibilityLabel("Recording in \(flow.countdown)")
                        Button("Cancel countdown") { flow.cancelCountdown() }.frame(minHeight: 44)
                            .accessibilityIdentifier("teleprompter-cancel-countdown")
                    } else if flow.stage == .starting || flow.stage == .finalizing {
                        ProgressView(flow.stage == .starting ? "Starting recording…" : "Saving take…").tint(.white).padding(16)
                    } else {
                        TeleprompterCaptureControls(script: flow.script, camera: flow.camera,
                            recording: flow.stage == .recording,
                            manual: manualScrolling,
                            enableAutomaticScroll: UIAccessibility.isVoiceOverRunning ? nil : { allowsAutomaticScroll = true },
                            record: { flow.record(manual: manualScrolling) },
                            stop: { flow.stop() })
                    }
                }.foregroundStyle(.white).padding(.top, 8).padding(.bottom, 16)
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
                                .frame(height: 340).clipShape(RoundedRectangle(cornerRadius: 20))
                        } else {
                            Label("This interrupted take couldn’t be played.", systemImage: "exclamationmark.triangle")
                                .font(KriaFont.body(18)).padding(24)
                        }
                        if take.interrupted, take.status != .invalid {
                            Text("Recording was interrupted. Review this take before using it.").font(KriaFont.body(14))
                        }
                        if flow.queueAccepted || flow.usingTake { ProgressView("Adding take to your edit…") }
                        if let message = admissionMessage(take), !flow.queueAccepted { Text(message).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk) }
                        if let error = flow.error { errorText(error).accessibilityIdentifier("teleprompter-review-error") }
                        if take.status != .invalid {
                            Button("Use take") { Task { await flow.useTake(intake) } }
                                .buttonStyle(KriaPrimaryButtonStyle()).frame(maxWidth: .infinity)
                                .disabled(admissionMessage(take) != nil || flow.usingTake || flow.queueAccepted)
                                .accessibilityIdentifier("teleprompter-use-take")
                        }
                        Button("Retake") { Task { await flow.retake() } }.buttonStyle(KriaSecondaryButtonStyle())
                            .disabled(flow.usingTake || flow.queueAccepted).accessibilityIdentifier("teleprompter-retake")
                        if let previous = flow.pendingTake, previous.id != take.id {
                            Button("Previous take") { flow.previousTake() }.frame(minHeight: 44)
                                .disabled(flow.usingTake || flow.queueAccepted)
                            Text("Returning to the previous take discards this candidate.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        }
                        Button("Discard take", role: .destructive) { confirmsDiscard = true }.frame(minHeight: 44)
                            .disabled(flow.usingTake || flow.queueAccepted)
                    } else if flow.needsMetadataRetry {
                        if let error = flow.error { errorText(error) }
                        Button("Retry saving take") { flow.retryMetadata() }.buttonStyle(KriaPrimaryButtonStyle())
                    } else {
                        if let error = flow.error { errorText(error) }
                        Button("Edit script") { flow.editScript() }.buttonStyle(KriaPrimaryButtonStyle())
                    }
                }.padding(20)
            }.navigationTitle("Review take").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarTrailing) { closeButton.disabled(flow.usingTake) } }
        }
    }

    private var closeButton: some View {
        Button { flow.persistDraft(); dismiss() } label: { Image(systemName: "xmark").frame(width: 44, height: 44) }
            .accessibilityLabel("Close, keeping script and take").accessibilityIdentifier("teleprompter-close")
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
            .padding(8).background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16))
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
        .padding(16).background(Color.black.opacity(0.85), in: RoundedRectangle(cornerRadius: 16)).padding(.horizontal, 16)
    }

    @ViewBuilder private var actions: some View {
        Button("Edit script", action: editScript).buttonStyle(.bordered).fixedSize(horizontal: false, vertical: true)
            .accessibilityIdentifier("teleprompter-edit-script")
        Button("Try again", action: retry).buttonStyle(.bordered).fixedSize(horizontal: false, vertical: true)
        Button("Settings", action: openSettings).buttonStyle(.bordered).fixedSize(horizontal: false, vertical: true)
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
    private var optionsHeight: CGFloat { usesAccessibilityLayout ? 200 : 260 }

    var body: some View {
        VStack(spacing: 12) {
            ScrollView {
                options
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: optionsHeight)
            .accessibilityIdentifier("teleprompter-capture-options")
            recordButton
        }.padding(16).background(Color.black.opacity(0.82), in: RoundedRectangle(cornerRadius: 20)).padding(.horizontal, 16)
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
            VStack(spacing: 8) {
                if manual {
                    Button("Previous line") { script.backLine() }.frame(maxWidth: .infinity, minHeight: 44)
                    Button("Next line") { script.nextLine() }.frame(maxWidth: .infinity, minHeight: 44)
                } else {
                    Button(recording ? (script.isPlaying ? "Pause text" : "Resume text") : (script.isPlaying ? "Pause rehearsal" : "Rehearse")) {
                        if script.isPlaying { script.pause() } else { script.resume() }
                    }.frame(maxWidth: .infinity, minHeight: 44).accessibilityIdentifier("teleprompter-pause-text")
                    Button("Back a line") { script.backLine() }.frame(maxWidth: .infinity, minHeight: 44)
                }
            }.font(KriaFont.body(14))
            if manual, let enableAutomaticScroll {
                Button("Use steady scrolling", action: enableAutomaticScroll).frame(maxWidth: .infinity, minHeight: 44)
                    .font(KriaFont.body(14))
            }
            if script.hasEnded { Text("End of script").font(KriaFont.body(13)) }
            if !recording {
                Button("Start of script") { script.reset() }.frame(maxWidth: .infinity, minHeight: 44)
                Stepper("Text size \(Int(script.fontSize))", value: Binding(get: { Double(script.fontSize) }, set: { script.setFontSize(CGFloat($0)) }), in: 18...32, step: 2)
                    .font(KriaFont.body(14)).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var recordButton: some View {
            Button(action: recording ? stop : record) {
                Group {
                    if usesAccessibilityLayout {
                        Text(recording ? "Stop recording" : "Record")
                    } else {
                        Label(recording ? "Stop recording" : "Record", systemImage: recording ? "stop.fill" : "record.circle")
                    }
                }.font(KriaFont.body(18).weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, minHeight: 56).background(Color.red, in: Capsule())
            }.disabled(!recording && camera.phase != .ready)
                .accessibilityIdentifier(recording ? "teleprompter-stop-recording" : "teleprompter-record")
    }
}
private struct TeleprompterElapsed: View {
    let start: TimeInterval?
    var body: some View {
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { _ in
            let elapsed = max(0, ProcessInfo.processInfo.systemUptime - (start ?? ProcessInfo.processInfo.systemUptime))
            Label(DurationFormatter.clock(elapsed), systemImage: "circle.fill")
                .font(KriaFont.body(14).monospacedDigit()).foregroundStyle(.white)
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
