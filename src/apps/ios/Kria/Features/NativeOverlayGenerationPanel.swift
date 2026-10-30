import SwiftUI

struct NativeOverlayGenerationPanel: View {
    @ObservedObject var session: NativeEditorSession
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var auth: AuthStore
    let onBack: () -> Void
    let onReference: () -> Void
    let onAdded: (OverlayProposal) -> Void
    @FocusState private var editingPrompt: Bool
    @State private var showsTastePreferences = false
    @State private var mode: OverlayGenerationMode = .prompt
    @State private var prompt = ""
    @State private var tasteNotes = ""
    @State private var plan: OverlayPlan?
    @State private var previousPlans: [OverlayPlan] = []
    @State private var busy = false
    @State private var error: String?
    private enum Availability { case checking, available, unavailable, failed }
    @State private var availability: Availability = .checking
    @State private var generatedDraft: GeneratedMediaDraft?
    @State private var generationRowID: String?
    @State private var work: Task<Void, Never>?
    @State private var availabilityTask: Task<Void, Never>?

    private var generationAvailable: Bool { availability == .available }

    private struct SavedDraft: Codable {
        let mode: OverlayGenerationMode
        let prompt: String
        let tasteNotes: String
        let plan: OverlayPlan?
        var previousPlans: [OverlayPlan]? = nil
    }

    private var recoveryKey: String? {
        guard let accountID = recoveryAccountID, let scope = session.overlayPlanScope else { return nil }
        return "overlay-plan." + accountID + "." + scope
    }
    private var stale: Bool {
        guard let plan else { return false }
        return plan.baseGeneration != session.document.revision.baseGeneration
            || plan.documentFingerprint != (try? session.overlayDocumentFingerprint())
    }

