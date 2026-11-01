import Foundation
import Combine
import UIKit
import UserNotifications

struct NativeNotificationReceipt: Decodable, Sendable {
    let id: UUID
    let enabled: Bool
    let registrationVersion: Int
    enum CodingKeys: String, CodingKey {
        case id, enabled
        case registrationVersion = "registration_version"
    }
}

@MainActor protocol NativeNotificationRegistrationClient {
    func capabilities(owner: String) async throws -> Bool
    func put(id: UUID, token: String, owner: String) async throws -> NativeNotificationReceipt
    func revoke(id: UUID, owner: String) async throws
}

struct KriaNotificationRegistrationClient: NativeNotificationRegistrationClient {
    let api: KriaAPI
    private struct Capabilities: Decodable { let available: Bool }
    private struct Registration: Encodable { let token: String; let enabled: Bool }

    func capabilities(owner: String) async throws -> Bool {
        try await api.request(path: "me/notification-installations/capabilities", method: "GET", bodyData: nil, decode: Capabilities.self, expectedOwner: owner).available
    }
    func put(id: UUID, token: String, owner: String) async throws -> NativeNotificationReceipt {
        try await api.request(path: "me/notification-installations/\(id.uuidString)", method: "PUT", bodyData: JSONEncoder().encode(Registration(token: token, enabled: true)), decode: NativeNotificationReceipt.self, expectedOwner: owner)
    }
    func revoke(id: UUID, owner: String) async throws {
        let _: EmptyProjectResponse = try await api.request(path: "me/notification-installations/\(id.uuidString)", method: "DELETE", bodyData: nil, decode: EmptyProjectResponse.self, expectedOwner: owner)
    }
}

@MainActor protocol NativeNotificationPermissionClient {
    func requestPermission() async throws -> Bool
    func hasPermission() async -> Bool
    func register()
    func unregister()
}

struct SystemNotificationPermissionClient: NativeNotificationPermissionClient {
    func requestPermission() async throws -> Bool {
        try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
    }
    func hasPermission() async -> Bool {
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        return status == .authorized || status == .provisional || status == .ephemeral
    }
    func register() { UIApplication.shared.registerForRemoteNotifications() }
    func unregister() { UIApplication.shared.unregisterForRemoteNotifications() }
}

/// What the Account screen offers for notifications. A failed server cleanup gets its own retry
/// because `enable()` refuses to run until the old registration is revoked.
enum NativeNotificationControl: Equatable, Sendable {
    case hidden, off, on, retryCleanup
}

@MainActor final class NativeNotificationCoordinator: ObservableObject {
    @Published private(set) var available = false
    @Published private(set) var optedIn = false
    @Published private(set) var message: String?
    @Published private(set) var registrationVersion: Int?
    @Published private(set) var isRequestingPermission = false
    @Published private(set) var needsRevocation = false
    var openActivity: ((UUID, String) async throws -> Bool)?
    private let permission: any NativeNotificationPermissionClient
    private let defaults: UserDefaults
    private var client: (any NativeNotificationRegistrationClient)?
    private var owner: String?
    private var previousOwner: String?
    private var epoch = UUID()
    private var requestedRegistration = false
    private var acknowledgedToken: String?
    private var pendingTap: UUID?
    private var tapTask: Task<Void, Never>?
    private var writeTask: Task<Void, Never>?

    init(permission: any NativeNotificationPermissionClient = SystemNotificationPermissionClient(), defaults: UserDefaults = .standard, client: (any NativeNotificationRegistrationClient)? = nil) {
        self.permission = permission
        self.defaults = defaults
        self.client = client
    }

    var control: NativeNotificationControl {
        guard available else { return .hidden }
        if needsRevocation { return .retryCleanup }
        return optedIn ? .on : .off
    }

    func configure(api: any KriaAPIClient) {
        guard client == nil, let api = api as? KriaAPI else { return }
        client = KriaNotificationRegistrationClient(api: api)
    }

    func bind(owner nextOwner: String?) async {
        if owner != nextOwner {
            if let owner { previousOwner = owner }
            if previousOwner != nil { pendingTap = nil }
            epoch = UUID()
            tapTask?.cancel()
            requestedRegistration = false
            acknowledgedToken = nil
            if owner != nil { permission.unregister() }
            owner = nextOwner
            available = false
            message = nil
            registrationVersion = nil
            optedIn = nextOwner.map { defaults.bool(forKey: key("opt-in", owner: $0)) } ?? false
            needsRevocation = nextOwner.map { defaults.bool(forKey: key("revoke", owner: $0)) } ?? false
            registrationVersion = nextOwner.flatMap { defaults.object(forKey: key("version", owner: $0)) as? Int }
        }
        guard let owner, let client else { return }
        if needsRevocation { await disable() }
        guard self.owner == owner else { return }
        let captured = epoch
        do {
            let available = try await client.capabilities(owner: owner)
            guard epoch == captured else { return }
            self.available = available
            if available, optedIn {
                let allowed = await permission.hasPermission()
                guard epoch == captured else { return }
                if allowed { requestedRegistration = true; permission.register() }
            }
        } catch {
            guard epoch == captured else { return }
            available = false
        }
        await openPendingTap()
    }

