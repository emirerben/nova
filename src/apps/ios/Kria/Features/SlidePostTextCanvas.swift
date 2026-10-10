import SwiftUI
import UIKit

// MARK: Canvas text

/// The text layer of a slide preview. It drives the same transform layer as the
/// native video preview (`NativeTextLiveTransform` / `NativeTextTransformMath`):
/// select a text, drag it (alignment-guide haptics), drag the corner handle to
/// scale and rotate it (90-degree detents), pinch to resize, twist to rotate,
/// and tap empty canvas to deselect. Every sample is written through
/// `onTransform` with one key per gesture, so undo steps back a whole gesture.
struct SlidePostTextCanvas: View {
    let texts: [SlidePostTextElement]
    let size: CGSize
    let selectedID: String?
    /// The Text panel is open (editing): drag/pinch/twist work as before and a drag selects (onSelect).
    let interactive: Bool
    /// A text is selected for direct manipulation with NO panel (hold-select): frame + handle shown, gestures live.
    var directSelected = false
    /// Browse/look mode: the canvas does not take gestures, but a tap on a text still reaches `onTapText`
    /// (only over the text itself, so a video's own controls keep working everywhere else).
    var tapsToEdit = false
    /// A drag that starts on a text selects it (nil deselects).
    let onSelect: (String?) -> Void
    /// Select for transform only: must not open the panel, change its tab or raise the keyboard, and is not an undo step.
    var onDirectSelect: (String?) -> Void = { _ in }
    /// A tap: the text under the finger, or nil for empty canvas.
    var onTapText: (String?) -> Void = { _ in }
    /// (text id, gesture key, mutation). The key is unique per gesture.
    let onTransform: (String, String, (inout SlidePostTextElement) -> Void) -> Void

    private enum Mode {
        case idle
        case move(TextTransformBaseline)
        case corner(TextTransformBaseline, anchorPoint: CGPoint, start: CGVector)
        case pinch(TextTransformBaseline)

        var isPinch: Bool { if case .pinch = self { true } else { false } }
        var isIdle: Bool { if case .idle = self { true } else { false } }
    }

    @State private var blockSizes: [String: CGSize] = [:]
    @State private var mode = Mode.idle
    @State private var live = NativeTextLiveTransform()
    @State private var gestureKey = UUID().uuidString
    @State private var feedback = NativeTextAlignmentFeedback()
    @State private var haptic = UISelectionFeedbackGenerator()
    @State private var clock = SlidePostGestureClock()
    @State private var touch = SlidePostTouchResolver()
    @State private var holdTimer = SlidePostHoldTimer()
    /// A pinch's fingers never move perfectly symmetrically; rotation only engages once the twist is deliberate.
    @State private var twistLatched = false
    private static let twistThreshold = 8.0

    /// Radius of the corner handle's drawn circle, and how far the handle is
    /// kept from the stage edge so it is always fully visible and grabbable.
    private static let handleRadius: CGFloat = 11
    private static let handleInset: CGFloat = 13
    private static let selectionPadding: CGFloat = 4

