import SwiftUI
import UIKit.UIGestureRecognizerSubclass

struct KriaMenuAction: Identifiable {
    let id = UUID()
    let title: String
    var systemImage: String?
    var isDestructive = false
    let handler: () -> Void
}

/// One attached item (Paper S4.08): a 52x64 thumbnail, a name, a status line and one trailing 44pt control.
///
/// States: ready and preparing show a label; uploading shows a `KriaProgressTrack` and the percentage; failed
/// shows the failure in failureText and, when `onRetry` is set, a compact "Retry". The name wraps to two lines
/// and truncates in the middle, so a long file name keeps its extension.
struct KriaMediaRow<Thumbnail: View>: View {
    enum Status {
        case ready(String)
        case preparing
        case uploading(Double)
        case failed(String)
    }

    enum Trailing {
        case none
        /// A finished item that needs no action: the green check disc.
        case check
        case remove(label: String = String(localized: "Remove"), () -> Void)
        case menu(label: String = String(localized: "More actions"), [KriaMenuAction])
    }

    let name: String
    let status: Status
    var trailing: Trailing = .none
    var onRetry: (() -> Void)?
    /// The row is its own fill card (r20) instead of a paper row that needs a fill wrapper around it.
    var onFill = false
    @ViewBuilder var thumbnail: () -> Thumbnail

    var body: some View {
        HStack(spacing: 12) {
            thumbnail()
                .frame(width: 52, height: 64)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(name).kriaType(.button).foregroundStyle(KriaColor.ink)
                    .lineLimit(2).truncationMode(.middle)
                statusLine
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            if case .uploading(let progress) = status {
                Text("\(Int((min(1, max(0, progress)) * 100).rounded()))%")
                    .kriaType(.label).foregroundStyle(KriaColor.mutedInk).monospacedDigit()
            }
            if case .failed = status, let onRetry {
                Button("Retry", action: onRetry).buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
            }
            trailingControl
        }
        .padding(8)
        .frame(maxWidth: .infinity)
        .background(onFill ? KriaColor.fill : KriaColor.paper, in: RoundedRectangle(cornerRadius: onFill ? 20 : 16, style: .continuous))
    }

    @ViewBuilder private var statusLine: some View {
        switch status {
        case .ready(let text):
            Text(text).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
        case .preparing:
            HStack(spacing: 6) {
                KriaSpinner(size: 13)
                Text("Preparing…").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            }
        case .uploading(let progress):
            KriaProgressTrack(progress: progress)
                .accessibilityLabel("Uploading")
                .accessibilityValue("\(Int((min(1, max(0, progress)) * 100).rounded())) percent")
        case .failed(let text):
            Text(text).kriaType(.label).foregroundStyle(KriaColor.failureText)
        }
    }

    @ViewBuilder private var trailingControl: some View {
        switch trailing {
        case .none:
            EmptyView()
        case .check:
            KriaCheckDisc(size: 28).accessibilityHidden(true)
        case .remove(let label, let action):
            Button(action: action) { Image(systemName: "trash").font(.system(size: 17)) }
                .buttonStyle(.kriaIcon).accessibilityLabel(label)
        case .menu(let label, let actions):
            Menu {
                ForEach(actions) { item in
                    Button(role: item.isDestructive ? .destructive : nil, action: item.handler) {
                        if let image = item.systemImage { Label(item.title, systemImage: image) } else { Text(item.title) }
                    }
                }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 17, weight: .bold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(width: 44, height: 44)
                    .background(KriaColor.fill, in: Circle())
                    .contentShape(Circle())
            }
            .accessibilityLabel(label)
        }
    }
}

/// The fill placeholder with a 17pt glyph for a thumbnail that has not loaded.
struct KriaThumbnailPlaceholder: View {
    var systemImage = "photo"
    var failed = false

    var body: some View {
        ZStack {
            (failed ? KriaColor.failureSoft : KriaColor.fill)
            Image(systemName: systemImage).font(.system(size: 17))
                .foregroundStyle(failed ? KriaColor.failureText : KriaColor.mutedInk)
        }
    }
}

