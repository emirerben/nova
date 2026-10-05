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
    case browse, text, look
}

// MARK: Header

/// The native editor's floating header (`WorkspaceTopRow` + `kriaFloatingSurface`): a circle back
/// button, a centred title and one floating action capsule. Undo/redo and the status line live in
/// `SlidePostTransportRow`, where the editor keeps its own transport row.
struct SlidePostHeader: View {
    enum Action { case save, create, rendering, share, saving }
    let title: String
    let action: Action
    let actionEnabled: Bool
    let onBack: () -> Void
    let onAction: () -> Void
    let onSaveToPhotos: () -> Void
    let onShare: () -> Void

    var body: some View {
        WorkspaceTopRow(title: title) {
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

/// Scroll geometry the strip reads back for drag math and auto-scroll.
private struct SlidePostStripMetrics: Equatable {
    var offset: CGFloat = 0
    var content: CGFloat = 0
    var viewport: CGFloat = 0
}

/// One horizontal row: every slide, then any media still arriving, then the "+" block, all with the
/// same 60x76 footprint and number-label row. Long-press a slide and drag to reorder; the strip
/// auto-scrolls when the finger nears either edge.
struct SlidePostStrip: View {
    let slides: [SlidePostSlide]
    let coverIndex: Int
    let selectedID: String?
    let asset: (SlidePostSlide) -> SlidePostAsset?
    var pending: [SlidePostPendingTile] = []
    let onSelect: (String) -> Void
    let onMove: (String, Int) -> Void
    var onTapPending: (SlidePostPendingTile) -> Void = { _ in }
    let onAdd: () -> Void

    static let thumbSize = CGSize(width: 56, height: 72)
    static let tileWidth: CGFloat = 60   // 56pt tile + 2pt selection ring each side
    static let spacing: CGFloat = 6
    static let pitch = tileWidth + spacing
    static let leadingInset: CGFloat = 16

    @State private var dragID: String?
    @State private var fingerX: CGFloat = 0
    @State private var startFingerX: CGFloat?
    @State private var startOffset: CGFloat = 0
    @State private var lastTarget: Int?
    @State private var autoOffset: CGFloat = 0
    @State private var metrics = SlidePostStripMetrics()
    @State private var position = ScrollPosition(edge: .leading)

    private var dragFrom: Int? { dragID.flatMap { id in slides.firstIndex { $0.id == id } } }
    /// Finger travel plus however far the strip has scrolled since the lift.
    private var dragDelta: CGFloat {
        SlidePostReorderMath.liftedOffset(fingerTravel: fingerX - (startFingerX ?? fingerX), scrollTravel: metrics.offset - startOffset)
    }
    private var dragTarget: Int? {
        dragFrom.map { SlidePostReorderMath.targetIndex(from: $0, delta: dragDelta, pitch: Self.pitch, count: slides.count) }
    }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: Self.spacing) {
                ForEach(Array(slides.enumerated()), id: \.element.id) { index, slide in tile(slide, index: index) }
                ForEach(Array(pending.enumerated()), id: \.element.id) { index, tile in pendingTile(tile, index: index) }
                addTile
            }
            .overlay(alignment: .topLeading) { insertionBar }
            // The hold-to-reorder recogniser lives on the scroll view's own UIKit layer, beside its pan,
            // so a swipe is never claimed by the reorder gesture (see `SlidePostStripReorderRecognizer`).
            .background {
                SlidePostStripReorderRecognizer(
                    slideIndexAt: { SlidePostReorderMath.slideIndex(atContentX: $0, count: slides.count, leading: Self.leadingInset, pitch: Self.pitch, tileWidth: Self.tileWidth) },
                    onBegan: beginReorder, onMoved: { fingerX = $0; noteTargetChange() }, onFinished: finishReorder
                )
                .allowsHitTesting(false)
            }
            .padding(.horizontal, Self.leadingInset).padding(.vertical, 4)
        }
        .scrollPosition($position)
        .onScrollGeometryChange(for: SlidePostStripMetrics.self) {
            SlidePostStripMetrics(offset: $0.contentOffset.x, content: $0.contentSize.width, viewport: $0.containerSize.width)
        } action: { _, new in metrics = new }
        .scrollDisabled(dragID != nil)
        .mask(overflowFade)
        .frame(minHeight: 100, alignment: .top)
        .background(KriaColor.paper)
        .excludesDrawerGesture()
        .task(id: dragID) { await autoScrollLoop() }
        .onChange(of: selectedID) { _, id in
            // A slide picked from the preview, undo or the AI is brought into view (never while a block is lifted).
            guard dragID == nil, let id, let index = slides.firstIndex(where: { $0.id == id }) else { return }
            revealSlide(at: index)
        }
        .onChange(of: slides.map(\.id)) { old, new in
            // A new slide (auto-added media, undo) scrolls into view; a mid-drag change drops the lift.
            if dragID != nil, dragFrom == nil { endDrag() }
            if new.count > old.count, dragID == nil { withAnimation(.snappy) { position.scrollTo(edge: .trailing) } }
        }
        .onChange(of: pending.count) { old, new in
            if new > old, dragID == nil { withAnimation(.snappy) { position.scrollTo(edge: .trailing) } }
        }
    }

