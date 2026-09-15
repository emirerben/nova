import Foundation
import Combine

struct AccountDeletionRequest: Decodable, Sendable {
    let requested: Bool
    let appleAuthorizationCount: Int

    enum CodingKeys: String, CodingKey {
        case requested
        case appleAuthorizationRequired = "apple_authorization_required"
        case appleAuthorizationCount = "apple_authorization_count"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        requested = try values.decode(Bool.self, forKey: .requested)
        let required = try values.decodeIfPresent(Bool.self, forKey: .appleAuthorizationRequired) ?? false
        appleAuthorizationCount = max(required ? 1 : 0, try values.decodeIfPresent(Int.self, forKey: .appleAuthorizationCount) ?? 0)
    }
}

struct AppleDeletionAuthorization: Encodable, Sendable {
    let idToken: String
    let nonce: String
    let authorizationCode: String
    enum CodingKeys: String, CodingKey {
        case idToken = "id_token", nonce, authorizationCode = "authorization_code"
    }
}

struct AccountDeletionConfirmation: Encodable, Sendable {
    let token: String
    let appleAuthorizations: [AppleDeletionAuthorization]
    enum CodingKeys: String, CodingKey { case token, appleAuthorizations = "apple_authorizations" }
}

enum AccountDeletionError: Error, LocalizedError, Equatable {
    case appleAuthorizationRequired(Int)
    case invalidConfirmation
    case unavailable

    var errorDescription: String? {
        switch self {
        case .appleAuthorizationRequired:
            "Authorize with the Apple account linked to Kria, then try deleting again."
        case .invalidConfirmation:
            "The confirmation code or Apple authorization is invalid or expired. Request a new code and try again."
        case .unavailable:
            "Account deletion is temporarily unavailable. Your account has not been deleted. Please try again later or contact support."
        }
    }
}

/// Single-use Apple codes stay in memory only. Any failed confirmation drops
/// them because the server may already have exchanged a code with Apple.
@MainActor final class AccountDeletionModel: ObservableObject {
    @Published private(set) var codeRequested = false
    @Published private(set) var appleAuthorizationCount = 0
    @Published private(set) var authorizedAppleAccounts = 0
    @Published private(set) var isWorking = false
    @Published private(set) var errorMessage: String?
    private var authorizations: [String: AppleDeletionAuthorization] = [:]
    private let api: any KriaAPIClient

    init(api: any KriaAPIClient) { self.api = api }

    var hasRequiredAppleAuthorization: Bool { authorizedAppleAccounts >= appleAuthorizationCount }

    func requestCode() async {
        guard !isWorking else { return }
        isWorking = true
        errorMessage = nil
        defer { isWorking = false }
        do {
            let response = try await api.requestAccountDeletion()
            guard response.requested else { throw APIError.invalidResponse }
            appleAuthorizationCount = response.appleAuthorizationCount
            clearAuthorizations()
            codeRequested = true
        } catch { errorMessage = error.localizedDescription }
    }

    func authorizeAppleAccount(subject: String, authorization: AppleDeletionAuthorization) {
        guard !isWorking, !subject.isEmpty, !authorization.authorizationCode.isEmpty else { return }
        authorizations[subject] = authorization
        authorizedAppleAccounts = authorizations.count
        errorMessage = nil
    }

    func reportAuthorizationError(_ message: String) { errorMessage = message }

    func confirm(token: String) async -> Bool {
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !isWorking, codeRequested, !token.isEmpty, hasRequiredAppleAuthorization else { return false }
        isWorking = true
        errorMessage = nil
        defer { isWorking = false; clearAuthorizations() }
        do {
            try await api.confirmAccountDeletion(.init(token: token, appleAuthorizations: Array(authorizations.values)))
            return true
        } catch AccountDeletionError.appleAuthorizationRequired(let count) {
            appleAuthorizationCount = max(1, count)
            errorMessage = AccountDeletionError.appleAuthorizationRequired(count).localizedDescription
        } catch { errorMessage = error.localizedDescription }
        return false
    }

    private func clearAuthorizations() {
        authorizations.removeAll()
        authorizedAppleAccounts = 0
    }
}
