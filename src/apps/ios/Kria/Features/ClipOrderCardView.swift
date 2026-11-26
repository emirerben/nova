import SwiftUI

/// What the chat hands the strip for the newest clip-order card in the thread. `interactive` is false once a video
/// exists (the timeline owns the order). `confirm` sends the whole order the creator sees. `previous` is the card
/// before this one, the base for each moved clip's "was N".
struct ClipOrderStripInput {
    let card: ClipOrderCard
    let previous: ClipOrderCard?
    let coordinator: ClipOrderCoordinator
    let interactive: Bool
    let confirm: (_ order: [String], _ moved: Set<String>) -> Void
}

extension ClipOrderCard {
    func attached(_ id: String, in media: [CreationAttachedMedia]) -> CreationAttachedMedia {
        media.first { $0.id == id }
            ?? CreationAttachedMedia(id: id, filename: "", kind: item(id)?.isPhoto == true ? "image" : "video", previewURL: nil)
    }
}

/// KRI-1591: the decided clip order under the draft message: a scroller of numbered clips (tap one to see it large),
/// then "Use this order" and "Edit order".
struct ClipOrderStripCard: View {
    let card: ClipOrderCard
    let previous: ClipOrderCard?
    @ObservedObject var coordinator: ClipOrderCoordinator
    let interactive: Bool
    let media: [CreationAttachedMedia]
    let projectID: UUID?
    let confirm: (_ order: [String], _ moved: Set<String>) -> Void

    init(_ input: ClipOrderStripInput, media: [CreationAttachedMedia], projectID: UUID?) {
        card = input.card
        previous = input.previous
        coordinator = input.coordinator
        interactive = input.interactive
        self.media = media
        self.projectID = projectID
        confirm = input.confirm
    }

    @ScaledMetric(relativeTo: .caption) private var tileWidth: CGFloat = 72
    @ScaledMetric(relativeTo: .caption) private var captionSlot: CGFloat = 20

