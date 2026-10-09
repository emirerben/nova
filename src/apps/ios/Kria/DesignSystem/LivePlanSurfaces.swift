import SwiftUI

// KRI-450: the "color behind the glass" pieces from the Paper -B artboards (LV1-B / LV2-B / LV3-B / L1-B / L3-B).
//
// Rules this file owns:
// - Real `.glassEffect` ONLY on leaf surfaces, in three branches checked in order: Reduce Transparency (solid
//   paper), iOS 26 with a Swift 6.2 toolchain (Liquid Glass), everything else (material). Same pattern as
//   `NativeEditorIslandSurface`.
// - Glass is always a BACKGROUND LEAF next to the content, never a wrapper around it, and never carries an
//   accessibility identifier. `.glassEffect()` collapses the accessibility/hit-test frame of an ancestor that
//   carries an identifier, so identifiers live on the plain content views beside the glass leaf.
// - Inter only, fills not strokes (the one 1pt white highlight is a light edge, not a border), no Fraunces.

enum KriaMotion {
    /// `UI_TEST_REDUCE_MOTION=1` forces the reduced branch in simulator runs (same idea as
    /// `KriaTransparency.isReduced`).
    static func isReduced(_ systemValue: Bool) -> Bool {
        systemValue || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }
}

// MARK: - Glass leaf

/// A white glass surface (white at ~62% over whatever colour sits behind it). Put it in `.background { }` of the
/// content it carries; never wrap content in it and never give it an identifier.
struct KriaGlassBackground<S: InsettableShape>: View {
    let shape: S
    /// How much white the glass carries. 0.62 is the card glass, 0.5 the bottom panel, 0.7 the close button.
    var whiteness: Double = 0.62
    var shadowOpacity: Double = 0.12
    var shadowRadius: CGFloat = 14
    var shadowY: CGFloat = 8
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    var body: some View {
        if KriaTransparency.isReduced(reduceTransparency) {
            // Solid paper, no blur: a soft shadow is the only definition.
            shape.fill(KriaColor.paper)
                .shadow(color: KriaColor.ink.opacity(shadowOpacity), radius: shadowRadius, y: shadowY)
                .shadow(color: KriaColor.ink.opacity(0.05), radius: 1.5, y: 1)
        } else {
            glass
        }
    }

    @ViewBuilder private var glass: some View {
        #if compiler(>=6.2)
        if #available(iOS 26.0, *) {
            Color.clear
                .glassEffect(.regular.tint(Color.white.opacity(whiteness * 0.6)), in: shape)
                .shadow(color: KriaColor.ink.opacity(shadowOpacity), radius: shadowRadius, y: shadowY)
                .shadow(color: KriaColor.ink.opacity(0.05), radius: 1.5, y: 1)
        } else {
            material
        }
        #else
        material
        #endif
    }

    private var material: some View {
        shape.fill(.ultraThinMaterial)
            .overlay(shape.fill(Color.white.opacity(whiteness * 0.8)))
            // The 1pt white top light from the Paper shadow stack.
            .overlay(shape.strokeBorder(LinearGradient(colors: [Color.white, Color.white.opacity(0.1)], startPoint: .top, endPoint: .bottom), lineWidth: 1))
            .shadow(color: KriaColor.ink.opacity(shadowOpacity), radius: shadowRadius, y: shadowY)
            .shadow(color: KriaColor.ink.opacity(0.05), radius: 1.5, y: 1)
    }
}

extension View {
    /// White glass card (collapsed row radius 22, expanded card radius 28).
    func kriaGlassCard(cornerRadius: CGFloat = 28) -> some View {
        background { KriaGlassBackground(shape: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)) }
    }

    /// The deciding / flagged card: a butter gradient (160deg) over glass with a warm glow.
    func kriaButterCard(cornerRadius: CGFloat = 28) -> some View {
        modifier(KriaButterCardModifier(cornerRadius: cornerRadius))
    }

    /// The glass bottom panel (radius 32).
    func kriaGlassPanel(cornerRadius: CGFloat = 32) -> some View {
        background {
            KriaGlassBackground(
                shape: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous),
                whiteness: 0.5, shadowOpacity: 0.14, shadowRadius: 22, shadowY: 12
            )
        }
    }
}

