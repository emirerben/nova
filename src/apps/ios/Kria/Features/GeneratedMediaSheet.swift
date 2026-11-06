import SwiftUI
import AVKit

@MainActor final class GeneratedMediaDraft: ObservableObject, Identifiable {
    let id = UUID()
    let capability: GeneratedMediaCapability
    var source: CreationVisual? = nil
    var script: GeneratedNarrationScript? = nil
    @Published var prompt = ""
    @Published var title = ""
    var transparentBackground = false
    @Published var operation: GeneratedMediaOperation?
    @Published var submittedRequest: GeneratedMediaRequest?
    @Published var recovery: GeneratedMediaRecovery?
    @Published var submissionRejection: String?
    private var recoveryKey: String?

    func configureRecovery(accountID: String, itemID: String, placementID: String? = nil) {
        let parts = [accountID, itemID, capability.rawValue, source?.id ?? ""] + (placementID.map { [$0] } ?? [])
        guard let data = try? JSONEncoder().encode(parts) else { return }
        recoveryKey = "generated-media-recovery." + data.base64EncodedString()
        if let recoveryKey, let saved = UserDefaults.standard.data(forKey: recoveryKey) {
            recovery = try? JSONDecoder().decode(GeneratedMediaRecovery.self, from: saved)
        }
    }

    func handleSubmissionFailure(_ error: Error) -> Bool {
        guard GeneratedMediaRequestFailure.isDefiniteRejection(error) else { return false }
        clearRecovery()
        submissionRejection = "The server rejected this request. Edit it and review the current source or script before generating again."
        return true
    }

    func resetRejectedRequest() { submissionRejection = nil }

    func rememberRequest() {
        recovery = GeneratedMediaRecovery(clientEventID: UUID().uuidString, operationID: nil)
        persistRecovery()
    }

    func rememberOperation(_ value: GeneratedMediaOperation) {
        guard recovery != nil, !Task.isCancelled else { return }
        operation = value
        recovery?.operationID = value.id
        persistRecovery()
    }

    private func persistRecovery() {
        guard let recoveryKey, let recovery, let data = try? JSONEncoder().encode(recovery) else { return }
        UserDefaults.standard.set(data, forKey: recoveryKey)
    }

    func clearRecovery() {
        if let recoveryKey { UserDefaults.standard.removeObject(forKey: recoveryKey) }
        recovery = nil
        operation = nil
        submittedRequest = nil
    }

    init(capability: GeneratedMediaCapability, source: CreationVisual? = nil, script: GeneratedNarrationScript? = nil) {
        self.capability = capability
        self.source = source
        self.script = script
    }
}

struct GeneratedMediaSheet: View {
    let itemID: String
    @ObservedObject var draft: GeneratedMediaDraft
    let accepted: () async -> Void
    var useImage: ((GeneratedMediaOperation) async throws -> Void)? = nil
    var allowsNewGeneration = true
    var allowsUse = true
    var embedded = false
    var apiOverride: (any KriaAPIClient)? = nil
    /// False only for screenshot previews, which carry a fixed server state and must not ask the server.
    var restoresOnAppear = true
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var problem: OverlayProblem?
    @State private var previewLoaded = false
    @State private var stalled = false
    @State private var showsFullScript = false
    @StateObject private var voice = NarrationPlayer()
    @State private var pollTask: Task<Void, Never>?

