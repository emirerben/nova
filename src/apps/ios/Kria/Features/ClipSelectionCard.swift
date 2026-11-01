import AVKit
import SwiftUI

/// How a clip question is shown under its message (KRI-282).
enum ClipSelectionCardMode {
    /// The latest unanswered question: interactive. `submit` receives the structured answer and its readable message.
    case active(isSending: Bool, submit: (ClipSelectionSubmission, String) -> Void)
    /// Already answered (or superseded): read-only, collapsed. `summary` is nil after a relaunch.
    case answered(summary: String?)
}

/// The clip question under the assistant's text: a compact summary card whose "Choose clips" button opens a
/// full-height grid picker (`ClipPickerSheet`). The chat stays short however many clips the thread has.
struct ClipSelectionCard: View {
    let question: ClipQuestion
    let media: [CreationAttachedMedia]
    let projectID: UUID?
    let mode: ClipSelectionCardMode
    /// The language of the answer this card posts (the chat's reply language, KRI-694).
    let language: ClipAnswerLanguage
    /// KRI-802: what happens next while the chosen clips wait for the reply's other answer.
    let note: String?
    @State private var state: ClipSelectionState
    @State private var showsPicker = false

    init(question: ClipQuestion, media: [CreationAttachedMedia], projectID: UUID? = nil, mode: ClipSelectionCardMode,
         language: ClipAnswerLanguage = .english, note: String? = nil) {
        self.question = question
        self.media = media
        self.projectID = projectID
        self.mode = mode
        self.language = language
        self.note = note
        _state = State(initialValue: ClipSelectionState(question: question))
    }

    private var positions: [String: Int] { ClipPositions.map(media: media, question: question) }

    var body: some View {
        content
            // On the wrapper (not the active card) so Done can flip the card to its answered summary while the
            // sheet is still dismissing.
            .sheet(isPresented: $showsPicker) {
                ClipPickerSheet(question: question, state: $state, media: media, positions: positions, projectID: projectID, isSending: isSendingNow, language: language) { submission, text in
                    if case .active(_, let submit) = mode { submit(submission, text) }
                }
            }
    }

    private var isSendingNow: Bool {
        if case .active(let isSending, _) = mode { return isSending }
        return true
    }

    @ViewBuilder private var content: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                Text(summary ?? "Clips chosen")
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("clip-card-answered")
        case .active(let isSending, let submit):
            activeCard(isSending: isSending, submit: submit)
        }
    }

    private var title: String {
        question.categories.count == 1 ? "Which clips show \(question.categories[0].label)?" : "Which clips are which?"
    }

    private func activeCard(isSending: Bool, submit: @escaping (ClipSelectionSubmission, String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "film.stack")
                    .font(.system(size: 18, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 40, height: 40)
                    .background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(KriaFont.body(16).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                    ForEach(question.categories) { category in
                        Text(progressLine(category))
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                            .accessibilityIdentifier("clip-progress-\(category.key)")
                    }
                    if let note {
                        Text(note)
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("clip-batch-note")
                    }
                }
            }
            VStack(spacing: 8) {
                Button { showsPicker = true } label: {
                    Text(state.canSend ? "Review clips" : "Choose clips").frame(maxWidth: .infinity)
                }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 48))
                .disabled(isSending)
                .accessibilityIdentifier("clip-choose")
                Button("Skip, decide for me") {
                    let submission = ClipSelectionSubmission.skip(question)
                    submit(submission, submission.message(question: question, positions: positions, language: language))
                }
                .font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
                .buttonStyle(.plain)
                .disabled(isSending)
                .accessibilityIdentifier("clip-skip")
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("clip-card")
    }

    private func progressLine(_ category: ClipQuestion.Category) -> String {
        let total = category.candidateMediaIDs.count
        let prefix = question.categories.count == 1 ? "" : "\(category.label) · "
        if state.isNone(category.key) { return prefix + "None of these" }
        return prefix + "\(state.count(in: category.key)) of \(total) selected"
    }
}