private struct KriaButterCardModifier: ViewModifier {
    let cornerRadius: CGFloat
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: cornerRadius, style: .continuous) }

    func body(content: Content) -> some View {
        content.background {
            if KriaTransparency.isReduced(reduceTransparency) {
                shape.fill(LinearGradient(colors: [KriaColor.butterWarm, KriaColor.butterPale], startPoint: .topLeading, endPoint: .bottomTrailing))
                    .shadow(color: KriaColor.butterGlow.opacity(0.22), radius: 16, y: 12)
            } else {
                ZStack {
                    KriaGlassBackground(shape: shape, whiteness: 0.3, shadowOpacity: 0, shadowRadius: 0, shadowY: 0)
                    shape.fill(LinearGradient(
                        colors: [KriaColor.butterWarm.opacity(0.70), KriaColor.butterPale.opacity(0.50)],
                        startPoint: .topLeading, endPoint: .bottomTrailing
                    ))
                }
                .shadow(color: KriaColor.butterGlow.opacity(0.32), radius: 18, y: 14)
                .shadow(color: KriaColor.ink.opacity(0.05), radius: 1.5, y: 1)
            }
        }
    }
}

// MARK: - Wash

/// The soft butter wash that sits behind the glass so it has colour to refract. Decorative; not hit-testable and
/// hidden from accessibility. Under Reduce Transparency it is just the near-white screen gradient.
struct KriaButterWash: View {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    var body: some View {
        ZStack {
            LinearGradient(colors: [KriaColor.screenTop, KriaColor.paper], startPoint: .top, endPoint: .bottom)
            if !KriaTransparency.isReduced(reduceTransparency) {
                GeometryReader { proxy in
                    Circle().fill(KriaColor.butter.opacity(0.32))
                        .frame(width: proxy.size.width * 0.9)
                        .blur(radius: 70)
                        .offset(x: -proxy.size.width * 0.25, y: proxy.size.height * 0.1)
                    Circle().fill(KriaColor.butterWarm.opacity(0.28))
                        .frame(width: proxy.size.width * 0.8)
                        .blur(radius: 80)
                        .offset(x: proxy.size.width * 0.35, y: proxy.size.height * 0.55)
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - Marks

struct KriaCheckShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        // M5 12 l5 5 9-10 in a 24-unit box.
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: rect.minX + rect.width * x / 24, y: rect.minY + rect.height * y / 24) }
        path.move(to: point(5, 12))
        path.addLine(to: point(10, 17))
        path.addLine(to: point(19, 7))
        return path
    }
}

/// The yellow check disc that marks a decided section (24pt in a row, 26pt in an expanded card).
/// `dash` reads "Not used" for a skipped section.
struct KriaCheckDisc: View {
    var size: CGFloat = 24
    var dash = false

