import SwiftUI

/// One destination owns both the visible inspector and the rail highlight.
enum NativeEditorPanel: Equatable {
    case text(String), textCreation, captions, visuals, sounds

    var tool: NativeEditorTool {
        switch self {
        case .text, .textCreation: return .text
        case .captions: return .captions
        case .visuals: return .visuals
        case .sounds: return .sounds
        }
    }
}

/// The panel and timeline resize handles share one expansion value. Keeping
/// the drag math here makes both handles continuous in global coordinates and
/// gives the connected handle a real, accessible hit target.
struct NativeEditorPanelResizeGrabber: View {
    @Binding var expansion: CGFloat
    let range: CGFloat
    let reduceMotion: Bool
    let accessibilityIdentifier: String
    var topAligned = false
    @State private var dragOrigin: CGFloat?
    @State private var feedback = 0

    var body: some View {
        Capsule()
            .fill(KriaColor.ink.opacity(0.28))
            .frame(width: 38, height: 4)
            .padding(.top, topAligned ? 8 : 0)
            .frame(width: 80, height: 44, alignment: topAligned ? .top : .center)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 3, coordinateSpace: .global)
                    .onChanged { value in
                        guard range > 0 else { return }
                        if dragOrigin == nil {
                            dragOrigin = expansion
                            feedback += 1
                        }
                        let next = min(1, max(0, (dragOrigin ?? 0) - value.translation.height / range))
                        if next != expansion && (next == 0 || next == 1) { feedback += 1 }
                        expansion = next
                    }
                    .onEnded { _ in
                        dragOrigin = nil
                        feedback += 1
                    }
            )
            .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: feedback)
            .accessibilityElement()
            .accessibilityLabel("Editor panel size")
            .accessibilityValue("\(Int(expansion * 100)) percent expanded")
            .accessibilityHint("Swipe up or down to resize the editor panel and preview")
            .accessibilityAdjustableAction { direction in
                let step: CGFloat = direction == .increment ? 0.25 : -0.25
                let next = min(1, max(0, expansion + step))
                guard next != expansion else { return }
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.18)) { expansion = next }
                feedback += 1
            }
            .accessibilityIdentifier(accessibilityIdentifier)
    }
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
}

struct NativeEditorPanelTabs<Tab: Hashable & RawRepresentable>: View where Tab.RawValue == String {
    let tabs: [Tab]
    @Binding var selection: Tab
    let accessibilityPrefix: String

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
                .accessibilityIdentifier(accessibilityPrefix + "-tab-" + value.rawValue)
            }
        }
        .padding(3)
        .background(KriaColor.ink.opacity(0.025), in: Capsule())
    }
}
