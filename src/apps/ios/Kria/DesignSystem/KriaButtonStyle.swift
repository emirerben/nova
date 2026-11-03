import SwiftUI

/// The button system (Paper S4.01): fills, never strokes; capsule; 44pt at minimum.
///
/// - Primary is butter with an ink label. It is the one main action of a surface.
/// - Secondary is `fill`; pass `onFill: true` when the button sits on a `fill` card so it switches to paper.
/// - Tertiary is a text button. `tertiaryDestructive` is its failure-coloured twin.
/// - Destructive is a failureSoft capsule with a failureText label.
/// - Ink is the button for a failure surface, where butter never sits beside red. White label on ink.
/// - Icon is a 44pt circle; `iconGlass` is its translucent twin for controls above photos and video.
///
/// Disabled primary and secondary fall to `fill` with a zinc label (the reason belongs in
/// `kriaDisabledReason`). Loading keeps the role fill, adds a spinner and ignores taps.
struct KriaButtonStyle: ButtonStyle {
    enum Role { case primary, secondary, tertiary, tertiaryDestructive, destructive, ink, icon, iconGlass }
    enum Size {
        case large, medium
        var height: CGFloat { self == .large ? 48 : 44 }
    }

    var role: Role = .primary
    var size: Size = .large
    /// Stretches to the container's width (sheets and cards); inline chat buttons hug their label.
    var fullWidth = false
    /// The button sits on a `fill` surface, so a secondary becomes paper.
    var onFill = false
    var isLoading = false

    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        let live = isEnabled || isLoading
        let label = labelColor(live: live)
        let isIcon = role == .icon || role == .iconGlass
        return HStack(spacing: 8) {
            if isLoading { KriaSpinner(size: 16, tint: label) }
            configuration.label
        }
        .kriaType(.button)
        .foregroundStyle(label)
        .lineLimit(isIcon ? 1 : nil)
        .multilineTextAlignment(.center)
        .padding(.horizontal, isIcon ? 0 : horizontalPadding)
        .padding(.vertical, isIcon ? 0 : 8)
        .frame(width: isIcon ? 44 : nil, height: isIcon ? 44 : nil)
        .frame(maxWidth: fullWidth && !isIcon ? .infinity : nil, minHeight: isIcon ? 44 : size.height)
        .background(fill(pressed: pressed, live: live), in: Capsule())
        .contentShape(Capsule())
        .opacity(role == .icon && !live ? 0.5 : 1)
        .scaleEffect(pressed && !KriaMotion.isReduced(systemReduceMotion) ? 0.97 : 1)
        .animation(.easeOut(duration: 0.12), value: pressed)
        .allowsHitTesting(!isLoading)
    }

    private var horizontalPadding: CGFloat {
        switch role {
        case .tertiary, .tertiaryDestructive: 12
        default: size == .large ? 24 : 20
        }
    }

    private func labelColor(live: Bool) -> Color {
        switch role {
        case .primary, .tertiary: live ? KriaColor.ink : KriaColor.zinc
        case .secondary: live ? KriaColor.ink : (onFill ? KriaColor.mutedInk : KriaColor.zinc)
        case .tertiaryDestructive, .destructive: live ? KriaColor.failureText : KriaColor.zinc
        case .ink: live ? KriaColor.paper : KriaColor.zinc
        case .icon: live ? KriaColor.ink : KriaColor.zinc
        case .iconGlass: KriaColor.paper
        }
    }

    private func fill(pressed: Bool, live: Bool) -> Color {
        switch role {
        case .primary:
            live ? (pressed ? KriaColor.butterPressed : KriaColor.butter) : KriaColor.fill
        case .secondary:
            if onFill { live ? (pressed ? KriaColor.fillPressed : KriaColor.paper) : KriaColor.paper.opacity(0.6) }
            else { live ? (pressed ? KriaColor.fillPressed : KriaColor.fill) : KriaColor.fill }
        case .tertiary:
            pressed ? KriaColor.ink.opacity(0.06) : .clear
        case .tertiaryDestructive:
            pressed ? KriaColor.failureText.opacity(0.08) : .clear
        case .destructive:
            live ? (pressed ? KriaColor.failurePressed : KriaColor.failureSoft) : KriaColor.fill
        case .ink:
            live ? KriaColor.ink.opacity(pressed ? 0.85 : 1) : KriaColor.fill
        case .icon:
            pressed ? KriaColor.fillPressed : KriaColor.fill
        case .iconGlass:
            Color.white.opacity(pressed ? 0.4 : 0.28)
        }
    }
}

extension ButtonStyle where Self == KriaButtonStyle {
    static var kriaPrimary: KriaButtonStyle { KriaButtonStyle(role: .primary) }
    static var kriaSecondary: KriaButtonStyle { KriaButtonStyle(role: .secondary, size: .medium) }
    static var kriaTertiary: KriaButtonStyle { KriaButtonStyle(role: .tertiary, size: .medium) }
    static var kriaIcon: KriaButtonStyle { KriaButtonStyle(role: .icon, size: .medium) }
}

extension View {
    /// Puts the reason a control is disabled under it, in `KriaType.caption`. Pass nil when it is enabled.
    func kriaDisabledReason(_ reason: String?) -> some View {
        VStack(spacing: 6) {
            self
            if let reason {
                Text(reason)
                    .kriaType(.caption)
                    .foregroundStyle(KriaColor.mutedInk)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }
        }
    }
}