    var body: some View {
        ZStack(alignment: .topLeading) {
            SlidePostTextLayerView(texts: texts, size: size, accessibility: .init(
                selectedID: showsSelection ? selectedID : nil, onSelect: onSelect, onTapText: onTapText))
                .allowsHitTesting(false)
            if interactive || tapsToEdit {
                gestureSurface
            }
            if showsSelection {
                if let selected = texts.first(where: { $0.id == selectedID }), let rect = selectionRect(for: selected) {
                    selectionOverlay(for: selected, rect: rect)
                }
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .coordinateSpace(name: "slidepost-canvas")
        .onPreferenceChange(SlidePostBlockSizeKey.self) { blockSizes = $0 }
        .onDisappear { holdTimer.cancel(); touch.reset(); settle() }
    }

    private var showsSelection: Bool { interactive || directSelected }

    // MARK: Geometry

    private func blockFrame(_ element: SlidePostTextElement) -> CGRect? {
        blockSizes[element.id].map { SlidePostTextGeometry.frame(for: element, blockSize: $0, canvas: size) }
    }

    /// The padded selection box: what is drawn, hit-tested and grabbed.
    private func selectionRect(for element: SlidePostTextElement) -> CGRect? {
        blockFrame(element)?.insetBy(dx: -Self.selectionPadding, dy: -Self.selectionPadding)
    }

    private func handleCenter(for element: SlidePostTextElement, rect: CGRect) -> CGPoint {
        NativeTextTransformMath.handleCenter(of: rect, rotationDegrees: element.rotationDeg, canvas: size, inset: Self.handleInset)
    }

    private func hit(at point: CGPoint) -> SlidePostTextElement? {
        texts.reversed().first { element in
            guard let rect = selectionRect(for: element) else { return false }
            return NativeEditorInteraction.contains(point, in: rect, rotationDegrees: element.rotationDeg)
        }
    }

    // MARK: Gestures

    private var gestureSurface: some View {
        Color.clear
            .frame(width: size.width, height: size.height)
            // Over text only while browsing (so the media's own controls keep working); the whole
            // stage while editing or direct-manipulating (empty-canvas tap deselects).
            .contentShape(SlidePostTextHitShape(rects: showsSelection ? nil : texts.compactMap { element in
                selectionRect(for: element).map { ($0, element.rotationDeg) }
            }))
            .highPriorityGesture(touchGesture)
            .simultaneousGesture(pinchGesture)
    }

    private var touchGesture: some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .named("slidepost-canvas"))
            .onChanged { value in
                if touch.isIdle { touchBegan(at: value.startLocation) }
                switch touch.moved(to: value.location) {
                case .beginDrag:
                    holdTimer.cancel()
                    clock.lastChange = Date()
                    dragChanged(value)
                default:
                    if !mode.isIdle, !mode.isPinch { clock.lastChange = Date(); dragChanged(value) }
                }
            }
            .onEnded { value in
                holdTimer.cancel()
                let action = touch.touchUp(at: value.location)
                if !mode.isPinch { settle() }
                if action == .tap { tap(at: value.location) }
            }
    }

    private func touchBegan(at start: CGPoint) {
        let onTarget = isOnTarget(start)
        touch.touchDown(at: start, time: ProcessInfo.processInfo.systemUptime, onTarget: onTarget, holdAllowed: !interactive && tapsToEdit)
        guard !interactive, tapsToEdit, onTarget else { return }
        holdTimer.schedule(after: SlidePostTouchResolver.holdDuration) {
            guard touch.holdTimerFired(at: ProcessInfo.processInfo.systemUptime) == .beginHold else { return }
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            if let target = hit(at: start), target.id != selectedID { onDirectSelect(target.id) }
        }
    }

    private func isOnTarget(_ point: CGPoint) -> Bool {
        if showsSelection, let selected = texts.first(where: { $0.id == selectedID }), let rect = selectionRect(for: selected),
           NativeTextTransformMath.grabsCorner(at: point, corner: handleCenter(for: selected, rect: rect), bounds: rect) { return true }
        return hit(at: point) != nil
    }

    private var pinchGesture: some Gesture {
        MagnificationGesture().simultaneously(with: RotationGesture())
            .onChanged { value in
                clock.lastChange = Date()
                holdTimer.cancel(); touch.cancel()
                pinchChanged(scale: value.first.map(Double.init) ?? 1, degrees: value.second?.degrees ?? 0,
                             twisting: value.second != nil)
            }
            .onEnded { _ in
                settle()
                // A pinch cancels the one-finger resolver; the cancelled drag may never deliver its own end, which
                // left the resolver stuck and swallowed every later tap (and its onChanged never re-armed it).
                touch.reset()
            }
    }

