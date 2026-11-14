import SwiftUI

enum KriaColor {
    static let paper = Color.white
    static let ink = Color(hex: 0x30352C)
    static let mutedInk = Color(hex: 0x526071)
    static let zinc = Color(hex: 0x677587)
    static let line = Color(hex: 0xCAD2DB)
    static let border = Color(hex: 0x677587)
    static let softZinc = Color(hex: 0xF7F7F8)
    static let sky = Color(hex: 0x9BCAFF)
    static let selectionSoft = Color(hex: 0xEBF3FF)
    static let butter = Color(hex: 0xFFF0A6)
    static let sage = Color(hex: 0xDDE6CB)
    static let lilac = Color(hex: 0xE7DDF5)
    static let plum = Color(hex: 0x332847)
    static let menu = Color(hex: 0xFAF8F0)
    static let success = Color(hex: 0x17633B)
    static let successSoft = Color(hex: 0xEAF6EE)
    static let failureText = Color(hex: 0xB42318)
    static let failureSoft = Color(hex: 0xFFF0ED)
    /// Warnings are slate, never yellow or amber: butter is the primary action and never sits in a notice.
    static let warning = Color(hex: 0x526071)
    static let warningSoft = Color(hex: 0xEAEEF3)

    // Pressed fills (Paper "Kria iOS — Component Redesign" S4.01).
    static let fillPressed = Color(hex: 0xE6E9EE)
    static let butterPressed = Color(hex: 0xF2E495)
    static let failurePressed = Color(hex: 0xF8DDD8)

    // Live plan "color behind the glass" (Paper -B artboards).
    /// Pill / chip fill (`--color-fill`).
    static let fill = Color(hex: 0xF1F3F6)
    /// Deciding-card gradient stops (160deg), over the butter wash.
    static let butterWarm = Color(hex: 0xFFF3B8)
    static let butterPale = Color(hex: 0xFFFBE6)
    /// The deeper end of the progress fill and the deciding-card glow (#F5BE28).
    static let butterDeep = Color(hex: 0xF1D048)
    static let butterGlow = Color(hex: 0xF5BE28)
    /// Near-white screen gradient behind the wash.
    static let screenTop = Color(hex: 0xFCFDFF)
}

enum KriaTransparency {
    /// Neither `xcrun simctl ui` nor the `com.apple.Accessibility` defaults flip
    /// `UIAccessibility.isReduceTransparencyEnabled` for a simulator app process,
    /// so UI tests and screenshots force the reduced branch with this env var
    /// (same pattern as `UI_TEST_REDUCE_MOTION`).
    static func isReduced(_ systemValue: Bool) -> Bool {
        systemValue || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_TRANSPARENCY"] == "1"
    }
}

private extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 255) / 255, green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255)
    }
}

enum KriaFont {
    static func body(_ size: CGFloat = 16) -> Font { .custom("Inter", size: size, relativeTo: .body) }
    /// Inter Bold display type. Inter is the only face for app chrome; Fraunces survives only as a text-layer
    /// font the creator can pick for their own video. New type uses `KriaType`.
    static func headline(_ size: CGFloat) -> Font { .custom("Inter", size: size, relativeTo: .largeTitle).weight(.bold) }
}

/// Legacy button styles. New and migrated surfaces use `KriaButtonStyle(role:size:)`; these stay until their
/// call sites move over.
struct KriaPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var fill = KriaColor.butter
    var usesLightText = false
    var minHeight: CGFloat = 48
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(usesLightText ? KriaColor.paper : KriaColor.ink)
            .frame(minHeight: minHeight)
            .padding(.horizontal, 22)
            .background(fill)
            .clipShape(Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.78 : 1) : 0.45)
    }
}

extension EnvironmentValues {
    /// Set by a card that is itself a `fill` surface, so a secondary button inside it reads as paper instead of vanishing.
    @Entry var kriaOnFill = false
}

struct KriaSecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.kriaOnFill) private var onFill
    var minHeight: CGFloat = 48
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(minHeight: minHeight)
            .padding(.horizontal, 20)
            .background(configuration.isPressed ? KriaColor.fillPressed : (onFill ? KriaColor.paper : KriaColor.fill))
            .clipShape(Capsule())
            .opacity(isEnabled ? 1 : 0.45)
    }
}

/// A group heading in sentence case: `KriaType.label` in mutedInk. There is no tracked all-caps eyebrow style.
struct KriaSectionLabel: View {
    let title: String
    var body: some View {
        Text(title)
            .kriaType(.label)
            .foregroundStyle(KriaColor.mutedInk)
            .accessibilityAddTraits(.isHeader)
    }
}

/// An empty or recovery state: Inter only, no illustration. `title` is the screen-level heading; pass
/// `.heading` for a state that sits inside a sheet or list that already has a title.
struct KriaEmptyState: View {
    let title: String
    var message: String?
    let action: String
    var actionRole: KriaButtonStyle.Role = .primary
    var titleStyle: KriaType = .title
    let onAction: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).kriaType(titleStyle).foregroundStyle(KriaColor.ink)
            if let message {
                Text(message).kriaType(.body).foregroundStyle(KriaColor.mutedInk)
            }
            Button(action, action: onAction)
                .buttonStyle(KriaButtonStyle(role: actionRole, size: actionRole == .primary ? .large : .medium, fullWidth: true))
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct KriaStatusPill: View {
    let status: ProjectStatus

    private var label: String {
        switch status {
        case .draft: String(localized: "Draft")
        case .rendering: String(localized: "Rendering")
        case .ready: String(localized: "Ready")
        case .failed: String(localized: "Needs attention")
        }
    }

    private var treatment: (foreground: Color, background: Color) {
        switch status {
        case .ready: (KriaColor.success, KriaColor.successSoft)
        case .draft: (KriaColor.mutedInk, KriaColor.fill)
        case .rendering: (KriaColor.ink, KriaColor.selectionSoft)
        case .failed: (KriaColor.failureText, KriaColor.failureSoft)
        }
    }

    var body: some View {
        Text(label)
            .kriaType(.label)
            .foregroundStyle(treatment.foreground)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(treatment.background, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .accessibilityLabel(String(localized: "Project status: \(label)"))
    }
}

extension View {
    func kriaPage() -> some View {
        self.font(KriaFont.body()).foregroundStyle(KriaColor.ink).tint(KriaColor.ink)
    }
}
