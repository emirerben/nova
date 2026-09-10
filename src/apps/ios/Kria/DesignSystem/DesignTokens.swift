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
}

private extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 255) / 255, green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255)
    }
}

enum KriaFont {
    static func display(_ size: CGFloat) -> Font { .custom("Fraunces", size: size, relativeTo: .title) }
    static func body(_ size: CGFloat = 16) -> Font { .custom("Inter", size: size, relativeTo: .body) }
}

struct KriaPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var fill = KriaColor.butter
    var usesLightText = false
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
        case .ready: (KriaColor.success, KriaColor.successSoft)
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