    private var recoveryAccountID: String? {
        if let owner = auth.localStorageIdentity { return owner }
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-overlays") { return "overlay-ui-fixture" }
        #endif
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button { persist(); onBack() } label: { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                    .accessibilityLabel("Back to media").accessibilityIdentifier("overlay-generation-back")
                Text(generatedDraft == nil ? "Generate media" : "Preview").font(KriaFont.body(16).weight(.semibold))
                Spacer()
                if generatedDraft == nil || !previousPlans.isEmpty {
                    Menu {
                        if generatedDraft == nil {
                            Button("Style preferences") { showsTastePreferences.toggle() }
                        }
                        if !previousPlans.isEmpty {
                            Menu("Earlier previews") {
                                ForEach(previousPlans.indices, id: \.self) { index in
                                    Button(previousPlans[index].overlays.first?.title ?? "Earlier plan") {
                                        generatedDraft = nil
                                        let earlier = previousPlans.remove(at: index)
                                        if let plan { previousPlans.append(plan) }
                                        plan = earlier
                                        persist()
                                    }
                                }
                            }
                        }
                    } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44) }
                        .accessibilityLabel("More generation options").disabled(busy)
                }
            }
            if let draft = generatedDraft, let itemID = session.visualItemID,
               let row = plan?.overlays.first(where: { $0.id == generationRowID }) {
                HStack {
                    Text(row.title).font(KriaFont.body(16).weight(.semibold))
                    Spacer()
                    Button("Edit prompt") { generatedDraft = nil }.disabled(busy)
                }
                Text(timing(row)).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                GeneratedMediaSheet(itemID: itemID, draft: draft, accepted: {
                    try? recordApplied()
                    onAdded(row)
                }, useImage: { operation in
                    guard let plan, let assetID = operation.assetID else { throw APIError.invalidResponse }
                    try await session.applyOverlayProposal(row, assetID: assetID, plan: plan)
                }, allowsNewGeneration: !stale && generationAvailable, allowsUse: !stale,
                    embedded: true,
                    apiOverride: session.overlayGenerationAPI)
                Text("Then edit it like any other image.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            } else {
                Picker("Generation mode", selection: $mode) {
                    Text("Follow my prompt").tag(OverlayGenerationMode.prompt)
                    Text("Let Kria choose").tag(OverlayGenerationMode.automatic)
                }.pickerStyle(.segmented).disabled(busy)
                    .accessibilityIdentifier("overlay-generation-mode")
                if mode == .prompt {
                    TextField("Show a football icon when I mention the match", text: $prompt, axis: .vertical)
                        .lineLimit(2...4).padding(14)
                        .background(KriaColor.line.opacity(0.25), in: RoundedRectangle(cornerRadius: 14))
                        .focused($editingPrompt).disabled(busy).accessibilityIdentifier("overlay-generation-prompt")
                } else {
                    Text("Kria chooses a few overlays from your video and finds their moments.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                }
                Button(action: onReference) { Label("Add reference", systemImage: "plus") }
                    .frame(minHeight: 44).font(KriaFont.body(12))
                if showsTastePreferences {
                    TextField("Simple icons, warm colors, fewer overlays", text: $tasteNotes, axis: .vertical)
                        .lineLimit(2...4).disabled(busy).font(KriaFont.body(12))
                }
                Button { createPlan() } label: { Text("Generate preview").frame(maxWidth: .infinity) }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(busy || !generationAvailable || (mode == .prompt && prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) || prompt.count > 1500 || tasteNotes.count > 800)
                    .accessibilityIdentifier("overlay-generation-plan")
                if busy {
                    ProgressView("Finding the right moment…")
                    Button("Stop waiting") { work?.cancel(); persist() }
                }
                if let plan {
                    if !plan.message.isEmpty { Text(plan.message).font(KriaFont.body(13)).accessibilityIdentifier("overlay-generation-message") }
                    ForEach(plan.overlays) { row in
                        proposalView(row)
                    }
                }
                availabilityFooter
            }
            if stale {
                Text("Your edit changed. Generate a new preview before adding. Earlier previews are preserved.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("overlay-generation-stale")
            }
            if let error { Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("overlay-generation-error") }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-overlay-generator")
        .onDisappear { work?.cancel(); availabilityTask?.cancel(); persist() }
        .onChange(of: prompt) { _, _ in persist() }
        .onChange(of: tasteNotes) { _, _ in persist() }
        .onChange(of: mode) { _, _ in persist() }
        .task {
            restore()
            await checkAvailability()
        }
    }

    @ViewBuilder private var availabilityFooter: some View {
        switch availability {
        case .checking:
            Text("Checking availability…").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        case .failed:
            HStack {
                Text("Couldn’t check availability. Try again when connected.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
                Button("Try again") { availabilityTask?.cancel(); availabilityTask = Task { await checkAvailability() } }
                    .frame(minHeight: 44).font(KriaFont.body(12))
                    .accessibilityIdentifier("overlay-generation-availability-retry")
            }
        case .available:
            Text("Planning and image generation use your account’s allowance. Your footage stays as it is.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        case .unavailable:
            Text("Generate media is unavailable for this account.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        }
    }

    private func checkAvailability() async {
        availability = .checking
        do {
            let capabilities = try await (session.overlayGenerationAPI ?? model.api).creationCapabilities()
            guard !Task.isCancelled else { return }
            availability = capabilities.generatedImages == true ? .available : .unavailable
        } catch is CancellationError {
        } catch let error as URLError where error.code == .cancelled {
        } catch {
            guard !Task.isCancelled else { return }
            availability = .failed
        }
    }

    private func timing(_ row: OverlayProposal) -> String {
        let start = session.overlayOutputTime(row.startS).formatted(.number.precision(.fractionLength(1)))
        let end = session.overlayOutputTime(row.endS).formatted(.number.precision(.fractionLength(1)))
        return "Appears at \(start)s to \(end)s"
    }

    private func proposalView(_ row: OverlayProposal) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if row.kind == .text {
                Text(row.text).font(KriaFont.body(32).weight(.bold))
                    .frame(maxWidth: .infinity, minHeight: 76)
                    .background(KriaColor.line.opacity(0.2), in: RoundedRectangle(cornerRadius: 14))
            }
            Text(row.title).font(KriaFont.body(14).weight(.semibold))
            Text(timing(row)).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            Button(session.containsOverlayProposal(row.id) ? "Edit in timeline" : row.kind == .image ? "Review image" : "Add to timeline") { use(row) }
                .buttonStyle(KriaPrimaryButtonStyle())
                .disabled(busy || (!session.containsOverlayProposal(row.id) && (stale || !row.isValid(duration: session.overlayBaseDuration) || (row.kind == .image && !generationAvailable)) && !hasRecoverablePreview(row)))
                .accessibilityIdentifier("overlay-generation-use-" + row.id)
        }.padding(.vertical, 8)
    }

    private func createPlan() {
        guard !busy else { return }
        editingPrompt = false
        busy = true
        error = nil
        work = Task { @MainActor in
            defer { busy = false }
            do {
                let value = try await session.requestOverlayPlan(mode: mode, prompt: mode == .prompt ? prompt : "", tasteNotes: tasteNotes)
                try Task.checkCancellation()
                if let plan { previousPlans.append(plan) }
                plan = value
                persist()
                if let first = value.overlays.first, first.kind == .image, generationAvailable { busy = false; use(first) }
            } catch is CancellationError { }
            catch { self.error = "Couldn’t plan overlays. " + error.localizedDescription }
        }
    }

    private func use(_ row: OverlayProposal) {
        if session.containsOverlayProposal(row.id) { onAdded(row); return }
        guard let plan, !busy, !stale || hasRecoverablePreview(row) else { return }
        if row.kind == .image {
            guard let accountID = recoveryAccountID, let itemID = session.visualItemID else { return }
            let draft = GeneratedMediaDraft(capability: .image)
            draft.transparentBackground = true
            draft.prompt = row.prompt
            draft.title = row.title
            draft.configureRecovery(accountID: accountID, itemID: itemID, placementID: row.id)
            generationRowID = row.id
            generatedDraft = draft
            return
        }
        busy = true
        work = Task { @MainActor in
            defer { busy = false }
            do {
                try await session.applyOverlayProposal(row, assetID: row.assetID, plan: plan)
                try recordApplied()
                onAdded(row)
            } catch is CancellationError { }
            catch { self.error = "Couldn’t add this overlay. Your preview is preserved. " + error.localizedDescription }
        }
    }

    private func recordApplied() throws {
        plan?.documentFingerprint = try session.overlayDocumentFingerprint()
        persist()
    }

    private func hasRecoverablePreview(_ row: OverlayProposal) -> Bool {
        guard row.kind == .image, let accountID = recoveryAccountID, let itemID = session.visualItemID else { return false }
        let draft = GeneratedMediaDraft(capability: .image)
        draft.configureRecovery(accountID: accountID, itemID: itemID, placementID: row.id)
        return draft.recovery != nil
    }

    private func persist() {
        guard let recoveryKey, let data = try? JSONEncoder().encode(SavedDraft(mode: mode, prompt: prompt, tasteNotes: tasteNotes, plan: plan, previousPlans: previousPlans)) else { return }
        UserDefaults.standard.set(data, forKey: recoveryKey)
    }

    private func restore() {
        guard let recoveryKey, let data = UserDefaults.standard.data(forKey: recoveryKey),
              let saved = try? JSONDecoder().decode(SavedDraft.self, from: data) else { return }
        mode = saved.mode
        prompt = saved.prompt
        tasteNotes = saved.tasteNotes
        plan = saved.plan
        previousPlans = saved.previousPlans ?? []
    }
}