/// Which steps a format uses, with the current one marked (Paper S4.09). One step means no rail.
/// Current is an ink bar and ink label; done is a check disc and a mutedInk label; upcoming is a fill bar and
/// a zinc label. There are no step digits.
struct KriaStepRail: View {
    let steps: [String]
    let current: Int
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        if steps.count > 1 {
            if typeSize.isAccessibilitySize {
                // Three labels can't share a row at this size without clipping: keep the bars and name only the current step.
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        ForEach(steps.indices, id: \.self) { index in
                            RoundedRectangle(cornerRadius: 2).fill(barColor(index)).frame(height: 4)
                        }
                    }
                    .accessibilityHidden(true)
                    Text("\(steps[min(current, steps.count - 1)]) · step \(current + 1) of \(steps.count)")
                        .kriaType(.label).fontWeight(.semibold).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(steps[min(current, steps.count - 1)]), current step, \(current + 1) of \(steps.count)")
            } else {
                HStack(spacing: 8) {
                    ForEach(steps.indices, id: \.self) { index in
                        VStack(alignment: .leading, spacing: 8) {
                            RoundedRectangle(cornerRadius: 2).fill(barColor(index)).frame(height: 4)
                            HStack(spacing: 6) {
                                if index < current { KriaCheckDisc(size: 16) }
                                Text(steps[index]).kriaType(.label)
                                    .fontWeight(index == current ? .semibold : .medium)
                                    .foregroundStyle(labelColor(index))
                                    .lineLimit(1)
                            }
                            .frame(minHeight: 20)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityElement(children: .combine)
                        .accessibilityLabel(index < current ? String(localized: "\(steps[index]), done") : index == current ? String(localized: "\(steps[index]), current step") : String(localized: "\(steps[index]), not started"))
                    }
                }
            }
        }
    }

    private func barColor(_ index: Int) -> Color {
        index < current ? KriaColor.line : index == current ? KriaColor.ink : KriaColor.fill
    }

    private func labelColor(_ index: Int) -> Color {
        index < current ? KriaColor.mutedInk : index == current ? KriaColor.ink : KriaColor.zinc
    }
}

extension View {
    /// Swiping the row left reveals a Delete capsule; a long swipe deletes. VoiceOver gets the same as a "Remove"
    /// action. Vertical drags are left to the enclosing scroll view.
    func kriaSwipeToDelete(label: String, identifier: String, isEnabled: Bool = true, action: @escaping () -> Void) -> some View {
        modifier(KriaSwipeToDelete(label: label, identifier: identifier, isEnabled: isEnabled, action: action))
    }
}

private struct KriaSwipeToDelete: ViewModifier {
    let label: String
    let identifier: String
    let isEnabled: Bool
    let action: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var offset: CGFloat = 0
    @State private var restingOffset: CGFloat = 0
    @State private var rowWidth: CGFloat = 0
    private let revealWidth: CGFloat = 96
    private let fullSwipeFraction: CGFloat = 0.6

