import SwiftUI

/// "Make overlays" (Paper S1.02 to S1.08): one idea, several variations, each overlay with its own prompt.
/// The sheet only renders `GeneratedOverlaySet` and forwards taps to it.
struct GeneratedOverlaysSheet: View {
    let itemID: String
    @ObservedObject var overlays: GeneratedOverlaySet
    let ideaTitle: String
    let accepted: () async -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Group {
            switch overlays.screen {
            case .choose: ChooseScreen(overlays: overlays, ideaTitle: ideaTitle, close: close)
            case .compose: ComposeScreen(overlays: overlays, close: close)
            case .making: MakingScreen(overlays: overlays, close: close)
            case .results: ResultsScreen(overlays: overlays, accepted: accepted, close: close, finish: { dismiss() })
            case .edit(let id): EditScreen(overlays: overlays, cardID: id, close: close).id(id)
            case .remade(let id): RemadeScreen(overlays: overlays, cardID: id, close: close)
            }
        }
        .background(KriaColor.paper)
        .presentationDetents([.large])
        .presentationDragIndicator(.hidden)
    }

    private func close() { dismiss() }
}

// MARK: - Choose (S1.02)

private struct ChooseScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let ideaTitle: String
    let close: () -> Void

    var body: some View {
        KriaSheetScaffold(heading: "Make overlays",
                          message: "Start from one idea. Add variations of it, each with its own prompt.",
                          onClose: close) {
            VStack(spacing: 12) {
                ChoiceCard(symbol: "sparkle", title: "Let Kria decide",
                           detail: "Kria suggests a few variations. You can change any prompt.",
                           identifier: "overlay-choice-decide") {
                    overlays.start(.decide, ideas: OverlayIdeaSource.ideas(for: ideaTitle))
                }
                ChoiceCard(symbol: "pencil", title: "I’ll describe it",
                           detail: "Write one prompt. Add more overlays any time, each with its own.",
                           identifier: "overlay-choice-describe") {
                    overlays.start(.describe)
                }
                Text("Either way, every overlay keeps its own prompt. You can edit, redo or remove any of them afterwards.")
                    .kriaType(.body).foregroundStyle(KriaColor.mutedInk).padding(.top, 8)
            }
        }
    }
}

private struct ChoiceCard: View {
    let symbol: String
    let title: String
    let detail: String
    let identifier: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 16) {
                ZStack {
                    Circle().fill(KriaColor.paper)
                    Image(systemName: symbol).font(.system(size: 20, weight: .regular)).foregroundStyle(KriaColor.ink)
                }
                .frame(width: 48, height: 48)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).kriaType(.button).foregroundStyle(KriaColor.ink)
                    Text(detail).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        .multilineTextAlignment(.leading)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "chevron.right").font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.mutedInk)
            }
            .padding(20)
            .frame(maxWidth: .infinity, minHeight: 64)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(identifier)
    }
}

// MARK: - Compose (S1.03, S1.03b, S1.03c)

private struct ComposeScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let close: () -> Void
    @FocusState private var focus: UUID?

    private var drafts: [OverlayCard] { overlays.drafts }
    private var draftCount: Int { drafts.count }
    private var heading: String { overlays.origin == .decide ? "Kria’s overlay ideas" : "Your overlays" }
    private var message: String {
        if overlays.origin == .decide { return "Variations of one look. Change any prompt or remove the ones you don’t want." }
        return draftCount > 1 ? "Each overlay has its own prompt. Add as many variations as you like."
            : "Describe what you want to see. Add more overlays for variations of it."
    }
    private var canMake: Bool { drafts.allSatisfy(\.promptIsValid) && draftCount > 0 }
    private var reason: String? {
        if canMake { return nil }
        return drafts.contains { $0.prompt.count > OverlayLimits.maxPrompt }
            ? "Shorten the longest prompt to start." : "Describe each overlay to start."
    }

    var body: some View {
        ScrollViewReader { proxy in
            KriaSheetScaffold(heading: heading, message: message, onBack: { overlays.backFromCompose() }, onClose: close) {
                VStack(spacing: 8) {
                    ForEach(overlays.cards.filter(\.isDraft)) { card in
                        PromptCard(number: overlays.number(of: card.id), card: card,
                                   canRemove: overlays.cards.count > 1,
                                   focus: $focus,
                                   setPrompt: { overlays.setPrompt($0, for: card.id) },
                                   remove: { overlays.remove(card.id) })
                            .id(card.id)
                    }
                    AddOverlayRow(isEnabled: overlays.canAddMore) {
                        overlays.addCard()
                        if let last = overlays.cards.last {
                            focus = last.id
                            withAnimation { proxy.scrollTo(last.id, anchor: .center) }
                        }
                    }
                }
            } pinnedBar: {
                Button(makeTitle) { focus = nil; overlays.makeOverlays() }
                    .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                    .disabled(!canMake)
                    .accessibilityIdentifier("generated-media-submit")
                    .kriaDisabledReason(reason)
                if overlays.origin == .decide, canMake {
                    Text("You can edit, redo or remove each one afterwards.")
                        .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
            }
        }
        .onAppear {
            if overlays.origin == .describe, drafts.count == 1, drafts[0].prompt.isEmpty { focus = drafts[0].id }
        }
    }

    private var makeTitle: String { draftCount == 1 ? "Make overlay" : "Make \(draftCount) overlays" }
}

