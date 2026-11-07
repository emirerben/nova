import SwiftUI

// KRI-1273: the look of an AI suggestion chip, so a suggestion reads differently from a gray answer.
//
// - Glass fill (`KriaGlassBackground`: Reduce Transparency -> solid, iOS 26 -> Liquid Glass, else material) with a
//   faint cool tint, a soft turning multi-hue light on its edge, and a faint sheen crossing the glass.
// - A leaf background only: it never carries an accessibility identifier and never changes the chip's size, so the
//   button's identifier, 44pt target and hit shape stay on the chip itself.
// - The edge is a light, not a border: a blurred inner glow plus a thin highlight whose colour fades to clear
//   between hues. Reduce Motion or Reduce Transparency freezes it.

/// One wall clock for every AI chip: each chip derives its rotation from the same absolute time, so chips that
/// appear at different moments still shine in lockstep. No timers and no per-chip phase.
enum KriaAIClock {
    static let period: Double = 8

    static var angle: Double {
        let now = Date().timeIntervalSinceReferenceDate
        return now.truncatingRemainder(dividingBy: period) / period * 360
    }
}

private enum KriaAIColor {
    static let lime = Color(red: 0.55, green: 0.74, blue: 0.20)
    static let sky = Color(red: 0.55, green: 0.74, blue: 0.98)
    static let violet = Color(red: 0.71, green: 0.62, blue: 0.96)

    /// A cyclic sweep: bright arcs separated by near-clear gaps, so the edge reads as moving light, not an outline.
    static func ring(strength: Double) -> AngularGradient {
        AngularGradient(
            colors: [
                lime.opacity(0.9 * strength), lime.opacity(0.12 * strength), sky.opacity(0.85 * strength),
                violet.opacity(0.95 * strength), violet.opacity(0.12 * strength), lime.opacity(0.9 * strength),
            ],
            center: .center
        )
    }

    /// One soft white wedge: as it turns it reads as a faint sheen crossing the glass.
    static let sheen = AngularGradient(
        stops: [
            .init(color: .white.opacity(0), location: 0),
            .init(color: .white.opacity(0), location: 0.82),
            .init(color: .white.opacity(0.5), location: 0.91),
            .init(color: .white.opacity(0), location: 1),
        ],
        center: .center
    )
}

/// The background leaf of an AI suggestion chip: white glass, plus a slow turning multi-hue light.
struct KriaAIChipBackground: View {
    var isLead = false
    var pressed = false
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var spin = KriaAIClock.angle

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 22, style: .continuous) }
    private var solid: Bool { KriaTransparency.isReduced(reduceTransparency) }
    private var still: Bool { solid || KriaMotion.isReduced(systemReduceMotion) }
    private var strength: Double { (isLead ? 1.2 : 1) * (pressed ? 0.7 : 1) }

    var body: some View {
        ZStack {
            fill
            edgeLight
        }
        .onAppear { startSpin() }
        .accessibilityHidden(true)
        .allowsHitTesting(false)
    }

    @ViewBuilder private var fill: some View {
        KriaGlassBackground(shape: shape, whiteness: solid ? 1 : 0.8, shadowOpacity: 0.07, shadowRadius: 8, shadowY: 3)
            .overlay(shape.fill(LinearGradient(colors: [.clear, KriaAIColor.sky.opacity(pressed ? 0.12 : 0.06)],
                                               startPoint: .topLeading, endPoint: .bottomTrailing)))
    }

    private func turning<Content: View>(_ content: Content) -> some View {
        GeometryReader { geo in
            let side = hypot(geo.size.width, geo.size.height)
            content
                .frame(width: side, height: side)
                .rotationEffect(.degrees(still ? 40 : spin))
                .frame(width: geo.size.width, height: geo.size.height)
        }
    }

    private var edgeLight: some View {
        ZStack {
            turning(Rectangle().fill(KriaAIColor.ring(strength: 0.22 * strength)))
                .mask(shape.strokeBorder(lineWidth: 6).blur(radius: 3.5))
            turning(Rectangle().fill(KriaAIColor.ring(strength: 0.34 * strength)))
                .mask(shape.strokeBorder(lineWidth: 1))
            if !still {
                turning(Rectangle().fill(KriaAIColor.sheen))
                    .opacity(0.22)
                    .clipShape(shape)
            }
        }
    }

    private func startSpin() {
        guard !still else { return }
        let start = KriaAIClock.angle
        spin = start
        withAnimation(.linear(duration: KriaAIClock.period).repeatForever(autoreverses: false)) { spin = start + 360 }
    }
}
