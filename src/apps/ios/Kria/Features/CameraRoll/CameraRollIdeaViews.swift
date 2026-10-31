import SwiftUI

/// Small type styles shared by the camera-roll screens (Paper "Kria iOS — Camera roll").
extension View {
    /// The 12 pt tracked label above a block: "IDEAS FOR THIS EVENT".
    func cameraRollKicker() -> some View {
        font(KriaFont.body(12).weight(.semibold)).tracking(0.72).textCase(.uppercase).foregroundStyle(KriaColor.mutedInk)
    }
    /// The 28 pt screen title.
    func cameraRollTitle() -> some View {
        font(KriaFont.body(28).weight(.bold)).tracking(-0.56).foregroundStyle(KriaColor.ink)
    }
}

/// A 44 pt white circle that floats over the page: the menu, back and taste buttons.
struct CameraRollCircleButton<Label: View>: View {
    let identifier: String
    let label: String
    let action: () -> Void
    @ViewBuilder let content: Label

    var body: some View {
        Button(action: action) { content.frame(width: 44, height: 44).kriaFloatingSurface(Circle()).contentShape(Circle()) }
            .buttonStyle(.plain).accessibilityLabel(label).accessibilityIdentifier(identifier)
    }
}

/// The dots that open an event or idea menu. `size` is the visible disc; the tap target stays at least 44 pt.
struct CameraRollDots: View {
    var size: CGFloat = 36
    var fill: Color = KriaColor.fill
    var body: some View {
        ZStack {
            Circle().fill(fill)
            HStack(spacing: 4) { ForEach(0..<3, id: \.self) { _ in Circle().fill(KriaColor.ink).frame(width: 3.2, height: 3.2) } }
        }
        .frame(width: size, height: size).frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
    }
}

// MARK: - Direction

/// What the creator wants from the next ideas, sent with every request for this event: the first
/// "Find ideas" and each "Another idea". The chips are shortcuts for asks the idea AI acts on.
struct CameraRollDirectionField: View {
    @Bindable var model: CameraRollSurfaceModel
    let group: String

    static let shortcuts: [(title: String, phrase: String)] = [
        ("More clips", "Use more clips"),
        ("Vlog", "Make it a vlog"),
        ("Short and punchy", "Short and punchy"),
    ]

    var body: some View {
        let query = model.draft(for: group).query
        VStack(alignment: .leading, spacing: 8) {
            TextField("Any direction? e.g. the food, the boat day", text: Binding(
                get: { model.draft(for: group).query }, set: { setQuery($0) }))
                .font(KriaFont.body(15).weight(.medium)).submitLabel(.done)
                .padding(.horizontal, 16).frame(height: 48).background(Color.white, in: Capsule())
                .accessibilityIdentifier("camera-roll-direction-\(group)")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Self.shortcuts, id: \.phrase) { shortcut in
                        let selected = Self.parts(query).contains { $0.caseInsensitiveCompare(shortcut.phrase) == .orderedSame }
                        Button { toggle(shortcut.phrase, selected: selected) } label: {
                            Text(shortcut.title).font(KriaFont.body(13).weight(.semibold)).lineLimit(1)
                                .foregroundStyle(selected ? Color.white : KriaColor.ink).padding(.horizontal, 14).frame(height: 36)
                                .background(selected ? KriaColor.ink : Color.white, in: Capsule())
                                .frame(minHeight: 44).contentShape(Capsule())
                        }
                        .buttonStyle(.plain).accessibilityAddTraits(selected ? .isSelected : [])
                        .accessibilityIdentifier("camera-roll-direction-chip-\(shortcut.title)")
                    }
                }
            }
        }
    }

    private static func parts(_ query: String) -> [String] {
        query.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    private func toggle(_ phrase: String, selected: Bool) {
        var parts = Self.parts(model.draft(for: group).query)
        if selected { parts.removeAll { $0.caseInsensitiveCompare(phrase) == .orderedSame } } else { parts.append(phrase) }
        setQuery(parts.joined(separator: ", "))
    }

    private func setQuery(_ text: String) {
        var next = model.draft(for: group)
        next.query = String(text.prefix(500))
        model.setDraft(next, for: group)
    }
}

// MARK: - Find ideas panel (Paper A3)

struct CameraRollFinderPanel: View {
    @Bindable var model: CameraRollSurfaceModel
    let event: CameraRollEvent
    let openTaste: () -> Void

