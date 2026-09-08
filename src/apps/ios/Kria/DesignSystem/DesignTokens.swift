import SwiftUI

enum KriaColor {
    static let paper = Color.white
    static let ink = Color(red: 0.094, green: 0.094, blue: 0.106)
    static let mutedInk = Color(red: 0.39, green: 0.39, blue: 0.42)
    static let zinc = Color(red: 0.443, green: 0.443, blue: 0.478)
    static let line = Color(red: 0.894, green: 0.894, blue: 0.906)
    static let border = Color(red: 0.831, green: 0.831, blue: 0.847)
    static let softZinc = Color(red: 0.957, green: 0.957, blue: 0.965)
    static let lime = Color(red: 0.518, green: 0.80, blue: 0.086)
    static let limeText = Color(red: 0.247, green: 0.384, blue: 0.071)
    static let limeSoft = Color(red: 0.925, green: 0.988, blue: 0.796)
    static let failureText = Color(red: 0.58, green: 0.20, blue: 0.20)
    static let failureSoft = Color(red: 0.98, green: 0.94, blue: 0.94)
}

enum KriaFont {
    static func display(_ size: CGFloat) -> Font { .custom("Fraunces", size: size, relativeTo: .title) }
    static func body(_ size: CGFloat = 16) -> Font { .custom("Inter", size: size, relativeTo: .body) }
}

struct KriaPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var fill = KriaColor.ink
    var usesLightText = true
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(usesLightText ? KriaColor.paper : KriaColor.ink)
            .frame(minHeight: 48)
            .padding(.horizontal, 22)
            .background(fill)
            .clipShape(Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.78 : 1) : 0.45)
    }
}

struct KriaSecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(minHeight: 48)
            .padding(.horizontal, 20)
            .background(KriaColor.paper)
            .overlay(Capsule().stroke(KriaColor.line, lineWidth: 1))
            .clipShape(Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.65 : 1) : 0.45)
    }
}

struct KriaSectionLabel: View {
    let title: String
    var body: some View {
        Text(title.uppercased())
            .font(KriaFont.body(12).weight(.semibold))
            .tracking(1.2)
            .foregroundStyle(KriaColor.zinc)
            .accessibilityAddTraits(.isHeader)
    }
}

struct KriaEmptyState: View {
    let title: String
    let action: String
    let onAction: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(title).font(KriaFont.display(28)).foregroundStyle(KriaColor.ink)
            Button(action, action: onAction).buttonStyle(KriaPrimaryButtonStyle())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(24)
        .background(KriaColor.paper)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct KriaStatusPill: View {
    let status: ProjectStatus

    private var label: String {
        switch status {
        case .draft: "Draft"
        case .rendering: "Rendering"
        case .ready: "Ready"
        case .failed: "Needs attention"
        }
    }

    private var treatment: (foreground: Color, background: Color) {
        switch status {
        case .ready: (KriaColor.limeText, KriaColor.limeSoft)
        case .draft, .rendering: (KriaColor.zinc, KriaColor.softZinc)
        case .failed: (KriaColor.failureText, KriaColor.failureSoft)
        }
    }

    var body: some View {
        Text(label)
            .font(KriaFont.body(12).weight(.semibold))
            .foregroundStyle(treatment.foreground)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(treatment.background)
            .clipShape(Capsule())
            .accessibilityLabel("Project status: \(label)")
    }
}

extension View {
    func kriaPage() -> some View {
        self.font(KriaFont.body()).foregroundStyle(KriaColor.ink).tint(KriaColor.ink)
    }
}
