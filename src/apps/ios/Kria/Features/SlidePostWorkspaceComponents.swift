import SwiftUI
import UIKit

// Building blocks for the redesigned slide-post workspace (KRI-298 Lane D, Paper-approved).
// Everything here is presentational: state lives in `SlidePostSession` / `SlidePostWorkspaceView`.

/// Slide-post specific tones that have no `KriaColor` token (taken from the approved Paper frames).
enum SlidePostTone {
    /// "Unsaved changes" subtitle.
    static let warning = Color(red: 0x8A / 255, green: 0x4B / 255, blue: 0x14 / 255)
    /// Insertion bar while reordering.
    static let insertion = KriaColor.sky.mix(with: KriaColor.ink, by: 0.42)
    static let stage = KriaColor.paper
}

extension View {
    /// Registers this view's frame as a region where the left-drawer swipe must not start, so a
    /// horizontal scroller (or a text drag) keeps its own gesture.
    func excludesDrawerGesture() -> some View { excludesDrawerGestureWhen(true) }
    /// Same, switched by state without changing the view's identity.
    func excludesDrawerGestureWhen(_ active: Bool) -> some View {
        background {
            GeometryReader { geometry in
                Color.clear.preference(key: DrawerGestureExclusionPreference.self, value: active ? [geometry.frame(in: .global)] : [])
            }
        }
    }
}

extension Color {
    /// `#RRGGBB` (the server's colour format). Anything else falls back to white.
    init(slideHex hex: String) {
        let digits = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
        guard digits.count == 6, let value = UInt32(digits, radix: 16) else { self = .white; return }
        self.init(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
    }
    var slideHexString: String {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        UIColor(self).getRed(&r, green: &g, blue: &b, alpha: &a)
        return String(format: "#%02X%02X%02X", Int((r * 255).rounded()), Int((g * 255).rounded()), Int((b * 255).rounded()))
    }
}

enum SlidePostMode: Equatable {
    case browse, arrange, text, look
    /// The Kria thread only renders in browse mode, so sending a chat message always returns there;
    /// otherwise an edit sent from Arrange would run with no visible feedback.
    static let afterChatSend: SlidePostMode = .browse
    static func showsChatThread(chatEnabled: Bool, chatOpen: Bool, mode: SlidePostMode) -> Bool {
        chatEnabled && chatOpen && mode == .browse
    }
}

// MARK: Header

/// The native editor's floating header (`WorkspaceTopRow` + `kriaFloatingSurface`): a circle back
/// button, a centred title and one floating action capsule. Undo/redo and the status line live in
/// `SlidePostTransportRow`, where the editor keeps its own transport row.
struct SlidePostHeader: View {
    enum Action { case save, create, rendering, share, saving }
    let action: Action
    let actionEnabled: Bool
    let onBack: () -> Void
    let onAction: () -> Void
    let onSaveToPhotos: () -> Void
    let onShare: () -> Void

    var body: some View {
        WorkspaceTopRow(title: "Photo post") {
            Button(action: onBack) {
                Image(systemName: "chevron.left").font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 44, height: 44).kriaFloatingSurface(Circle())
            }
            .accessibilityLabel("Back to creation")
        } trailing: {
            trailing.frame(minWidth: 44, alignment: .trailing)
        }
        .buttonStyle(.plain)
        .foregroundStyle(KriaColor.ink)
    }

