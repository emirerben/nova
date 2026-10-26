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
            TextField("Any direction? e.g. the food, the boat day", text: Binding(
                get: { draft.query }, set: { var next = model.draft(for: event.id); next.query = String($0.prefix(500)); model.setDraft(next, for: event.id) }))
                .font(KriaFont.body(15).weight(.medium)).submitLabel(.done)
                .padding(.horizontal, 16).frame(height: 48).background(Color.white, in: Capsule())
                .accessibilityIdentifier("camera-roll-direction-\(event.id)")
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

struct CameraRollIdeaCard: View {
    @Bindable var model: CameraRollSurfaceModel
    let story: CameraRollStory
    let event: CameraRollEvent
    let isFirst: Bool
    let open: (_ autoSelect: Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(summary).font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk).lineLimit(1)
                Spacer(minLength: 0)
                if isFirst {
                    Text("Best fit").font(KriaFont.body(11).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 8).frame(height: 22).background(KriaColor.butter, in: Capsule())
                }
                menu
            }
            Button { open(false) } label: {
                VStack(alignment: .leading, spacing: 10) {
                    Text(story.title).font(KriaFont.body(isFirst ? 19 : 17).weight(.bold)).tracking(isFirst ? -0.19 : 0)
                        .foregroundStyle(KriaColor.ink).multilineTextAlignment(.leading).frame(maxWidth: .infinity, alignment: .leading)
                    if isFirst { clips }
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-idea-open-\(story.id)")
            if isFirst {
                Button { open(true) } label: {
                    Text("Use this idea").font(KriaFont.body(15).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 44).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-idea-use-\(story.id)")
            }
        }
        .padding(14).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-idea-\(story.id)")
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
            Button { open(false) } label: { Label("Edit opening text", systemImage: "pencil") }
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
                CameraRollCollectionThumbnail(entry: entry, api: model.api, metadataOnly: model.thumbnailsDisabled, size: CGSize(width: 52, height: 66))
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
        }
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
