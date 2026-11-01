import SwiftUI

/// Every page the Camera roll pushes. An event's page and its ideas carry the event's group, so they close together
/// when the event goes away.
enum CameraRollPage: Hashable {
    case event(String)
    case idea(CameraRollIdeaRoute)
    case taste

    var group: String? {
        switch self {
        case let .event(group): group
        case let .idea(route): route.group
        case .taste: nil
        }
    }
}

// MARK: - Event card (home)

/// One event on the home list: its title, one row of footage, and what to do next with its ideas.
struct CameraRollEventCard: View {
    let model: CameraRollSurfaceModel
    let event: CameraRollEvent
    let open: () -> Void
    private let thumb = CGSize(width: 54, height: 70)

    var body: some View {
        let title = model.title(for: event.id)
        let note = model.readinessNote(event)
        let next = nextStep
        Button(action: open) {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(KriaFont.headline(22)).foregroundStyle(KriaColor.ink).lineLimit(2).multilineTextAlignment(.leading)
                    if let note { Text(note).font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk) }
                }
                footage
                HStack(spacing: 4) {
                    if model.isGenerating(event.id) { KriaSpinner(size: 12) }
                    Text(next).font(KriaFont.body(15).weight(.bold))
                    Image(systemName: "chevron.right").font(.system(size: 12, weight: .bold))
                }.foregroundStyle(KriaColor.ink)
            }
            .padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel([title, "\(event.entries.count) \(event.entries.count == 1 ? "item" : "items")", note].compactMap { $0 }.joined(separator: ", "))
        .accessibilityValue(next)
        .accessibilityIdentifier("camera-roll-event-\(event.id)")
    }

    private var nextStep: String {
        let count = model.ideas(in: event.id).count
        if count > 0 { return "\(count) \(count == 1 ? "idea" : "ideas")" }
        if event.entries.isEmpty { return "Add footage" }
        return model.isGenerating(event.id) ? "Finding ideas" : "Find ideas"
    }

    @ViewBuilder private var footage: some View {
        if event.entries.isEmpty {
            Text("No photos or videos yet").font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, minHeight: thumb.height, alignment: .leading)
        } else {
            HStack(spacing: 6) {
                ForEach(event.entries.prefix(4)) { entry in
                    CameraRollCollectionThumbnail(entry: entry, api: model.api, metadataOnly: model.thumbnailsDisabled, size: thumb)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                if event.entries.count > 4 {
                    Text("+\(event.entries.count - 4)").font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.mutedInk)
                        .frame(width: thumb.width, height: thumb.height)
                        .background(Color.white, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
            }
        }
    }
}

// MARK: - Event page