    @ViewBuilder private var trailing: some View {
        switch action {
        case .save: pill("Save", id: "slidepost-save", enabled: actionEnabled, action: onAction)
        case .saving: pill("Saving…", id: "slidepost-save", enabled: false, action: {})
        case .create: pill("Create post", id: "slidepost-create", enabled: actionEnabled, action: onAction)
        case .rendering: pill("Rendering…", id: "slidepost-rendering", enabled: false, action: {})
        case .share:
            Menu {
                Button("Save to Photos", action: onSaveToPhotos).accessibilityIdentifier("slidepost-save-photos")
                Button("Share files", action: onShare).accessibilityIdentifier("slidepost-share-files")
            } label: { pillLabel("Share", enabled: true) }
            .accessibilityIdentifier("slidepost-share")
        }
    }
    private func pill(_ title: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { pillLabel(title, enabled: enabled) }
            .disabled(!enabled).accessibilityIdentifier(id)
    }
    // Same capsule as the editor's Save: 14pt semibold, 44pt tall, floating frosted surface.
    private func pillLabel(_ title: String, enabled: Bool) -> some View {
        Text(title).font(KriaFont.body(14).weight(.semibold)).foregroundStyle(enabled ? KriaColor.ink : KriaColor.zinc)
            .lineLimit(1).fixedSize().padding(.horizontal, 16).frame(minWidth: 44, minHeight: 44)
            .kriaFloatingSurface(Capsule())
    }
}

/// The row under the preview, in the slot (and metrics) of the editor's transport row: status line
/// on the left, undo/redo on the right.
struct SlidePostTransportRow: View {
    let subtitle: String
    let unsaved: Bool
    let canUndo: Bool
    let canRedo: Bool
    let onUndo: () -> Void
    let onRedo: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text(subtitle)
                .font(KriaFont.body(13).weight(.semibold))
                .foregroundStyle(unsaved ? SlidePostTone.warning : KriaColor.zinc)
                .lineLimit(1).minimumScaleFactor(0.7)
                .accessibilityIdentifier("slidepost-subtitle")
            Spacer(minLength: 4)
            historyButton("arrow.uturn.backward", label: "Undo", id: "slidepost-undo", enabled: canUndo, action: onUndo)
            historyButton("arrow.uturn.forward", label: "Redo", id: "slidepost-redo", enabled: canRedo, action: onRedo)
        }
        .padding(.horizontal, 14).frame(height: 54).background(KriaColor.paper)
    }

    private func historyButton(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 18, weight: .medium)).frame(width: 44, height: 44)
                .foregroundStyle(enabled ? KriaColor.ink : KriaColor.line)
        }
        .buttonStyle(.plain).disabled(!enabled).accessibilityLabel(label).accessibilityIdentifier(id)
    }
}

// MARK: Slide strip

struct SlidePostStrip: View {
    let slides: [SlidePostSlide]
    let coverIndex: Int
    let selectedID: String?
    let arranging: Bool
    let asset: (SlidePostSlide) -> SlidePostAsset?
    let onSelect: (String) -> Void
    let onMove: (String, Int) -> Void
    let onSetCover: (String) -> Void
    let onRemove: (String) -> Void
    let onAdd: () -> Void

    private static let tileWidth: CGFloat = 60   // 56pt tile + 2pt selection ring each side
    private static let spacing: CGFloat = 6
    private static let pitch = tileWidth + spacing

    @State private var dragID: String?
    @State private var dragOffset: CGFloat = 0

    private var dragTarget: Int? {
        guard let dragID, let from = slides.firstIndex(where: { $0.id == dragID }) else { return nil }
        return min(max(from + Int((dragOffset / Self.pitch).rounded()), 0), slides.count - 1)
    }

