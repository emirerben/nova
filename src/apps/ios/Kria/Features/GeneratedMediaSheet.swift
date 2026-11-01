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
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var error: String?
    @State private var previewLoaded = false
    @State private var previewPlayed = false
    @State private var player: AVPlayer?
    @State private var pollTask: Task<Void, Never>?

    private var narration: Bool { draft.capability == .narration }
    private var heading: String { narration ? "Generate narration" : (draft.capability == .imageEdit ? "Edit image" : "Generate image") }

    private var api: any KriaAPIClient { apiOverride ?? model.api }

    var body: some View {
        Group {
            if embedded { contents }
            else {
                NavigationStack {
                    ScrollView { contents.padding(24) }
                        .navigationTitle(heading)
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
                        .interactiveDismissDisabled(busy)
                }
            }
        }
        .onDisappear { pollTask?.cancel(); player?.pause() }
        .onAppear {
            pollTask = Task { await restore() }
        }
    }

    private var contents: some View {
        VStack(alignment: .leading, spacing: embedded ? 10 : 18) {
            if narration {
                Text("This voice is AI-generated. Review the script and listen before using it.")
                Text(draft.script?.text ?? "").textSelection(.enabled)
            } else {
                if !embedded {
                    TextField("Image title", text: $draft.title)
                        .accessibilityIdentifier("generated-media-title").disabled(draft.recovery != nil || draft.submissionRejection != nil)
                }
                // The exact prompt that will be paid for is always visible before the first request.
                TextField(draft.source == nil ? "Describe your image" : "Describe the changes", text: $draft.prompt, axis: .vertical)
                    .lineLimit(embedded ? 2...6 : 3...8).accessibilityIdentifier("generated-media-prompt").disabled(draft.recovery != nil || draft.submissionRejection != nil)
                if embedded && draft.recovery == nil && draft.submissionRejection == nil {
                    Text("Generating uses your account’s allowance.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            if draft.recovery == nil, draft.submissionRejection == nil {
                Button(narration ? "Approve script and generate AI voice" : "Generate preview") { submit() }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(busy || !allowsNewGeneration || (!narration && draft.prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                    .accessibilityIdentifier("generated-media-submit")
            }
            if let rejection = draft.submissionRejection {
                Text(rejection).foregroundStyle(KriaColor.failureText)
                Button("Edit request") { editRejectedRequest() }.disabled(busy)
                    .accessibilityIdentifier("generated-media-edit-rejected")
            }
            if let operation = draft.operation { operationView(operation) }
            else if draft.recovery != nil && !busy {
                Text("Check your existing preview without generating again.")
                Button("Check status") { pollTask = Task { await restore() } }.disabled(busy)
            }
            if busy { ProgressView("Preparing preview…") }
            if let error { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("generated-media-error") }
            if draft.recovery != nil && draft.operation?.state != .ready && draft.operation?.state != .failed {
                Text("Leaving this preview keeps your generation request for later.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            }
        }
    }

    private func matchesApproval(_ value: GeneratedMediaOperation) -> Bool {
        value.capability == draft.capability && value.matchesApproval(sourceAssetID: draft.source?.id,
            sourceGeneration: draft.source?.sourceGeneration, script: draft.script)
    }

    @ViewBuilder private func operationView(_ value: GeneratedMediaOperation) -> some View {
        if value.state == .ready, let url = value.previewURL ?? value.sourceURL {
            if narration {
                Button("Play AI voice preview") {
                    player?.pause()
                    let playback = AVPlayer(url: url)
                    player = playback
                    playback.play()
                    previewPlayed = true
                }
                .accessibilityIdentifier("generated-media-play")
                // AVPlayer status establishes that the signed audio can actually be played.
                .task(id: url) {
                    do {
                        let playable = try await AVURLAsset(url: url).load(.isPlayable)
                        previewLoaded = playable
                    } catch { previewLoaded = false; self.error = "Couldn’t load the voice preview. Check status to refresh it." }
                }
            } else {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().scaledToFit().frame(maxWidth: .infinity, maxHeight: embedded ? 112 : 480).onAppear { previewLoaded = true }
                    case .failure:
                        Text("Couldn’t load the image preview. Check status to refresh it.").onAppear { previewLoaded = false }
                    default: ProgressView("Loading preview…")
                    }
                }
                .accessibilityIdentifier("generated-media-preview")
            }
            if embedded && allowsNewGeneration {
                Button("Try another look") {
                    guard !busy else { return }
                    draft.clearRecovery()
                    previewLoaded = false
                    error = nil
                    submit()
                }.disabled(busy)
            }
            Button { accept(value) } label: {
                Text(narration ? "Use AI voice" : embedded ? "Add to timeline" : "Use image")
                    .frame(maxWidth: embedded ? .infinity : nil)
            }
                .buttonStyle(KriaPrimaryButtonStyle())
                .disabled(busy || !allowsUse || !value.canAccept || !matchesApproval(value) || !previewLoaded || (narration && !previewPlayed))
                .accessibilityIdentifier("generated-media-use")
        } else {
            Text(statusText(value))
                .accessibilityIdentifier("generated-media-status")
        }
        if value.state == .ready, !matchesApproval(value) {
            Text("The source or script changed. Discard this preview and approve the latest version before generating again.")
                .foregroundStyle(KriaColor.failureText)
        }
        if (!embedded && value.state == .ready) || value.state == .failed {
            Button("Discard preview") { pollTask?.cancel(); draft.clearRecovery(); error = nil; previewLoaded = false; previewPlayed = false }
                .disabled(busy)
        }
        if value.canRetryStorage {
            Button("Retry saving preview") {
                guard !busy else { return }
                busy = true
                pollTask?.cancel()
                pollTask = Task {
                    defer { busy = false }
                    do {
                        draft.rememberOperation(try await api.retryGeneratedMediaStorage(itemID: itemID, operationID: value.id))
                        error = nil
                        busy = false
                        await poll()
                    } catch { self.error = "Couldn’t save the preview. No new generation was requested." }
                }
            }.disabled(busy)
        }
        if !embedded || value.state != .ready || !previewLoaded {
            Button(embedded && value.state == .ready ? "Refresh preview" : "Check status") {
                guard !busy else { return }
                pollTask?.cancel()
                pollTask = Task { await restore() }
            }.disabled(busy)
        }
    }

    private func statusText(_ value: GeneratedMediaOperation) -> String {
        switch value.state {
        case .pending, .submitting: "Generating your preview…"
        case .providerComplete: "The preview was generated and is being saved."
        case .ready: "The preview is not available yet. Check status again."
        case .unknown: "The provider’s result is uncertain. Generation will not be retried automatically. Check status later."
        case .failed: "Generation could not finish. No replacement generation has been started."
        }
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
        error = nil
        pollTask = Task {
            do {
                draft.rememberOperation(try await api.generateMedia(itemID: itemID, body: request))
                busy = false
                await poll()
            } catch {
                busy = false
                if draft.handleSubmissionFailure(error) { self.error = nil }
                else { self.error = "Couldn’t confirm the request. Check it again before starting another generation." }
            }
        }
    }

    private func editRejectedRequest() {
        guard !busy else { return }
        busy = true
        pollTask = Task {
            defer { busy = false }
            do {
                switch draft.capability {
                case .image: break
                case .imageEdit:
                    let visuals = try await api.visuals(itemID: itemID)
                    guard let source = visuals.assets.first(where: { $0.id == draft.source?.id && $0.status == "ready" }) else {
                        error = "This source is no longer available. Close this sheet and choose another image."
                        return
                    }
                    draft.source = source
                case .narration:
                    draft.script = try await api.generatedNarrationScript(itemID: itemID)
                }
                draft.resetRejectedRequest()
                error = nil
            } catch { self.error = "Couldn’t refresh the source or script. Try again when connected." }
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
            error = nil
            busy = false
            await poll()
        } catch {
            busy = false
            self.error = "The result is not confirmed. Check status later. No new generation was requested."
        }
    }

    private func poll() async {
        for _ in 0..<60 {
            guard let value = draft.operation, value.shouldPoll, !Task.isCancelled else { return }
            do {
                try await Task.sleep(for: .seconds(3))
                draft.rememberOperation(try await api.generatedMedia(itemID: itemID, operationID: value.id))
            } catch is CancellationError { return }
            catch { self.error = "Couldn’t check status. Check again when connected."; return }
        }
    }

    private func accept(_ value: GeneratedMediaOperation) {
        guard !busy, allowsUse, value.canAccept, matchesApproval(value), previewLoaded, !narration || previewPlayed else { return }
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
                self.error = useImage == nil
                    ? "Couldn’t use this preview. If the script changed, approve its latest version first."
                    : "Couldn’t place this overlay. The generated preview is preserved. If your edit changed, close and plan again."
            }
        }
    }
}
