import SwiftUI
import AuthenticationServices

/// Split out of `AccountPrivacyViews.swift` (KRI-161) so that file's
/// signup/consent typography guard (no `KriaFont.display`/Fraunces) can be
/// scoped to just the signup and consent screens without also constraining
/// this unrelated account-deletion flow, which keeps its existing Fraunces
/// display type.
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