    func enable() async {
        guard let owner, let client, !isRequestingPermission, !needsRevocation else { return }
        isRequestingPermission = true
        defer { isRequestingPermission = false }
        let captured = epoch
        do {
            guard try await client.capabilities(owner: owner), epoch == captured else { return }
            available = true
            let granted = try await permission.requestPermission()
            guard epoch == captured else { return }
            guard granted else {
                message = String(localized: "Notifications are off. You can allow them in Settings.")
                return
            }
            defaults.set(true, forKey: key("opt-in", owner: owner))
            optedIn = true
            requestedRegistration = true
            message = nil
            permission.register()
        } catch {
            guard epoch == captured else { return }
            message = String(localized: "Notifications couldn't be enabled. Try again later.")
        }
    }

    func registered(token data: Data) async {
        guard let owner, let client, available, optedIn, requestedRegistration, !data.isEmpty else { return }
        let token = data.map { String(format: "%02x", $0) }.joined()
        let captured = epoch
        let id = installation(owner: owner)
        let previous = writeTask
        let task = Task { [weak self] in
            await previous?.value
            guard let self, self.epoch == captured, self.optedIn, self.acknowledgedToken != token else { return }
            do {
                let receipt = try await client.put(id: id, token: token, owner: owner)
                guard self.epoch == captured, self.optedIn else { return }
                guard receipt.id == id, receipt.enabled else { throw APIError.invalidResponse }
                self.acknowledgedToken = token
                self.registrationVersion = receipt.registrationVersion
                self.defaults.set(receipt.registrationVersion, forKey: self.key("version", owner: owner))
                self.message = nil
            } catch {
                guard self.epoch == captured else { return }
                self.message = String(localized: "Notifications couldn't connect to this account. Try again later.")
            }
        }
        writeTask = task
        await task.value
    }

    func registrationFailed() {
        guard requestedRegistration else { return }
        requestedRegistration = false
        message = String(localized: "Notifications couldn't connect. Try again later.")
    }

    func disable() async {
        guard let owner, let client else { return }
        defaults.set(false, forKey: key("opt-in", owner: owner))
        optedIn = false
        needsRevocation = true
        defaults.set(true, forKey: key("revoke", owner: owner))
        requestedRegistration = false
        acknowledgedToken = nil
        permission.unregister()
        epoch = UUID()
        let captured = epoch
        let id = installation(owner: owner)
        let previous = writeTask
        let task = Task { [weak self] in
            await previous?.value
            guard let self, self.epoch == captured else { return }
            do {
                try await client.revoke(id: id, owner: owner)
                guard self.epoch == captured else { return }
                self.registrationVersion = nil
                self.needsRevocation = false
                self.defaults.removeObject(forKey: self.key("version", owner: owner))
                self.defaults.set(false, forKey: self.key("revoke", owner: owner))
                self.message = nil
            } catch {
                guard self.epoch == captured else { return }
                self.message = String(localized: "Notifications are off on this phone. Server cleanup will need another try.")
            }
        }
        writeTask = task
        await task.value
    }

    func receiveTap(_ payload: [AnyHashable: Any]) async {
        guard let raw = payload["activity_id"] as? String, let id = UUID(uuidString: raw) else { return }
        guard owner != nil || previousOwner == nil else { return }
        pendingTap = id
        await openPendingTap()
    }

    private func openPendingTap() async {
        guard let owner, let id = pendingTap, let openActivity else { return }
        pendingTap = nil
        tapTask?.cancel()
        let captured = epoch
        let task = Task { [weak self] in
            do {
                let opened = try await openActivity(id, owner)
                guard let self, self.epoch == captured, !Task.isCancelled else { return }
                if !opened { self.message = String(localized: "This project is no longer available.") }
            } catch {
                guard let self, self.epoch == captured, !Task.isCancelled else { return }
                self.message = String(localized: "This project is no longer available.")
            }
        }
        tapTask = task
        await task.value
    }

    private func key(_ field: String, owner: String) -> String { "kria.notifications.\(owner).\(field)" }
    private func installation(owner: String) -> UUID {
        let key = key("installation", owner: owner)
        if let raw = defaults.string(forKey: key), let id = UUID(uuidString: raw) { return id }
        let id = UUID()
        defaults.set(id.uuidString, forKey: key)
        return id
    }
}
