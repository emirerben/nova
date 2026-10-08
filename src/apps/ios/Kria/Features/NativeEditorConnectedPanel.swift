import SwiftUI

/// One destination owns both the visible inspector and the rail highlight.
enum NativeEditorPanel: Equatable {
    /// `textInline` is typing on the video (KRI-508): the panel shrinks to the bar that rides the keyboard.
    case text(String), textInline(String), textCreation, captions, visuals, sounds

    var tool: NativeEditorTool {
        switch self {
        case .text, .textInline, .textCreation: return .text
        case .captions: return .captions
        case .visuals: return .visuals
        case .sounds: return .sounds
        }
    }
}

/// The panel and timeline resize handles each own their own value (KRI-170).
/// Keeping the drag math here makes both handles continuous in global
/// coordinates and gives them a real, accessible hit target. `range` is how
/// many points of finger travel move `value` by one unit; `bounds` may extend
/// below zero (the preview handle grows the preview with negative values).
struct NativeEditorPanelResizeGrabber: View {
    @Binding var expansion: CGFloat
    let range: CGFloat
    let reduceMotion: Bool
    let accessibilityIdentifier: String
    var topAligned = false
    var bounds: ClosedRange<CGFloat> = 0...1
    var accessibilityTitle = "Editor panel size"
    var accessibilityHint = "Swipe up or down to resize the editor panel"
    /// Spoken value, given the current value and its bounds.
    var describe: (CGFloat, ClosedRange<CGFloat>) -> String = { value, bounds in
        "\(Int(((value - bounds.lowerBound) / max(0.0001, bounds.upperBound - bounds.lowerBound)) * 100)) percent expanded"
    }
    /// Signed fraction of the span added by a VoiceOver "increment".
    var incrementFraction: CGFloat = 0.25
    /// The whole row resizes, not just the 80 pt around the line: the preview
    /// handle sits in an otherwise empty band people drag anywhere in.
    var spansRow = false
    /// Lets a drag past the smallest size close the panel (KRI-253).
    var dismiss: NativeEditorPanelDismiss?
    @State private var feedback = 0

    private func clamp(_ value: CGFloat) -> CGFloat {
        min(bounds.upperBound, max(bounds.lowerBound, value))
    }

    var body: some View {
        Capsule()
            .fill(KriaColor.ink.opacity(0.28))
            .frame(width: 38, height: 4)
            .padding(.top, topAligned ? 8 : 0)
            .frame(width: spansRow ? nil : 80, height: 44, alignment: topAligned ? .top : .center)
            .frame(maxWidth: spansRow ? .infinity : nil)
            .contentShape(Rectangle())
            .modifier(NativeEditorPanelResizeDrag(expansion: $expansion, range: range, bounds: bounds, dismiss: dismiss))
            .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: feedback)
            .accessibilityElement()
            .accessibilityLabel(accessibilityTitle)
            .accessibilityValue(describe(expansion, bounds))
            .accessibilityHint(accessibilityHint)
            .accessibilityAdjustableAction { direction in
                let span = bounds.upperBound - bounds.lowerBound
                let step = (direction == .increment ? incrementFraction : -incrementFraction) * span
                let next = clamp(expansion + step)
                guard next != expansion else { return }
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) { expansion = next }
                feedback += 1
            }
            .accessibilityIdentifier(accessibilityIdentifier)
    }
}

/// The finger-tracking resize drag shared by the grabbers and the panel's
/// header surface. Each attachment keeps its own origin; only one drag runs at
/// a time.
struct NativeEditorPanelResizeDrag: ViewModifier {
    @Binding var expansion: CGFloat
    let range: CGFloat
    var bounds: ClosedRange<CGFloat> = 0...1
    var minimumDistance: CGFloat = 3
    /// Lets a drag past the smallest size close the panel (KRI-253).
    var dismiss: NativeEditorPanelDismiss?
    @State private var dragOrigin: CGFloat?
    @State private var feedback = 0
    @GestureState private var isDragging = false