/// One event: its title, what the creator says about it, its footage (one row until Show all), then its ideas.
struct CameraRollEventPage: View {
    @Bindable var model: CameraRollSurfaceModel
    let group: String
    let openIdea: (CameraRollIdeaRoute) -> Void
    let addFootage: () -> Void
    let delete: () -> Void
    let openTaste: () -> Void
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.dismiss) private var dismiss
    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 3)

    var body: some View {
        Group {
            if let event = model.events.first(where: { $0.id == group }) {
                if model.selecting?.group == group {
                    CameraRollEventEditor(model: model, event: event)
                } else {
                    page(event)
                }
            } else {
                Color.clear
            }
        }
        .background(KriaColor.paper).toolbar(.hidden, for: .navigationBar)
    }

    private func page(_ event: CameraRollEvent) -> some View {
        VStack(spacing: 0) {
            HStack {
                CameraRollCircleButton(identifier: "camera-roll-back", label: "Back", action: { dismiss() }) {
                    Image(systemName: "chevron.left").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink)
                }
                Spacer()
                menu(event)
            }.padding(.horizontal, 16).padding(.top, 4).frame(minHeight: 52)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    CameraRollNoticeBanner(model: model)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.title(for: event.id)).cameraRollTitle().fixedSize(horizontal: false, vertical: true)
                            .accessibilityAddTraits(.isHeader).accessibilityIdentifier("camera-roll-event-title")
                        if let note = model.readinessNote(event) {
                            Text(note).font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                        }
                    }
                    if model.aboutAvailable { CameraRollAboutEditor(model: model, group: event.id) }
                    footage(event)
                    ideas(event)
                }
                .padding(.horizontal, 20).padding(.top, 8).padding(.bottom, 140)
            }
            .scrollDismissesKeyboard(.interactively).kriaScrollEdgeFade()
        }
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-event-page-\(event.id)")
    }

    private func menu(_ event: CameraRollEvent) -> some View {
        Menu {
            Button { model.beginSelecting(in: event.id, renaming: true) } label: { Label("Rename", systemImage: "pencil") }
            Button { model.beginSelecting(in: event.id) } label: { Label("Select and move items", systemImage: "checkmark.square") }
                .disabled(event.entries.isEmpty)
            Button(action: addFootage) { Label("Add photos and videos", systemImage: "plus") }.disabled(!model.canAdd)
            Button(role: .destructive, action: delete) { Label("Delete event", systemImage: "trash") }
        } label: { CameraRollDots(size: 44, fill: .white) }
            .accessibilityLabel("Event options").accessibilityIdentifier("camera-roll-event-menu-\(event.id)")
    }

    // MARK: Footage

    @ViewBuilder private func footage(_ event: CameraRollEvent) -> some View {
        if event.entries.isEmpty {
            Button(action: addFootage) {
                Label("Add photos and videos", systemImage: "plus").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
            }
            .buttonStyle(.plain).disabled(!model.canAdd).accessibilityIdentifier("camera-roll-event-add-\(event.id)")
        } else {
            let expanded = model.expanded.contains(event.id)
            let shown = expanded ? event.entries : Array(event.entries.prefix(3))
            VStack(alignment: .leading, spacing: 8) {
                LazyVGrid(columns: columns, spacing: 8) {
                    ForEach(shown) { entry in
                        CameraRollTileView(entry: entry, tile: model.store.tile(entry.id), api: model.api, thumbnailsDisabled: model.thumbnailsDisabled,
                                           onRetry: { model.retry(entry) })
                    }
                }
                if event.entries.count > 3 {
                    Button {
                        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
                            if expanded { model.expanded.remove(event.id) } else { model.expanded.insert(event.id) }
                        }
                    } label: {
                        Text(expanded ? "Show fewer" : "Show all \(event.entries.count)").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 16).frame(height: 40).background(KriaColor.fill, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
                    }
                    .buttonStyle(.plain).accessibilityIdentifier(expanded ? "camera-roll-collapse-\(event.id)" : "camera-roll-expand-\(event.id)")
                }
            }
        }
    }

    // MARK: Ideas

    private func ideas(_ event: CameraRollEvent) -> some View {
        let ideas = model.ideas(in: event.id)
        let generating = model.isGenerating(event.id)
        let ready = model.readyCount(in: event)
        return VStack(alignment: .leading, spacing: 10) {
            Text("Ideas").cameraRollKicker().padding(.top, 6).accessibilityAddTraits(.isHeader)
            if model.finderGroup == event.id, !generating {
                CameraRollFinderPanel(model: model, event: event, openTaste: openTaste)
            } else if ideas.isEmpty, !generating {
                Button { withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { model.finderGroup = event.id } } label: {
                    Label("Find ideas", systemImage: "sparkle").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(.plain).disabled(ready == 0).opacity(ready == 0 ? 0.45 : 1)
                .accessibilityIdentifier("camera-roll-find-ideas-\(event.id)")
                if ready == 0 {
                    Text(event.entries.isEmpty ? "Add footage to find ideas." : "Ideas need at least one item that is ready.")
                        .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            if generating { CameraRollWorkingCard(ready: ready, identifier: "camera-roll-working-\(event.id)") }
            ForEach(Array(ideas.enumerated()), id: \.element.id) { index, story in
                CameraRollIdeaCard(model: model, story: story, event: event, isBestFit: index == 0) {
                    openIdea(CameraRollIdeaRoute(group: event.id, storyID: story.id))
                }
            }
            if !ideas.isEmpty {
                if !generating { CameraRollDirectionField(model: model, group: event.id).padding(.top, 4) }
                Button { model.anotherIdea(in: event.id) } label: {
                    HStack(spacing: 8) {
                        if generating { KriaSpinner(size: 14) } else { Image(systemName: "sparkle").font(.system(size: 12, weight: .bold)) }
                        Text(generating ? "Finding another" : "Another idea").font(KriaFont.body(15).weight(.bold))
                    }
                    .foregroundStyle(KriaColor.ink).padding(.horizontal, 18).frame(height: 44).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(.plain).disabled(generating || ready == 0).opacity(generating ? 0.6 : 1)
                .accessibilityIdentifier("camera-roll-another-idea-\(event.id)")
            }
        }
    }
}

// MARK: - About this event

/// What the creator tells the idea AI about the event. Saved when they tap Save or leave the field.
struct CameraRollAboutEditor: View {
    let model: CameraRollSurfaceModel
    let group: String
    @State private var text = ""
    @State private var loaded = false
    @FocusState private var focused: Bool

    private var changed: Bool { text.trimmingCharacters(in: .whitespacesAndNewlines) != model.about(for: group) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("About this event").cameraRollKicker()
                Spacer()
                if changed {
                    Button("Save") { commit() }.font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 14).frame(height: 32).background(KriaColor.butter, in: Capsule()).frame(minHeight: 44)
                        .buttonStyle(.plain).accessibilityIdentifier("camera-roll-event-about-save")
                }
            }
            TextField("Where, who, and what made it worth filming", text: $text, axis: .vertical)
                .lineLimit(2...6).font(KriaFont.body(15).weight(.medium)).foregroundStyle(KriaColor.ink).focused($focused)
                .padding(.horizontal, 16).padding(.vertical, 14)
                .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .accessibilityIdentifier("camera-roll-event-about-\(group)")
            Text("Kria reads this when it looks for ideas.").font(KriaFont.body(12).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
        }
        .onAppear { if !loaded { loaded = true; text = model.about(for: group) } }
        .onChange(of: text) { _, value in if value.count > CameraRollEventInfo.maximumAbout { text = String(value.prefix(CameraRollEventInfo.maximumAbout)) } }
        .onChange(of: focused) { _, isFocused in if !isFocused { commit() } }
        .onDisappear { commit() }
    }

    private func commit() {
        focused = false
        guard changed else { return }
        model.setAbout(group: group, to: text)
    }
}

// MARK: - New event

/// A title, what it is about, and then (optionally) its footage from Photos.
struct CameraRollNewEventSheet: View {
    let model: CameraRollSurfaceModel
    /// `choose` opens Photos for the new event once the sheet is gone.
    let create: (_ title: String, _ about: String, _ choose: Bool) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var about = ""
    @FocusState private var titleFocused: Bool

    private var trimmedTitle: String { title.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("New event").cameraRollTitle().accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Cancel") { dismiss() }.font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink).frame(minHeight: 44)
                    .accessibilityIdentifier("camera-roll-new-event-cancel")
            }
            TextField("Name it, e.g. Cunda Island", text: $title).font(KriaFont.body(17).weight(.bold)).submitLabel(.next).focused($titleFocused)
                .padding(.horizontal, 18).frame(height: 52).background(KriaColor.fill, in: Capsule())
                .accessibilityIdentifier("camera-roll-new-event-title")
                .onChange(of: title) { _, value in if value.count > CameraRollEventInfo.maximumTitle { title = String(value.prefix(CameraRollEventInfo.maximumTitle)) } }
            if model.aboutAvailable {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("What is it about? Where, who, and what made it worth filming", text: $about, axis: .vertical)
                        .lineLimit(3...6).font(KriaFont.body(15).weight(.medium))
                        .padding(.horizontal, 16).padding(.vertical, 14)
                        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                        .accessibilityIdentifier("camera-roll-new-event-about")
                        .onChange(of: about) { _, value in if value.count > CameraRollEventInfo.maximumAbout { about = String(value.prefix(CameraRollEventInfo.maximumAbout)) } }
                    Text("Kria reads this when it looks for ideas.").font(KriaFont.body(12).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            Spacer(minLength: 0)
            VStack(spacing: 8) {
                Button { finish(choose: true) } label: { Text("Choose photos and videos").frame(maxWidth: .infinity) }
                    .buttonStyle(KriaPrimaryButtonStyle(minHeight: 52)).disabled(trimmedTitle.isEmpty || !model.canAdd)
                    .accessibilityIdentifier("camera-roll-new-event-choose")
                Button { finish(choose: false) } label: {
                    Text("Create event").font(KriaFont.body(16).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.fill, in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(.plain).disabled(trimmedTitle.isEmpty).opacity(trimmedTitle.isEmpty ? 0.45 : 1)
                .accessibilityIdentifier("camera-roll-new-event-create")
            }
        }
        .padding(.horizontal, 20).padding(.top, 24).padding(.bottom, 12)
        .background(KriaColor.paper)
        .presentationDetents([.large]).presentationDragIndicator(.visible)
        .onAppear { titleFocused = true }
    }

    private func finish(choose: Bool) {
        create(trimmedTitle, about, choose)
        dismiss()
    }
}

// MARK: - Notice

/// The surface's one notice line and, after an edit conflict, the way to the latest collection. Shown on home and
/// on an event's page, wherever the creator is when it arrives.
struct CameraRollNoticeBanner: View {
    @Bindable var model: CameraRollSurfaceModel

    var body: some View {
        if let notice = model.notice {
            HStack(alignment: .top, spacing: 8) {
                Text(notice.text).font(KriaFont.body(14).weight(.medium))
                    .foregroundStyle(notice.kind == .error ? KriaColor.failureText : KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("camera-roll-notice")
                Button { model.notice = nil } label: {
                    Image(systemName: "xmark").font(.system(size: 12, weight: .bold)).foregroundStyle(KriaColor.mutedInk).frame(width: 44, height: 28)
                }.buttonStyle(.plain).accessibilityLabel("Dismiss")
            }
            .padding(.leading, 16).padding(.vertical, 12)
            .background(notice.kind == .error ? KriaColor.failureSoft : KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
            .padding(.top, 14)
        }
        if model.collectionChanged {
            Button("Review latest collection") { Task { await model.loadLatestCollection() } }
                .font(KriaFont.body(14).weight(.semibold)).frame(minHeight: 44).accessibilityIdentifier("camera-roll-review-latest")
        }
    }
}
