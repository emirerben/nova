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
    static let stage = KriaColor.softZinc
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

enum SlidePostMode: Equatable { case browse, arrange, text, look }

// MARK: Header

struct SlidePostHeader: View {
    enum Action { case save, create, rendering, share, saving }
    let subtitle: String
    let unsaved: Bool
    let inTextMode: Bool
    let canUndo: Bool
    let canRedo: Bool
    let action: Action
    let actionEnabled: Bool
    let onBack: () -> Void
    let onUndo: () -> Void
    let onRedo: () -> Void
    let onDone: () -> Void
    let onAction: () -> Void
    let onSaveToPhotos: () -> Void
    let onShare: () -> Void

    var body: some View {
        HStack(spacing: 2) {
            Button(action: onBack) { Image(systemName: "chevron.left").font(.system(size: 18, weight: .semibold)).frame(width: 44, height: 44) }
                .accessibilityLabel("Back to creation")
            VStack(alignment: .leading, spacing: 1) {
                Text("Photo post").font(KriaFont.headline(19)).foregroundStyle(KriaColor.ink).lineLimit(1).minimumScaleFactor(0.8)
                Text(subtitle).font(KriaFont.body(12.5).weight(unsaved ? .semibold : .regular))
                    .foregroundStyle(unsaved ? SlidePostTone.warning : KriaColor.zinc).lineLimit(1).minimumScaleFactor(0.8)
                    .accessibilityIdentifier("slidepost-subtitle")
            }
            Spacer(minLength: 4)
            historyButton("arrow.uturn.backward", label: "Undo", id: "slidepost-undo", enabled: canUndo, action: onUndo)
            historyButton("arrow.uturn.forward", label: "Redo", id: "slidepost-redo", enabled: canRedo, action: onRedo)
            trailing
        }
        .padding(.leading, 6).padding(.trailing, 14).frame(minHeight: 60).background(KriaColor.paper)
    }

    private func historyButton(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 18, weight: .medium)).frame(width: 40, height: 44)
                .foregroundStyle(enabled ? KriaColor.ink : KriaColor.line)
        }
        .disabled(!enabled).accessibilityLabel(label).accessibilityIdentifier(id)
    }

    @ViewBuilder private var trailing: some View {
        if inTextMode {
            pill("Done", id: "slidepost-done", enabled: true, action: onDone)
        } else {
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
    }
    private func pill(_ title: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { pillLabel(title, enabled: enabled) }
            .disabled(!enabled).accessibilityIdentifier(id)
    }
    private func pillLabel(_ title: String, enabled: Bool) -> some View {
        Text(title).font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 20).frame(minHeight: 44).background(KriaColor.butter, in: Capsule())
            .opacity(enabled ? 1 : 0.55)
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

// MARK: Glass tool bar

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
        // Fits the width at normal type; at accessibility sizes the same row scrolls instead of truncating.
        ViewThatFits(in: .horizontal) {
            row
            ScrollView(.horizontal, showsIndicators: false) { row.padding(.horizontal, 4) }
        }
        .frame(minHeight: 58)
        .padding(.vertical, 2)
        .background(KriaColor.paper.opacity(0.82), in: Capsule())
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(KriaColor.ink.opacity(0.14), lineWidth: 1))
        .shadow(color: .black.opacity(0.10), radius: 14, y: 6)
        .padding(.horizontal, 16)
        .excludesDrawerGesture()
    }

    private var row: some View {
        HStack(spacing: 0) {
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
        .padding(.horizontal, 8)
    }

    private func item(_ symbol: String, _ title: String, id: String, active: Bool, enabled: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) { label(symbol, title, active: active) }
            .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.4)
            .accessibilityLabel(title).accessibilityAddTraits(active ? .isSelected : [])
            .accessibilityIdentifier(id)
    }

    private func label(_ symbol: String, _ title: String, active: Bool) -> some View {
        VStack(spacing: 2) {
            Image(systemName: symbol).font(.system(size: 20, weight: .regular)).frame(height: 24)
            Text(title).font(KriaFont.body(11).weight(.semibold)).lineLimit(1).fixedSize()
        }
        .foregroundStyle(KriaColor.ink)
        .padding(.horizontal, 12).padding(.vertical, 6).frame(minWidth: 64, minHeight: 50)
        .background(active ? KriaColor.selectionSoft : .clear, in: Capsule())
        .contentShape(Capsule())
    }
}