private struct PromptCard: View {
    let number: Int
    let card: OverlayCard
    let canRemove: Bool
    var focus: FocusState<UUID?>.Binding
    let setPrompt: (String) -> Void
    let remove: () -> Void

    private var focused: Bool { focus.wrappedValue == card.id }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Overlay \(number)").kriaType(.button).foregroundStyle(KriaColor.ink)
                    if card.showsCopyHint {
                        Text("Copied from overlay \(card.copiedFrom ?? 1). Edit freely.")
                            .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                Spacer(minLength: 8)
                if canRemove {
                    OverlayIconButton(symbol: "trash", label: "Remove overlay \(number)", identifier: "overlay-remove-\(number)", action: remove)
                }
            }
            .frame(minHeight: 44)
            TextField("Describe the overlay", text: Binding(get: { card.prompt }, set: setPrompt), axis: .vertical)
                .kriaType(.body).foregroundStyle(KriaColor.ink).tint(KriaColor.sky)
                .focused(focus, equals: card.id)
                .padding(.trailing, 8).padding(.bottom, focused ? 0 : 16).padding(.top, 4)
                .accessibilityIdentifier("overlay-prompt-\(number)")
            if focused {
                HStack {
                    Text("\(card.prompt.count) / \(OverlayLimits.maxPrompt)")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
                    Spacer()
                }
                .frame(minHeight: 44)
            }
        }
        .padding(.leading, 16).padding(.trailing, 8).padding(.top, 8).padding(.bottom, focused ? 0 : 0)
        .background(focused ? KriaColor.selectionSoft : KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .onTapGesture { focus.wrappedValue = card.id }
    }
}

private struct AddOverlayRow: View {
    let isEnabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(KriaColor.ink)
                    Image(systemName: "plus").font(.system(size: 16, weight: .bold)).foregroundStyle(KriaColor.paper)
                }
                .frame(width: 44, height: 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Add overlay").kriaType(.button).foregroundStyle(KriaColor.ink)
                    Text(isEnabled ? "Starts from your last prompt" : "You can make up to \(OverlayLimits.maxOverlays) at once")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 10).padding(.leading, 10).padding(.trailing, 16)
            .frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            .opacity(isEnabled ? 1 : 0.5)
        }
        .buttonStyle(.plain)
        .disabled(!isEnabled)
        .accessibilityIdentifier("overlay-add")
    }
}

// MARK: - Making and taking longer (S1.05, S1.06)

private struct MakingScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let close: () -> Void

    private var total: Int { overlays.madeCards.count }
    private var ready: Int { overlays.madeCards.filter { $0.generation?.phase == .ready }.count }
    private var stalled: Bool { overlays.isStalled }
    private var heading: String {
        if stalled { return "Still making your overlays" }
        return total == 1 ? "Making your overlay" : "Making \(total) overlays"
    }
    private var message: String {
        if stalled {
            let waiting = overlays.makingCount
            return "\(ready) of \(total) \(total == 1 ? "is" : "are") ready. \(waiting == 1 ? "The last one is" : "\(waiting) are") taking longer than usual. You can close this."
        }
        return "\(ready) of \(total) \(total == 1 ? "is" : "are") ready. You can close this, it keeps going."
    }

    var body: some View {
        Group {
            if stalled {
                KriaSheetScaffold(heading: heading, message: message, onClose: close) { list } pinnedBar: {
                    HStack(spacing: 12) {
                        Button("Check again") { overlays.checkAgain() }
                            .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
                            .accessibilityIdentifier("overlay-check-again")
                        Button("Discard unfinished") { overlays.discardUnfinished() }
                            .buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium, fullWidth: true))
                            .accessibilityIdentifier("overlay-discard-unfinished")
                    }
                }
            } else {
                KriaSheetScaffold(heading: heading, message: message, onClose: close) { list }
            }
        }
        .onChange(of: ready) { _, value in
            AccessibilityNotification.Announcement("\(value) of \(total) ready").post()
        }
    }

    private var list: some View {
        VStack(spacing: 8) {
            ForEach(overlays.problems, id: \.self) { problem in
                OverlayProblemCard(problem: problem, handlers: .init(
                    tryAgain: { overlays.retryFailed() },
                    editPrompt: { if let id = failedID { overlays.screen = .edit(id) } },
                    checkAgain: { overlays.checkAgain() }, saveAgain: { overlays.saveAgain() }))
            }
            ForEach(overlays.madeCards) { card in
                OverlayRow(number: overlays.number(of: card.id), card: card, style: .status(stalled: card.generation?.stalled == true))
            }
        }
    }

    private var failedID: UUID? { overlays.madeCards.first { $0.generation?.phase == .failed }?.id }
}