    private func tap(at point: CGPoint) {
        // A drag that also resolves as a tap must never deselect.
        guard Date().timeIntervalSince(clock.lastChange) > 0.4 else { return }
        if showsSelection, let selected = texts.first(where: { $0.id == selectedID }), let rect = selectionRect(for: selected),
           hypot(point.x - handleCenter(for: selected, rect: rect).x, point.y - handleCenter(for: selected, rect: rect).y) <= NativeTextTransformMath.cornerGrabRadius {
            return
        }
        onTapText(hit(at: point)?.id)
    }

    private func dragChanged(_ value: DragGesture.Value) {
        guard !mode.isPinch, size.width > 0, size.height > 0 else { return }
        if mode.isIdle { beginDrag(at: value.startLocation) }
        switch mode {
        case .idle, .pinch:
            return
        case let .corner(baseline, anchorPoint, start):
            let next = CGVector(dx: value.location.x - anchorPoint.x, dy: value.location.y - anchorPoint.y)
            guard let delta = NativeTextTransformMath.cornerDelta(start: start, next: next) else { return }
            live.resize(current: baseline, scale: delta.scale, rotation: delta.rotationDegrees, snapRotation: true)
            publishResize(baseline)
        case let .move(baseline):
            let position = NativeTextTransformMath.movedPosition(from: baseline.anchor, translation: value.translation, canvas: size)
            live.resize(current: baseline, scale: 1, rotation: 0, snapRotation: false)
            live.move(to: position)
            tickAlignment()
            let key = gestureKey, id = baseline.id
            onTransform(id, key) { element in
                element.position = "custom"; element.xFrac = Double(position.x); element.yFrac = Double(position.y)
            }
        }
    }

    private func beginDrag(at start: CGPoint) {
        feedback.reset(); haptic.prepare()
        if showsSelection, let selected = texts.first(where: { $0.id == selectedID }), let rect = selectionRect(for: selected),
           NativeTextTransformMath.grabsCorner(at: start, corner: handleCenter(for: selected, rect: rect), bounds: rect) {
            let baseline = selected.transformBaseline
            let anchorPoint = CGPoint(x: baseline.anchor.x * size.width, y: baseline.anchor.y * size.height)
            begin(baseline, for: selected)
            mode = .corner(baseline, anchorPoint: anchorPoint, start: CGVector(dx: start.x - anchorPoint.x, dy: start.y - anchorPoint.y))
            return
        }
        guard let target = hit(at: start) else { return }
        if target.id != selectedID { interactive ? onSelect(target.id) : onDirectSelect(target.id) }
        let baseline = target.transformBaseline
        begin(baseline, for: target)
        mode = .move(baseline)
    }

    private func begin(_ baseline: TextTransformBaseline, for element: SlidePostTextElement) {
        gestureKey = UUID().uuidString
        live.reset()
        let bounds = blockSizes[element.id].map { SlidePostTextGeometry.bounds(for: element, blockSize: $0, canvas: size) }
        live.begin(baseline: baseline, bounds: bounds)
    }

    private func pinchChanged(scale: Double, degrees: Double, twisting: Bool) {
        guard size.width > 0, let selected = texts.first(where: { $0.id == selectedID }) else { return }
        let baseline: TextTransformBaseline
        if case let .pinch(existing) = mode {
            baseline = existing
        } else {
            // A two-finger gesture owns the transform; drop any one-finger drag in flight.
            feedback.reset(); haptic.prepare()
            baseline = selected.transformBaseline
            begin(baseline, for: selected)
            mode = .pinch(baseline)
        }
        if twisting, abs(degrees) >= Self.twistThreshold { twistLatched = true }
        live.resize(current: baseline, scale: scale, rotation: twistLatched ? degrees : 0, snapRotation: twistLatched)
        publishResize(baseline)
    }

    /// Writes the live size / width / rotation sample and ticks the guides.
    private func publishResize(_ baseline: TextTransformBaseline) {
        tickAlignment()
        let key = gestureKey, snapshot = live
        onTransform(baseline.id, key) { $0.applyTransform(from: baseline, live: snapshot) }
    }

    private func tickAlignment() {
        if live.updateAlignment(&feedback, in: size) { haptic.selectionChanged(); haptic.prepare() }
    }

