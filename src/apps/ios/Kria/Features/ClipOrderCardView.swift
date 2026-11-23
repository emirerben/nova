import SwiftUI

/// KRI-1591: how the clip-order strip is shown under a draft message.
enum ClipOrderCardMode {
    /// The newest card in the thread. `interactive` is false once a video exists (the timeline owns the order).
    /// `confirm` sends the whole order the creator sees.
    case current(card: ClipOrderCard, coordinator: ClipOrderCoordinator, interactive: Bool,
                 confirm: (_ order: [String], _ moved: Set<String>) -> Void)
    /// A newer card replaced this one.
    case superseded
}

/// The decided clip order under the draft message: numbered thumbnails, then "Change order" and "Use this order".
struct ClipOrderStripCard: View {
    let mode: ClipOrderCardMode
    let media: [CreationAttachedMedia]
    let projectID: UUID?

    var body: some View {
        switch mode {
        case .superseded:
            KriaSupersededLine().accessibilityIdentifier("clip-order-superseded")
        case .current(let card, let coordinator, let interactive, let confirm):
            ClipOrderStrip(card: card, coordinator: coordinator, interactive: interactive, media: media,
                           projectID: projectID, confirm: confirm)
        }
    }
}

private struct ClipOrderStrip: View {
    let card: ClipOrderCard
    @ObservedObject var coordinator: ClipOrderCoordinator
    let interactive: Bool
    let media: [CreationAttachedMedia]
    let projectID: UUID?
    let confirm: (_ order: [String], _ moved: Set<String>) -> Void
    @ScaledMetric(relativeTo: .caption) private var tileWidth: CGFloat = 64

    /// More than this many and the rest are summarised as "+N" (the sheet lists them all).
    private static let visibleCount = 6

    var body: some View {
        let shown = coordinator.display(for: card)
        let canEdit = interactive && card.allowsEditing
        KriaQuestionCard {
            Text("Order · \(shown.order.count) clips")
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("clip-order-header")
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(Array(shown.order.prefix(Self.visibleCount).enumerated()), id: \.element) { index, id in
                        tile(id, number: index + 1, total: shown.order.count, moved: shown.moved.contains(id))
                    }
                    if shown.order.count > Self.visibleCount {
                        overflow(shown.order.count - Self.visibleCount)
                    }
                }
            }
            .accessibilityIdentifier("clip-order-tiles")
            if canEdit {
                actions(shown: shown)
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

    private func attached(_ id: String) -> CreationAttachedMedia {
        media.first { $0.id == id }
            ?? CreationAttachedMedia(id: id, filename: "", kind: card.item(id)?.isPhoto == true ? "image" : "video", previewURL: nil)
    }

    private func tile(_ id: String, number: Int, total: Int, moved: Bool) -> some View {
        let item = card.item(id)
        let label = item.map { ClipOrderLabel.text($0, number: number) } ?? String(localized: "Clip \(number)")
        return VStack(alignment: .leading, spacing: 4) {
            ClipThumbnailView(media: attached(id), position: number, projectID: projectID, name: label)
                .frame(width: tileWidth, height: tileWidth * 84 / 64)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(alignment: .topLeading) { NumberBadge(number: number).padding(5) }
            Text(label)
                .kriaType(.caption).foregroundStyle(KriaColor.ink)
                .lineLimit(2).multilineTextAlignment(.leading)
                .frame(width: tileWidth, alignment: .leading)
                .accessibilityLabel(String(localized: "\(label), position \(number) of \(total)"))
                .accessibilityIdentifier("clip-order-label-\(id)")
            if moved {
                MovedTag().accessibilityIdentifier("clip-order-moved-\(id)")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("clip-order-card-\(id)")
    }

    private func overflow(_ count: Int) -> some View {
        Text(verbatim: "+\(count)")
            .kriaType(.button).foregroundStyle(KriaColor.mutedInk)
            .frame(width: tileWidth, height: tileWidth * 84 / 64)
            .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .accessibilityLabel(String(localized: "\(count) more clips"))
            .accessibilityIdentifier("clip-order-overflow")
    }

    @ViewBuilder private func actions(shown: (order: [String], moved: Set<String>)) -> some View {
        let confirmed = card.confirmed && coordinator.status == .idle && shown.order == card.orderedMediaIDs
        VStack(alignment: .leading, spacing: 8) {
            if coordinator.status == .failed {
                Text("Kria couldn’t save this order. Try again.")
                    .kriaType(.caption).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("clip-order-failed")
            }
            HStack(spacing: 8) {
                Button("Change order") {
                    coordinator.sheet = ClipOrderSheetSeed(card: card, order: shown.order)
                }
                .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, onFill: true))
                .accessibilityIdentifier("clip-order-change")
                Button(coordinator.status == .failed ? String(localized: "Try again") : confirmed ? String(localized: "Order confirmed") : String(localized: "Use this order")) {
                    confirm(shown.order, shown.moved)
                }
                .buttonStyle(KriaButtonStyle(role: .primary, size: .medium))
                .disabled(confirmed)
                .accessibilityIdentifier("clip-order-use")
            }
        }
    }
}

/// The dark numbered badge on a thumbnail's top-left corner.
private struct NumberBadge: View {
    let number: Int
    var body: some View {
        Text(verbatim: "\(number)")
            .kriaType(.caption).fontWeight(.bold).foregroundStyle(Color.white)
            .frame(minWidth: 20, minHeight: 20).padding(.horizontal, 3)
            .background(KriaColor.ink.opacity(0.78), in: Capsule())
            .accessibilityHidden(true)
    }
}

/// The small sky-blue "Moved" tag under a clip that changed place.
private struct MovedTag: View {
    var body: some View {
        Text("Moved")
            .kriaType(.caption).foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(KriaColor.sky, in: Capsule())
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

/// "Change order": every clip in play order. Drag to reorder (or use the Move up / Move down actions), tap a
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
                    projectID: projectID, close: { self.previewID = nil })
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
                Text("Change order").kriaType(.title).foregroundStyle(KriaColor.ink)
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