// MARK: - Results (S1.07)

private struct ResultsScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let accepted: () async -> Void
    let close: () -> Void
    let finish: () -> Void

    private var count: Int { overlays.readyCount }
    private var heading: String { overlays.madeCards.count == 1 ? "Your overlay" : "Your \(overlays.madeCards.count) overlays" }
    private var useTitle: String { count == 1 ? "Use overlay" : "Use \(count) overlays" }
    private var reason: String? {
        if overlays.makingCount > 0 { return "Waiting for the rest to finish." }
        return count == 0 ? "Nothing is ready to use yet." : nil
    }

    var body: some View {
        KriaSheetScaffold(heading: heading, message: "Each overlay has its own prompt.", onClose: close) {
            VStack(spacing: 8) {
                ForEach(overlays.problems, id: \.self) { problem in
                    OverlayProblemCard(problem: problem, handlers: .init(
                        tryAgain: { problem == .couldNotAdd ? use() : overlays.retryFailed() },
                        editPrompt: { if let id = failedID { overlays.screen = .edit(id) } },
                        checkAgain: { overlays.checkAgain() }, saveAgain: { overlays.saveAgain() }))
                }
                ForEach(overlays.madeCards) { card in
                    let number = overlays.number(of: card.id)
                    OverlayRow(number: number, card: card, style: .actions(
                        edit: { overlays.screen = .edit(card.id) },
                        redo: { overlays.redo(card.id) },
                        remove: { overlays.remove(card.id) }))
                }
                AddOverlayRow(isEnabled: overlays.canAddMore) { overlays.addCard() }
            }
        } pinnedBar: {
            Button(useTitle) { use() }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: overlays.isAdding))
                .disabled(count == 0 || overlays.makingCount > 0)
                .accessibilityIdentifier("generated-media-use")
                .kriaDisabledReason(reason)
        }
    }

    private var failedID: UUID? { overlays.madeCards.first { $0.generation?.phase == .failed }?.id }

    private func use() {
        Task { if await overlays.useOverlays(done: accepted) { finish() } }
    }
}

// MARK: - Edit one overlay (S1.04) and its remade result (S1.08)

