import SwiftUI

/// Entrance stagger + ambient hero loop for `SignInView` (KRI-161). Kept as
/// pure, testable timing/curve constants so `SignInMotionTests` can cover the
/// values without SwiftUI.
enum SignInMotion {
    enum Element: CaseIterable { case wordmark, hero, headline, promise, providers, footer }

    /// Delay before each element's entrance, in seconds. Monotone in declaration order.
    static func delay(for element: Element) -> Double {
        switch element {
        case .wordmark: return 0
        case .hero: return 0.06
        case .headline: return 0.12
        case .promise: return 0.17
        case .providers: return 0.23
        case .footer: return 0.28
        }
    }

    static func animation(reduceMotion: Bool) -> Animation? {
        reduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88)
    }

    static let entranceRise: CGFloat = 10
    /// Seconds per ambient hero drift cycle (a full ping-pong loop).
    static let ambientPeriod: Double = 4.0
    /// Vertical ping-pong drift for the hero frames, in points.
    static let ambientDrift: CGFloat = 2
    /// Tilt ping-pong for the left/right hero frames only, in degrees.
    static let ambientTilt: Double = 1
}

extension View {
    /// Opacity 0→1 and a small rise→0 offset, animated with the element's
    /// entrance delay when `appeared` flips true. When `reduceMotion` is on,
    /// the view renders fully visible immediately, with no animation and no
    /// hit-testing gap (opacity/offset only — never `.disabled`).
    func signInEntrance(_ element: SignInMotion.Element, appeared: Bool, reduceMotion: Bool) -> some View {
        let visible = appeared || reduceMotion
        return self
            .opacity(visible ? 1 : 0)
            .offset(y: visible ? 0 : SignInMotion.entranceRise)
            .animation(SignInMotion.animation(reduceMotion: reduceMotion)?.delay(SignInMotion.delay(for: element)), value: appeared)
    }
}
