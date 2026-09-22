import SwiftUI

/// One destination owns both the visible inspector and the rail highlight.
enum NativeEditorPanel: Equatable {
    case text(String), captions, visuals, sounds

    var tool: NativeEditorTool {
        switch self {
        case .text: return .text
        case .captions: return .captions
        case .visuals: return .visuals
        case .sounds: return .sounds
        }
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