    var body: some View {
        let shown = coordinator.display(for: card)
        let canEdit = interactive && card.allowsEditing
        let phase = ClipOrderCoordinator.phase(
            status: coordinator.status, confirmed: card.confirmed && shown.order == card.orderedMediaIDs, rebased: card.rebased,
            order: shown.order, failure: String(localized: "Kria couldn’t save this order. Try again."))
        let base = coordinator.showsPending(for: card) ? card.orderedMediaIDs : previous?.orderedMediaIDs
        let was = ClipOrderWas.positions(base: base, order: shown.order, moved: shown.moved)
        KriaQuestionCard {
            header(count: shown.order.count, moved: shown.moved.count)
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(alignment: .top, spacing: 6) {
                    ForEach(Array(shown.order.enumerated()), id: \.element) { index, id in
                        tile(id, number: index + 1, total: shown.order.count, moved: shown.moved.contains(id), was: was[id])
                    }
                }
                .frame(height: tileWidth * 1.5 + (was.isEmpty ? 0 : captionSlot))
            }
            .contentMargins(.horizontal, 16, for: .scrollContent)
            .padding(.horizontal, -16)
            .excludesDrawerGesture()
            .accessibilityIdentifier("clip-order-tiles")
            if canEdit {
                actions(shown: shown, phase: phase)
            } else {
                Text("The order that plays")
                    .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("clip-order-readonly")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityValue(shown.order.joined(separator: ","))
        .accessibilityIdentifier("clip-order-strip")
    }

    private func header(count: Int, moved: Int) -> some View {
        let clips = String(localized: "\(count) clips")
        return VStack(alignment: .leading, spacing: 2) {
            Text("Clip order")
                .kriaType(.heading).foregroundStyle(KriaColor.ink)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("clip-order-header")
            Text(moved > 0 ? clips + " · " + String(localized: "\(moved) moved") : clips)
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier("clip-order-count")
        }
    }

    private func tile(_ id: String, number: Int, total: Int, moved: Bool, was: Int?) -> some View {
        let item = card.item(id)
        let label = item.map { ClipOrderLabel.text($0, number: number) } ?? String(localized: "Clip \(number)")
        let words = label == String(localized: "Clip \(number)") ? nil : label
        return VStack(spacing: 4) {
            Button {
                guard let item else { return }
                var instant = Transaction(); instant.disablesAnimations = true
                withTransaction(instant) {
                    coordinator.preview = ClipOrderPreviewSeed(item: item, media: card.attached(id, in: media), number: number, total: total, was: was)
                }
            } label: {
                ClipThumbnailView(media: card.attached(id, in: media), position: number, projectID: projectID, name: label)
                    .frame(width: tileWidth, height: tileWidth * 1.5)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(alignment: .topLeading) { NumberBadge(number: number, moved: moved).padding(6) }
                    .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(String(localized: "Clip \(number), open preview"))
            .accessibilityValue(words ?? "")
            .accessibilityIdentifier("clip-order-tile-\(id)")
            if let was {
                Text(ClipOrderWas.text(was))
                    .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .lineLimit(1).minimumScaleFactor(0.8)
                    .accessibilityIdentifier("clip-order-moved-\(id)")
            }
        }
        .frame(width: tileWidth)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("clip-order-card-\(id)")
    }

    @ViewBuilder private func actions(shown: (order: [String], moved: Set<String>), phase: AskPhase<[String]>) -> some View {
        VStack(spacing: 8) {
            switch phase {
            case .failed(let text):
                KriaInlineFailure(text: text).accessibilityIdentifier("clip-order-failed")
            case .stale:
                KriaInlineNotice(text: String(localized: "The order changed while you were editing. Your move was applied on top."))
                    .accessibilityIdentifier("clip-order-notice")
            default:
                EmptyView()
            }
            primary(shown: shown, phase: phase)
            Button("Edit order") {
                coordinator.sheet = ClipOrderSheetSeed(card: card, order: shown.order)
            }
            .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
            .accessibilityIdentifier("clip-order-change")
        }
    }

    @ViewBuilder private func primary(shown: (order: [String], moved: Set<String>), phase: AskPhase<[String]>) -> some View {
        switch phase {
        case .answered:
            KriaAnsweredReceipt(text: String(localized: "Order confirmed"))
                .accessibilityIdentifier("clip-order-receipt")
        case .submitting:
            Button("Sending order") {}
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: true))
                .accessibilityIdentifier("clip-order-use")
        case .failed:
            Button("Try again") { confirm(shown.order, shown.moved) }
                .buttonStyle(KriaButtonStyle(role: .ink, fullWidth: true))
                .accessibilityIdentifier("clip-order-use")
        case .open, .stale:
            Button("Use this order") { confirm(shown.order, shown.moved) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .accessibilityIdentifier("clip-order-use")
        }
    }
}

/// The position badge on a clip's top-left corner: ink when the creator moved the clip, white otherwise.
private struct NumberBadge: View {
    let number: Int
    let moved: Bool
    var body: some View {
        Text(verbatim: "\(number)")
            .kriaType(.caption).fontWeight(.semibold).foregroundStyle(moved ? Color.white : KriaColor.ink)
            .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
            .frame(minWidth: 22, minHeight: 22).padding(.horizontal, 2)
            .background(moved ? KriaColor.ink : Color.white, in: Capsule())
            .accessibilityHidden(true)
    }
}

/// The badge that says where a clip's place came from.
private struct OriginBadge: View {
    let origin: ClipOrderCard.Origin
    var body: some View {
        let yours = origin == .yours
        Text(title)
            .kriaType(.caption).foregroundStyle(yours ? KriaColor.ink : KriaColor.mutedInk)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(yours ? KriaColor.sky : KriaColor.fill, in: Capsule())
    }