// MARK: - Picker sheet

/// Full-height grid of 9:16 clip tiles with number badges. A tap opens the clip (`ClipPreviewPager`); the circle on a
/// tile, or a slide across tiles, selects. A sticky bar carries the count, "None of these" and the main button:
/// "Next" while another group is unseen or unanswered (a pre-ticked one included), then "Done", which sends every
/// group's answer at once.
struct ClipPickerSheet: View {
    let question: ClipQuestion
    @Binding var state: ClipSelectionState
    let media: [CreationAttachedMedia]
    let positions: [String: Int]
    let projectID: UUID?
    let isSending: Bool
    let language: ClipAnswerLanguage
    let submit: (ClipSelectionSubmission, String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var categoryKey: String
    @State private var previewing: PreviewTarget?
    @State private var doneToken = 0
    /// Selection as it was when a slide-to-select began; each update re-applies the touched range onto it.
    @State private var dragBase: ClipSelectionState?

    struct PreviewTarget: Identifiable { let index: Int; var id: Int { index } }

    init(question: ClipQuestion, state: Binding<ClipSelectionState>, media: [CreationAttachedMedia], positions: [String: Int],
         projectID: UUID?, isSending: Bool, language: ClipAnswerLanguage = .english,
         submit: @escaping (ClipSelectionSubmission, String) -> Void) {
        self.question = question
        _state = state
        self.media = media
        self.positions = positions
        self.projectID = projectID
        self.isSending = isSending
        self.language = language
        self.submit = submit
        _categoryKey = State(initialValue: question.categories[0].key)
    }

    private var category: ClipQuestion.Category { question.categories.first { $0.key == categoryKey } ?? question.categories[0] }
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 8), count: 3)

    var body: some View {
        VStack(spacing: 0) {
            header
            if question.categories.count > 1 { categoryPicker }
            DragSelectScrollView(
                isEnabled: !isSending,
                isSelected: { index in
                    guard category.candidateMediaIDs.indices.contains(index) else { return false }
                    return state.isSelected(category.candidateMediaIDs[index], in: category.key)
                },
                begin: { dragBase = state },
                apply: { indices, isOn in
                    guard let base = dragBase else { return }
                    let ids = category.candidateMediaIDs
                    var next = base
                    next.set(indices.compactMap { ids.indices.contains($0) ? ids[$0] : nil }, selected: isOn, in: category.key)
                    if next != state { state = next }
                },
                end: { dragBase = nil }
            ) {
                LazyVGrid(columns: columns, spacing: 8) {
                    ForEach(Array(category.candidateMediaIDs.enumerated()), id: \.element) { index, mediaID in
                        tile(mediaID).dragSelectTile(index: index)
                    }
                }
                .padding(.horizontal, 16).padding(.top, 4).padding(.bottom, 20)
            }
            // A new group starts at the top of its grid, like a new page.
            .id(category.key)
            .accessibilityIdentifier("clip-grid")
            bottomBar
        }
        .background(KriaColor.paper)
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("clip-sheet")
        .sensoryFeedback(.selection, trigger: state.selectedCount)
        .sensoryFeedback(.selection, trigger: categoryKey)
        .sensoryFeedback(.success, trigger: doneToken)
        // Next and the group tabs both change `categoryKey`; either way the creator has now seen that group.
        .onAppear { state.markShown(categoryKey) }
        .onChange(of: categoryKey) { _, key in state.markShown(key) }
        .sheet(item: $previewing) { target in
            ClipPreviewPager(
                ids: category.candidateMediaIDs, startIndex: target.index, state: $state, categoryKey: category.key,
                media: media, positions: positions, projectID: projectID
            )
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(question.categories.count == 1 ? "Which clips show \(category.label)?" : "Which clips are \(category.label)?")
                    .font(KriaFont.headline(22)).foregroundStyle(KriaColor.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("clip-sheet-title")
                Spacer(minLength: 8)
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("Close")
                .accessibilityIdentifier("clip-sheet-close")
            }
            HStack(spacing: 8) {
                Text("Tap a clip to watch it. Tap its circle, or slide across clips, to select.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                let allSelected = state.count(in: category.key) == category.candidateMediaIDs.count
                Button(allSelected ? "Clear" : "Select all") {
                    if allSelected { state.clear(in: category.key) } else { state.selectAll(in: category.key) }
                }
                .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                .frame(minWidth: 44, minHeight: 44)
                .accessibilityIdentifier(allSelected ? "clip-clear" : "clip-select-all")
            }
        }
        .padding(.leading, 16).padding(.trailing, 12).padding(.top, 18)
    }

    private var categoryPicker: some View {
        Picker("Group", selection: $categoryKey) {
            ForEach(question.categories) { item in
                Text("\(item.label) \(state.count(in: item.key))").tag(item.key)
            }
        }
        .pickerStyle(.segmented)
        .padding(.horizontal, 16).padding(.vertical, 6)
        .accessibilityIdentifier("clip-category-picker")
    }

    private func attached(_ mediaID: String) -> CreationAttachedMedia {
        media.first { $0.id == mediaID }
            ?? CreationAttachedMedia(id: mediaID, filename: "Clip \(positions[mediaID] ?? 0)",
                                     kind: question.isPhoto(mediaID) ? "image" : "video", previewURL: nil)
    }

    private func tile(_ mediaID: String) -> some View {
        let position = positions[mediaID] ?? 0
        let info = ClipTileInfo(mediaID: mediaID, position: position, question: question, media: media.first { $0.id == mediaID })
        let isOn = state.isSelected(mediaID, in: category.key)
        let suggested = category.suggestedMediaIDs.contains(mediaID)
        let open = { previewing = PreviewTarget(index: category.candidateMediaIDs.firstIndex(of: mediaID) ?? 0) }
        return ZStack(alignment: .bottomTrailing) {
            Button(action: open) {
                Color.clear
                    .aspectRatio(9.0 / 16.0, contentMode: .fit)
                    .overlay { ClipThumbnailView(media: attached(mediaID), position: position, projectID: projectID, name: info.name) }
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(alignment: .topLeading) {
                        Text("\(position)")
                            .font(KriaFont.body(12).weight(.bold)).foregroundStyle(Color.white)
                            .frame(minWidth: 22, minHeight: 22).padding(.horizontal, 3)
                            .background(KriaColor.ink.opacity(0.78), in: Capsule())
                            .padding(6)
                    }
                    .overlay(alignment: .bottomLeading) {
                        VStack(alignment: .leading, spacing: 4) {
                            // KRI-693: a photo says so on the tile, in the chat's language.
                            if info.isPhoto { photoName(info) }
                            if suggested {
                                Text("Suggested")
                                    .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                    .padding(.horizontal, 7).padding(.vertical, 3)
                                    .background(KriaColor.butter, in: Capsule())
                            }
                        }
                        // Clear of the select circle in the bottom-right corner.
                        .padding(.leading, 6).padding(.vertical, 6).padding(.trailing, 38)
                    }
                    .overlay {
                        RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .stroke(isOn ? KriaColor.ink : .clear, lineWidth: 3)
                    }
                    .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .buttonStyle(.plain)
            .disabled(isSending)
            .accessibilityLabel(info.name + (suggested ? ", suggested" : ""))
            .accessibilityValue(isOn ? "Selected" : "Not selected")
            .accessibilityAddTraits(isOn ? .isSelected : [])
            .accessibilityHint(info.isPhoto ? "Opens the photo" : "Opens the clip")
            .accessibilityAction(named: isOn ? "Deselect" : "Select") { state.toggle(mediaID, in: category.key) }
            .accessibilityIdentifier("clip-thumb-\(category.key)-\(mediaID)")
            Button { state.toggle(mediaID, in: category.key) } label: {
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 26)).foregroundStyle(Color.white, KriaColor.ink)
                    // A dim disc keeps the empty circle visible on a bright poster.
                    .background(Circle().fill(KriaColor.ink.opacity(isOn ? 0 : 0.3)).padding(2))
                    .padding(5)
                    .frame(width: 44, height: 44, alignment: .bottomTrailing)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isSending)
            .accessibilityLabel("Select \(info.name)")
            .accessibilityValue(isOn ? "Selected" : "Not selected")
            .accessibilityAddTraits(isOn ? .isSelected : [])
            .accessibilityIdentifier("clip-select-\(category.key)-\(mediaID)")
        }
    }

    private func photoName(_ info: ClipTileInfo) -> some View {
        HStack(spacing: 3) {
            Image(systemName: info.symbol).font(.system(size: 10, weight: .semibold))
            Text(info.name).font(KriaFont.body(11).weight(.semibold)).lineLimit(1)
        }
        .foregroundStyle(Color.white)
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(KriaColor.ink.opacity(0.78), in: Capsule())
        .accessibilityHidden(true)
    }

    private var bottomBar: some View {
        VStack(spacing: 10) {
            HStack {
                Text("\(state.count(in: category.key)) of \(category.candidateMediaIDs.count) selected")
                    .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityIdentifier("clip-count")
                Spacer()
                if question.allowNone {
                    let none = state.isNone(category.key)
                    Button { state.toggleNone(category.key) } label: {
                        HStack(spacing: 6) {
                            Image(systemName: none ? "checkmark.circle.fill" : "circle")
                            Text("None of these")
                        }
                        .font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 12).frame(minHeight: 44)
                        .background(none ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                        .overlay(Capsule().stroke(none ? KriaColor.ink : KriaColor.line, lineWidth: none ? 1.5 : 1))
                        .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(isSending)
                    .accessibilityAddTraits(none ? .isSelected : [])
                    .accessibilityIdentifier("clip-none-\(category.key)")
                }
            }
            let answered = state.isAnswered(category.key)
            let next = answered ? state.nextPending(after: category.key) : nil
            Button {
                if let next {
                    categoryKey = next.key
                    return
                }
                let submission = state.submission
                doneToken += 1
                submit(submission, submission.message(question: question, positions: positions, language: language))
                dismiss()
            } label: {
                Text(!answered ? "Pick a clip or None of these" : next == nil ? "Done" : "Next").frame(maxWidth: .infinity)
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 48))
            .disabled(!answered || isSending)
            .accessibilityHint(next.map { "Shows \($0.label)" } ?? "")
            .accessibilityIdentifier("clip-done")
        }
        .padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 10)
        .background(.regularMaterial)
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Thumbnail with fallbacks