    private func clamp(_ value: CGFloat) -> CGFloat {
        min(bounds.upperBound, max(bounds.lowerBound, value))
    }

    private func pull(_ translation: CGFloat, origin: CGFloat) -> CGFloat {
        NativeEditorPanelDismissRule.pull(translation: translation, aboveMinimum: (origin - bounds.lowerBound) * range)
    }

    func body(content: Content) -> some View {
        content
            .gesture(
                DragGesture(minimumDistance: minimumDistance, coordinateSpace: .global)
                    .updating($isDragging) { _, dragging, _ in dragging = true }
                    .onChanged { value in
                        guard range > 0 || dismiss != nil else { return }
                        if dragOrigin == nil {
                            dragOrigin = clamp(expansion)
                            feedback += 1
                        }
                        let origin = dragOrigin ?? 0
                        if range > 0 {
                            let next = clamp(origin - value.translation.height / range)
                            if next != expansion && (next == bounds.lowerBound || next == bounds.upperBound) { feedback += 1 }
                            expansion = next
                        }
                        if let dismiss {
                            let next = pull(value.translation.height, origin: origin)
                            if NativeEditorPanelDismissRule.isArmed(next) != NativeEditorPanelDismissRule.isArmed(dismiss.pull.wrappedValue) {
                                feedback += 1
                            }
                            dismiss.pull.wrappedValue = next
                        }
                    }
                    .onEnded { value in
                        if let dismiss, let origin = dragOrigin {
                            dismiss.pull.wrappedValue = 0
                            if NativeEditorPanelDismissRule.shouldDismiss(
                                pull: pull(value.translation.height, origin: origin),
                                projectedPull: pull(value.predictedEndTranslation.height, origin: origin),
                                startedAtMinimum: origin <= bounds.lowerBound
                            ) {
                                dismiss.complete()
                            }
                        }
                        dragOrigin = nil
                        feedback += 1
                    }
            )
            // A cancelled drag never reaches onEnded; don't leave the panel pulled down.
            .onChange(of: isDragging) { _, dragging in
                if !dragging, let dismiss, dismiss.pull.wrappedValue != 0 { dismiss.pull.wrappedValue = 0 }
            }
            .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: feedback)
    }
}

/// Dragging the connected panel down past its smallest size closes it the way
/// its Done button does (KRI-253).
struct NativeEditorPanelDismiss {
    /// Points the finger has travelled below the panel's smallest size.
    let pull: Binding<CGFloat>
    /// The open panel's Done.
    let complete: () -> Void
}

enum NativeEditorPanelDismissRule {
    /// A release this far below the smallest size closes the panel.
    static let distance: CGFloat = 64
    /// A quick flick closes it sooner, but only when the drag began at the
    /// smallest size: collapsing a raised panel never closes it by momentum.
    static let flickMinimum: CGFloat = 12
    static let flickProjection: CGFloat = 160
    /// How far the panel can follow the finger down.
    static let maxOffset: CGFloat = 96

    /// Points below the smallest size, given the drag's translation and how far
    /// above the smallest size the drag started.
    static func pull(translation: CGFloat, aboveMinimum: CGFloat) -> CGFloat {
        max(0, translation - max(0, aboveMinimum))
    }

    static func isArmed(_ pull: CGFloat) -> Bool { pull >= distance }

    static func shouldDismiss(pull: CGFloat, projectedPull: CGFloat, startedAtMinimum: Bool) -> Bool {
        isArmed(pull) || (startedAtMinimum && pull >= flickMinimum && projectedPull >= flickProjection)
    }

    /// The panel follows the finger 1:1 at first, then resists.
    static func offset(forPull pull: CGFloat) -> CGFloat {
        guard pull > 0 else { return 0 }
        return maxOffset * pull / (pull + maxOffset)
    }
}

/// What the connected panel's header needs to resize the panel (KRI-235)
/// and to close it with a drag down (KRI-253).
struct NativeEditorPanelResize {
    let expansion: Binding<CGFloat>
    let range: CGFloat
    var dismiss: NativeEditorPanelDismiss?
}