private struct EditScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let cardID: UUID
    let close: () -> Void
    @State private var prompt = ""
    @FocusState private var focused: Bool

    private var number: Int { overlays.number(of: cardID) }
    private var card: OverlayCard? { overlays.card(cardID) }
    private var valid: Bool {
        let trimmed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty && prompt.count <= OverlayLimits.maxPrompt
    }

    var body: some View {
        KriaSheetScaffold(heading: "Edit overlay \(number)", onBack: { overlays.resumeScreen() }, onClose: close) {
            VStack(spacing: 16) {
                VStack(spacing: 8) {
                    OverlayThumbnail(url: card?.generation?.previewURL, phase: card?.generation?.phase, width: 140, height: 210, radius: 16)
                    Text(card?.generation?.phase == .ready ? "How overlay \(number) looks now" : "Overlay \(number) isn’t ready yet")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                }
                .frame(maxWidth: .infinity)
                VStack(alignment: .leading, spacing: 8) {
                    Text("Prompt for overlay \(number)").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    TextField("Describe the overlay", text: $prompt, axis: .vertical)
                        .kriaType(.body).foregroundStyle(KriaColor.ink).tint(KriaColor.sky)
                        .focused($focused)
                        .accessibilityIdentifier("overlay-edit-prompt")
                    HStack {
                        Text("\(prompt.count) / \(OverlayLimits.maxPrompt)")
                            .kriaType(.label).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
                        Spacer()
                    }
                    .frame(minHeight: 44)
                }
                .padding(.top, 16).padding(.horizontal, 16).padding(.bottom, 0)
                .background(focused ? KriaColor.selectionSoft : KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .onTapGesture { focused = true }
            }
        } pinnedBar: {
            Button("Remake overlay \(number)") { focused = false; overlays.remake(cardID, prompt: prompt) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .disabled(!valid)
                .accessibilityIdentifier("generated-media-submit")
                .kriaDisabledReason(valid ? nil : "Describe the overlay to remake it.")
            if valid {
                Text("Only this overlay changes. The others stay as they are.")
                    .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            }
        }
        .onAppear { prompt = card?.generation?.prompt ?? card?.prompt ?? "" }
    }
}

private struct RemadeScreen: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let cardID: UUID
    let close: () -> Void

    private var number: Int { overlays.number(of: cardID) }
    private var remake: OverlayGeneration? { overlays.card(cardID)?.remake }
    private var phase: OverlayGeneration.Phase { remake?.phase ?? .making }
    private var heading: String { phase == .ready ? "Overlay \(number), remade" : "Remaking overlay \(number)" }

    var body: some View {
        KriaSheetScaffold(heading: heading, onClose: close) {
            VStack(spacing: 16) {
                if let remake, phase == .failed || phase == .unsure || phase == .unsaved {
                    OverlayProblemCard(problem: problem(for: remake), handlers: .init(
                        tryAgain: { overlays.retryRemake(cardID) }, editPrompt: { overlays.screen = .edit(cardID) },
                        checkAgain: { overlays.checkAgain() }, saveAgain: { overlays.saveAgain() }))
                }
                OverlayThumbnail(url: remake?.previewURL, phase: phase, width: 240, height: 360, radius: 16)
                if let remake {
                    VStack(spacing: 4) {
                        Text("Overlay \(number)").kriaType(.button).foregroundStyle(KriaColor.ink)
                        Text(remake.prompt).kriaType(.label).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
                    }
                }
            }
            .frame(maxWidth: .infinity)
        } pinnedBar: {
            Button("Use this version") { overlays.useRemake(cardID) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .disabled(phase != .ready)
                .accessibilityIdentifier("generated-media-use")
                .kriaDisabledReason(phase == .making ? "Making your overlay…" : nil)
            HStack(spacing: 12) {
                Button("Try again") { overlays.retryRemake(cardID) }
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
                    .disabled(phase == .making)
                    .accessibilityIdentifier("overlay-remake-try-again")
                Button("Discard") { overlays.discardRemake(cardID) }
                    .buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium, fullWidth: true))
                    .accessibilityIdentifier("overlay-remake-discard")
            }
        }
    }

    private func problem(for generation: OverlayGeneration) -> OverlayProblem {
        switch generation.phase {
        case .unsure: generation.problem == .offline ? .offline : .unsure
        case .unsaved: .didNotSave(count: 1)
        default: generation.problem?.isSetWide == true ? (generation.problem ?? .couldNotMake(failed: 1, total: 1)) : .couldNotMake(failed: 1, total: 1)
        }
    }
}

// MARK: - Shared pieces

private struct OverlayRow: View {
    enum Style {
        case status(stalled: Bool)
        case actions(edit: () -> Void, redo: () -> Void, remove: () -> Void)
    }

    let number: Int
    let card: OverlayCard
    let style: Style

    private var generation: OverlayGeneration? { card.generation }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            OverlayThumbnail(url: generation?.previewURL, phase: generation?.phase, width: 80, height: 120, radius: 12)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text("Overlay \(number)").kriaType(.button).foregroundStyle(KriaColor.ink)
                    Spacer(minLength: 4)
                    switch style {
                    case .status(let stalled): StatusLabel(phase: generation?.phase ?? .making, stalled: stalled)
                    case .actions(let edit, let redo, let remove):
                        OverlayIconButton(symbol: "pencil", label: "Edit prompt for overlay \(number)", identifier: "overlay-edit-\(number)", action: edit)
                        OverlayIconButton(symbol: "arrow.clockwise", label: "Redo overlay \(number)", identifier: "overlay-redo-\(number)", action: redo)
                        OverlayIconButton(symbol: "trash", label: "Remove overlay \(number)", identifier: "overlay-remove-\(number)", action: remove)
                    }
                }
                .frame(minHeight: 44)
                Text(generation?.prompt ?? card.prompt).kriaType(.body).foregroundStyle(KriaColor.ink)
                if case .actions = style, let phase = generation?.phase, phase != .ready {
                    StatusLabel(phase: phase, stalled: generation?.stalled == true).padding(.top, 2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("overlay-row-\(number)")
    }
}