/// Poster for one clip. Order: cached thumbnail -> the server's poster URL -> regenerated from the clip still on
/// this iPhone -> a labelled placeholder. Never a blank tile.
struct ClipThumbnailView: View {
    let media: CreationAttachedMedia
    let position: Int
    let projectID: UUID?
    /// What the placeholder calls the media; nil reads "Clip N". Photos are named by the server (KRI-693).
    let name: String?
    @State private var image: UIImage?

    init(media: CreationAttachedMedia, position: Int, projectID: UUID?, name: String? = nil) {
        self.media = media
        self.position = position
        self.projectID = projectID
        self.name = name
        _image = State(initialValue: UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: media.id).path))
    }

    var body: some View {
        ZStack {
            KriaColor.zinc.opacity(0.14)
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else if let url = media.previewURL {
                AsyncImage(url: url) { phase in
                    if let loaded = phase.image { loaded.resizable().scaledToFill() } else { placeholder }
                }
            } else {
                placeholder
            }
        }
        .clipped()
        .task(id: media.id) {
            guard image == nil else { return }
            if let regenerated = await CreationMediaPreview.image(mediaID: media.id, projectID: projectID) { image = regenerated }
        }
        .accessibilityHidden(true)
    }

    private var placeholder: some View {
        VStack(spacing: 4) {
            Image(systemName: media.kind == "image" ? "photo" : "film").font(.system(size: 20)).foregroundStyle(KriaColor.zinc)
            Text(name ?? "Clip \(position)").font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk)
        }
        .accessibilityIdentifier("clip-placeholder-\(media.id)")
    }
}

