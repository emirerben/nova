import SwiftUI

struct OverlayGenerationSheet: View {
    @ObservedObject var session: NativeEditorSession
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dismiss) private var dismiss
    @State private var mode: OverlayGenerationMode = .prompt
    @State private var prompt = ""
    @State private var tasteNotes = ""
    @State private var plan: OverlayPlan?
    @State private var previousPlans: [OverlayPlan] = []
    @State private var busy = false
    @State private var error: String?
    @State private var generationAvailable = false
    @State private var generatedDraft: GeneratedMediaDraft?
    @State private var generationRowID: String?
    @State private var work: Task<Void, Never>?

    private struct SavedDraft: Codable {
        let mode: OverlayGenerationMode
        let prompt: String
        let tasteNotes: String
        let plan: OverlayPlan?
        var previousPlans: [OverlayPlan]? = nil
    }

    private var recoveryKey: String? {
        guard let accountID = auth.localStorageIdentity, let scope = session.overlayPlanScope else { return nil }
        return "overlay-plan." + accountID + "." + scope
    }
    private var stale: Bool {
        guard let plan else { return false }
        return plan.baseGeneration != session.document.revision.baseGeneration
            || plan.documentFingerprint != (try? session.overlayDocumentFingerprint())
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Create supporting graphics for your footage. Review each overlay before adding it.")
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                    Picker("Generation mode", selection: $mode) {
                        ForEach(OverlayGenerationMode.allCases) { Text($0.title).tag($0) }
                    }.pickerStyle(.inline).disabled(busy)
                        .accessibilityIdentifier("overlay-generation-mode")
                    if mode == .prompt {
                        TextField("For example, show 10 when I say number ten", text: $prompt, axis: .vertical)
                            .lineLimit(3...7).disabled(busy)
                            .accessibilityIdentifier("overlay-generation-prompt")
                    } else {
                        Text("Kria uses the speech and available visual context to propose a few relevant overlays. It can also choose to add none.")
                            .font(KriaFont.body(13))
                    }
                    DisclosureGroup("Style preferences") {
                        TextField("For example, simple cutout icons, warm colors, fewer overlays", text: $tasteNotes, axis: .vertical)
                            .lineLimit(2...5).disabled(busy)
                            .accessibilityIdentifier("overlay-generation-taste")
                    }
                    Button(plan == nil ? "Plan overlays" : "Plan again") { createPlan() }
                        .buttonStyle(KriaPrimaryButtonStyle())
                        .disabled(busy || (mode == .prompt && prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) || prompt.count > 1500 || tasteNotes.count > 800)
                        .accessibilityIdentifier("overlay-generation-plan")
                    if !previousPlans.isEmpty {
                        Menu("Previous plans and previews") {
                            ForEach(previousPlans.indices, id: \.self) { index in
                                Button(previousPlans[index].overlays.first?.title ?? "Earlier plan") {
                                    let earlier = previousPlans.remove(at: index)
                                    if let plan { previousPlans.append(plan) }
                                    plan = earlier
                                    persist()
                                }
                            }
                        }.disabled(busy)
                    }
                    if busy {
                        ProgressView("Preparing overlays…")
                        Button("Stop waiting") { work?.cancel(); persist() }
                    }
                    if let plan {
                        if !plan.message.isEmpty { Text(plan.message).font(KriaFont.body(13)) }
                        Text("Review placement over your footage. Keep graphics clear of faces, captions and important screen content.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        if stale {
                            Text("This edit changed after planning. Plan again before adding an overlay. Your existing graphics are preserved.")
                                .foregroundStyle(KriaColor.failureText)
                                .accessibilityIdentifier("overlay-generation-stale")
                        }
                        ForEach(Array(plan.overlays.enumerated()), id: \.element.id) { index, proposal in
                            proposalView(proposal, index: index)
                        }
                    }
                    if !generationAvailable {
                        Text("Image generation is currently unavailable for this account. You can still review plans and use text or uploaded graphics.")
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    } else {
                        Text("Generating an image uses your account's generation allowance. Planning does not generate images.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                    if let error { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("overlay-generation-error") }
                }.padding(24)
            }
            .navigationTitle("Generate media")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { persist(); dismiss() } } }
            .onDisappear { work?.cancel(); persist() }
            .onChange(of: prompt) { _, _ in persist() }
            .onChange(of: tasteNotes) { _, _ in persist() }
            .onChange(of: mode) { _, _ in persist() }
            .task {
                restore()
                do { generationAvailable = try await model.api.creationCapabilities().generatedImages == true }
                catch { self.error = "Couldn’t check image availability. Reopen when connected to check again." }
            }
            .sheet(item: $generatedDraft) { draft in
                if let itemID = session.visualItemID {
                    GeneratedMediaSheet(itemID: itemID, draft: draft, accepted: {}, useImage: { operation in
                        guard let plan, let row = plan.overlays.first(where: { $0.id == generationRowID }),
                              let assetID = operation.assetID else { throw APIError.invalidResponse }
                        try await session.applyOverlayProposal(row, assetID: assetID, plan: plan)
                        try recordApplied()
                    }, allowsNewGeneration: !stale && generationAvailable, allowsUse: !stale)
                }
            }
        }
    }

    private func proposalView(_ proposal: OverlayProposal, index: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(proposal.title).font(KriaFont.body(16).weight(.semibold))
            Text(proposal.reason).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            if session.containsOverlayProposal(proposal.id) {
                Label("Added to your edit", systemImage: "checkmark.circle")
                Text("Close to preview, adjust its placement or timing, or undo in the editor.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
            } else {
                if proposal.kind == .image {
                    TextField("Graphic description", text: rowBinding(index, \.prompt, fallback: proposal.prompt), axis: .vertical)
                        .lineLimit(2...5)
                } else if proposal.kind == .text {
                    TextField("Overlay text", text: rowBinding(index, \.text, fallback: proposal.text))
                }
                HStack {
                    Text("Start")
                    TextField("Start seconds", value: timeBinding(index, \.startS, fallback: proposal.startS), format: .number.precision(.fractionLength(2)))
                        .keyboardType(.decimalPad).accessibilityLabel("Overlay start in seconds")
                    Text("End")
                    TextField("End seconds", value: timeBinding(index, \.endS, fallback: proposal.endS), format: .number.precision(.fractionLength(2)))
                        .keyboardType(.decimalPad).accessibilityLabel("Overlay end in seconds")
                }
                HStack {
                    Text("Horizontal")
                    Slider(value: rowBinding(index, \.xFrac, fallback: proposal.xFrac), in: 0.1...0.9)
                        .accessibilityLabel("Overlay horizontal position")
                }
                HStack {
                    Text("Vertical")
                    Slider(value: rowBinding(index, \.yFrac, fallback: proposal.yFrac), in: 0.1...0.9)
                        .accessibilityLabel("Overlay vertical position")
                }
                HStack {
                    Text("Size")
                    Slider(value: rowBinding(index, \.scale, fallback: proposal.scale), in: 0.1...0.5)
                        .accessibilityLabel("Overlay size")
                }
                Button(proposal.kind == .image ? "Generate and review image" : "Add overlay") { use(proposal) }
                    .buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(busy || ((stale || !proposal.isValid(duration: session.overlayBaseDuration) || (proposal.kind == .image && !generationAvailable)) && !hasRecoverablePreview(proposal)))
                    .accessibilityIdentifier("overlay-generation-use-" + proposal.id)
            }
        }
        .padding(16)
        .background(KriaColor.line.opacity(0.2), in: RoundedRectangle(cornerRadius: 12))
    }

    private func rowBinding<T>(_ index: Int, _ path: WritableKeyPath<OverlayProposal, T>, fallback: T) -> Binding<T> {
        Binding(get: {
            guard let plan, plan.overlays.indices.contains(index) else { return fallback }
            return plan.overlays[index][keyPath: path]
        }, set: { value in
            guard plan?.overlays.indices.contains(index) == true else { return }
            plan?.overlays[index][keyPath: path] = value
            persist()
        })
    }

    private func timeBinding(_ index: Int, _ path: WritableKeyPath<OverlayProposal, Double>, fallback: Double) -> Binding<Double> {
        let base = rowBinding(index, path, fallback: fallback)
        return Binding(get: { session.overlayOutputTime(base.wrappedValue) }, set: { outputTime in
            guard let value = session.overlayBaseTime(outputTime) else {
                error = "Choose a time outside the inserted carousel. Overlays within that insert are not supported yet."
                return
            }
            base.wrappedValue = value
        })
    }

    private func createPlan() {
        guard !busy else { return }
        busy = true
        error = nil
        work = Task { @MainActor in
            defer { busy = false }
            do {
                let value = try await session.requestOverlayPlan(mode: mode, prompt: prompt, tasteNotes: tasteNotes)
                try Task.checkCancellation()
                if let plan { previousPlans.append(plan) }
                plan = value
                persist()
            } catch is CancellationError { }
            catch { self.error = "Couldn’t plan overlays. " + error.localizedDescription }
        }
    }

    private func use(_ row: OverlayProposal) {
        guard let plan, !busy, !stale || hasRecoverablePreview(row) else { return }
        if row.kind == .image {
            guard let accountID = auth.localStorageIdentity, let itemID = session.visualItemID else { return }
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
            } catch is CancellationError { }
            catch { self.error = "Couldn’t add this overlay. Your preview is preserved. " + error.localizedDescription }
        }
    }

    private func recordApplied() throws {
        plan?.documentFingerprint = try session.overlayDocumentFingerprint()
        persist()
    }

    private func hasRecoverablePreview(_ row: OverlayProposal) -> Bool {
        guard row.kind == .image, let accountID = auth.localStorageIdentity, let itemID = session.visualItemID else { return false }
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