private struct StatusLabel: View {
    let phase: OverlayGeneration.Phase
    let stalled: Bool

    var body: some View {
        HStack(spacing: 6) {
            switch phase {
            case .ready:
                KriaCheckDisc(size: 16)
                Text("Ready").foregroundStyle(KriaColor.mutedInk)
            case .making:
                if !stalled { KriaSpinner(size: 13) }
                Text(stalled ? "Still working" : "Making…").foregroundStyle(KriaColor.mutedInk)
            case .failed: Text("Didn’t finish").foregroundStyle(KriaColor.failureText)
            case .unsure: Text("Not sure yet").foregroundStyle(KriaColor.mutedInk)
            case .unsaved: Text("Didn’t save").foregroundStyle(KriaColor.failureText)
            }
        }
        .kriaType(.label)
        .accessibilityElement(children: .combine)
    }
}

/// The 2:3 image frame every overlay lives in. It holds a spinner while the image is made, so nothing jumps
/// when the picture lands, and a Reload inside the frame when the picture itself will not load.
struct OverlayThumbnail: View {
    let url: URL?
    let phase: OverlayGeneration.Phase?
    let width: CGFloat
    let height: CGFloat
    let radius: CGFloat
    @State private var reload = 0

    var body: some View {
        ZStack {
            KriaColor.paper
            content
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
    }

    @ViewBuilder private var content: some View {
        if let url, phase == .ready {
            AsyncImage(url: url) { result in
                switch result {
                case .success(let image): image.resizable().scaledToFill().frame(width: width, height: height).clipped()
                case .failure:
                    Button { reload += 1 } label: {
                        VStack(spacing: 4) {
                            Image(systemName: "arrow.clockwise").font(.system(size: 17))
                            Text("Reload").kriaType(.caption)
                        }
                        .foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(KriaColor.failureSoft)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Couldn’t show your overlay. Reload")
                default: KriaColor.fill.overlay { KriaSpinner(size: 20) }
                }
            }
            .id(reload)
        } else {
            KriaColor.fill.overlay {
                switch phase {
                case .failed, .unsaved: Image(systemName: "exclamationmark").font(.system(size: 17, weight: .bold)).foregroundStyle(KriaColor.failureText)
                case .unsure: Image(systemName: "questionmark").font(.system(size: 17, weight: .bold)).foregroundStyle(KriaColor.mutedInk)
                default: KriaSpinner(size: 20)
                }
            }
        }
    }
}

/// A 44pt white disc with an SF Symbol, for the actions on a fill card.
private struct OverlayIconButton: View {
    let symbol: String
    let label: String
    let identifier: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 15, weight: .semibold))
        }
        .buttonStyle(OverlayIconStyle())
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier)
    }
}

private struct OverlayIconStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(KriaColor.ink)
            .frame(width: 44, height: 44)
            .background(configuration.isPressed ? KriaColor.fillPressed : KriaColor.paper, in: Circle())
            .contentShape(Circle())
    }
}

// MARK: - The notice in the Add media sheet (S1.01)

/// "3 overlays are still being made" / "Your overlays are ready to review", with a live spinner and a 44pt
/// Open. It never shows a disabled form: Open goes straight to the making or results screen.
struct OverlaySetNotice: View {
    @ObservedObject var overlays: GeneratedOverlaySet
    let open: () -> Void

    private var total: Int { overlays.madeCards.count }

    var body: some View {
        if overlays.hasUnreviewed, overlays.hasContent {
            HStack(spacing: 12) {
                if overlays.makingCount > 0 { KriaSpinner(size: 16) }
                else { KriaNoticeIcon(tone: overlays.problems.isEmpty ? .success : .info, size: 20) }
                Text(title).kriaType(.label).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button(overlays.makingCount > 0 || !overlays.problems.isEmpty ? "Open" : "Review", action: open)
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium))
                    .accessibilityIdentifier("generated-media-resume")
            }
            .padding(.leading, 14).padding(.trailing, 4).padding(.vertical, 4)
            .frame(minHeight: 52)
            .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .accessibilityElement(children: .contain)
        }
    }

    private var title: String {
        if overlays.makingCount > 0 {
            return total == 1 ? "Your overlay is still being made" : "\(overlays.makingCount) overlays are still being made"
        }
        return overlays.problems.isEmpty
            ? (total == 1 ? "Your overlay is ready to review" : "Your overlays are ready to review")
            : "Some overlays need your attention"
    }
}
