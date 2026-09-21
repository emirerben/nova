import SwiftUI
import AuthenticationServices

enum KriaLegal {
    static let privacyURL = URL(string: "https://www.usekria.com/privacy")!
    static let termsURL = URL(string: "https://www.usekria.com/terms")!
    static let supportURL = URL(string: "mailto:usekria@gmail.com")!
}

struct KriaLegalLinks: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Link("Privacy Policy", destination: KriaLegal.privacyURL)
                .accessibilityIdentifier("kria-privacy-link")
            Link("Terms of Service", destination: KriaLegal.termsURL)
                .accessibilityIdentifier("kria-terms-link")
            Link("Contact support", destination: KriaLegal.supportURL)
                .accessibilityIdentifier("kria-support-link")
        }
        .font(KriaFont.body(15))
        .buttonStyle(LegalLinkStyle())
    }
}

private struct LegalLinkStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .underline()
            .frame(minHeight: 44, alignment: .leading)
            .opacity(configuration.isPressed ? 0.65 : 1)
    }
}

struct AIConsentView: View {
    let accept: () -> Void
    let decline: () -> Void
    @EnvironmentObject private var model: AppModel
    @State private var consent = false
    @State private var showsAccount = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    KriaWordmark()
                    Text("Choose how you create.")
                        .font(KriaFont.display(32))
                        .accessibilityAddTraits(.isHeader)
                    Text("Kria uses AI to plan edits and create captions from your instructions and footage.")
                    Text("Your messages and selected media, including faces, voices, and transcripts, are sent to Google Gemini and OpenAI for analysis, transcription, and editing. Kria stores your projects and finished videos in your account.")
                    Text("Kria only uploads what you choose. Depending on how a project renders, that is either full-quality originals for cloud editing or smaller copies for analysis. Nothing is uploaded from your camera roll until you choose it.")
                    Text("If you don’t agree, you can sign out or manage and delete your account. AI editing won’t start.")
                        .foregroundStyle(KriaColor.mutedInk)
                    Link("Read the Privacy Policy", destination: KriaLegal.privacyURL)
                        .buttonStyle(LegalLinkStyle())
                    Toggle("I agree to share my messages and selected media with Google Gemini and OpenAI for AI editing", isOn: $consent)
                        .accessibilityIdentifier("ai-consent-toggle")
                    Button("Agree and continue", action: accept)
                        .buttonStyle(KriaPrimaryButtonStyle())
                        .disabled(!consent)
                        .accessibilityIdentifier("ai-consent-continue")
                    Button("Not now — sign out", action: decline)
                        .buttonStyle(KriaSecondaryButtonStyle())
                        .accessibilityIdentifier("ai-consent-decline")
                    Button("Manage account") { showsAccount = true }
                        .frame(minHeight: 44)
                }
                .font(KriaFont.body(16))
                .padding(24)
                .frame(maxWidth: 560, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .background(KriaColor.paper)
            .sheet(isPresented: $showsAccount) { NavigationStack { AccountView() } }
        }
    }
}

struct AccountDeletionView: View {
    @StateObject private var deletion: AccountDeletionModel
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dismiss) private var dismiss
    @State private var token = ""
    @State private var appleNonce = UUID().uuidString
    @State private var confirmsDeletion = false

    init(api: any KriaAPIClient) { _deletion = StateObject(wrappedValue: AccountDeletionModel(api: api)) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Delete your account")
                    .font(KriaFont.display(28))
                    .accessibilityAddTraits(.isHeader)
                Text("This permanently deletes your Kria account, projects, and uploaded and finished media. Copies you saved to Photos stay in Photos. This cannot be undone.")
                if deletion.codeRequested {
                    Text("Check the email address you use with Kria. Paste the confirmation code from the email below. It expires after one hour.")
                    TextField("Confirmation code", text: $token, axis: .vertical)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.oneTimeCode)
                        .padding(12)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                        .accessibilityIdentifier("account-deletion-code")
                    if deletion.appleAuthorizationCount > 0 {
                        Text("Authorize with the same Apple account you linked to Kria so we can revoke Kria’s access when your account is deleted.")
                        if deletion.appleAuthorizationCount > 1 {
                            Text("Apple accounts authorized: \(deletion.authorizedAppleAccounts) of \(deletion.appleAuthorizationCount). Authorize each linked account.")
                        }
                        if deletion.hasRequiredAppleAuthorization {
                            Label("Apple authorization ready", systemImage: "checkmark.circle")
                        } else {
                            SignInWithAppleButton(.continue, onRequest: { request in
                                appleNonce = UUID().uuidString
                                request.nonce = appleNonce.sha256Hex
                                request.requestedScopes = [.email]
                            }, onCompletion: authorizeApple)
                            .signInWithAppleButtonStyle(.black)
                            .frame(height: 52)
                            .clipShape(Capsule())
                            .accessibilityLabel("Authorize Apple account for deletion")
                        }
                    }
                    Button("Delete account permanently", role: .destructive) { confirmsDeletion = true }
                        .buttonStyle(KriaPrimaryButtonStyle(fill: KriaColor.failureSoft))
                        .disabled(deletion.isWorking || token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !deletion.hasRequiredAppleAuthorization)
                        .accessibilityIdentifier("account-deletion-confirm")
                    Button("Send a new code") { Task { await deletion.requestCode() } }
                        .frame(minHeight: 44)
                        .disabled(deletion.isWorking)
                } else {
                    Button("Email confirmation code") { Task { await deletion.requestCode() } }
                        .buttonStyle(KriaSecondaryButtonStyle())
                        .disabled(deletion.isWorking)
                        .accessibilityIdentifier("account-deletion-request")
                }
                if deletion.isWorking { ProgressView("Please wait…") }
                if let error = deletion.errorMessage {
                    Text(error).foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("account-deletion-error")
                }
                KriaLegalLinks()
            }
            .font(KriaFont.body(16))
            .padding(24)
            .frame(maxWidth: 560, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(KriaColor.paper)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") { dismiss() }.disabled(deletion.isWorking)
            }
        }
        .interactiveDismissDisabled(deletion.isWorking)
        .alert("Permanently delete your account?", isPresented: $confirmsDeletion) {
            Button("Cancel", role: .cancel) {}
            Button("Delete account", role: .destructive) {
                Task {
                    if await deletion.confirm(token: token) { auth.signOut() }
                }
            }
        } message: {
            Text("Your account and projects will be erased. You cannot recover them.")
        }
    }

    private func authorizeApple(_ result: Result<ASAuthorization, any Error>) {
        do {
            let result = try result.get()
            guard let credential = result.credential as? ASAuthorizationAppleIDCredential,
                  let code = credential.authorizationCode.flatMap({ String(data: $0, encoding: .utf8) }), !code.isEmpty else {
                throw AuthError.invalidCredential
            }
            let verified = try AppleAuthProvider().credential(from: result, nonce: appleNonce)
            deletion.authorizeAppleAccount(subject: credential.user, authorization: .init(
                idToken: verified.token, nonce: appleNonce, authorizationCode: code
            ))
        } catch {
            deletion.reportAuthorizationError("Apple authorization wasn’t completed. Your account has not been deleted. Try again when you’re ready.")
        }
    }
}