    // MARK: Blocks

    private var addTile: some View {
        VStack(spacing: 3) {
            Button(action: onAdd) {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(KriaColor.softZinc)
                    .frame(width: Self.thumbSize.width, height: Self.thumbSize.height)
                    .overlay { Image(systemName: "plus").font(.system(size: 20, weight: .medium)).foregroundStyle(KriaColor.ink) }
                    .padding(2)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Add photos and videos").accessibilityIdentifier("slidepost-add-tile")
            Text("Add").font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.zinc).accessibilityHidden(true)
        }
        .frame(width: Self.tileWidth)
    }

    private func pendingTile(_ tile: SlidePostPendingTile, index: Int) -> some View {
        let failed: Bool = { if case .failed = tile.phase { true } else { false } }()
        return VStack(spacing: 3) {
            Button { onTapPending(tile) } label: {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(failed ? KriaColor.failureSoft : KriaColor.softZinc)
                    .frame(width: Self.thumbSize.width, height: Self.thumbSize.height)
                    .overlay { pendingContent(tile.phase) }
                    .padding(2)
            }
            .buttonStyle(.plain).disabled(!failed)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(pendingLabel(tile.phase))
            .accessibilityValue(pendingValue(tile.phase))
            .accessibilityIdentifier("slidepost-pending-\(index + 1)")
            Text(failed ? "Retry" : "Adding").font(KriaFont.body(11).weight(.semibold))
                .foregroundStyle(failed ? KriaColor.failureText : KriaColor.zinc).lineLimit(1).minimumScaleFactor(0.7).accessibilityHidden(true)
        }
        .frame(width: Self.tileWidth)
    }

    @ViewBuilder private func pendingContent(_ phase: SlidePostPendingTile.Phase) -> some View {
        switch phase {
        case .uploading(let progress?):
            ZStack {
                Circle().stroke(KriaColor.line, lineWidth: 3)
                Circle().trim(from: 0, to: max(0.04, min(1, progress))).stroke(KriaColor.ink, style: StrokeStyle(lineWidth: 3, lineCap: .round)).rotationEffect(.degrees(-90))
            }
            .frame(width: 26, height: 26)
        case .uploading(nil), .processing:
            ProgressView()
        case .failed:
            Image(systemName: "arrow.clockwise").font(.system(size: 18, weight: .semibold)).foregroundStyle(KriaColor.failureText)
        }
    }
    private func pendingLabel(_ phase: SlidePostPendingTile.Phase) -> String {
        switch phase {
        case .uploading: "Uploading media"
        case .processing: "Preparing media"
        case .failed(let message, let record): record == nil ? "Failed, choose again. \(message)" : "Failed, retry. \(message)"
        }
    }
    private func pendingValue(_ phase: SlidePostPendingTile.Phase) -> String {
        if case .uploading(let progress?) = phase { return "\(Int((progress * 100).rounded())) percent" }
        return ""
    }