    func body(content: Content) -> some View {
        ZStack(alignment: .trailing) {
            if offset < 0 {
                Button(action: commit) {
                    Image(systemName: "trash").font(.system(size: 18, weight: .semibold)).foregroundStyle(.white)
                        .frame(width: revealWidth - 8)
                        .frame(maxHeight: .infinity)
                        .background(KriaColor.failureText, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(label)
                .accessibilityIdentifier(identifier)
                .transition(.opacity)
            }
            content
                .offset(x: offset)
                .accessibilityAction(named: Text(label)) { if isEnabled { commit() } }
        }
        .background(GeometryReader { geometry in Color.clear.onAppear { rowWidth = geometry.size.width }.onChange(of: geometry.size.width) { _, width in rowWidth = width } })
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        // A UIKit pan that only begins for a sideways drag. A SwiftUI DragGesture here, even a simultaneous one,
        // kept the chat from scrolling when a vertical drag started on a media row (KRI-1550).
        .gesture(KriaSidewaysSwipeRecognizer(isEnabled: isEnabled, handler: handle))
        .onChange(of: isEnabled) { _, enabled in if !enabled { settle(to: 0) } }
    }

    private func handle(_ pan: UIPanGestureRecognizer) {
        let travel = pan.translation(in: pan.view).x
        switch pan.state {
        case .began, .changed:
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { offset = min(0, max(-max(rowWidth, revealWidth), restingOffset + travel)) }
        case .ended:
            // UIKit keeps reporting the last movement's speed after the finger has stopped, so a swipe held still
            // before it lifts is not a flick.
            let moving = (pan as? SidewaysPanGestureRecognizer)?.movedRecently ?? true
            let predicted = restingOffset + travel + (moving ? Self.flickTravel(velocity: pan.velocity(in: pan.view).x) : 0)
            if offset < -rowWidth * fullSwipeFraction || predicted < -rowWidth * 0.9 { commit() }
            else { settle(to: offset < -revealWidth / 2 || predicted < -revealWidth ? -revealWidth : 0) }
        case .cancelled, .failed:
            settle(to: restingOffset)
        default:
            break
        }
    }

    /// How much further a flick at `velocity` (pt/s) would carry, decelerating like a scroll view.
    private static func flickTravel(velocity: CGFloat) -> CGFloat {
        let rate = UIScrollView.DecelerationRate.normal.rawValue
        return velocity / 1000 * rate / (1 - rate)
    }

    private func settle(to target: CGFloat) {
        restingOffset = target
        withAnimation(reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 0.85)) { offset = target }
    }

    private func commit() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        action()
        settle(to: 0)
    }
}

/// Pan recognizer that only begins for a mostly-sideways drag, so a vertical drag that starts on the row is never
/// claimed and the enclosing scroll view scrolls as it would without it.
private struct KriaSidewaysSwipeRecognizer: UIGestureRecognizerRepresentable {
    let isEnabled: Bool
    let handler: (UIPanGestureRecognizer) -> Void

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var isEnabled = true
        func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool { isEnabled }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }

    func makeCoordinator(converter: CoordinateSpaceConverter) -> Coordinator { Coordinator() }

    func makeUIGestureRecognizer(context: Context) -> SidewaysPanGestureRecognizer {
        let pan = SidewaysPanGestureRecognizer()
        pan.maximumNumberOfTouches = 1
        pan.delegate = context.coordinator
        return pan
    }

    func updateUIGestureRecognizer(_ recognizer: SidewaysPanGestureRecognizer, context: Context) {
        context.coordinator.isEnabled = isEnabled
    }

    func handleUIGestureRecognizerAction(_ recognizer: SidewaysPanGestureRecognizer, context: Context) {
        handler(recognizer)
    }
}

/// A pan that fails as soon as the finger's travel is more vertical than sideways. The direction is read from the
/// touches themselves: under SwiftUI, `gestureRecognizerShouldBegin` was asked with a zero translation (KRI-1550).
private final class SidewaysPanGestureRecognizer: UIPanGestureRecognizer {
    private var start: CGPoint?
    private var lastMove: CFTimeInterval = 0
    /// The finger moved within the last 0.1 s.
    var movedRecently: Bool { CACurrentMediaTime() - lastMove < 0.1 }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        if start == nil, let touch = touches.first { start = touch.location(in: view) }
        super.touchesBegan(touches, with: event)
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        lastMove = CACurrentMediaTime()
        if state == .possible, let start, let touch = touches.first {
            let point = touch.location(in: view)
            let dx = abs(point.x - start.x), dy = abs(point.y - start.y)
            if dy > 2, dy >= dx {
                state = .failed
                return
            }
        }
        super.touchesMoved(touches, with: event)
    }

    override func reset() {
        super.reset()
        start = nil
    }
}
