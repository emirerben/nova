import SwiftUI

/// Presented from `SignInView`'s "Sign in with email" affordance (`signin.email`).
/// Exists so Apple's Beta App Review team can sign in with a demo
/// username/password instead of Sign in with Apple/Google — App Review
/// flagged the absence of this path (KRI-111). The button that presents
/// this sheet stays visible in every build configuration, Release included;
/// the server decides availability and answers 404 when the feature is
/// disabled (`auth/mobile/reviewer-login`).
struct ReviewerSignInView: View {
    @EnvironmentObject private var auth: AuthStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @State private var password = ""
    @State private var isSubmitting = false
    @State private var errorMessage: String?

    private var canSubmit: Bool { !email.isEmpty && !password.isEmpty && !isSubmitting }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("Sign in with the email and password provided to you.")
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.zinc)
                TextField("Email", text: $email)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textContentType(.username)
                    .textFieldStyle(.roundedBorder)
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("signin.email.field")
                SecureField("Password", text: $password)
                    .textContentType(.password)
                    .textFieldStyle(.roundedBorder)
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("signin.email.password")
                if let errorMessage {
                    Text(errorMessage)
                        .font(KriaFont.body(13))
                        .foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("signin.email.error")
                }
                Button {
                    Task { await submit() }
                } label: {
                    if isSubmitting {
                        ProgressView().frame(maxWidth: .infinity)
                    } else {
                        Text("Sign in").frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(KriaPrimaryButtonStyle())
                .disabled(!canSubmit)
                .accessibilityIdentifier("signin.email.submit")
                Spacer()
            }
            .padding(24)
            .navigationTitle("Sign in with email")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .frame(minWidth: 44, minHeight: 44)
                        .accessibilityIdentifier("signin.email.cancel")
                }
            }
        }
    }

    private func submit() async {
        errorMessage = nil
        isSubmitting = true
        defer { isSubmitting = false }
        do {
            let session = try await model.api.reviewerSignIn(email: email, password: password)
            try auth.signIn(with: session, displayName: "Kria Reviewer")
            dismiss()
        } catch APIError.requestFailed(status: 401, detail: _) {
            errorMessage = "Invalid email or password."
        } catch APIError.requestFailed(status: 404, detail: _) {
            errorMessage = "Email sign-in isn't enabled for this app."
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
