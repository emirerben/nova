import SwiftUI
import Photos
import PhotosUI

/// Which idea the detail page shows, and whether it should start building it on arrival ("Use this idea").
struct CameraRollIdeaRoute: Hashable, Identifiable {
    let group: String
    let storyID: UUID
    let autoSelect: Bool
    var id: String { "\(group)/\(storyID)/\(autoSelect)" }
}

/// The full-screen Camera roll (Paper boards A to A8). It is workspace content beside the projects drawer, not a
/// sheet: the menu circle and the edge swipe still open the drawer. Footage is a contact sheet grouped by event;
/// each event has its own "Find ideas" and its own ideas. Every tile reads its state from `CameraRollItemState`.
struct CameraRollSurfaceView: View {
    @Bindable var model: CameraRollSurfaceModel
    let openProjects: () -> Void
    let onSelected: @MainActor (CreationThread) async -> Void

    @Environment(\.projectsDrawerOpen) private var drawerOpen
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("kria.cameraRoll.limitedAcknowledged") private var limitedAcknowledged = false
    @State private var showsPicker = false
    @State private var showsSaved = false
    @State private var pickerSelection: [PhotosPickerItem] = []
    @State private var route: CameraRollIdeaRoute?
    @State private var showsTaste = false
    @State private var confirmingDelete: String?
    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 3)

    var body: some View {
        NavigationStack {
            ZStack(alignment: .bottom) {
                VStack(spacing: 0) {
                    if let selecting = model.selecting, let event = model.events.first(where: { $0.id == selecting.group }) {
                        CameraRollEventEditor(model: model, event: event)
                    } else {
                        header
                        content
                    }
                }
                overlays
            }
            .background(KriaColor.paper)
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(item: $route) { route in detail(route) }
            .navigationDestination(isPresented: $showsTaste) {
                CameraRollTasteView(api: model.api) { model.tasteChanged($0) }
            }
        }
        .kriaPage()
        .photosPicker(isPresented: $showsPicker, selection: $pickerSelection, maxSelectionCount: max(1, model.remainingCapacity),
                      matching: .any(of: [.images, .videos]), photoLibrary: .shared())
        .onChange(of: pickerSelection) { _, picked in model.add(picked); pickerSelection = [] }
        .sheet(isPresented: $showsSaved) {
            CameraRollSavedPicker(api: model.api, capacity: model.remainingCapacity) { model.addSaved($0) }
        }
        .confirmationDialog("Open the updated collection?", isPresented: $model.showsLatestReview, titleVisibility: .visible) {
            Button("Open latest collection") { Task { await model.adoptLatestCollection() } }
            Button("Keep my local collection", role: .cancel) { }
        } message: {
            Text("\(model.latestCollection?.name ?? model.collection.name) · \(model.latestCollection?.members?.count ?? 0) items. Your local changes will remain available as a separate collection.")
        }
        .task { await model.start() }
        .onChange(of: scenePhase) { _, phase in model.scenePhaseChanged(active: phase == .active) }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: model.board.undo != nil)
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            CameraRollCircleButton(identifier: "workspace-menu-toggle", label: drawerOpen ? "Close projects" : "Open projects", action: openProjects) {
                KriaIcon(.menu)
            }
            Text("Camera roll").font(KriaFont.body(15).weight(.semibold)).lineLimit(1).frame(maxWidth: .infinity)
                .accessibilityHidden(drawerOpen).accessibilityAddTraits(.isHeader).accessibilityIdentifier("camera-roll-title")
            if model.tasteAvailable {
                CameraRollCircleButton(identifier: "camera-roll-taste", label: "Your taste", action: { showsTaste = true }) {
                    Image(systemName: "slider.horizontal.3").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink)
                }.accessibilityHidden(drawerOpen)
            } else { Color.clear.frame(width: 44, height: 44) }
        }
        .padding(.horizontal, 16).padding(.top, 4).frame(minHeight: 52).foregroundStyle(KriaColor.ink)
    }

    // MARK: Content

    private var content: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                banners
                if model.isEmpty {
                    emptyState
                } else {
                    progressHeader
                    ForEach(model.events) { event in eventSection(event) }
                }
            }
            .padding(.horizontal, 20).padding(.bottom, 140)
        }
        .scrollDismissesKeyboard(.interactively)
        .kriaScrollEdgeFade()
        .accessibilityHidden(drawerOpen)
    }

    @ViewBuilder private var banners: some View {
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
        if model.accessRemovedCount > 0 {
            VStack(alignment: .leading, spacing: 10) {
                Text("Photos access was turned off. \(model.accessRemovedCount) \(model.accessRemovedCount == 1 ? "item was" : "items were") removed from this camera roll.")
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                Button {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    model.dismissAccessRemoved()
                } label: {
                    Text("Open Settings").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 16).frame(height: 40).background(KriaColor.failureText, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
                }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-open-settings")
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(16)
            .background(KriaColor.failureSoft, in: RoundedRectangle(cornerRadius: 24, style: .continuous)).padding(.top, 14)
            .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-access-removed")
        }
        if model.permission == .limited, !limitedAcknowledged, model.mode != .metadataOnly {
            VStack(alignment: .leading, spacing: 10) {
                Text("When Photos access is limited").cameraRollKicker()
                Text("Kria can only see the \(model.sharedItemCount) items you shared. Everything else stays private.")
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                HStack(spacing: 8) {
                    Button { presentLimitedPicker() } label: { smallChip("Share more", muted: false) }
                        .buttonStyle(.plain).accessibilityIdentifier("camera-roll-share-more")
                    Button { limitedAcknowledged = true } label: { smallChip("Keep as is", muted: true) }
                        .buttonStyle(.plain).accessibilityIdentifier("camera-roll-keep-limited")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading).padding(16)
            .background(KriaColor.menu, in: RoundedRectangle(cornerRadius: 24, style: .continuous)).padding(.top, 14)
            .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-limited-access")
        }
    }

    private func smallChip(_ title: String, muted: Bool) -> some View {
        Text(title).font(KriaFont.body(14).weight(.semibold)).foregroundStyle(muted ? KriaColor.mutedInk : KriaColor.ink)
            .padding(.horizontal, 16).frame(height: 40).background(Color.white, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("What did you film lately?").cameraRollTitle()
            Text("Pick a weekend, a trip or a whole month. Kria looks through it and finds the videos worth making.")
                .font(KriaFont.body(16).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
            VStack(spacing: 10) {
                Button { model.beginAdding(toGroup: nil); showsPicker = true } label: {
                    Text("Choose photos and videos").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                }.buttonStyle(.plain).disabled(!model.canAdd).accessibilityIdentifier("camera-roll-choose")
                Button { model.beginAdding(toGroup: nil); showsSaved = true } label: {
                    Text("Use saved uploads").font(KriaFont.body(16).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.fill, in: Capsule()).contentShape(Capsule())
                }.buttonStyle(.plain).disabled(model.mode != .live).accessibilityIdentifier("camera-roll-saved")
            }.padding(.top, 8)
            Text("Kria uploads small copies to look at. Full-quality videos stay on your iPhone.")
                .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
        }
        .padding(.horizontal, 8).padding(.top, 56)
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-empty")
    }

    @ViewBuilder private var progressHeader: some View {
        let progress = model.progress
        if progress.isWorking || progress.needRetry > 0 {
            VStack(alignment: .leading, spacing: 10) {
                Text(progress.isWorking ? "Looking at your footage" : "Almost there").cameraRollTitle()
                KriaProgressTrack(progress: progress.fraction)
                HStack {
                    Text("\(progress.done) of \(progress.total) done").font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    Spacer(minLength: 8)
                    if progress.needRetry > 0 {
                        Button { model.retryAllFailed() } label: {
                            Text("\(progress.needRetry) need a retry · Try all")
                                .font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                                .padding(.horizontal, 12).frame(height: 32).background(KriaColor.failureSoft, in: Capsule())
                                .frame(minHeight: 44).contentShape(Capsule())
                        }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-retry-all")
                    }
                }
            }
            .padding(.top, 18).padding(.bottom, 6).accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-progress")
        }
    }

    // MARK: Events

    private func eventSection(_ event: CameraRollEvent) -> some View {
        let ideas = model.ideas(in: event.id)
        let generating = model.isGenerating(event.id)
        let collapsible = event.entries.count > 3 && (!ideas.isEmpty || generating || model.finderGroup == event.id)
        let isCollapsed = collapsible && !model.expanded.contains(event.id)
        return VStack(alignment: .leading, spacing: 12) {
            eventHeader(event, ideas: ideas, generating: generating)
            if isCollapsed { collapsedStrip(event) } else { grid(event, showsHide: collapsible) }
            if model.finderGroup == event.id, !generating {
                CameraRollFinderPanel(model: model, event: event) { showsTaste = true }
            }
            if generating { CameraRollWorkingCard(ready: model.readyCount(in: event), identifier: "camera-roll-working-\(event.id)") }
            if !ideas.isEmpty { ideasSection(event, ideas: ideas, generating: generating) }
        }
        .padding(.top, 22)
        .accessibilityElement(children: .contain).accessibilityIdentifier("camera-roll-event-\(event.id)")
    }

    private func eventHeader(_ event: CameraRollEvent, ideas: [CameraRollStory], generating: Bool) -> some View {
        let ready = model.readyCount(in: event)
        return HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.title(for: event.id)).cameraRollKicker().lineLimit(1).accessibilityAddTraits(.isHeader)
                Text(subtitle(event, ready: ready)).font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            HStack(spacing: 0) {
                if generating && ideas.isEmpty {
                    HStack(spacing: 6) { KriaSpinner(size: 12); Text("Finding").font(KriaFont.body(13).weight(.bold)) }
                        .foregroundStyle(KriaColor.ink).padding(.horizontal, 14).frame(height: 34).background(KriaColor.butter.opacity(0.6), in: Capsule())
                } else if ideas.isEmpty {
                    Button { withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { model.finderGroup = model.finderGroup == event.id ? nil : event.id } } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "sparkle").font(.system(size: 11, weight: .bold))
                            Text("Find ideas").font(KriaFont.body(13).weight(.bold))
                        }
                        .foregroundStyle(KriaColor.ink).padding(.horizontal, 14).frame(height: 34).background(KriaColor.butter, in: Capsule())
                        .frame(minHeight: 44).contentShape(Capsule())
                    }
                    .buttonStyle(.plain).disabled(ready == 0).opacity(ready == 0 ? 0.45 : 1)
                    .accessibilityIdentifier("camera-roll-find-ideas-\(event.id)")
                } else {
                    HStack(spacing: 6) {
                        KriaCheckShape().stroke(KriaColor.success, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round)).frame(width: 12, height: 12)
                        Text("\(ideas.count) \(ideas.count == 1 ? "idea" : "ideas")").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.success)
                    }
                    .padding(.horizontal, 10).frame(height: 28).background(KriaColor.successSoft, in: Capsule())
                    .accessibilityElement(children: .combine).accessibilityIdentifier("camera-roll-idea-count-\(event.id)")
                }
                eventMenu(event, ideaCount: ideas.count)
            }
        }
    }

    private func subtitle(_ event: CameraRollEvent, ready: Int) -> String {
        var parts = ["\(event.entries.count) \(event.entries.count == 1 ? "item" : "items")"]
        let waiting = model.waitingCount(in: event)
        if waiting == 0 { parts.append(ready == event.entries.count ? "all ready" : "\(ready) ready") }
        else { parts.append("\(ready) ready"); parts.append("\(waiting) still looking") }
        return parts.joined(separator: " · ")
    }

    private func eventMenu(_ event: CameraRollEvent, ideaCount: Int) -> some View {
        Menu {
            Button { model.beginSelecting(in: event.id, renaming: true) } label: { Label("Rename", systemImage: "pencil") }
            Button { model.beginSelecting(in: event.id) } label: { Label("Select and move items", systemImage: "checkmark.square") }
            Button { model.beginAdding(toGroup: event.id); showsPicker = true } label: { Label("Add photos and videos", systemImage: "plus") }
                .disabled(!model.canAdd)
            Button(role: .destructive) {
                if ideaCount == 0 { model.deleteEvent(event.id) } else { confirmingDelete = event.id }
            } label: { Label("Delete event", systemImage: "trash") }
        } label: { CameraRollDots() }
            .accessibilityLabel("Event options").accessibilityIdentifier("camera-roll-event-menu-\(event.id)")
    }

    private func grid(_ event: CameraRollEvent, showsHide: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            LazyVGrid(columns: columns, spacing: 8) {
                ForEach(event.entries) { entry in tile(entry) }
            }
            if showsHide {
                Button { model.expanded.remove(event.id) } label: {
                    Text("Show fewer").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk).frame(minHeight: 44)
                }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-collapse-\(event.id)")
            }
        }
    }

    private func tile(_ entry: CameraRollCollection.Entry, aspect: CGFloat = 1) -> some View {
        CameraRollTileView(entry: entry, tile: model.store.tile(entry.id), api: model.api, thumbnailsDisabled: model.thumbnailsDisabled,
                           aspect: aspect, onRetry: { model.retry(entry) })
    }

    private func collapsedStrip(_ event: CameraRollEvent) -> some View {
        let aspect: CGFloat = 112.0 / 72.0
        return LazyVGrid(columns: columns, spacing: 8) {
            ForEach(event.entries.prefix(2)) { entry in tile(entry, aspect: aspect) }
            Button { model.expanded.insert(event.id) } label: {
                Color.clear.aspectRatio(aspect, contentMode: .fit)
                    .overlay { Text("+\(event.entries.count - 2)").font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.mutedInk) }
                    .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
            .buttonStyle(.plain).accessibilityLabel("Show all \(event.entries.count) items").accessibilityIdentifier("camera-roll-expand-\(event.id)")
        }
    }

    private func ideasSection(_ event: CameraRollEvent, ideas: [CameraRollStory], generating: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Ideas for this event").cameraRollKicker().padding(.top, 6).accessibilityAddTraits(.isHeader)
            ForEach(Array(ideas.enumerated()), id: \.element.id) { index, story in
                CameraRollIdeaCard(model: model, story: story, event: event, isFirst: index == 0) { autoSelect in
                    route = CameraRollIdeaRoute(group: event.id, storyID: story.id, autoSelect: autoSelect)
                }
            }
            Button { model.anotherIdea(in: event.id) } label: {
                HStack(spacing: 8) {
                    if generating { KriaSpinner(size: 14) } else { Image(systemName: "sparkle").font(.system(size: 12, weight: .bold)) }
                    Text(generating ? "Finding another" : "Another idea").font(KriaFont.body(15).weight(.bold))
                }
                .foregroundStyle(KriaColor.ink).padding(.horizontal, 18).frame(height: 44).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
            }
            .buttonStyle(.plain).disabled(generating || model.readyCount(in: event) == 0).opacity(generating ? 0.6 : 1)
            .accessibilityIdentifier("camera-roll-another-idea-\(event.id)")
        }
    }

    // MARK: Overlays

    @ViewBuilder private var overlays: some View {
        if let id = confirmingDelete, let event = model.events.first(where: { $0.id == id }) {
            ZStack(alignment: .bottom) {
                KriaColor.ink.opacity(0.28).ignoresSafeArea().onTapGesture { confirmingDelete = nil }
                    .accessibilityHidden(true)
                CameraRollDeleteEventCard(title: model.title(for: id), itemCount: event.entries.count, ideaCount: model.ideas(in: id).count,
                                          delete: { confirmingDelete = nil; model.deleteEvent(id) }, keep: { confirmingDelete = nil })
                    .padding(.horizontal, 16).padding(.bottom, 24)
            }.transition(.opacity)
        } else if let undo = model.board.undo, model.selecting == nil {
            CameraRollUndoToast(message: undoMessage(undo)) { model.undo() }
                .padding(.horizontal, 12).padding(.bottom, 28)
                .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
        }
    }

    private func undoMessage(_ undo: CameraRollIdeaBoard.Undo) -> String {
        switch undo { case .idea: "Idea deleted"; case .event: "Event deleted" }
    }

    @ViewBuilder private func detail(_ route: CameraRollIdeaRoute) -> some View {
        if let story = model.board.storiesByGroup[route.group]?.first(where: { $0.id == route.storyID }) {
            CameraRollIdeaDetailScreen(model: model, group: route.group, story: story, autoSelect: route.autoSelect, onSelected: onSelected)
        } else {
            VStack(spacing: 0) {
                CameraRollPageHeader(title: model.title(for: route.group))
                Text("This idea was removed.").font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk).frame(maxHeight: .infinity)
            }.toolbar(.hidden, for: .navigationBar)
        }
    }

    private func presentLimitedPicker() {
        guard let controller = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow?.rootViewController }).first else { return }
        PHPhotoLibrary.shared().presentLimitedLibraryPicker(from: controller)
    }
}