    private func settle() {
        guard !mode.isIdle || live.isActive else { return }
        mode = .idle
        twistLatched = false
        live.reset()
        feedback.reset()
    }

    // MARK: Selection chrome

    @ViewBuilder private func selectionOverlay(for element: SlidePostTextElement, rect: CGRect) -> some View {
        let handle = handleCenter(for: element, rect: rect)
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(KriaColor.sky, lineWidth: 2)
                .frame(width: rect.width, height: rect.height)
                .rotationEffect(.degrees(element.rotationDeg))
                .position(x: rect.midX, y: rect.midY)
                .accessibilityElement(children: .ignore)
                .accessibilityIdentifier("slidepost-text-selection")
                .accessibilityValue("selected")
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: Self.handleRadius * 2, height: Self.handleRadius * 2)
                .background(KriaColor.sky, in: Circle())
                .position(x: handle.x, y: handle.y)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Resize and rotate")
                .accessibilityIdentifier("slidepost-text-handle")
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .allowsHitTesting(false)
    }
}

/// The pure text layer of a slide: every text element positioned, styled and rotated on a `size` canvas
/// (text scales by `size.width / 1080`). Shared by the editor canvas (which layers gestures on top) and
/// the on-device exporter (which renders it at full output pixels), so preview and export cannot drift.
struct SlidePostTextLayerView: View {
    /// Present only in the editor: VoiceOver labels and actions for each text. Export renders none.
    struct Accessibility {
        var selectedID: String?
        var onSelect: (String?) -> Void
        var onTapText: (String?) -> Void
    }
    let texts: [SlidePostTextElement]
    let size: CGSize
    var accessibility: Accessibility? = nil

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(texts) { element in textView(element) }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
    }

    private func textView(_ element: SlidePostTextElement) -> some View {
        let scale = size.width / 1080
        // No legibility floor: the export scales exactly, so a floor here makes small text look bigger on the
        // preview than in the saved image (KRI-564).
        let points = max(1, CGFloat(element.sizePx) * scale)
        let font = NativeFontCatalog.shared.ctFont(element.fontFamily, size: points).map(Font.init) ?? KriaFont.body(points).weight(.bold)
        let selected = accessibility?.selectedID == element.id
        let alignment: TextAlignment = element.alignment == "left" ? .leading : (element.alignment == "right" ? .trailing : .center)
        let stroke = element.strokeWidth > 0 ? max(0.6, CGFloat(element.strokeWidth) * scale) : 0
        let anchor = SlidePostTextLayout.anchor(for: element)
        // Server parity: x is the left edge / centre / right edge by alignment; y is the block centre.
        let frameAlignment: Alignment = element.alignment == "left" ? .leading : (element.alignment == "right" ? .trailing : .center)
        let offsetX = element.alignment == "left" ? anchor.x : (element.alignment == "right" ? anchor.x - 1 : anchor.x - 0.5)
        // The block rotates about its anchor, which sits at the box's leading edge / centre / trailing edge.
        let pivotX: CGFloat = element.alignment == "left" ? 0 : (element.alignment == "right" ? 1 : 0.5)
        let makeText: (Color) -> Text = { Text(element.text).font(font).foregroundStyle($0) }
        return styled(makeText, color: Color(slideHex: element.color), alignment: alignment, stroke: stroke, shadow: element.shadowEnabled, scale: scale)
            .padding(.horizontal, element.background == "box" ? points * 0.45 : 0).padding(.vertical, element.background == "box" ? points * 0.22 : 0)
            .background { if element.background == "box" { RoundedRectangle(cornerRadius: points * 0.25, style: .continuous).fill(.black.opacity(0.55)) } }
            .modifier(SlidePostHuggingWidth(maxWidth: element.wrapsLines ? size.width * CGFloat(element.maxWidthFrac ?? SlidePostTextElement.defaultWidthFrac) : nil))
            .background(GeometryReader { geometry in
                Color.clear.preference(key: SlidePostBlockSizeKey.self, value: [element.id: geometry.size])
            })
            .frame(width: size.width, height: size.height, alignment: frameAlignment)
            .rotationEffect(.degrees(element.rotationDeg), anchor: UnitPoint(x: pivotX, y: 0.5))
            .offset(x: CGFloat(offsetX) * size.width, y: CGFloat(anchor.y - 0.5) * size.height)
            .modifier(SlidePostTextAccessibility(element: element, selected: selected, accessibility: accessibility))
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
}