    private func attached(_ id: String) -> CreationAttachedMedia {
        media.first { $0.id == id }
            ?? CreationAttachedMedia(id: id, filename: "", kind: seed.card.item(id)?.isPhoto == true ? "image" : "video", previewURL: nil)
    }

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
        let origin = draft.moved.contains(id) ? ClipOrderCard.Origin.yours : item?.source.origin
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
                if let origin { OriginBadge(origin: origin) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(rowLabel(label: label, position: position, origin: origin))
            // The actions sit on this element, which is a real accessibility element (a container's are never exposed).
            .accessibilityAction(named: "Move up") { if draft.canMoveUp(id) { reorder(id) { $0.moveUp(id) } } }
            .accessibilityAction(named: "Move down") { if draft.canMoveDown(id) { reorder(id) { $0.moveDown(id) } } }
            .accessibilityIdentifier("clip-order-row-\(id)")
        }
    }

    private func rowLabel(label: String, position: Int, origin: ClipOrderCard.Origin?) -> String {
        let base = String(localized: "\(label), position \(position) of \(draft.order.count)")
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
/// back to the poster with a note when the original is not on this iPhone.
struct ClipOrderLargePreview: View {
    let media: CreationAttachedMedia
    let item: ClipOrderCard.Item
    let number: Int
    let projectID: UUID?
    let close: () -> Void

    var body: some View {
        GeometryReader { proxy in
            let size = NativeEditorLayoutMetrics.fullscreenSize(screen: proxy.size, aspect: 9.0 / 16.0)
            ZStack(alignment: .topTrailing) {
                Color.black.opacity(0.8).ignoresSafeArea()
                ClipPreviewPage(media: media, position: number, projectID: projectID, isCurrent: true,
                                name: ClipOrderLabel.text(item, number: number),
                                missingNote: String(localized: "This clip isn’t on this iPhone"))
                    .frame(width: size.width, height: size.height)
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