    var body: some View {
        ZStack(alignment: .trailing) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: Self.spacing) {
                    ForEach(Array(slides.enumerated()), id: \.element.id) { index, slide in tile(slide, index: index) }
                }
                .overlay(alignment: .topLeading) { insertionBar }
                .padding(.leading, 16).padding(.trailing, 92).padding(.vertical, 4)
            }
            .scrollDisabled(dragID != nil)
            HStack(spacing: 0) {
                LinearGradient(colors: [KriaColor.paper.opacity(0), KriaColor.paper], startPoint: .leading, endPoint: .trailing).frame(width: 28)
                addTile.padding(.trailing, 16).background(KriaColor.paper)
            }
            .frame(height: 100, alignment: .top).padding(.top, 4)
        }
        .frame(minHeight: 104, alignment: .top)
        .background(KriaColor.paper)
        .excludesDrawerGesture()
    }

    private var addTile: some View {
        Button(action: onAdd) {
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(KriaColor.zinc, style: StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                .frame(width: 56, height: 72)
                .overlay { Image(systemName: "plus").font(.system(size: 20, weight: .medium)).foregroundStyle(KriaColor.ink) }
                .padding(2)
        }
        .accessibilityLabel("Add photos and videos").accessibilityIdentifier("slidepost-add-tile")
    }

    @ViewBuilder private var insertionBar: some View {
        if let dragID, let target = dragTarget, let from = slides.firstIndex(where: { $0.id == dragID }), target != from {
            let gap = target > from ? CGFloat(target + 1) * Self.pitch - Self.spacing / 2 : CGFloat(target) * Self.pitch - Self.spacing / 2
            RoundedRectangle(cornerRadius: 2).fill(SlidePostTone.insertion).frame(width: 4, height: 72).offset(x: gap - 2, y: 2)
        }
    }

    private func tile(_ slide: SlidePostSlide, index: Int) -> some View {
        let selected = slide.id == selectedID
        let lifted = slide.id == dragID
        return VStack(spacing: 3) {
            Button { onSelect(slide.id) } label: {
                SlidePostAssetThumbnail(asset: asset(slide), size: CGSize(width: 56, height: 72), radius: 10)
                    .overlay(alignment: .bottomLeading) {
                        if index == coverIndex {
                            Text("Cover").font(KriaFont.body(10).weight(.bold)).foregroundStyle(KriaColor.ink)
                                .padding(.horizontal, 6).padding(.vertical, 2).background(KriaColor.butter, in: Capsule()).padding(3)
                        }
                    }
                    .overlay { if selected { RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(KriaColor.paper, lineWidth: 2) } }
                    .padding(2)
                    .overlay { if selected { RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(KriaColor.ink, lineWidth: 2) } }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Slide \(index + 1)")
            .accessibilityValue(index == coverIndex ? "Cover" : "")
            .accessibilityAddTraits(selected ? .isSelected : [])
            .accessibilityIdentifier("slidepost-tile-\(index + 1)")
            Text("\(index + 1)").font(KriaFont.body(11).weight(selected ? .bold : .semibold)).foregroundStyle(selected ? KriaColor.ink : KriaColor.zinc)
        }
        .frame(width: Self.tileWidth)
        .scaleEffect(lifted ? 1.06 : 1).rotationEffect(.degrees(lifted ? 4 : 0))
        .shadow(color: .black.opacity(lifted ? 0.28 : 0), radius: 10, y: 6)
        .offset(x: lifted ? dragOffset : 0).zIndex(lifted ? 1 : 0)
        .animation(.snappy(duration: 0.18), value: lifted)
        .contextMenu {
            Button { onSetCover(slide.id) } label: { Label("Set as cover", systemImage: "star.square") }
            Button(role: .destructive) { onRemove(slide.id) } label: { Label("Remove slide", systemImage: "trash") }
                .disabled(slides.count <= 1)
        }
        .highPriorityGesture(reorderGesture(for: slide), isEnabled: arranging)
    }

    private func reorderGesture(for slide: SlidePostSlide) -> some Gesture {
        LongPressGesture(minimumDuration: 0.2).sequenced(before: DragGesture(minimumDistance: 0))
            .onChanged { value in
                guard case .second(true, let drag) = value else { return }
                if dragID != slide.id { dragID = slide.id; UIImpactFeedbackGenerator(style: .light).impactOccurred() }
                dragOffset = drag?.translation.width ?? 0
            }
            .onEnded { value in
                defer { dragID = nil; dragOffset = 0 }
                guard case .second(true, let drag?) = value, let from = slides.firstIndex(where: { $0.id == slide.id }) else { return }
                let to = min(max(from + Int((drag.translation.width / Self.pitch).rounded()), 0), slides.count - 1)
                if to != from { onMove(slide.id, to) }
            }
    }
}

// MARK: Tool dock

/// The editor's tool island (`NativeEditorIslandGroup` + `nativeEditorIslandSurface`) with the
/// editor's tool metrics: 62pt tall, 17pt medium icons, 11pt labels, ink-7% selection capsule.
struct SlidePostToolBar: View {
    let mode: SlidePostMode
    let canCover: Bool
    let canRemove: Bool
    let canDuplicateText: Bool
    let onText: () -> Void
    let onArrange: () -> Void
    let onCover: () -> Void
    let onLook: () -> Void
    let onRemove: () -> Void
    let onDuplicateText: () -> Void
    let onCaption: () -> Void

    var body: some View {
        NativeEditorIslandGroup {
            // Fits the width at normal type; at accessibility sizes the same row scrolls instead of truncating.
            ViewThatFits(in: .horizontal) {
                row
                ScrollView(.horizontal, showsIndicators: false) { row }
            }
            .frame(height: NativeEditorIslandMetrics.islandHeight)
            .nativeEditorIslandSurface()
        }
        .padding(.horizontal, 12)
        .excludesDrawerGesture()
    }

    private var row: some View {
        HStack(spacing: NativeEditorIslandMetrics.toolSpacing) {
            item("textformat", "Text", id: "slidepost-tool-text", active: mode == .text, action: onText)
            item("arrow.up.arrow.down", "Arrange", id: "slidepost-tool-arrange", active: mode == .arrange, action: onArrange)
            item("star.square", "Cover", id: "slidepost-tool-cover", active: false, enabled: canCover, action: onCover)
            item("circle.lefthalf.filled", "Look", id: "slidepost-tool-look", active: mode == .look, action: onLook)
            Menu {
                Button(role: .destructive, action: onRemove) { Label("Remove slide", systemImage: "trash") }.disabled(!canRemove)
                Button(action: onDuplicateText) { Label("Duplicate text", systemImage: "plus.square.on.square") }.disabled(!canDuplicateText)
                Button(action: onCaption) { Label("Caption", systemImage: "text.alignleft") }
            } label: { label("ellipsis", "More", active: false) }
            .accessibilityIdentifier("slidepost-tool-more")
        }
        .padding(.horizontal, NativeEditorIslandMetrics.islandHorizontalPadding)
        .padding(.vertical, NativeEditorIslandMetrics.islandVerticalPadding)
    }

    private func item(_ symbol: String, _ title: String, id: String, active: Bool, enabled: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) { label(symbol, title, active: active) }
            .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.4)
            .accessibilityLabel(title).accessibilityAddTraits(active ? .isSelected : [])
            .accessibilityIdentifier(id)
    }

    private func label(_ symbol: String, _ title: String, active: Bool) -> some View {
        VStack(spacing: 4) {
            Image(systemName: symbol).font(.system(size: 17, weight: .medium)).frame(height: 22)
            Text(title).font(KriaFont.body(11).weight(active ? .semibold : .medium))
                .dynamicTypeSize(...DynamicTypeSize.xxxLarge).lineLimit(1).minimumScaleFactor(0.7)
        }
        .foregroundStyle(active ? KriaColor.ink : KriaColor.zinc)
        .frame(minWidth: 62, maxWidth: .infinity).frame(height: NativeEditorIslandMetrics.toolHeight)
        .background { if active { Capsule().fill(KriaColor.ink.opacity(0.07)) } }
        .contentShape(Capsule())
    }
}