    var body: some View {
        let ready = model.readyCount(in: event)
        let waiting = model.waitingCount(in: event)
        let draft = model.draft(for: event.id)
        VStack(alignment: .leading, spacing: 14) {
            Text("Ideas for this event").font(KriaFont.body(17).weight(.bold)).foregroundStyle(KriaColor.ink)
            CameraRollDirectionField(model: model, group: event.id)
            HStack(spacing: 8) {
                if model.tasteAvailable {
                    chip("Use my taste", selected: model.taste?.enabled == true && model.taste?.memoryPaused == false, identifier: "camera-roll-chip-taste", action: openTaste)
                }
                chip(researchLabel(draft), selected: draft.researchOn, identifier: "camera-roll-chip-research") {
                    var next = model.draft(for: event.id); next.researchOn.toggle()
                    if next.researchOn, next.topic.isEmpty, let suggested = model.suggestedTopic { next.topic = suggested }
                    model.setDraft(next, for: event.id)
                }
            }
            if draft.researchOn {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Public place or topic", text: Binding(
                        get: { draft.topic }, set: { var next = model.draft(for: event.id); next.topic = String($0.prefix(200)); model.setDraft(next, for: event.id) }))
                        .font(KriaFont.body(15).weight(.medium)).submitLabel(.done)
                        .padding(.horizontal, 16).frame(height: 48).background(Color.white, in: Capsule())
                        .accessibilityIdentifier("camera-roll-research-topic-\(event.id)")
                    Text("Only this topic is searched. Your clips and direction stay private.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            Button {
                model.finderGroup = nil
                model.anotherIdea(in: event.id)
            } label: {
                Text("Find ideas from \(ready) ready").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
            }
            .buttonStyle(.plain).disabled(ready == 0 || model.isGenerating(event.id))
            .opacity(ready == 0 ? 0.45 : 1).accessibilityIdentifier("camera-roll-find-ideas-go-\(event.id)")
            if waiting > 0 {
                Text("The other \(waiting) are still being looked at. Add them later with Another idea.")
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }
        }
        .padding(16).background(KriaColor.menu, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-finder-\(event.id)")
    }

    private func researchLabel(_ draft: CameraRollSurfaceModel.FinderDraft) -> String {
        let topic = draft.topic.trimmingCharacters(in: .whitespacesAndNewlines)
        if !topic.isEmpty { return "Research \(topic)" }
        if let suggested = model.suggestedTopic { return "Research \(suggested)" }
        return "Research a place"
    }

    private func chip(_ title: String, selected: Bool, identifier: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if selected { KriaCheckShape().stroke(.white, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round)).frame(width: 12, height: 12) }
                Text(title).font(KriaFont.body(13).weight(.semibold)).lineLimit(1)
            }
            .foregroundStyle(selected ? Color.white : KriaColor.ink).padding(.horizontal, 14).frame(height: 36)
            .background(selected ? KriaColor.ink : Color.white, in: Capsule())
            .frame(minHeight: 44).contentShape(Capsule())
        }
        .buttonStyle(.plain).accessibilityIdentifier(identifier).accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// "While it runs": what Kria is doing, with the same light sweep the tiles use.
struct CameraRollWorkingCard: View {
    let ready: Int
    let identifier: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                KriaSpinner(size: 16)
                Text("Reading your \(ready) \(ready == 1 ? "clip" : "clips")").font(KriaFont.body(15).weight(.semibold))
            }
            Text("Picking the moments worth a video. Usually under 30 seconds.")
                .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(16)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .overlay { CameraRollShimmer().clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous)) }
        .accessibilityElement(children: .combine).accessibilityIdentifier(identifier)
    }
}

// MARK: - Idea card (Paper A2)

/// Every idea has the same card: its form and length, its clips, any open questions, and Use this idea.
struct CameraRollIdeaCard: View {
    @Bindable var model: CameraRollSurfaceModel
    let story: CameraRollStory
    let event: CameraRollEvent
    let isBestFit: Bool
    let open: () -> Void