// MARK: - Preview pager

/// Full preview opened by tapping a tile: a horizontal pager over the same candidate list in grid order, starting at
/// the tapped clip. Each page autoplays muted and loops while it is the current one (the speaker control unmutes).
/// "Use this clip" picks the clip being shown and goes back to the grid; on a picked clip the button removes it
/// and stays. Swipe down or Close dismisses.
struct ClipPreviewPager: View {
    let ids: [String]
    @Binding var state: ClipSelectionState
    let categoryKey: String
    let media: [CreationAttachedMedia]
    let positions: [String: Int]
    let projectID: UUID?
    @State private var index: Int
    @Environment(\.dismiss) private var dismiss

    init(ids: [String], startIndex: Int, state: Binding<ClipSelectionState>, categoryKey: String,
         media: [CreationAttachedMedia], positions: [String: Int], projectID: UUID?) {
        self.ids = ids
        _state = state
        self.categoryKey = categoryKey
        self.media = media
        self.positions = positions
        self.projectID = projectID
        _index = State(initialValue: min(max(startIndex, 0), max(ids.count - 1, 0)))
    }

    private var currentID: String { ids[index] }
    private var position: Int { positions[currentID] ?? (index + 1) }
    private var currentInfo: ClipTileInfo {
        ClipTileInfo(mediaID: currentID, position: position, question: state.question, media: media.first { $0.id == currentID })
    }