// MARK: Chat composer

/// "Ask Kria to edit your slides…". Lane D sends it to the existing propose flow; Lane E swaps in chat-edit.
struct SlidePostComposer: View {
    @Binding var text: String
    let canSend: Bool
    var isLocked = false
    let onSend: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle").font(.system(size: 17, weight: .regular)).foregroundStyle(KriaColor.mutedInk).padding(.leading, 6)
            TextField("Ask Kria to edit your slides…", text: $text)
                .font(KriaFont.body(16)).submitLabel(.send).onSubmit { if canSend { onSend() } }
                .disabled(isLocked)
                .accessibilityIdentifier("slidepost-composer")
            Button(action: onSend) {
                Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 40, height: 40).background(KriaColor.ink, in: Circle())
                    .opacity(canSend ? 1 : 0.4)
            }
            .disabled(!canSend).accessibilityLabel("Send to Kria").accessibilityIdentifier("slidepost-composer-send")
        }
        .padding(.leading, 12).padding(.trailing, 6).frame(minHeight: 52)
        .kriaFloatingSurface(Capsule())
        .padding(.horizontal, 16)
    }
}

// MARK: Edge states

struct SlidePostNotice: View {
    let title: String
    let detail: String
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "info.circle").font(.system(size: 20)).foregroundStyle(KriaColor.zinc)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(KriaFont.body(15).weight(.bold)).foregroundStyle(KriaColor.ink)
                Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(KriaColor.line, lineWidth: 1))
        .padding(.horizontal, 16).accessibilityElement(children: .combine)
    }
}