    var body: some View {
        let unresolved = story.contentPlan?.unresolvedQuestions.count ?? 0
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text(summary).font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk).lineLimit(1)
                Spacer(minLength: 0)
                if isBestFit {
                    Text("Best fit").font(KriaFont.body(11).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 8).frame(height: 22).background(KriaColor.butter, in: Capsule())
                        .accessibilityIdentifier("camera-roll-idea-best-fit")
                }
                menu
            }
            Button(action: open) {
                Text(story.title).font(KriaFont.body(19).weight(.bold)).tracking(-0.19)
                    .foregroundStyle(KriaColor.ink).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-idea-open-\(story.id)")
            clips
            if let plan = story.contentPlan, unresolved > 0 {
                CameraRollOpenQuestionsView(story: story, plan: plan) { revised in await model.answerQuestions(story, plan: revised) }
            }
            Button { model.use(story) } label: { Text(useTitle).frame(maxWidth: .infinity) }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 48)).disabled(unresolved > 0)
                .accessibilityIdentifier("camera-roll-idea-use-\(story.id)")
            if unresolved > 0 {
                Text(unresolved == 1 ? "Answer or skip the question above to use this idea." : "Answer or skip the \(unresolved) questions above to use this idea.")
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity).multilineTextAlignment(.center)
                    .accessibilityIdentifier("camera-roll-idea-use-reason-\(story.id)")
            }
        }
        .padding(14).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-idea-\(story.id)")
    }

    /// While the idea is being built, Use reopens its progress.
    private var useTitle: String {
        guard let use = model.uses[story.id] else { return "Use this idea" }
        switch use.phase {
        case .running: return "Building · Show progress"
        case .failed, .needsOriginals, .blocked: return "Needs you · Show progress"
        case .opened: return "Use this idea"
        }
    }

    private var summary: String {
        let seconds = Int(story.duration.rounded())
        let form = story.contentPlan?.formLabel ?? "Idea"
        return "\(form) · \(story.moments.count) \(story.moments.count == 1 ? "clip" : "clips") · \(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }

    private var menu: some View {
        Menu {
            Button { model.anotherIdea(in: event.id) } label: { Label("More like this", systemImage: "sparkles") }
                .disabled(model.isGenerating(event.id))
            Button(action: open) { Label("Edit opening text", systemImage: "pencil") }
            Button(role: .destructive) { model.deleteIdea(story, in: event.id) } label: { Label("Delete idea", systemImage: "trash") }
        } label: { CameraRollDots(size: 32, fill: .white) }
            .accessibilityLabel("Idea options").accessibilityIdentifier("camera-roll-idea-menu-\(story.id)")
    }

    private var clips: some View {
        let entries = story.moments.reduce(into: [CameraRollCollection.Entry]()) { found, moment in
            if let entry = model.collection.entries.first(where: { $0.savedAsset?.id == moment.assetID }), !found.contains(where: { $0.id == entry.id }) { found.append(entry) }
        }
        return HStack(spacing: 6) {
            ForEach(entries.prefix(4)) { entry in
                CameraRollCollectionThumbnail(entry: entry, api: model.api, metadataOnly: model.thumbnailsDisabled, size: CGSize(width: 56, height: 72))
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            if entries.count > 4 {
                Text("+\(entries.count - 4)").font(KriaFont.body(13).weight(.bold)).foregroundStyle(KriaColor.mutedInk)
                    .frame(width: 56, height: 72).background(Color.white, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
        .contentShape(Rectangle()).onTapGesture(perform: open)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(entries.count) \(entries.count == 1 ? "clip" : "clips")").accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("camera-roll-idea-clips-\(story.id)")
    }
}

// MARK: - Use this idea

/// The progress of turning an idea into a chat. Closing it leaves the work running; Use on the card reopens it.
struct CameraRollUseSheet: View {
    let use: CameraRollIdeaUse
    @Environment(\.dismiss) private var dismiss
    @State private var choosingOriginals = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                Text(use.story.title).font(KriaFont.headline(22)).foregroundStyle(KriaColor.ink).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                CameraRollCircleButton(identifier: "camera-roll-use-close", label: "Close", action: { dismiss() }) {
                    Image(systemName: "xmark").font(.system(size: 14, weight: .bold)).foregroundStyle(KriaColor.ink)
                }
            }
            VStack(alignment: .leading, spacing: 14) {
                ForEach(CameraRollIdeaUse.Step.allCases, id: \.self) { step in row(step) }
            }
            outcome
            Spacer(minLength: 0)
            Text("You can close this. Kria keeps going and opens your chat when it is ready.")
                .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
        }
        .padding(.horizontal, 20).padding(.top, 24).padding(.bottom, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(KriaColor.paper)
        .presentationDetents([.medium, .large]).presentationDragIndicator(.visible)
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-use-sheet")
        .sheet(isPresented: $choosingOriginals) {
            if let owner = use.owner {
                CollectionOriginalsReview(assets: use.missingOriginals, owner: owner) { choosingOriginals = false; use.start() }
            }
        }
    }

    private enum RowState: String { case waiting = "Waiting", active = "In progress", done = "Done", failed = "Failed" }

    private func state(_ step: CameraRollIdeaUse.Step) -> RowState {
        func relative(_ current: CameraRollIdeaUse.Step, at: RowState) -> RowState {
            step.rawValue < current.rawValue ? .done : step == current ? at : .waiting
        }
        switch use.phase {
        case let .running(current): return relative(current, at: .active)
        case let .failed(current, _): return relative(current, at: .failed)
        case .needsOriginals: return relative(.originals, at: .failed)
        case .blocked: return relative(.chat, at: .failed)
        case .opened: return .done
        }
    }

    private func row(_ step: CameraRollIdeaUse.Step) -> some View {
        let state = state(step)
        let title = step == .clips && use.clipsTotal > 0 ? "\(step.title) \(use.clipsDone)/\(use.clipsTotal)" : step.title
        return HStack(spacing: 12) {
            ZStack {
                switch state {
                case .done:
                    Circle().fill(KriaColor.successSoft)
                    KriaCheckShape().stroke(KriaColor.success, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round)).frame(width: 12, height: 12)
                case .active: KriaSpinner(size: 18)
                case .failed:
                    Circle().fill(KriaColor.failureSoft)
                    Image(systemName: "exclamationmark").font(.system(size: 13, weight: .bold)).foregroundStyle(KriaColor.failureText)
                case .waiting: Circle().fill(KriaColor.fill)
                }
            }.frame(width: 28, height: 28)
            Text(title).font(KriaFont.body(16).weight(state == .waiting ? .medium : .semibold))
                .foregroundStyle(state == .waiting ? KriaColor.mutedInk : KriaColor.ink)
        }
        .accessibilityElement(children: .ignore).accessibilityLabel(title).accessibilityValue(state.rawValue)
        .accessibilityIdentifier("camera-roll-use-step-\(step)")
    }

    @ViewBuilder private var outcome: some View {
        switch use.phase {
        case let .failed(_, message):
            failure(message)
            Button { use.start() } label: { Text("Retry").frame(maxWidth: .infinity) }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 52)).accessibilityIdentifier("camera-roll-use-retry")
        case let .blocked(message):
            failure(message)
        case .needsOriginals:
            failure("\(use.missingOriginals.count) \(use.missingOriginals.count == 1 ? "video needs its" : "videos need their") full-quality original from Photos.")
            Button { choosingOriginals = true } label: { Text("Choose originals").frame(maxWidth: .infinity) }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 52)).accessibilityIdentifier("camera-roll-use-choose-originals")
        case .running, .opened:
            EmptyView()
        }
    }

    private func failure(_ message: String) -> some View {
        Text(message).font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.failureText)
            .frame(maxWidth: .infinity, alignment: .leading).padding(14)
            .background(KriaColor.failureSoft, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .accessibilityIdentifier("camera-roll-use-error")
    }
}