    private func page(_ id: String, offset: Int) -> some View {
        let attached = media.first { $0.id == id }
        let number = positions[id] ?? (offset + 1)
        let info = ClipTileInfo(mediaID: id, position: number, question: state.question, media: attached)
        let fallbackKind = info.isPhoto ? "image" : "video"
        return ClipPreviewPage(
            media: attached ?? CreationAttachedMedia(id: id, filename: "Clip", kind: fallbackKind, previewURL: nil),
            position: number, projectID: projectID, isCurrent: offset == index, name: info.name
        )
    }

    var body: some View {
        VStack(spacing: 10) {
            HStack {
                Text(currentInfo.isPhoto ? currentInfo.name : "Clip \(position) of \(max(positions.count, ids.count))")
                    .font(KriaFont.headline(20)).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("clip-preview-title")
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("Close preview")
                .accessibilityIdentifier("clip-preview-close")
            }
            TabView(selection: $index) {
                ForEach(Array(ids.enumerated()), id: \.offset) { offset, id in
                    page(id, offset: offset).tag(offset)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .accessibilityIdentifier("clip-preview-pager")
            let isOn = state.isSelected(currentID, in: categoryKey)
            Text("\(index + 1) of \(ids.count) · swipe for the next clip")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier("clip-preview-index")
            Button {
                state.toggle(currentID, in: categoryKey)
                if !isOn { dismiss() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: isOn ? "checkmark.circle.fill" : "plus.circle")
                    Text(isOn ? "Selected" : "Use this clip")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(KriaPrimaryButtonStyle(fill: isOn ? KriaColor.ink : KriaColor.butter, usesLightText: isOn, minHeight: 48))
            .accessibilityLabel(isOn ? "Selected" : "Use this clip")
            .accessibilityValue(isOn ? "Selected" : "Not selected")
            .accessibilityHint(isOn ? "Removes this clip" : "Picks this clip and goes back to the grid")
            .accessibilityIdentifier("clip-preview-toggle")
        }
        .padding(16)
        .background(KriaColor.paper)
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .sensoryFeedback(.selection, trigger: index)
        .sensoryFeedback(.impact(weight: .light), trigger: state.selectedCount)
    }
}

/// One page: looping muted video of the clip when its original is still on this iPhone, otherwise the poster
/// (cached, regenerated, or a labelled placeholder) with a note. Never blank.
struct ClipPreviewPage: View {
    let media: CreationAttachedMedia
    let position: Int
    let projectID: UUID?
    let isCurrent: Bool
    var name: String? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var player: LoopingPlayer?
    @State private var muted = true
    @State private var hasLocalVideo: Bool?

    private var previewLabel: String {
        let photoName = name ?? "Photo"
        return media.kind == "image" ? "\(photoName) preview" : "Clip \(position) preview"
    }

    var body: some View {
        ZStack {
            ClipThumbnailView(media: media, position: position, projectID: projectID, name: name)
            if let player, isCurrent {
                // A bare layer: `VideoPlayer`'s hidden controls stall XCUITest (see `SongOrderPlayerSurface`).
                SongOrderPlayerSurface(player: player.player)
                    .onAppear { if !reduceMotion { player.player.play() } }
                    .onDisappear { player.player.pause() }
                    .overlay(alignment: .topTrailing) {
                        Button {
                            muted.toggle()
                            player.player.isMuted = muted
                        } label: {
                            Image(systemName: muted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                                .font(.system(size: 14, weight: .semibold)).foregroundStyle(Color.white)
                                .frame(width: 34, height: 34).background(KriaColor.ink.opacity(0.7), in: Circle())
                                .frame(width: 44, height: 44).contentShape(Rectangle())
                        }
                        .accessibilityLabel(muted ? "Unmute" : "Mute")
                        .accessibilityIdentifier("clip-preview-mute")
                    }
                    .overlay(alignment: .center) {
                        if reduceMotion {
                            Button { player.player.timeControlStatus == .playing ? player.player.pause() : player.player.play() } label: {
                                Image(systemName: "play.circle.fill").font(.system(size: 54)).foregroundStyle(Color.white)
                                    .frame(width: 64, height: 64)
                            }
                            .accessibilityLabel("Play or pause")
                        }
                    }
            } else if hasLocalVideo == false {
                VStack {
                    Spacer()
                    Text("This clip isn't stored on this iPhone, so only its poster can be shown.")
                        .font(KriaFont.body(12)).foregroundStyle(Color.white).multilineTextAlignment(.center)
                        .padding(8).background(KriaColor.ink.opacity(0.7), in: RoundedRectangle(cornerRadius: 8))
                        .padding(10)
                        .accessibilityIdentifier("clip-preview-not-on-device")
                }
            }
        }
        .aspectRatio(9.0 / 16.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task(id: isCurrent) {
            guard isCurrent else { player?.player.pause(); return }
            // A photo has no video to play: its poster is the whole picture, so there is no "poster only" note.
            guard media.kind != "image" else { return }
            if player == nil {
                if let url = ClipPreviewSource.localURL(mediaID: media.id, projectID: projectID) {
                    player = LoopingPlayer(url: url)
                    hasLocalVideo = true
                } else {
                    hasLocalVideo = false
                }
            } else if !reduceMotion {
                player?.player.play()
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(previewLabel)
        .accessibilityIdentifier("clip-preview-player-\(media.id)")
    }
}

/// A clip's original on this iPhone, for the preview (phone projects keep originals in the project directory).
@MainActor enum ClipPreviewSource {
    static func localURL(mediaID: String, projectID: UUID?) -> URL? {
        #if DEBUG
        // `KRIA_CHAT_CLIP_QUESTION=1`: the first fixture clip has a local original (a generated clip), the others
        // do not, so a UI test sees both the playing and the poster-only page.
        if ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "1", mediaID == "fixture-clip" {
            return SongTimelineFixture.clipURL(mediaID: mediaID, force: true)
        }
        #endif
        guard let projectID else { return nil }
        return CreationMediaPreview.localOriginal(mediaID: mediaID, projectID: projectID)
    }
}

@MainActor final class LoopingPlayer {
    let player = AVQueuePlayer()
    private var looper: AVPlayerLooper?
    init(url: URL) {
        player.isMuted = true
        looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
    }
}
