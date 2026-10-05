import SwiftUI

/// Tile frames in the scroll view's viewport space, keyed by tile index. Only tiles currently laid out report.
private struct DragSelectFramesKey: PreferenceKey {
    static let defaultValue: [Int: CGRect] = [:]
    static func reduce(value: inout [Int: CGRect], nextValue: () -> [Int: CGRect]) {
        value.merge(nextValue()) { _, new in new }
    }
}

private let dragSelectSpace = "kria.dragSelect.viewport"

extension View {
    /// Registers this grid tile with the enclosing `DragSelectScrollView` by index. No gesture is attached here:
    /// hit-testing is done centrally from these frames, so tiles never fight each other for touches.
    func dragSelectTile(index: Int) -> some View {
        background {
            GeometryReader { proxy in
                Color.clear.preference(key: DragSelectFramesKey.self, value: [index: proxy.frame(in: .named(dragSelectSpace))])
            }
        }
    }
}

/// A vertical `ScrollView` that adds Photos-style slide-to-select to a grid of `.dragSelectTile(index:)` tiles
/// (KRI-282 follow-up).
///
/// - Press and slide mostly sideways across tiles: the first tile touched decides the mode (unselected -> the
///   slide selects, selected -> the slide deselects); sliding back over tiles un-applies them.
/// - A mostly-vertical drag is never claimed: it scrolls the grid. Once selecting, scrolling is locked and the
///   grid auto-scrolls when the finger nears the top or bottom edge.
/// - Taps, press-and-hold and VoiceOver actions on the tiles are untouched; this gesture is purely additive.
///
/// The container never mutates selection itself: `apply` receives the touched indices in order plus the target
/// state, and the caller re-applies them on top of the snapshot it took in `begin`, so reversal is exact.
struct DragSelectScrollView<Content: View>: View {
    let isEnabled: Bool
    let isSelected: (Int) -> Bool
    let begin: () -> Void
    let apply: (_ indices: [Int], _ selected: Bool) -> Void
    let end: () -> Void
    @ViewBuilder let content: () -> Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Only `locked` is observed: flipping state on a plain scroll would re-render the ScrollView mid-pan.
    @State private var locked = false
    @State private var position = ScrollPosition()
    @State private var trail = DragSelect.Trail()
    @State private var mode = DragSelect.Mode.select
    @State private var autoScroll: Task<Void, Never>?

    /// Non-observed scratch values (updated on every scroll frame / touch sample; must not re-render).
    private enum Phase { case idle, scrolling, selecting }
    private final class Metrics {
        var frames: [Int: CGRect] = [:]
        var offsetY: CGFloat = 0
        var maxOffsetY: CGFloat = 0
        var viewportHeight: CGFloat = 0
        var finger: CGPoint = .zero
        var phase: Phase = .idle
    }
    @State private var scratch = Metrics()

    private var metricsRef: Metrics { scratch }

    var body: some View {
        ScrollView {
            content()
                .gesture(DragSelectRecognizer(isEnabled: isEnabled, handler: handle))
        }
        .scrollPosition($position)
        .scrollDisabled(locked)
        .coordinateSpace(name: dragSelectSpace)
        .onPreferenceChange(DragSelectFramesKey.self) { metricsRef.frames = $0 }
        .onScrollGeometryChange(for: ScrollMetrics.self) { geometry in
            ScrollMetrics(
                offsetY: geometry.contentOffset.y + geometry.contentInsets.top,
                maxOffsetY: max(0, geometry.contentSize.height - geometry.containerSize.height),
                viewportHeight: geometry.containerSize.height
            )
        } action: { _, new in
            metricsRef.offsetY = new.offsetY
            metricsRef.maxOffsetY = new.maxOffsetY
            metricsRef.viewportHeight = new.viewportHeight
        }
        .onDisappear { finish() }
    }

    private struct ScrollMetrics: Equatable { var offsetY: CGFloat; var maxOffsetY: CGFloat; var viewportHeight: CGFloat }

    private func handle(_ pan: UIPanGestureRecognizer, _ context: UIGestureRecognizerRepresentableContext<DragSelectRecognizer>) {
        switch pan.state {
        case .began:
            guard isEnabled else { return }
            let location = context.converter.location(in: .named(dragSelectSpace))
            let translation = pan.translation(in: pan.view)
            let start = CGPoint(x: location.x - translation.x, y: location.y - translation.y)
            metricsRef.finger = location
            guard let first = DragSelect.index(at: start, in: metricsRef.frames) else {
                metricsRef.phase = .scrolling
                return
            }
            begin()
            mode = .forFirstTile(isSelected: isSelected(first))
            trail = DragSelect.Trail()
            trail.start(at: start, tile: first)
            metricsRef.phase = .selecting
            locked = true
            advance(to: location)
            startAutoScroll()
        case .changed:
            guard metricsRef.phase == .selecting else { return }
            let location = context.converter.location(in: .named(dragSelectSpace))
            metricsRef.finger = location
            advance(to: location)
        case .ended, .cancelled, .failed:
            finish()
        default:
            break
        }
    }

    private func advance(to point: CGPoint) {
        trail.advance(to: point, in: metricsRef.frames)
        apply(trail.indices, mode.targetsSelected)
    }

    private func finish() {
        autoScroll?.cancel()
        autoScroll = nil
        if metricsRef.phase == .selecting { end() }
        metricsRef.phase = .idle
        if locked { locked = false }
    }

    /// Scrolls while the finger rests near an edge, re-hit-testing as new tiles come under it. Programmatic and
    /// un-animated; Reduce Motion halves the speed so the content never races past.
    private func startAutoScroll() {
        autoScroll?.cancel()
        autoScroll = Task { @MainActor in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(16))
                let m = metricsRef
                var step = DragSelect.autoScrollStep(fingerY: m.finger.y, viewportHeight: m.viewportHeight)
                if reduceMotion { step /= 2 }
                guard step != 0 else { continue }
                let target = min(max(m.offsetY + step, 0), m.maxOffsetY)
                guard target != m.offsetY else { continue }
                m.offsetY = target
                position.scrollTo(y: target)
                advance(to: m.finger)
            }
        }
    }
}

/// Pan recognizer that only BEGINS for a mostly-horizontal drag, so a vertical drag is never claimed and the
/// enclosing scroll view scrolls exactly as it would without us (a SwiftUI `DragGesture` here swallowed the scroll).
/// It recognises alongside the scroll view's own pan and cancels the tile buttons' touches once it begins, so a
/// slide never ends in a stray tap.
struct DragSelectRecognizer: UIGestureRecognizerRepresentable {
    let isEnabled: Bool
    let handler: (UIPanGestureRecognizer, UIGestureRecognizerRepresentableContext<DragSelectRecognizer>) -> Void

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var isEnabled = true
        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
            guard isEnabled, let pan = gestureRecognizer as? UIPanGestureRecognizer else { return false }
            let t = pan.translation(in: pan.view)
            return DragSelect.isHorizontalIntent(translation: CGSize(width: t.x, height: t.y))
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> UIPanGestureRecognizer {
        let pan = UIPanGestureRecognizer()
        pan.maximumNumberOfTouches = 1
        pan.delegate = context.coordinator
        return pan
    }

    func updateUIGestureRecognizer(_ recognizer: UIPanGestureRecognizer, context: Context) {
        context.coordinator.isEnabled = isEnabled
    }

    func handleUIGestureRecognizerAction(_ recognizer: UIPanGestureRecognizer, context: Context) {
        handler(recognizer, context)
    }
}