// MARK: - Undo toast and delete confirmation (Paper A8)

struct CameraRollUndoToast: View {
    let message: String
    let undo: () -> Void

    var body: some View {
        HStack {
            Text(message).font(KriaFont.body(15).weight(.semibold)).foregroundStyle(.white)
            Spacer(minLength: 8)
            Button(action: undo) {
                Text("Undo").font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 18).frame(height: 40).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
            }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-undo")
        }
        .padding(.leading, 20).padding(.trailing, 8).frame(height: 56).background(KriaColor.ink, in: Capsule())
        .shadow(color: KriaColor.ink.opacity(0.18), radius: 14, y: 6)
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-toast")
    }
}

struct CameraRollDeleteEventCard: View {
    let title: String
    let itemCount: Int
    let ideaCount: Int
    let delete: () -> Void
    let keep: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Delete \(title)?").font(KriaFont.body(20).weight(.bold)).tracking(-0.2).foregroundStyle(KriaColor.ink)
            Text("The \(itemCount) \(itemCount == 1 ? "item leaves" : "items leave") this camera roll and its \(ideaCount) \(ideaCount == 1 ? "idea is" : "ideas are") removed. Nothing is deleted from your Photos.")
                .font(KriaFont.body(15).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
            VStack(spacing: 8) {
                Button(action: delete) {
                    Text("Delete event").font(KriaFont.body(16).weight(.bold)).foregroundStyle(.white)
                        .frame(maxWidth: .infinity, minHeight: 50).background(KriaColor.failureText, in: Capsule()).contentShape(Capsule())
                }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-delete-event-confirm")
                Button(action: keep) {
                    Text("Keep it").font(KriaFont.body(16).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 50).background(KriaColor.fill, in: Capsule()).contentShape(Capsule())
                }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-delete-event-keep")
            }
        }
        .padding(22).background(Color.white, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .shadow(color: KriaColor.ink.opacity(0.2), radius: 32, y: 8)
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-delete-event-card")
    }
}