    private var narration: Bool { draft.capability == .narration }
    private var editing: Bool { draft.capability == .imageEdit }
    private var heading: String {
        if narration { return "AI voice" }
        if case .ready = stage { return editing ? "Your edit" : "Your image" }
        return editing ? "Edit image" : "Generate image"
    }
    private var noun: OverlayProblemNoun { narration ? .voice : OverlayProblemNoun(singular: "image", plural: "images") }
    private var promptIsTooLong: Bool { draft.prompt.count > OverlayLimits.maxPrompt }
    private var promptIsEmpty: Bool { draft.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private var api: any KriaAPIClient { apiOverride ?? model.api }

    private enum Stage {
        case compose, rejected, making, uncertain, failed, unsaved
        case ready(GeneratedMediaOperation)
    }

    private var stage: Stage {
        if draft.submissionRejection != nil { return .rejected }
        guard draft.recovery != nil else { return .compose }
        guard let operation = draft.operation else { return busy ? .making : .uncertain }
        switch operation.state {
        case .ready: return (operation.previewURL ?? operation.sourceURL) != nil && operation.canAccept ? .ready(operation) : .uncertain
        case .pending, .submitting: return .making
        case .providerComplete: return operation.canRetryStorage ? .unsaved : .making
        case .unknown: return .uncertain
        case .failed: return .failed
        }
    }

    var body: some View {
        Group {
            if embedded {
                VStack(alignment: .leading, spacing: 12) { notices; mainContent; inlineActions }
            } else if hasPinnedActions {
                KriaSheetScaffold(heading: heading, onClose: { dismiss() }) { sheetContent } pinnedBar: { pinnedActions }
            } else {
                KriaSheetScaffold(heading: heading, onClose: { dismiss() }) { sheetContent }
            }
        }
        .background(KriaColor.paper)
        .onDisappear { pollTask?.cancel(); voice.stop() }
        .onAppear { if restoresOnAppear { pollTask = Task { await restore() } } }
    }

    private var sheetContent: some View {
        VStack(alignment: .leading, spacing: 16) { notices; mainContent }
    }

    // MARK: Content

    @ViewBuilder private var notices: some View {
        ForEach(activeProblems, id: \.self) { value in
            OverlayProblemCard(problem: value, noun: noun, handlers: handlers)
        }
    }

    /// The one notice for what the creator is looking at: a stored request problem first, else the stage's own.
    private var activeProblems: [OverlayProblem] {
        if let problem { return [problem] }
        switch stage {
        case .rejected: return [.couldNotMake(failed: 1, total: 1)]
        case .failed: return [.couldNotMake(failed: 1, total: 1)]
        case .uncertain: return [.unsure]
        case .unsaved: return [.didNotSave(count: 1)]
        case .ready(let value) where !matchesApproval(value): return [.changedAfterStart]
        default: return []
        }
    }

    private var handlers: OverlayProblemHandlers {
        OverlayProblemHandlers(
            tryAgain: { retry() },
            editPrompt: draft.submissionRejection != nil || !narration ? { editRequest() } : nil,
            checkAgain: { checkAgain() },
            saveAgain: { saveAgain() },
            startAgain: { startAgain() },
            close: { dismiss() })
    }

    @ViewBuilder private var mainContent: some View {
        switch stage {
        case .compose, .rejected: composeContent
        case .making: makingContent
        case .ready(let value): readyContent(value)
        case .failed, .unsaved, .uncertain:
            if narration { scriptCard(full: true) } else { promptSummary }
        }
    }

    @ViewBuilder private var composeContent: some View {
        if narration {
            voiceDisclosure
            scriptCard(full: true)
        } else {
            if let source = draft.source, editing {
                HStack(spacing: 12) {
                    OverlayThumbnail(url: source.previewURL ?? source.displayURL, phase: .ready, width: 64, height: 96, radius: 12)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Editing this image").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        Text(source.sourceFilename ?? "Image").kriaType(.button).foregroundStyle(KriaColor.ink)
                    }
                }
            }
            KriaField(label: editing ? "Describe what to change" : "Describe your image", text: $draft.prompt,
                      placeholder: editing ? "Describe what to change" : "Describe the image you want",
                      helper: embedded ? "Generating uses your account’s allowance." : "Images come without words or titles. Add text in the editor.",
                      maxCount: embedded ? nil : OverlayLimits.maxPrompt, isMultiline: true, minHeight: embedded ? 76 : 132)
                .disabled(busy || draft.submissionRejection != nil)
                .accessibilityIdentifier("generated-media-prompt")
            if !embedded {
                KriaField(label: "Name", text: $draft.title, placeholder: "Name (optional)", helper: "Helps Kria know where the image fits.")
                    .disabled(busy || draft.submissionRejection != nil)
                    .accessibilityIdentifier("generated-media-title")
            }
        }
    }

