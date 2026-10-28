import SwiftUI
import UIKit
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
        heroReady && UIAccessibility.isVideoAutoplayEnabled && !effectiveReduceMotion && scenePhase == .active && !showsReviewerSignIn
    }

    var body: some View {
        GeometryReader { viewport in
            ScrollViewReader { scroller in
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
                            // KRI-609: a 13pt grey line under the buttons went unseen
                            // ("nothing shows up"). Ink body text on a notice card; per
                            // DESIGN.md, transient refusals stay neutral, never red.
                            Text(message)
                                .font(KriaFont.body(15))
                                .lineSpacing(3)
                                .foregroundStyle(KriaColor.ink)
                                .multilineTextAlignment(.center)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .center)
                                .padding(.horizontal, 16)
                                .padding(.vertical, 12)
                                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(KriaColor.line, lineWidth: 1))
                                .padding(.top, 12)
                                .id(Self.messageAnchor)
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
                .onChange(of: message, initial: true) { previous, current in
                    guard let current else { return }
                    // On a short screen the message sits below the fold; bring it up
                    // and read it out, so a failed sign-in never looks like nothing happened.
                    // Next run loop: the message row is inserted by this same update.
                    let animation: Animation? = effectiveReduceMotion ? nil : .easeOut(duration: 0.25)
                    DispatchQueue.main.async {
                        withAnimation(animation) { scroller.scrollTo(Self.messageAnchor, anchor: .bottom) }
                    }
                    if previous != current { UIAccessibility.post(notification: .announcement, argument: current) }
                }
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

            #if DEV_AUTH
            Button("Sign in with email") { showsReviewerSignIn = true }
                .font(KriaFont.body(13).weight(.medium))
                .foregroundStyle(KriaColor.zinc)
                .frame(maxWidth: .infinity, minHeight: 44)
                .accessibilityIdentifier("signin.email")

            #endif

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

    private static let messageAnchor = "signin.message.anchor"

    private func handleAppleRequest(_ request: ASAuthorizationAppleIDRequest) {
        // Clear the last failure, so a repeat of it reappears and is announced again.
        message = nil
        appleNonce = UUID().uuidString
        request.requestedScopes = [.fullName, .email]
        request.nonce = appleNonce.sha256Hex
        busyProvider = .apple
    }

    private func signInWithGoogle() async {
        guard busyProvider == nil else { return }
        busyProvider = .google
        message = nil
        defer { busyProvider = nil }
        do {
            let credential = try await GoogleAuthProvider().signIn()
            let session = try await model.api.exchangeMobileToken(credential, provider: "google")
            try auth.signIn(with: session, displayName: credential.displayName)
        } catch { message = SignInFailureCopy.message(for: error) }
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
                } catch { message = SignInFailureCopy.message(for: error) }
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

/// What the sign-in screen says when an attempt fails (KRI-609).
enum SignInFailureCopy {
    /// Shown when the server refuses an email that already opens another Kria
    /// account but sends no usable wording. Accounts are never merged, so the
    /// creator just needs to use the sign-in they used before.
    static let existingAccount = "This email already has a Kria account. Sign in with the method you used before."
    /// The server's wording before KRI-609, too technical to show a creator.
    private static let legacyProviderLinkMessage = "Sign in with the existing provider or explicitly link this provider first"

    static func message(for error: Error) -> String {
        guard let api = error as? APIError, api == .conflict else { return error.localizedDescription }
        if api.conflictCode == "provider_link_required" {
            // The server names the provider that opens the account ("Sign in with Google to open it.").
            guard let server = api.conflictDetail, server != legacyProviderLinkMessage else { return existingAccount }
            return server
        }
        // `APIError.conflict`'s own copy is about editor saves ("This edit changed
        // elsewhere…") and means nothing on this screen.
        return AuthError.tokenExchangeFailed.localizedDescription
    }
}

private struct SignInHero: View {
    let isAnimating: Bool
    @State private var activeIndex = 0
    @State private var playing = false

    var body: some View {
        HStack(spacing: 28) {
            preview(.bosphorus)
                .frame(width: 151, height: 151 / FeaturedEdit.bosphorus.aspectRatio)
            VStack(spacing: 8) {
                ForEach(Array(FeaturedEdit.onboarding.dropFirst())) { edit in
                    preview(edit).frame(width: 146, height: 146 / edit.aspectRatio)
                }
            }
        }
        .frame(width: 342, height: 270)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Example edits")
        .accessibilityValue("\(playing ? "Playing" : "Paused") edit \(activeIndex + 1) of 4")
        .accessibilityIdentifier("signin.featured-edits")
    }

    private func preview(_ edit: FeaturedEdit) -> some View {
        FeaturedEditPreview(edit: edit, active: FeaturedEdit.onboarding[activeIndex] == edit,
                            allowed: isAnimating, completed: {
            guard FeaturedEdit.onboarding[activeIndex] == edit else { return }
            activeIndex = (activeIndex + 1) % FeaturedEdit.onboarding.count
        }, playingChanged: { value in
            if FeaturedEdit.onboarding[activeIndex] == edit { playing = value }
        })
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 14, x: 0, y: 6)
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
