import SwiftUI
import AuthenticationServices

/// The signup screen (KRI-161). Paper source of truth:
/// https://app.paper.design/file/01M34NT2NRNCP6R49KGW19EY4M
struct SignInView: View {
    @EnvironmentObject private var auth: AuthStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var message: String?
    @State private var appleNonce = UUID().uuidString
    // Visible in every build configuration, Release included — the server,
    // not the client, decides whether `auth/mobile/reviewer-login` is
    // available (404 when the feature is disabled). Exists so Apple's Beta
    // App Review team can sign in with a demo username/password instead of
    // Sign in with Apple/Google. See KRI-111.
    @State private var showsReviewerSignIn = false
    @State private var appeared = false
    @State private var heroReady = false
    @State private var busyProvider: Provider?

    enum Provider { case apple, google, local }

    init(initialMessage: String? = nil) {
        _message = State(initialValue: initialMessage)
    }

    /// Honours the same UI-test override other motion-bearing views use
    /// (see `NativeTextAnimationPreview.allows(reduceMotion:environment:)`).
    private var effectiveReduceMotion: Bool {
        reduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    private var heroAnimating: Bool {
        heroReady && !effectiveReduceMotion && scenePhase == .active && !showsReviewerSignIn
    }

    var body: some View {
        GeometryReader { viewport in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    KriaWordmark()
                        .signInEntrance(.wordmark, appeared: appeared, reduceMotion: effectiveReduceMotion)

                    // Decorative only: at accessibility text sizes the 244pt
                    // hero would push every action a full screen down, so it
                    // yields its space to the copy and the providers.
                    if !dynamicTypeSize.isAccessibilitySize {
                        SignInHero(isAnimating: heroAnimating)
                            .frame(maxWidth: .infinity)
                            .padding(.top, 18)
                            .signInEntrance(.hero, appeared: appeared, reduceMotion: effectiveReduceMotion)
                    }

                    Text("Prompt to final edit\nin minutes")
                        .font(KriaFont.headline(38))
                        .tracking(-0.76)
                        .lineSpacing(2)
                        .foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                        .padding(.top, 22)
                        .signInEntrance(.headline, appeared: appeared, reduceMotion: effectiveReduceMotion)

                    Text("Tell Kria what you want. It builds the edit for you, no manual cutting.")
                        .font(KriaFont.body(16))
                        .lineSpacing(6)
                        .foregroundStyle(KriaColor.zinc)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 12)
                        .signInEntrance(.promise, appeared: appeared, reduceMotion: effectiveReduceMotion)

                    Spacer(minLength: 24)

                    providerStack
                        .signInEntrance(.providers, appeared: appeared, reduceMotion: effectiveReduceMotion)

                    if let message {
                        Text(message)
                            .font(KriaFont.body(13))
                            .foregroundStyle(KriaColor.zinc)
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.top, 12)
                            .accessibilityIdentifier("signin.message")
                            .transition(.opacity)
                    }

                    footer
                        .padding(.top, 4)
                        .signInEntrance(.footer, appeared: appeared, reduceMotion: effectiveReduceMotion)
                }
                .padding(.horizontal, 24)
                .padding(.top, 20)
                .frame(minHeight: max(0, viewport.size.height - 20), alignment: .leading)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .sheet(isPresented: $showsReviewerSignIn) { ReviewerSignInView() }
        .onAppear { appeared = true }
        .task { await readyHeroLoop() }
    }

    /// The hero's ambient loop starts only once its own entrance animation
    /// has settled, so it never fights the incoming rise/fade.
    private func readyHeroLoop() async {
        guard !effectiveReduceMotion else { heroReady = true; return }
        let settleSeconds = SignInMotion.delay(for: .hero) + 0.34
        try? await Task.sleep(nanoseconds: UInt64(settleSeconds * 1_000_000_000))
        if !Task.isCancelled { heroReady = true }
    }

    private var providerStack: some View {
        VStack(spacing: 10) {
            #if !LIVE_GOOGLE_ONLY
            SignInWithAppleButton(.signIn, onRequest: handleAppleRequest, onCompletion: handleApple)
                .signInWithAppleButtonStyle(.black)
                .frame(height: 52)
                .clipShape(Capsule())
                .accessibilityLabel("Sign in with Apple")
                .accessibilityIdentifier("signin.apple")
                .disabled(busyProvider != nil)
                .opacity(busyProvider != nil ? 0.45 : 1)
            #endif

            Button {
                Task { await signInWithGoogle() }
            } label: {
                HStack(spacing: 10) {
                    if busyProvider == .google {
                        ProgressView().controlSize(.small)
                    } else {
                        GoogleGlyph()
                    }
                    Text("Continue with Google")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(KriaSecondaryButtonStyle(minHeight: 52))
            .disabled(busyProvider != nil)
            .accessibilityLabel("Continue with Google")
            .accessibilityIdentifier("signin.google")

            Button("Sign in with email") { showsReviewerSignIn = true }
                .font(KriaFont.body(13).weight(.medium))
                .foregroundStyle(KriaColor.zinc)
                .frame(maxWidth: .infinity, minHeight: 44)
                .accessibilityIdentifier("signin.email")

            if AppConfiguration.current.allowsDevelopmentAuth {
                Button {
                    signInLocally()
                } label: {
                    HStack(spacing: 10) {
                        if busyProvider == .local { ProgressView().controlSize(.small) }
                        Text("Continue with local account")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(busyProvider != nil)
                .accessibilityIdentifier("signin.local")
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 8) {
            Text("By continuing you agree to Kria’s Terms and Privacy Policy.")
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity, alignment: .center)
            KriaLegalLinks(inline: true)
        }
        .frame(maxWidth: .infinity, alignment: .center)
    }

    private func handleAppleRequest(_ request: ASAuthorizationAppleIDRequest) {
        appleNonce = UUID().uuidString
        request.requestedScopes = [.fullName, .email]
        request.nonce = appleNonce.sha256Hex
        busyProvider = .apple
    }

    private func signInWithGoogle() async {
        guard busyProvider == nil else { return }
        busyProvider = .google
        defer { busyProvider = nil }
        do {
            let credential = try await GoogleAuthProvider().signIn()
            let session = try await model.api.exchangeMobileToken(credential, provider: "google")
            try auth.signIn(with: session, displayName: credential.displayName)
        } catch { message = error.localizedDescription }
    }

    private func handleApple(_ result: Result<ASAuthorization, any Error>) {
        switch result {
        case .success(let authorization):
            Task { @MainActor in
                defer { busyProvider = nil }
                do {
                    let credential = try AppleAuthProvider().credential(from: authorization, nonce: appleNonce)
                    let session = try await model.api.exchangeMobileToken(credential, provider: "apple")
                    try auth.signIn(with: session, displayName: credential.displayName)
                } catch { message = error.localizedDescription }
            }
        case .failure:
            message = AuthError.cancelled.localizedDescription
            busyProvider = nil
        }
    }

    private func signInLocally() {
        guard busyProvider == nil else { return }
        busyProvider = .local
        defer { busyProvider = nil }
        do { try auth.signIn(with: MobileSession(accessToken: "local-access", refreshToken: "local-refresh", expiresIn: 3600), displayName: "Local creator") }
        catch { message = error.localizedDescription }
    }
}

/// Three overlapping portrait frames, not interactive and hidden from
/// accessibility — purely decorative. Drifts gently forever once its own
/// entrance has settled; sits at rest under Reduce Motion, background, or
/// while the reviewer sign-in sheet is up.
private struct SignInHero: View {
    let isAnimating: Bool

    private enum Phase: CaseIterable { case rest, drift }

    var body: some View {
        Group {
            if isAnimating {
                PhaseAnimator(Phase.allCases) { phase in
                    frames(drift: phase == .drift)
                } animation: { _ in
                    .easeInOut(duration: SignInMotion.ambientPeriod / 2)
                }
            } else {
                frames(drift: false)
            }
        }
        .frame(width: 342, height: 244)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private func frames(drift: Bool) -> some View {
        let dy: CGFloat = drift ? SignInMotion.ambientDrift : -SignInMotion.ambientDrift
        let tilt: Double = drift ? SignInMotion.ambientTilt : -SignInMotion.ambientTilt
        ZStack {
            heroFrame("voiceover")
                .shadow(color: .black.opacity(0.14), radius: 22, x: 0, y: 10)
                .rotationEffect(.degrees(-11 + tilt), anchor: .bottom)
                .position(x: 58 + 59, y: 44 + 79 + dy)
            heroFrame("broll")
                .shadow(color: .black.opacity(0.14), radius: 22, x: 0, y: 10)
                .rotationEffect(.degrees(11 + tilt), anchor: .bottom)
                .position(x: 166 + 59, y: 44 + 79 - dy)
            heroFrame("montage")
                .shadow(color: .black.opacity(0.18), radius: 30, x: 0, y: 14)
                .position(x: 112 + 59, y: 24 + 79 - dy)
        }
    }

    private func heroFrame(_ name: String) -> some View {
        BundledPosterImage(name: name)
            .scaledToFill()
            .frame(width: 118, height: 158)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// A small four-colour Google "G", drawn locally so the signup screen adds no
/// new asset or dependency. `Circle().trim` runs clockwise from 3 o'clock, so
/// the segments below follow the real mark: blue on the right (with the bar),
/// green across the bottom, yellow on the left, red over the top, and the
/// opening at the top-right between red and the bar.
private struct GoogleGlyph: View {
    private static let size: CGFloat = 18
    private static let lineWidth: CGFloat = 4
    private static let blue = Color(red: 0.259, green: 0.522, blue: 0.957)   // #4285F4
    private static let green = Color(red: 0.204, green: 0.659, blue: 0.325)  // #34A853
    private static let yellow = Color(red: 0.984, green: 0.737, blue: 0.020) // #FBBC05
    private static let red = Color(red: 0.918, green: 0.263, blue: 0.208)    // #EA4335

    var body: some View {
        ZStack {
            segment(from: 0, to: 45, color: Self.blue)
            segment(from: 45, to: 135, color: Self.green)
            segment(from: 135, to: 225, color: Self.yellow)
            segment(from: 225, to: 315, color: Self.red)
            Rectangle()
                .fill(Self.blue)
                .frame(width: Self.size / 2 - Self.lineWidth / 2, height: Self.lineWidth)
                .offset(x: Self.size / 4 - Self.lineWidth / 4)
        }
        .frame(width: Self.size, height: Self.size)
        .accessibilityHidden(true)
    }

    private func segment(from start: Double, to end: Double, color: Color) -> some View {
        Circle()
            .trim(from: start / 360, to: end / 360)
            .stroke(color, style: StrokeStyle(lineWidth: Self.lineWidth, lineCap: .butt))
            .padding(Self.lineWidth / 2)
    }
}