    private var voiceDisclosure: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle().fill(KriaColor.sky)
                Image(systemName: "waveform").font(.system(size: 13, weight: .bold)).foregroundStyle(KriaColor.ink)
            }
            .frame(width: 28, height: 28)
            Text("This voice is AI-generated. Listen before you use it.")
                .kriaType(.button).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(14)
        .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private func scriptCard(full: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Your script").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            Text(draft.script?.text ?? "").kriaType(.body).foregroundStyle(KriaColor.ink)
                .lineLimit(full || showsFullScript ? nil : 3)
                .textSelection(.enabled)
            if !full {
                Button(showsFullScript ? "Show less" : "Show full script") { showsFullScript.toggle() }
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium))
                    .padding(.leading, -12)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    private var promptSummary: some View {
        Text(draft.submittedRequest?.prompt ?? draft.prompt).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            .lineLimit(3)
    }

    @ViewBuilder private var makingContent: some View {
        if narration {
            VStack(spacing: 8) {
                if stalled {
                    Text("This is taking longer than usual.").kriaType(.button).foregroundStyle(KriaColor.ink)
                } else {
                    KriaSpinner(size: 20)
                    Text("Making your AI voice…").kriaType(.button).foregroundStyle(KriaColor.ink)
                }
                Text("You can close this. It keeps going.").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                if stalled { checkAgainButton }
            }
            .padding(.vertical, 28).frame(maxWidth: .infinity)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        } else {
            VStack(spacing: 16) {
                imageFrame(url: nil, ready: false)
                VStack(spacing: 4) {
                    Text(embedded ? " " : (stalled ? "This is taking longer than usual." : "Making your image…"))
                        .kriaType(.button).foregroundStyle(KriaColor.ink)
                    Text("You can close this. It keeps going.").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
                if stalled { checkAgainButton }
                promptSummary
            }
            .frame(maxWidth: .infinity)
            .accessibilityIdentifier("generated-media-status")
        }
    }

    private var checkAgainButton: some View {
        Button("Check again") { checkAgain() }
            .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
            .accessibilityIdentifier("generated-media-check-again")
    }

    private func imageFrame(url: URL?, ready: Bool) -> some View {
        let width: CGFloat = embedded ? 75 : 240
        // Use stays off until the picture itself has loaded; a Reload frame never counts as seen.
        return OverlayThumbnail(url: url, phase: ready ? .ready : .making, width: width, height: width * 1.5, radius: 16,
                                onLoad: { previewLoaded = $0 })
            .accessibilityIdentifier("generated-media-preview")
    }

    @ViewBuilder private func readyContent(_ value: GeneratedMediaOperation) -> some View {
        if narration {
            if let url = value.previewURL ?? value.sourceURL {
                voicePlayer(url: url)
            }
            voiceDisclosure
            scriptCard(full: false)
        } else {
            VStack(spacing: 12) {
                imageFrame(url: value.previewURL ?? value.sourceURL, ready: true)
                if !embedded { promptSummary }
            }
            .frame(maxWidth: .infinity)
        }
    }

    private func voicePlayer(url: URL) -> some View {
        VStack(alignment: .trailing, spacing: 12) {
            HStack(spacing: 12) {
                Button { voice.toggle() } label: {
                    Image(systemName: voice.isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 15, weight: .bold)).foregroundStyle(KriaColor.paper)
                        .frame(width: 44, height: 44).background(KriaColor.ink, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(voice.isPlaying ? "Pause AI voice" : "Play AI voice")
                .accessibilityIdentifier("generated-media-play")
                NarrationWaveform(progress: voice.progress)
            }
            Text("\(NarrationPlayer.clock(voice.elapsed)) / \(NarrationPlayer.clock(voice.duration))")
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
        }
        .padding(16)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        // AVPlayer status establishes that the signed audio can actually be played.
        .task(id: url) {
            previewLoaded = await voice.load(url: url)
            if !previewLoaded { problem = .unsure }
        }
    }

    // MARK: Actions

    private var hasPinnedActions: Bool {
        switch stage {
        case .making: false
        case .failed, .unsaved, .uncertain: false
        default: true
        }
    }

    private var canUse: Bool {
        guard case .ready(let value) = stage else { return false }
        return !busy && allowsUse && value.canAccept && matchesApproval(value) && previewLoaded
    }

    @ViewBuilder private var pinnedActions: some View {
        switch stage {
        case .ready:
            Button { if case .ready(let value) = stage { accept(value) } } label: { Text(useTitle) }
                .buttonStyle(KriaButtonStyle(role: activeProblems.contains(where: \.isFailure) ? .ink : .primary, fullWidth: true, isLoading: busy))
                .disabled(!canUse)
                .accessibilityIdentifier("generated-media-use")
                .kriaDisabledReason(canUse || busy ? nil : (previewLoaded ? nil : "Loading your \(narration ? "voice" : "image")…"))
            if allowsNewGeneration {
                HStack(spacing: 12) {
                    Button("Try again") { retry() }
                        .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
                        .disabled(busy).accessibilityIdentifier("generated-media-try-again")
                    Button("Discard") { discard() }
                        .buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium, fullWidth: true))
                        .disabled(busy).accessibilityIdentifier("generated-media-discard")
                }
            }
        default:
            Button(submitTitle) { submit() }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: busy))
                .disabled(busy || !allowsNewGeneration || draft.submissionRejection != nil || (!narration && (promptIsEmpty || promptIsTooLong)))
                .accessibilityIdentifier("generated-media-submit")
                .kriaDisabledReason(!narration && draft.submissionRejection == nil ? (promptIsEmpty ? "Describe your image to start." : (promptIsTooLong ? "Shorten your prompt to start." : nil)) : nil)
        }
    }

    /// The embedded editor panel has no pinned bar, so the same actions sit inline.
    @ViewBuilder private var inlineActions: some View {
        if hasPinnedActions { VStack(spacing: 8) { pinnedActions } }
        else if case .failed = stage { Button("Discard") { discard() }.buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium)) }
    }

    private var useTitle: String {
        if narration { return "Use AI voice" }
        if embedded { return "Add to timeline" }
        return editing ? "Use edit" : "Use image"
    }
    private var submitTitle: String {
        if narration { return "Approve script and make voice" }
        return editing ? "Make edit" : "Generate image"
    }

    private func matchesApproval(_ value: GeneratedMediaOperation) -> Bool {
        value.capability == draft.capability && value.matchesApproval(sourceAssetID: draft.source?.id,
            sourceGeneration: draft.source?.sourceGeneration, script: draft.script)
    }

    private func discard() {
        pollTask?.cancel(); voice.stop()
        draft.clearRecovery()
        problem = nil; previewLoaded = false; stalled = false
    }

    /// One tap runs again: a failed or unwanted result is replaced by a new request with the same prompt.
    private func retry() {
        guard !busy, allowsNewGeneration else { return }
        discard()
        if draft.submissionRejection != nil { editRequest(); return }
        submit()
    }

    private func checkAgain() {
        guard !busy else { return }
        pollTask?.cancel()
        problem = nil; stalled = false
        pollTask = Task { await restore() }
    }

    private func saveAgain() {
        guard !busy, let operation = draft.operation else { return }
        busy = true
        pollTask?.cancel()
        pollTask = Task {
            defer { busy = false }
            do {
                draft.rememberOperation(try await api.retryGeneratedMediaStorage(itemID: itemID, operationID: operation.id))
                problem = nil
                busy = false
                await poll()
            } catch { problem = OverlayProblem.classifyRequest(error).isSetWide ? OverlayProblem.classifyRequest(error) : .didNotSave(count: 1) }
        }
    }

    private func startAgain() {
        discard()
        editRequest()
    }

    private func submit() {
        guard !busy, allowsNewGeneration, draft.recovery == nil, draft.submissionRejection == nil else { return }
        draft.rememberRequest()
        guard let recovery = draft.recovery else { return }
        let request = draft.submittedRequest ?? GeneratedMediaRequest(
            clientEventID: recovery.clientEventID, capability: draft.capability,
            prompt: draft.script?.text ?? draft.prompt, transparentBackground: draft.transparentBackground, editableTitle: draft.title,
            sourceAssetID: draft.source?.id, sourceGeneration: draft.source?.sourceGeneration,
            scriptID: draft.script?.scriptID, scriptVersion: draft.script?.scriptVersion,
            approvedScriptSHA256: draft.script?.approvedScriptSHA256
        )
        draft.submittedRequest = request
        busy = true
        problem = nil
        stalled = false
        pollTask = Task {
            do {
                draft.rememberOperation(try await api.generateMedia(itemID: itemID, body: request))
                busy = false
                await poll()
            } catch {
                busy = false
                let found = OverlayProblem.classifyRequest(error)
                if found == .sessionEnded || found == .updateRequired {
                    draft.clearRecovery()
                    problem = found
                } else if draft.handleSubmissionFailure(error) {
                    problem = nil
                } else {
                    problem = found
                }
            }
        }
    }

    /// Reopens the form with the source or script refreshed, so the next request is made against the newest one.
    private func editRequest() {
        guard !busy else { return }
        if draft.recovery != nil { discard() }
        busy = true
        pollTask = Task {
            defer { busy = false }
            do {
                switch draft.capability {
                case .image: break
                case .imageEdit:
                    let visuals = try await api.visuals(itemID: itemID)
                    guard let source = visuals.assets.first(where: { $0.id == draft.source?.id && $0.status == "ready" }) else {
                        problem = .sourceGone
                        return
                    }
                    draft.source = source
                case .narration:
                    draft.script = try await api.generatedNarrationScript(itemID: itemID)
                }
                draft.resetRejectedRequest()
                problem = nil
            } catch { problem = OverlayProblem.classifyRequest(error) == .offline ? .offline : .unsure }
        }
    }

    private func restore() async {
        guard let recovery = draft.recovery, !busy else { return }
        busy = true
        do {
            let value: GeneratedMediaOperation
            if let operationID = recovery.operationID {
                value = try await api.generatedMedia(itemID: itemID, operationID: operationID)
            } else {
                value = try await api.generatedMediaByEvent(itemID: itemID, clientEventID: recovery.clientEventID)
            }
            switch draft.capability {
            case .image: break
            case .imageEdit:
                let visuals = try await api.visuals(itemID: itemID)
                draft.source = visuals.assets.first { $0.id == value.sourceAssetID && $0.status == "ready" }
            case .narration:
                draft.script = try await api.generatedNarrationScript(itemID: itemID)
            }
            draft.rememberOperation(value)
            problem = nil
            busy = false
            await poll()
        } catch {
            busy = false
            problem = OverlayProblem.classifyRequest(error) == .offline ? .offline : .unsure
        }
    }

    private func poll() async {
        for _ in 0..<60 {
            guard let value = draft.operation, value.shouldPoll, !Task.isCancelled else { return }
            do {
                try await Task.sleep(for: .seconds(3))
                draft.rememberOperation(try await api.generatedMedia(itemID: itemID, operationID: value.id))
            } catch is CancellationError { return }
            catch { problem = OverlayProblem.classifyRequest(error) == .offline ? .offline : .unsure; return }
        }
        if draft.operation?.shouldPoll == true { stalled = true }
    }

    private func accept(_ value: GeneratedMediaOperation) {
        guard canUse else { return }
        busy = true
        pollTask?.cancel()
        pollTask = Task {
            defer { busy = false }
            do {
                _ = try await api.acceptGeneratedMedia(itemID: itemID, operationID: value.id,
                    body: GeneratedMediaAcceptance(scriptID: draft.script?.scriptID, scriptVersion: draft.script?.scriptVersion,
                                                   approvedScriptSHA256: draft.script?.approvedScriptSHA256))
                if let useImage { try await useImage(value) }
                draft.clearRecovery()
                await accepted()
                if !embedded { dismiss() }
            } catch {
                let found = OverlayProblem.classifyRequest(error)
                problem = found.isSetWide ? found : .couldNotAdd
            }
        }
    }
}

