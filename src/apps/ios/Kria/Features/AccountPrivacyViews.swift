import SwiftUI

enum KriaLegal {
    static let privacyURL = URL(string: "https://www.usekria.com/privacy")!
    static let termsURL = URL(string: "https://www.usekria.com/terms")!
    static let supportURL = URL(string: "mailto:usekria@gmail.com")!
}

struct KriaLegalLinks: View {
    /// `true` renders the same three links as a centered row (the signup
    /// screen's compact footer); `false` (default) keeps the stacked column
    /// used on the consent and account screens.
    var inline: Bool = false
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    /// At accessibility text sizes, three side-by-side links in a fixed
    /// 20pt-gap row don't have room to lay out on one line each — their text
    /// wraps to several lines and the row overflows off screen. Fall back to
    /// the stacked layout there instead (same convention as
    /// `GalleryView`'s `dynamicTypeSize.isAccessibilitySize` grid-column drop).
    private var usesInlineLayout: Bool { inline && !dynamicTypeSize.isAccessibilitySize }

    var body: some View {
        Group {
            if usesInlineLayout {
                HStack(spacing: 20) {
                    links
                }
                .frame(maxWidth: .infinity, alignment: .center)
                .font(KriaFont.body(12))
            } else {
                VStack(alignment: inline ? .center : .leading, spacing: inline ? 8 : 4) {
                    links
                }
                .frame(maxWidth: inline ? .infinity : nil, alignment: inline ? .center : .leading)
                .font(KriaFont.body(inline ? 12 : 15))
            }
        }
        .buttonStyle(LegalLinkStyle(inline: inline))
    }

    @ViewBuilder private var links: some View {
        Link("Privacy Policy", destination: KriaLegal.privacyURL)
            .accessibilityIdentifier("kria-privacy-link")
        Link("Terms of Service", destination: KriaLegal.termsURL)
            .accessibilityIdentifier("kria-terms-link")
        Link("Contact support", destination: KriaLegal.supportURL)
            .accessibilityIdentifier("kria-support-link")
    }
}

private struct LegalLinkStyle: ButtonStyle {
    var inline: Bool = false
    func makeBody(configuration: Configuration) -> some View {
        Group {
            if inline {
                configuration.label.underline().foregroundStyle(KriaColor.zinc)
            } else {
                configuration.label.underline()
            }
        }
        .frame(minHeight: inline ? 36 : 44, alignment: inline ? .center : .leading)
        .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

struct AIConsentView: View {
    let accept: () -> Void
    let decline: () -> Void
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var consent = false
    @State private var showsAccount = false
    @State private var appeared = false

    private var effectiveReduceMotion: Bool {
        reduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    KriaWordmark()
                        .signInEntrance(.wordmark, appeared: appeared, reduceMotion: effectiveReduceMotion)
                    // Same copy as before ("Choose how you create."), left
                    // unbroken by an explicit "\n" so its rendered
                    // accessibility label keeps matching
                    // `AccountPrivacyUITests` exactly; it still wraps to two
                    // lines at this size on a phone-width screen.
                    Text("Choose how you create.")
                        .font(KriaFont.headline(32))
                        .tracking(-0.64)
                        .lineSpacing(4)
                        .foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityAddTraits(.isHeader)
                        .signInEntrance(.headline, appeared: appeared, reduceMotion: effectiveReduceMotion)
                    VStack(alignment: .leading, spacing: 22) {
                        Text("Kria uses AI to plan edits and create captions from your instructions and footage.")
                            .font(KriaFont.body(15))
                            .lineSpacing(4)
                            .foregroundStyle(KriaColor.ink)
                        Text("Your messages and selected media, including faces, voices, and transcripts, are sent to Google Gemini and OpenAI for analysis, transcription, and editing. Kria stores your projects and finished videos in your account.")
                            .font(KriaFont.body(15))
                            .lineSpacing(4)
                            .foregroundStyle(KriaColor.ink)
                        Text("Kria only uploads what you choose. Depending on how a project renders, that is either full-quality originals for cloud editing or smaller copies for analysis. Nothing is uploaded from your camera roll until you choose it.")
                            .font(KriaFont.body(15))
                            .lineSpacing(4)
                            .foregroundStyle(KriaColor.ink)
                        Text("If you don’t agree, you can sign out or manage and delete your account. AI editing won’t start.")
                            .font(KriaFont.body(14))
                            .foregroundStyle(KriaColor.mutedInk)
                        Link("Read the Privacy Policy", destination: KriaLegal.privacyURL)
                            .buttonStyle(LegalLinkStyle())
                    }
                    .signInEntrance(.promise, appeared: appeared, reduceMotion: effectiveReduceMotion)
                    Toggle("I agree to share my messages and selected media with Google Gemini and OpenAI for AI editing", isOn: $consent)
                        .tint(KriaColor.sky)
                        .padding(.vertical, 14)
                        .padding(.horizontal, 16)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                        .accessibilityIdentifier("ai-consent-toggle")
                        .signInEntrance(.providers, appeared: appeared, reduceMotion: effectiveReduceMotion)
                    VStack(alignment: .leading, spacing: 12) {
                        Button("Agree and continue", action: accept)
                            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 52))
                            .disabled(!consent)
                            .accessibilityIdentifier("ai-consent-continue")
                        Button("Not now — sign out", action: decline)
                            .buttonStyle(KriaSecondaryButtonStyle(minHeight: 52))
                            .accessibilityIdentifier("ai-consent-decline")
                        Button("Manage account") { showsAccount = true }
                            .font(KriaFont.body(13).weight(.medium))
                            .foregroundStyle(KriaColor.zinc)
                            .frame(minHeight: 44)
                    }
                    .signInEntrance(.footer, appeared: appeared, reduceMotion: effectiveReduceMotion)
                }
                .font(KriaFont.body(16))
                .padding(24)
                .frame(maxWidth: 560, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .background(KriaColor.paper)
            .sheet(isPresented: $showsAccount) { NavigationStack { AccountView() } }
        }
        .onAppear { appeared = true }
    }
}
