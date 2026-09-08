import SwiftUI

enum KriaColor {
    static let paper = Color(red: 0.985, green: 0.982, blue: 0.965)
    static let ink = Color(red: 0.075, green: 0.075, blue: 0.07)
    static let zinc = Color(red: 0.42, green: 0.42, blue: 0.40)
    static let line = Color.black.opacity(0.12)
    static let lime = Color(red: 0.77, green: 0.95, blue: 0.18)
    static let limeText = Color(red: 0.30, green: 0.48, blue: 0.02)
}

enum KriaFont {
    static func display(_ size: CGFloat) -> Font { .custom("Fraunces", size: size, relativeTo: .title) }
    static func body(_ size: CGFloat = 16) -> Font { .custom("Inter", size: size, relativeTo: .body) }
}

struct KriaPrimaryButtonStyle: ButtonStyle {
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
            .opacity(configuration.isPressed ? 0.78 : 1)
    }
}

struct KriaSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(minHeight: 48)
            .padding(.horizontal, 20)
            .background(KriaColor.paper)
            .overlay(Capsule().stroke(KriaColor.line, lineWidth: 1))
            .clipShape(Capsule())
            .opacity(configuration.isPressed ? 0.65 : 1)
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
    let text: String
    var body: some View {
        Text(text)
            .font(KriaFont.body(12).weight(.semibold))
            .foregroundStyle(KriaColor.limeText)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(KriaColor.lime.opacity(0.38))
            .clipShape(Capsule())
    }
}

extension View {
    func kriaPage() -> some View {
        self.font(KriaFont.body()).foregroundStyle(KriaColor.ink).tint(KriaColor.ink)
    }
}