/// The whole fixed header of a connected panel (top band, title row, tabs)
/// resizes the panel, not just the grabber line (KRI-235), and drags it closed
/// past its smallest size (KRI-253). The scrolling body
/// is deliberately excluded so lists, sliders and text fields keep their own
/// gestures. The larger slop keeps header buttons tappable.
private struct NativeEditorPanelResizeSurface: ViewModifier {
    @Environment(\.nativeEditorPanelResize) private var resize

    func body(content: Content) -> some View {
        if let resize {
            content
                .contentShape(Rectangle())
                .modifier(NativeEditorPanelResizeDrag(
                    expansion: resize.expansion, range: resize.range, minimumDistance: 8, dismiss: resize.dismiss
                ))
        } else {
            content
        }
    }
}

extension View {
    func nativeEditorPanelResizeSurface() -> some View { modifier(NativeEditorPanelResizeSurface()) }
}

/// Flush view-local drafts before changing destinations. Owner tokens keep an
/// outgoing view's delayed onDisappear from removing its replacement's cleanup.
@MainActor
final class NativeEditorPanelLifecycle: ObservableObject {
    private var owner: UUID?
    private var cleanup: (() -> Void)?

    func register(owner: UUID, prepareToClose: @escaping () -> Void) {
        self.owner = owner
        cleanup = prepareToClose
    }

    func unregister(owner: UUID) {
        guard self.owner == owner else { return }
        self.owner = nil
        cleanup = nil
    }

    func prepareToClose() {
        let action = cleanup
        owner = nil
        cleanup = nil
        action?()
    }
}

private struct NativeEditorConnectedPanelKey: EnvironmentKey {
    static let defaultValue = false
}

private struct NativeEditorPanelContentWidthKey: EnvironmentKey {
    static let defaultValue: CGFloat = 340
}

private struct NativeEditorPanelLifecycleKey: EnvironmentKey {
    static let defaultValue: NativeEditorPanelLifecycle? = nil
}

private struct NativeEditorPanelResizeKey: EnvironmentKey {
    // Computed: the value carries a closure, so it can't be a stored static.
    static var defaultValue: NativeEditorPanelResize? { nil }
}

extension EnvironmentValues {
    var nativeEditorPanelContentWidth: CGFloat {
        get { self[NativeEditorPanelContentWidthKey.self] }
        set { self[NativeEditorPanelContentWidthKey.self] = newValue }
    }
    var nativeEditorConnectedPanel: Bool {
        get { self[NativeEditorConnectedPanelKey.self] }
        set { self[NativeEditorConnectedPanelKey.self] = newValue }
    }
    var nativeEditorPanelLifecycle: NativeEditorPanelLifecycle? {
        get { self[NativeEditorPanelLifecycleKey.self] }
        set { self[NativeEditorPanelLifecycleKey.self] = newValue }
    }
    var nativeEditorPanelResize: NativeEditorPanelResize? {
        get { self[NativeEditorPanelResizeKey.self] }
        set { self[NativeEditorPanelResizeKey.self] = newValue }
    }
}

struct NativeEditorPanelTabs<Tab: Hashable & RawRepresentable>: View where Tab.RawValue == String {
    let tabs: [Tab]
    @Binding var selection: Tab
    let accessibilityPrefix: String
    /// Reuses the control where an existing screen already publishes its own tab identifiers.
    var identifier: ((Tab) -> String)?

    var body: some View {
        HStack(spacing: 2) {
            ForEach(tabs, id: \.self) { value in
                Button { selection = value } label: {
                    Text(value.rawValue)
                        .font(KriaFont.body(14).weight(value == selection ? .semibold : .regular))
                        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                        .foregroundStyle(value == selection ? KriaColor.ink : KriaColor.zinc)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(value == selection ? KriaColor.paper : .clear, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(value == selection ? .isSelected : [])
                .accessibilityIdentifier(identifier?(value) ?? accessibilityPrefix + "-tab-" + value.rawValue)
            }
        }
        .padding(3)
        .background(KriaColor.ink.opacity(0.025), in: Capsule())
    }
}