// MARK: - Idea detail (Paper A4)

/// One idea on its own page. The review itself (clips, opening text, open questions, research, "Use this idea")
/// is `CameraRollStoriesView` in its embedded form; this adds the page chrome and wires its changes back.
struct CameraRollIdeaDetailScreen: View {
    let model: CameraRollSurfaceModel
    let group: String
    let story: CameraRollStory
    let autoSelect: Bool
    let onSelected: @MainActor (CreationThread) async -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            CameraRollPageHeader(title: model.title(for: group))
            CameraRollStoriesView(
                api: model.api, collection: model.collection,
                result: CameraRollStories(stories: [story], coverage: model.coverage[group]),
                cacheOnly: model.cacheOnlyRequired, embedded: true, thumbnailsDisabled: model.thumbnailsDisabled, autoSelect: autoSelect,
                onSelected: onSelected,
                onReviewed: { reviewed in for changed in reviewed.stories { model.replaceStory(changed) } },
                onDelete: { model.deleteIdea(story, in: group); dismiss() })
        }
        .background(KriaColor.paper).toolbar(.hidden, for: .navigationBar)
    }
}

// MARK: - Edit an event (Paper A5)

/// Rename an event and move items between events, in place. Tapping a destination moves what is selected right
/// away; "Done" tells the server.
struct CameraRollEventEditor: View {
    @Bindable var model: CameraRollSurfaceModel
    let event: CameraRollEvent
    @State private var name = ""
    @State private var loaded = false
    @FocusState private var nameFocused: Bool
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 3)

    var body: some View {
        let selected = model.selecting?.selected ?? []
        ZStack(alignment: .bottom) {
            VStack(spacing: 0) {
                HStack {
                    Button { model.cancelSelecting() } label: {
                        Text("Cancel").font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 16).frame(height: 44).kriaFloatingSurface(Capsule()).contentShape(Capsule())
                    }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-edit-cancel")
                    Spacer()
                    Text(selected.isEmpty ? "Select items" : "\(selected.count) selected").font(KriaFont.body(15).weight(.semibold))
                        .accessibilityIdentifier("camera-roll-edit-count")
                    Spacer()
                    Button { commitName(); model.finishSelecting() } label: {
                        Text("Done").font(KriaFont.body(15).weight(.bold)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 16).frame(height: 44).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                    }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-edit-done")
                }.padding(.horizontal, 16).padding(.top, 4).frame(minHeight: 52)
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Event name").cameraRollKicker()
                            HStack {
                                TextField("Event name", text: $name).font(KriaFont.body(17).weight(.bold)).submitLabel(.done)
                                    .focused($nameFocused).onSubmit { commitName() }
                                    .accessibilityIdentifier("camera-roll-event-name")
                                Image(systemName: "pencil").font(.system(size: 14, weight: .medium)).foregroundStyle(KriaColor.mutedInk).accessibilityHidden(true)
                            }
                            .padding(.horizontal, 18).frame(height: 52).background(KriaColor.fill, in: Capsule())
                        }
                        LazyVGrid(columns: columns, spacing: 8) {
                            ForEach(event.entries) { entry in
                                CameraRollTileView(entry: entry, tile: model.store.tile(entry.id), api: model.api,
                                                   thumbnailsDisabled: model.thumbnailsDisabled,
                                                   selection: .available(isSelected: selected.contains(entry.id)),
                                                   onToggle: { model.toggleSelected(entry.id) })
                            }
                        }
                    }.padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 170)
                }.scrollDismissesKeyboard(.interactively).kriaScrollEdgeFade()
            }
            movePanel(selectedCount: selected.count)
        }
        .onAppear {
            guard !loaded else { return }
            loaded = true; name = model.title(for: event.id)
            if model.selecting?.renaming == true { nameFocused = true }
        }
    }

    private func commitName() {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { model.rename(group: event.id, to: trimmed) }
    }

    private func movePanel(selectedCount: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(selectedCount == 0 ? "Tap items to move them" : "Move \(selectedCount) to").cameraRollKicker().padding(.horizontal, 4)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(model.moveTargets(from: event.id)) { target in
                        Button { model.moveSelected(to: target.id) } label: {
                            Text(model.title(for: target.id)).font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                .padding(.horizontal, 16).frame(height: 40).background(KriaColor.fill, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
                        }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-move-to-\(target.id)")
                    }
                    Button { model.moveSelected(to: model.newEventKey()) } label: {
                        Text("New event").font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 16).frame(height: 40).background(KriaColor.butter, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
                    }.buttonStyle(.plain).accessibilityIdentifier("camera-roll-move-to-new")
                }
            }
            .disabled(selectedCount == 0).opacity(selectedCount == 0 ? 0.45 : 1)
        }
        .padding(12).background(Color.white, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .shadow(color: KriaColor.ink.opacity(0.18), radius: 28, y: 6).padding(.horizontal, 12).padding(.bottom, 28)
    }
}