/// Dims the preview while a save/render is in flight; the caller disables the rest of the workspace.
struct SlidePostVeil: View {
    let message: String
    var body: some View {
        ZStack {
            Color.black.opacity(0.38)
            VStack(spacing: 12) {
                ProgressView().controlSize(.large).tint(.white)
                Text(message).font(KriaFont.body(16).weight(.bold)).foregroundStyle(.white).multilineTextAlignment(.center)
            }
            .padding(20)
        }
        .accessibilityElement(children: .combine).accessibilityIdentifier("slidepost-veil")
    }
}

// MARK: Canvas text

/// The text layer of the preview. Selection frame + drag handle when `interactive`; dragging sets
/// the free position (`x_frac` / `y_frac`, position "custom").
struct SlidePostTextCanvas: View {
    let texts: [SlidePostTextElement]
    let size: CGSize
    let selectedID: String?
    let interactive: Bool
    let onSelect: (String) -> Void
    let onDrag: (String, Double, Double) -> Void

    @State private var dragStart: (id: String, element: SlidePostTextElement)?

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(texts) { element in textView(element) }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .coordinateSpace(name: "slidepost-canvas")
        .allowsHitTesting(interactive)
    }

    private func textView(_ element: SlidePostTextElement) -> some View {
        let scale = size.width / 1080
        let points = max(8, CGFloat(element.sizePx) * scale)
        let font = NativeFontCatalog.shared.ctFont(element.fontFamily, size: points).map(Font.init) ?? KriaFont.body(points).weight(.bold)
        let selected = interactive && element.id == selectedID
        let alignment: TextAlignment = element.alignment == "left" ? .leading : (element.alignment == "right" ? .trailing : .center)
        let stroke = element.strokeWidth > 0 ? max(0.6, CGFloat(element.strokeWidth) * scale) : 0
        let anchor = SlidePostTextLayout.anchor(for: element)
        // Server parity: x is the left edge / centre / right edge by alignment; y is the block centre.
        let frameAlignment: Alignment = element.alignment == "left" ? .leading : (element.alignment == "right" ? .trailing : .center)
        let offsetX = element.alignment == "left" ? anchor.x : (element.alignment == "right" ? anchor.x - 1 : anchor.x - 0.5)
        let makeText: (Color) -> Text = { Text(element.text).font(font).foregroundStyle($0) }
        return styled(makeText, color: Color(slideHex: element.color), alignment: alignment, stroke: stroke, shadow: element.shadowEnabled, scale: scale)
            .padding(.horizontal, element.background == "box" ? points * 0.45 : 0).padding(.vertical, element.background == "box" ? points * 0.22 : 0)
            .background { if element.background == "box" { RoundedRectangle(cornerRadius: points * 0.25, style: .continuous).fill(.black.opacity(0.55)) } }
            .frame(maxWidth: size.width * CGFloat(element.maxWidthFrac ?? 0.88))
            .overlay { if selected { selectionFrame.padding(-6) } }
            .overlay(alignment: .bottom) { if selected { handle.offset(y: 40) } }
            .contentShape(Rectangle().inset(by: -6))
            .onTapGesture { onSelect(element.id) }
            .gesture(drag(for: element))
            .frame(width: size.width, height: size.height, alignment: frameAlignment)
            .offset(x: CGFloat(offsetX) * size.width, y: CGFloat(anchor.y - 0.5) * size.height)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(element.text)
            .accessibilityHint(selected ? "Drag to move" : "Double tap to edit")
            .accessibilityIdentifier("slidepost-canvas-text-\(element.id)")
    }

    @ViewBuilder private func styled(_ makeText: @escaping (Color) -> Text, color: Color, alignment: TextAlignment, stroke: CGFloat, shadow: Bool, scale: CGFloat) -> some View {
        let base = ZStack {
            if stroke > 0 {
                ForEach(Array(Self.outlineOffsets.enumerated()), id: \.offset) { _, offset in
                    makeText(.black).offset(x: offset.width * stroke, y: offset.height * stroke)
                }
            }
            makeText(color)
        }
        .multilineTextAlignment(alignment)
        if shadow { base.shadow(color: .black.opacity(0.55), radius: max(1, 5 * scale), x: 0, y: max(1, 2 * scale)) } else { base }
    }
    private static let outlineOffsets: [CGSize] = [(-1, 0), (1, 0), (0, -1), (0, 1), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7)].map { CGSize(width: $0.0, height: $0.1) }

    private var selectionFrame: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 3).strokeBorder(KriaColor.sky, lineWidth: 1.5)
            GeometryReader { geometry in
                ForEach(0..<4, id: \.self) { corner in
                    Circle().fill(KriaColor.paper).overlay(Circle().strokeBorder(KriaColor.sky, lineWidth: 2)).frame(width: 12, height: 12)
                        .position(x: corner % 2 == 0 ? 0 : geometry.size.width, y: corner < 2 ? 0 : geometry.size.height)
                }
            }
        }
        .allowsHitTesting(false)
    }
    private var handle: some View {
        Image(systemName: "arrow.up.and.down.and.arrow.left.and.right").font(.system(size: 16, weight: .medium)).foregroundStyle(KriaColor.ink)
            .frame(width: 40, height: 28).background(KriaColor.paper, in: Capsule()).shadow(color: .black.opacity(0.18), radius: 4, y: 2)
            .allowsHitTesting(false)
    }

    private func drag(for element: SlidePostTextElement) -> some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .named("slidepost-canvas"))
            .onChanged { value in
                guard interactive else { return }
                // The start is captured once per gesture so a mid-drag update can't compound.
                if dragStart?.id != element.id { dragStart = (element.id, element); onSelect(element.id) }
                let origin = dragStart?.element ?? element
                let moved = SlidePostTextLayout.dragged(from: origin, translation: value.translation, canvas: size)
                onDrag(element.id, moved.x, moved.y)
            }
            .onEnded { _ in dragStart = nil }
    }
}