// MARK: Chat composer

/// "Ask Kria to edit your slides…". Lane D sends it to the existing propose flow; Lane E swaps in chat-edit.
struct SlidePostComposer: View {
    @Binding var text: String
    let canSend: Bool
    let onSend: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle").font(.system(size: 17, weight: .regular)).foregroundStyle(KriaColor.mutedInk).padding(.leading, 6)
            TextField("Ask Kria to edit your slides…", text: $text)
                .font(KriaFont.body(16)).submitLabel(.send).onSubmit { if canSend { onSend() } }
                .accessibilityIdentifier("slidepost-composer")
            Button(action: onSend) {
                Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)).foregroundStyle(.white)
                    .frame(width: 40, height: 40).background(KriaColor.ink, in: Circle())
                    .opacity(canSend ? 1 : 0.4)
            }
            .disabled(!canSend).accessibilityLabel("Send to Kria").accessibilityIdentifier("slidepost-composer-send")
        }
        .padding(.leading, 12).padding(.trailing, 6).frame(minHeight: 52)
        .background(KriaColor.paper, in: Capsule())
        .overlay(Capsule().strokeBorder(KriaColor.line, lineWidth: 1))
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

    @State private var dragStart: (id: String, point: CGPoint)?

    private static let bucketY: [String: Double] = ["top": 0.14, "center": 0.5, "bottom": 0.82]

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(texts) { element in textView(element) }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .coordinateSpace(name: "slidepost-canvas")
        .allowsHitTesting(interactive)
    }

    private func center(of element: SlidePostTextElement) -> CGPoint {
        let x = element.position == "custom" ? (element.xFrac ?? 0.5) : 0.5
        let y = element.position == "custom" ? (element.yFrac ?? 0.5) : (Self.bucketY[element.position] ?? 0.82)
        return CGPoint(x: x * size.width, y: y * size.height)
    }

    private func textView(_ element: SlidePostTextElement) -> some View {
        let scale = size.width / 1080
        let points = max(8, CGFloat(element.sizePx) * scale)
        let font = NativeFontCatalog.shared.ctFont(element.fontFamily, size: points).map(Font.init) ?? KriaFont.body(points).weight(.bold)
        let selected = interactive && element.id == selectedID
        let alignment: TextAlignment = element.alignment == "left" ? .leading : (element.alignment == "right" ? .trailing : .center)
        let stroke = element.strokeWidth > 0 ? max(0.6, CGFloat(element.strokeWidth) * scale) : 0
        let point = center(of: element)
        let makeText: (Color) -> Text = { Text(element.text).font(font).foregroundStyle($0) }
        return styled(makeText, color: Color(slideHex: element.color), alignment: alignment, stroke: stroke, shadow: element.shadowEnabled, scale: scale)
            .padding(.horizontal, element.background == "box" ? points * 0.45 : 0).padding(.vertical, element.background == "box" ? points * 0.22 : 0)
            .background { if element.background == "box" { RoundedRectangle(cornerRadius: points * 0.25, style: .continuous).fill(.black.opacity(0.55)) } }
            .frame(maxWidth: size.width * CGFloat(element.maxWidthFrac ?? 0.88))
            .padding(6)
            .overlay { if selected { selectionFrame } }
            .overlay(alignment: .bottom) { if selected { handle.offset(y: 34) } }
            .contentShape(Rectangle())
            .onTapGesture { onSelect(element.id) }
            .gesture(drag(for: element, from: point))
            .position(point)
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

    private func drag(for element: SlidePostTextElement, from start: CGPoint) -> some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .named("slidepost-canvas"))
            .onChanged { value in
                guard interactive else { return }
                if dragStart?.id != element.id { dragStart = (element.id, start); onSelect(element.id) }
                let origin = dragStart?.point ?? start
                let x = min(max((origin.x + value.translation.width) / size.width, 0), 1)
                let y = min(max((origin.y + value.translation.height) / size.height, 0), 1)
                onDrag(element.id, x, y)
            }
            .onEnded { _ in dragStart = nil }
    }
}