    @ViewBuilder private var insertionBar: some View {
        if let from = dragFrom, let target = dragTarget, target != from {
            let gap = target > from ? CGFloat(target + 1) * Self.pitch - Self.spacing / 2 : CGFloat(target) * Self.pitch - Self.spacing / 2
            RoundedRectangle(cornerRadius: 2).fill(SlidePostTone.insertion).frame(width: 4, height: 72).offset(x: gap - 2, y: 2)
                .allowsHitTesting(false)
        }
    }

    private func tile(_ slide: SlidePostSlide, index: Int) -> some View {
        let selected = slide.id == selectedID
        let lifted = slide.id == dragID
        let shift = (dragFrom != nil && !lifted) ? SlidePostReorderMath.neighbourShift(index: index, from: dragFrom ?? index, target: dragTarget ?? index, pitch: Self.pitch) : 0
        return VStack(spacing: 3) {
            Button { onSelect(slide.id) } label: {
                SlidePostAssetThumbnail(asset: asset(slide), size: Self.thumbSize, radius: 10)
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
        .offset(x: lifted ? dragDelta : shift).zIndex(lifted ? 1 : 0)
        .animation(.snappy(duration: 0.18), value: lifted)
        .animation(.snappy(duration: 0.18), value: shift)
        .accessibilityAction(named: "Move earlier") { if index > 0 { onMove(slide.id, index - 1) } }
        .accessibilityAction(named: "Move later") { if index < slides.count - 1 { onMove(slide.id, index + 1) } }
    }

    // MARK: Reorder

    private func beginReorder(index: Int, fingerX x: CGFloat) {
        guard slides.indices.contains(index) else { return }
        dragID = slides[index].id; startOffset = metrics.offset; autoOffset = metrics.offset
        startFingerX = x; fingerX = x; lastTarget = index
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    private func noteTargetChange() {
        if let target = dragTarget, target != lastTarget { lastTarget = target; UISelectionFeedbackGenerator().selectionChanged() }
    }

    private func finishReorder(commit: Bool) {
        defer { endDrag() }
        guard commit, let id = dragID, let from = dragFrom, let target = dragTarget, target != from else { return }
        onMove(id, target)
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
    }

    /// Scrolls so the slide is fully on screen with its neighbours peeking (centred when it was cut or hidden).
    private func revealSlide(at index: Int) {
        guard let offset = SlidePostReorderMath.offsetToReveal(index: index, current: metrics.offset, viewport: metrics.viewport, content: metrics.content,
                                                               leading: Self.leadingInset, pitch: Self.pitch, tileWidth: Self.tileWidth) else { return }
        withAnimation(.snappy) { position.scrollTo(x: offset) }
    }

    /// Soft edges say there is more to scroll to (the next tile fades in rather than ending flush).
    private var overflowFade: some View {
        let canScrollBack = metrics.offset > 1
        let canScrollOn = metrics.content - metrics.viewport - metrics.offset > 1
        return LinearGradient(stops: [
            .init(color: canScrollBack ? .clear : .black, location: 0), .init(color: .black, location: canScrollBack ? 0.05 : 0.001),
            .init(color: .black, location: canScrollOn ? 0.93 : 0.999), .init(color: canScrollOn ? .black.opacity(0.35) : .black, location: 1),
        ], startPoint: .leading, endPoint: .trailing)
        .allowsHitTesting(false)
    }

    private func endDrag() {
        guard dragID != nil || startFingerX != nil else { return }
        withAnimation(.snappy(duration: 0.18)) { dragID = nil }
        startFingerX = nil; lastTarget = nil
    }

    /// Scrolls while a block is held near either edge. Runs only while a block is lifted.
    private func autoScrollLoop() async {
        guard dragID != nil else { return }
        var last = Date()
        while !Task.isCancelled, dragID != nil {
            try? await Task.sleep(for: .milliseconds(16))
            let now = Date(); let dt = now.timeIntervalSince(last); last = now
            guard startFingerX != nil else { continue }
            let velocity = SlidePostReorderMath.autoScrollVelocity(fingerX: fingerX, viewport: metrics.viewport, content: metrics.content)
            guard velocity != 0 else { autoOffset = metrics.offset; continue }
            autoOffset = SlidePostReorderMath.clampedOffset(autoOffset + velocity * CGFloat(dt), content: metrics.content, viewport: metrics.viewport)
            position.scrollTo(x: autoOffset)
        }
    }
}

// MARK: Hold-to-reorder recogniser

/// A `UILongPressGestureRecognizer` attached to the strip's own `UIScrollView`. It is a SIBLING of the
/// scroll view's pan (recognised simultaneously), so a quick drag scrolls normally and only a
/// deliberate hold (0.3s, under 10pt of movement) lifts a block. Replaces a SwiftUI
/// `LongPressGesture.sequenced(before: DragGesture(minimumDistance: 0))` on each tile, which claimed
/// the touch and left the strip unscrollable.
private struct SlidePostStripReorderRecognizer: UIViewRepresentable {
    /// The slide under a content-space x, or nil over a gap, the "+ Add" block or a placeholder.
    let slideIndexAt: (CGFloat) -> Int?
    let onBegan: (_ index: Int, _ viewportX: CGFloat) -> Void
    let onMoved: (_ viewportX: CGFloat) -> Void
    let onFinished: (_ commit: Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> AnchorView {
        let view = AnchorView()
        view.isUserInteractionEnabled = false
        view.onWindow = { [weak coordinator = context.coordinator] view in coordinator?.attach(from: view) }
        return view
    }
    func updateUIView(_ view: AnchorView, context: Context) {
        context.coordinator.parent = self
        context.coordinator.attach(from: view)
    }
    static func dismantleUIView(_ view: AnchorView, coordinator: Coordinator) { coordinator.detach() }

    final class AnchorView: UIView {
        var onWindow: ((UIView) -> Void)?
        override func didMoveToWindow() { super.didMoveToWindow(); if window != nil { onWindow?(self) } }
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var parent: SlidePostStripReorderRecognizer
        private weak var scrollView: UIScrollView?
        private var recognizer: UILongPressGestureRecognizer?
        init(_ parent: SlidePostStripReorderRecognizer) { self.parent = parent }

        func attach(from view: UIView) {
            var candidate = view.superview
            while let current = candidate, !(current is UIScrollView) { candidate = current.superview }
            guard let scroll = candidate as? UIScrollView, scroll !== scrollView else { return }
            detach()
            let press = UILongPressGestureRecognizer(target: self, action: #selector(handle(_:)))
            press.minimumPressDuration = 0.3
            press.allowableMovement = 10
            press.delegate = self
            scroll.addGestureRecognizer(press)
            scrollView = scroll; recognizer = press
        }
        func detach() {
            if let recognizer { scrollView?.removeGestureRecognizer(recognizer) }
            recognizer = nil; scrollView = nil
        }

        @objc private func handle(_ press: UILongPressGestureRecognizer) {
            guard let scroll = scrollView else { return }
            let contentX = press.location(in: scroll).x
            let viewportX = contentX - scroll.contentOffset.x
            switch press.state {
            case .began: if let index = parent.slideIndexAt(contentX) { parent.onBegan(index, viewportX) }
            case .changed: parent.onMoved(viewportX)
            case .ended: parent.onFinished(true)
            case .cancelled, .failed: parent.onFinished(false)
            default: break
            }
        }

        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
            guard let scroll = scrollView else { return false }
            return parent.slideIndexAt(gestureRecognizer.location(in: scroll).x) != nil
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
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
        .frame(width: NativeEditorIslandMetrics.toolWidth, height: NativeEditorIslandMetrics.toolHeight)
        .background { if active { Capsule().fill(KriaColor.ink.opacity(0.07)) } }
        .contentShape(Capsule())
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