    private var title: String {
        switch origin {
        case .yours: String(localized: "Your order")
        case .upload: String(localized: "Upload order")
        case .ai: String(localized: "AI order")
        }
    }
}

/// "Reorder clips": every clip in play order. Drag to reorder (or use the Move up / Move down actions), tap a
/// thumbnail to see that clip larger, "Use this order" sends the whole order once. Closing sends nothing.
struct ClipOrderSheet: View {
    let seed: ClipOrderSheetSeed
    let media: [CreationAttachedMedia]
    let projectID: UUID?
    let confirm: (_ order: [String], _ moved: Set<String>) -> Void
    let close: () -> Void
    @State private var draft: ClipOrderDraft
    @State private var previewID: String?
    @ScaledMetric(relativeTo: .body) private var thumbWidth: CGFloat = 44

    init(seed: ClipOrderSheetSeed, media: [CreationAttachedMedia], projectID: UUID?,
         confirm: @escaping (_ order: [String], _ moved: Set<String>) -> Void, close: @escaping () -> Void) {
        self.seed = seed
        self.media = media
        self.projectID = projectID
        self.confirm = confirm
        self.close = close
        _draft = State(initialValue: ClipOrderDraft(order: seed.order))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            list
        }
        .background(KriaColor.paper)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Button("Use this order") { confirm(draft.order, draft.moved) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .accessibilityIdentifier("clip-order-sheet-use")
                .padding(.horizontal, 24).padding(.top, 12).padding(.bottom, 16)
                .frame(maxWidth: .infinity)
                .background(KriaColor.paper)
        }
        .accessibilityHidden(previewID != nil)
        .overlay {
            if let previewID, let item = seed.card.item(previewID) {
                ClipOrderLargePreview(
                    media: attached(previewID), item: item, number: draft.position(of: previewID) ?? item.position,
                    total: draft.order.count, was: draft.was(previewID), projectID: projectID, close: { self.previewID = nil })
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.18), value: previewID)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("clip-order-sheet")
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Reorder clips").kriaType(.title).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                Text("\(draft.order.count) clips").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("clip-order-sheet-count")
            }
            Spacer(minLength: 0)
            Button(action: close) { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                .buttonStyle(.kriaIcon)
                .accessibilityLabel("Close")
                .accessibilityIdentifier("clip-order-sheet-close")
        }
        .padding(.horizontal, 24).padding(.top, 16).padding(.bottom, 8)
    }

    // A List gives real drag-to-reorder (`onMove`) and scrolls itself, so 19 or 40 clips just work.
    private var list: some View {
        List {
            ForEach(draft.order, id: \.self) { id in
                row(id)
                    .listRowInsets(EdgeInsets(top: 6, leading: 24, bottom: 6, trailing: 12))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            .onMove { source, destination in
                guard let first = source.min(), draft.order.indices.contains(first) else { return }
                reorder(draft.order[first]) { $0.move(from: source, to: destination) }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .environment(\.editMode, .constant(.active))
        .accessibilityValue(draft.order.joined(separator: ","))
        .accessibilityIdentifier("clip-order-sheet-list")
    }

    private func attached(_ id: String) -> CreationAttachedMedia { seed.card.attached(id, in: media) }

    /// Applies a reorder and tells VoiceOver where the clip ended up (the row moves, so focus alone says little).
    private func reorder(_ id: String, _ change: (inout ClipOrderDraft) -> Void) {
        change(&draft)
        let number = seed.card.item(id)?.position ?? 0
        let position = draft.position(of: id) ?? 0
        UIAccessibility.post(notification: .announcement,
                             argument: String(localized: "Clip \(number) moved to position \(position) of \(draft.order.count)"))
    }

    private func row(_ id: String) -> some View {
        let item = seed.card.item(id)
        let position = draft.position(of: id) ?? 0
        let label = item.map { ClipOrderLabel.text($0, number: position) } ?? String(localized: "Clip \(position)")
        let was = draft.was(id)
        let origin = was == nil ? item?.source.origin : nil
        return HStack(spacing: 12) {
            Button { previewID = id } label: {
                ClipThumbnailView(media: attached(id), position: position, projectID: projectID, name: label)
                    .frame(width: thumbWidth, height: thumbWidth * 58 / 44)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(String(localized: "Preview \(label)"))
            .accessibilityHint(String(localized: "Shows this clip larger"))
            .accessibilityIdentifier("clip-order-thumb-\(id)")
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: "\(position)  \(label)")
                    .kriaType(.button).foregroundStyle(KriaColor.ink)
                    .lineLimit(2).multilineTextAlignment(.leading)
                if let was {
                    Text(ClipOrderWas.text(was)).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("clip-order-sheet-was-\(id)")
                } else if let origin {
                    OriginBadge(origin: origin)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(rowLabel(label: label, position: position, origin: origin, was: was))
            // The actions sit on this element, which is a real accessibility element (a container's are never exposed).
            .accessibilityAction(named: "Move up") { if draft.canMoveUp(id) { reorder(id) { $0.moveUp(id) } } }
            .accessibilityAction(named: "Move down") { if draft.canMoveDown(id) { reorder(id) { $0.moveDown(id) } } }
            .accessibilityIdentifier("clip-order-row-\(id)")
        }
    }

    private func rowLabel(label: String, position: Int, origin: ClipOrderCard.Origin?, was: Int?) -> String {
        let base = String(localized: "\(label), position \(position) of \(draft.order.count)")
        if let was { return base + ", " + ClipOrderWas.text(was) }
        switch origin {
        case .yours: return base + ", " + String(localized: "Your order")
        case .upload: return base + ", " + String(localized: "Upload order")
        case .ai: return base + ", " + String(localized: "AI order")
        case nil: return base
        }
    }
}

/// The tapped clip, larger: the editor's fullscreen presentation (a scrim, a box 90% of the screen, tap or swipe to
/// leave) around the clip preview the clip picker already uses, which plays the local original muted and falls
/// back to the poster with a note when the original is not on this iPhone. Under it: "Clip 3 of 7 · was 1st".
struct ClipOrderLargePreview: View {
    let media: CreationAttachedMedia
    let item: ClipOrderCard.Item
    let number: Int
    let total: Int
    let was: Int?
    let projectID: UUID?
    let close: () -> Void

    private var caption: String {
        let place = String(localized: "Clip \(number) of \(total)")
        return was.map { place + " · " + ClipOrderWas.text($0) } ?? place
    }

    var body: some View {
        GeometryReader { proxy in
            let size = NativeEditorLayoutMetrics.fullscreenSize(
                screen: CGSize(width: proxy.size.width, height: proxy.size.height - 48), aspect: 9.0 / 16.0)
            ZStack(alignment: .topTrailing) {
                KriaColor.ink.opacity(0.72).ignoresSafeArea()
                VStack(spacing: 14) {
                    ClipPreviewPage(media: media, position: number, projectID: projectID, isCurrent: true,
                                    name: ClipOrderLabel.text(item, number: number),
                                    missingNote: String(localized: "This clip isn’t on this iPhone"))
                        .frame(width: size.width, height: size.height)
                        .background(KriaColor.fill)
                        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
                    Text(caption)
                        .kriaType(.button).foregroundStyle(Color.white)
                        .accessibilityIdentifier("clip-order-preview-caption")
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                Button(action: close) {
                    Image(systemName: "xmark").font(.system(size: 15, weight: .semibold))
                }
                .buttonStyle(KriaButtonStyle(role: .iconGlass, size: .medium))
                .padding(12)
                .accessibilityLabel("Close preview")
                .accessibilityIdentifier("clip-order-preview-close")
            }
            .contentShape(Rectangle())
            .onTapGesture(perform: close)
            .gesture(DragGesture(minimumDistance: 24).onEnded { value in
                if abs(value.translation.height) > 60 { close() }
            })
            .accessibilityElement(children: .contain)
            .accessibilityAddTraits(.isModal)
            .accessibilityAction(.escape, close)
            .accessibilityIdentifier("clip-order-preview")
        }
    }
}