// MARK: Kria thread (chat edit, KRI-298 Lane E; Paper art. 04)

/// Wraps its children onto new lines (the change chips).
private struct SlidePostWrap: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(proposal.width ?? 320, subviews).size
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrange(bounds.width, subviews)
        for (index, origin) in result.origins.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }
    private func arrange(_ width: CGFloat, _ subviews: Subviews) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = [], x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            origins.append(CGPoint(x: x, y: y)); x += size.width + spacing; rowHeight = max(rowHeight, size.height); maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), origins)
    }
}

/// The conversation below the compact preview: user bubble (ink, right), unboxed Kria reply, change
/// chips, then Unsaved / Undo / Save. The reply text is the server's, shown verbatim.
struct SlidePostChatThread: View {
    let messages: [SlidePostChatMessage]
    let isWorking: Bool
    let unsaved: Bool
    let canUndo: Bool
    let canSave: Bool
    let onUndo: () -> Void
    let onSave: () -> Void
    let onRetry: (SlidePostChatMessage) -> Void
    let onClose: () -> Void

    private static let noteFill = Color(red: 0xFD / 255, green: 0xF1 / 255, blue: 0xDC / 255)

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Kria").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.zinc)
                Spacer()
                Button(action: onClose) {
                    Text("Back to tools").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink).frame(minHeight: 44)
                }
                .accessibilityIdentifier("slidepost-chat-close")
            }
            .padding(.horizontal, 16)
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(messages) { message in bubble(message) }
                        if isWorking {
                            HStack(spacing: 10) {
                                ProgressView()
                                Text("Kria is editing…").font(KriaFont.body(16)).foregroundStyle(KriaColor.zinc)
                            }
                            .accessibilityElement(children: .combine).accessibilityIdentifier("slidepost-chat-working")
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 16).padding(.bottom, 8)
                }
                .onChange(of: messages.count) { _, _ in withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
                .onChange(of: isWorking) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            HStack(spacing: 10) {
                if unsaved {
                    HStack(spacing: 6) {
                        Circle().fill(SlidePostTone.warning).frame(width: 7, height: 7)
                        Text("Unsaved").font(KriaFont.body(15).weight(.semibold)).foregroundStyle(SlidePostTone.warning)
                    }
                    .padding(.horizontal, 12).frame(minHeight: 36).background(Self.noteFill, in: Capsule())
                    .accessibilityElement(children: .combine).accessibilityIdentifier("slidepost-chat-unsaved")
                }
                Spacer(minLength: 4)
                Button("Undo", action: onUndo).buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .disabled(!canUndo).accessibilityIdentifier("slidepost-chat-undo")
                Button("Save", action: onSave).buttonStyle(KriaPrimaryButtonStyle())
                    .disabled(!canSave).accessibilityIdentifier("slidepost-chat-save")
            }
            .padding(.horizontal, 16).padding(.vertical, 8)
        }
    }

    @ViewBuilder private func bubble(_ message: SlidePostChatMessage) -> some View {
        if message.isUser {
            Text(message.text).font(KriaFont.body(16)).foregroundStyle(.white)
                .padding(.horizontal, 16).padding(.vertical, 12)
                .background(KriaColor.ink, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .frame(maxWidth: .infinity, alignment: .trailing).padding(.leading, 56)
                .accessibilityIdentifier("slidepost-chat-user")
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text(message.text).font(KriaFont.body(16)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading).accessibilityIdentifier("slidepost-chat-reply")
                if !message.changes.isEmpty {
                    SlidePostWrap {
                        ForEach(message.changes, id: \.self) { change in
                            let note = SlidePostChatMessage.isNote(change)
                            Text(change).font(KriaFont.body(14).weight(.semibold))
                                .foregroundStyle(note ? SlidePostTone.warning : KriaColor.success)
                                .padding(.horizontal, 12).frame(minHeight: 32)
                                .background(note ? Self.noteFill : KriaColor.successSoft, in: Capsule())
                                .accessibilityIdentifier(note ? "slidepost-chat-note" : "slidepost-chat-change")
                        }
                    }
                }
                if message.retryText != nil {
                    Button("Try again") { onRetry(message) }.buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                        .accessibilityIdentifier("slidepost-chat-retry")
                }
            }
        }
    }
}