/// Plays the generated AI voice and reports where it is, so the player card can show a scrub position.
@MainActor final class NarrationPlayer: ObservableObject {
    @Published private(set) var isPlaying = false
    @Published private(set) var elapsed: Double = 0
    @Published private(set) var duration: Double = 0
    private var player: AVPlayer?
    private var observer: Any?
    private var ended: NSObjectProtocol?

    var progress: Double { duration > 0 ? min(1, elapsed / duration) : 0 }

    static func clock(_ seconds: Double) -> String {
        let whole = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }

    func load(url: URL) async -> Bool {
        stop()
        let asset = AVURLAsset(url: url)
        do {
            guard try await asset.load(.isPlayable) else { return false }
            let length = try await asset.load(.duration).seconds
            duration = length.isFinite ? length : 0
        } catch { return false }
        let item = AVPlayerItem(asset: asset)
        let playback = AVPlayer(playerItem: item)
        observer = playback.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.1, preferredTimescale: 600), queue: .main) { [weak self] time in
            let seconds = time.seconds
            Task { @MainActor in self?.elapsed = seconds.isFinite ? seconds : 0 }
        }
        ended = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
            Task { @MainActor in
                self?.isPlaying = false
                self?.player?.seek(to: .zero)
                self?.elapsed = 0
            }
        }
        player = playback
        return true
    }

    func toggle() {
        guard let player else { return }
        if isPlaying { player.pause() } else { player.play() }
        isPlaying.toggle()
    }

    func stop() {
        player?.pause()
        if let observer { player?.removeTimeObserver(observer) }
        if let ended { NotificationCenter.default.removeObserver(ended) }
        observer = nil; ended = nil; player = nil
        isPlaying = false; elapsed = 0
    }
}

/// 36 bars: ink where it has played, ink at 30% where it has not.
private struct NarrationWaveform: View {
    let progress: Double
    private static let heights: [CGFloat] = [8, 14, 22, 12, 28, 34, 20, 10, 26, 38, 30, 16, 24, 40, 32, 18, 10, 22,
                                             36, 28, 14, 20, 34, 26, 12, 18, 30, 24, 10, 16, 22, 12, 8, 14, 10, 6]

    var body: some View {
        HStack(spacing: 3.5) {
            ForEach(Self.heights.indices, id: \.self) { index in
                Capsule()
                    .fill(Double(index) / Double(Self.heights.count) < progress ? KriaColor.ink : KriaColor.ink.opacity(0.3))
                    .frame(width: 3.5, height: Self.heights[index])
            }
        }
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .accessibilityHidden(true)
    }
}