private struct SlidePostTextAccessibility: ViewModifier {
    let element: SlidePostTextElement
    let selected: Bool
    let accessibility: SlidePostTextLayerView.Accessibility?

    @ViewBuilder func body(content: Content) -> some View {
        if let accessibility {
            content
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(element.text)
                .accessibilityValue(value)
                .accessibilityHint(selected ? "Drag to move, pinch to resize, twist to rotate" : "Double tap to select")
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityAction(named: "Select") { accessibility.onSelect(element.id) }
                .accessibilityAction(named: "Edit text") { accessibility.onTapText(element.id) }
                .accessibilityIdentifier("slidepost-canvas-text-\(element.id)")
        } else { content }
    }

    private var value: String {
        let anchor = SlidePostTextLayout.anchor(for: element)
        var parts = ["size \(element.sizePx)", "rotation \(Int(element.rotationDeg))",
                     "position \(Int((anchor.x * 100).rounded()))%, \(Int((anchor.y * 100).rounded()))%"]
        if selected { parts.append("selected") }
        return parts.joined(separator: ", ")
    }
}

/// Last time a drag or pinch changed. A reference type so writing it never re-renders.
final class SlidePostGestureClock {
    var lastChange = Date.distantPast
}

private struct SlidePostBlockSizeKey: PreferenceKey {
    static var defaultValue: [String: CGSize] { [:] }
    static func reduce(value: inout [String: CGSize], nextValue: () -> [String: CGSize]) {
        value.merge(nextValue()) { _, new in new }
    }
}

/// Wraps its child at `maxWidth` (nil = never wrap: only the creator's own line breaks apply) but reports
/// the child's own (widest-line) width, so the measured block hugs the glyphs the way the native preview's selection does. `.frame(maxWidth:)` would
/// instead grow to the full wrap width and draw a selection box far wider than the text.
private struct SlidePostHuggingWidth: ViewModifier {
    let maxWidth: CGFloat?
    func body(content: Content) -> some View { HuggingLayout(maxWidth: maxWidth) { content } }

    private struct HuggingLayout: Layout {
        let maxWidth: CGFloat?
        func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
            // `.infinity` asks for the ideal size of each line; nil would collapse to the minimum.
            subviews.first?.sizeThatFits(ProposedViewSize(width: maxWidth ?? .infinity, height: nil)) ?? .zero
        }
        func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
            subviews.first?.place(at: bounds.origin, proposal: ProposedViewSize(width: bounds.width, height: bounds.height))
        }
    }
}


/// Reference-type one-shot timer for the hold recognizer (writing it never re-renders).
final class SlidePostHoldTimer {
    private var item: DispatchWorkItem?
    func schedule(after delay: TimeInterval, _ fire: @escaping () -> Void) {
        cancel()
        let work = DispatchWorkItem(block: fire)
        item = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }
    func cancel() { item?.cancel(); item = nil }
}

/// Hit area for the canvas gestures: nil = the whole stage, otherwise the union of the (rotated) text boxes.
struct SlidePostTextHitShape: Shape {
    var rects: [(CGRect, Double)]?
    func path(in bounds: CGRect) -> Path {
        guard let rects else { return Path(bounds) }
        var path = Path()
        for (rect, degrees) in rects {
            let transform = CGAffineTransform(translationX: -rect.midX, y: -rect.midY)
                .concatenating(CGAffineTransform(rotationAngle: degrees * .pi / 180))
                .concatenating(CGAffineTransform(translationX: rect.midX, y: rect.midY))
            path.addPath(Path(rect).applying(transform))
        }
        return path
    }
}