    var body: some View {
        ZStack {
            Circle().fill(dash ? KriaColor.fill : KriaColor.butter)
            if dash {
                Capsule().fill(KriaColor.zinc).frame(width: size * 0.4, height: 2)
            } else {
                KriaCheckShape()
                    .stroke(KriaColor.ink, style: StrokeStyle(lineWidth: max(1.6, size * 0.075), lineCap: .round, lineJoin: .round))
                    .frame(width: size * 0.5, height: size * 0.5)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// A 3/4 arc that turns; still (no rotation) under Reduce Motion.
struct KriaSpinner: View {
    var size: CGFloat = 13
    var tint: Color = KriaColor.ink
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var turning = false

    var body: some View {
        ZStack {
            Circle().stroke(tint.opacity(0.15), lineWidth: 2.2)
            Circle().trim(from: 0, to: 0.72)
                .stroke(tint, style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
                .rotationEffect(.degrees(turning ? 360 : 0))
        }
        .frame(width: size, height: size)
        .onAppear {
            guard !KriaMotion.isReduced(systemReduceMotion) else { return }
            withAnimation(.linear(duration: 0.9).repeatForever(autoreverses: false)) { turning = true }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - Pills

/// The 32pt pill family of the live plan and review screens. A visual only: the call site makes it a button and
/// extends the hit area to 44pt.
struct KriaPlanPill: View {
    enum Style: Equatable {
        /// "Change" on a white glass card.
        case change
        /// "Change" on a butter card (white 70%).
        case changeOnButter
        /// Flagged for change: ink with a white check.
        case changing
        /// Butter "Updated".
        case updated
        /// White 75% with a spinner and a verb ("Finding a track").
        case working(String)
    }

    let title: String
    var style: Style = .change

    var body: some View {
        HStack(spacing: 6) {
            switch style {
            case .changing:
                KriaCheckShape()
                    .stroke(Color.white, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                    .frame(width: 11, height: 11)
            case .updated:
                Text("✦").font(KriaFont.body(12).weight(.semibold))
            case .working:
                KriaSpinner(size: 13)
            default:
                EmptyView()
            }
            Text(label)
                .font(KriaFont.body(13).weight(.semibold))
                .lineLimit(1)
        }
        .foregroundStyle(foreground)
        .padding(.horizontal, 13)
        .frame(height: 32)
        .background(background, in: Capsule())
    }

    private var label: String {
        if case .working(let verb) = style { return verb }
        return title
    }

    private var foreground: Color { style == .changing ? Color.white : KriaColor.ink }

    private var background: Color {
        switch style {
        case .change: KriaColor.fill
        case .changeOnButter: Color.white.opacity(0.70)
        case .changing: KriaColor.ink
        case .updated: KriaColor.butter
        case .working: Color.white.opacity(0.75)
        }
    }
}

// MARK: - Buttons

/// The ink + butter capsule CTA ("Review your video", "Update video"): 48pt, ink fill, #FFF0A6 label. Disabled is
/// ink at 10% with the label at 40%.
struct KriaInkCTAButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var height: CGFloat = 48

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(15).weight(.semibold))
            .foregroundStyle(isEnabled ? KriaColor.butter : KriaColor.ink.opacity(0.4))
            .frame(maxWidth: .infinity, minHeight: height)
            .background(isEnabled ? KriaColor.ink : KriaColor.ink.opacity(0.10), in: Capsule())
            .contentShape(Capsule())
            .opacity(configuration.isPressed ? 0.85 : 1)
    }
}

/// A 44pt glass circle with a close mark. The glass is a background leaf beside the button, so the identifier
/// sits on the button and never on or above the glass.
struct KriaGlassCloseButton: View {
    var identifier: String?
    var label = "Close"
    let action: () -> Void

    var body: some View {
        ZStack {
            KriaGlassBackground(shape: Circle(), whiteness: 0.7, shadowOpacity: 0.12, shadowRadius: 14, shadowY: 8)
            Button(action: action) {
                KriaCloseMark()
                    .stroke(KriaColor.ink, style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
                    .frame(width: 14, height: 14)
                    .frame(width: 44, height: 44)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(label)
            .accessibilityIdentifier(identifier ?? "kria-glass-close")
        }
        .frame(width: 44, height: 44)
    }
}

struct KriaCloseMark: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.minY)); path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.move(to: CGPoint(x: rect.maxX, y: rect.minY)); path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        return path
    }
}

// MARK: - Progress

/// 6pt track (ink at 8%) with a butter gradient fill.
struct KriaProgressTrack: View {
    let progress: Double

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .leading) {
                Capsule().fill(KriaColor.ink.opacity(0.08))
                Capsule()
                    .fill(LinearGradient(colors: [KriaColor.butter, KriaColor.butterDeep], startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(progress > 0 ? 8 : 0, proxy.size.width * min(1, max(0, progress))))
            }
        }
        .frame(height: 6)
    }
}

/// The skeleton bars of a card that is still being decided (ink at 14% and 10%).
struct KriaSkeletonBar: View {
    var width: CGFloat? = nil
    var height: CGFloat = 12
    var strong = true

    var body: some View {
        Capsule()
            .fill(KriaColor.ink.opacity(strong ? 0.14 : 0.10))
            .frame(width: width, height: height)
    }
}
