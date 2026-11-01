import XCTest
@testable import Kria

@MainActor final class NativeNotificationsTests: XCTestCase {
    private func runtime(_ api: NotificationAPIFake, _ permission: NotificationPermissionFake) -> NativeNotificationCoordinator {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        return NativeNotificationCoordinator(permission: permission, defaults: defaults, client: api)
    }

    func testUnavailableBackendNeverPromptsOrRegisters() async {
        let api = NotificationAPIFake(); api.available = false
        let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a")
        await runtime.enable()
        XCTAssertEqual(permission.prompts, 0)
        XCTAssertEqual(permission.registrations, 0)
        XCTAssertTrue(api.writes.isEmpty)
    }

    func testDeniedPermissionNeverRegisters() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        permission.granted = false
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a")
        await runtime.enable()
        XCTAssertEqual(permission.prompts, 1)
        XCTAssertEqual(permission.registrations, 0)
        XCTAssertFalse(runtime.optedIn)
    }

    func testTokenRotationAndDisableUseOneInstallationAndServerVersion() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a")
        await runtime.enable()
        await runtime.registered(token: Data([1, 2]))
        await runtime.registered(token: Data([1, 2]))
        await runtime.registered(token: Data([3, 4]))
        XCTAssertEqual(api.writes.map(\.token), ["0102", "0304"])
        XCTAssertEqual(Set(api.writes.map(\.id)).count, 1)
        XCTAssertEqual(runtime.registrationVersion, 42)
        await runtime.disable()
        XCTAssertEqual(api.deleted, api.writes.prefix(1).map(\.id))
        XCTAssertFalse(runtime.optedIn)
    }

    func testAccountSwitchDropsTokenAndUsesDifferentInstallation() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a"); await runtime.enable()
        await runtime.registered(token: Data([1]))
        await runtime.bind(owner: "account-b")
        await runtime.registered(token: Data([2]))
        XCTAssertEqual(api.writes.count, 1)
        await runtime.enable(); await runtime.registered(token: Data([3]))
        XCTAssertNotEqual(api.writes[0].id, api.writes[1].id)
        XCTAssertEqual(api.writes.map(\.owner), ["account-a", "account-b"])
    }

    func testAccountSwitchDuringPermissionRequestNeverRegistersForNextAccount() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a")
        permission.onRequest = { await runtime.bind(owner: "account-b") }
        await runtime.enable()
        XCTAssertEqual(permission.registrations, 0)
        XCTAssertFalse(runtime.optedIn)
        XCTAssertTrue(api.writes.isEmpty)
    }

    func testAccountSwitchDuringPendingCleanupStopsOldCapabilityRead() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        await runtime.bind(owner: "account-a")
        api.revokeFails = true
        await runtime.disable()
        XCTAssertTrue(runtime.needsRevocation)
        api.revokeFails = false
        api.onRevoke = { await runtime.bind(owner: "account-b") }
        await runtime.bind(owner: "account-a")
        XCTAssertEqual(api.capabilityOwners.last, "account-b")
        XCTAssertFalse(runtime.needsRevocation)
    }

    func testColdStartTapWaitsForAuthenticationAndAccountSwitchDropsPendingTap() async {
        let api = NotificationAPIFake(); let permission = NotificationPermissionFake()
        let runtime = runtime(api, permission)
        var opened: [UUID] = []
        runtime.openActivity = { id, _ in opened.append(id); return true }
        let id = UUID()
        await runtime.receiveTap(["activity_id": id.uuidString])
        XCTAssertTrue(opened.isEmpty)
        await runtime.bind(owner: "account-a")
        XCTAssertEqual(opened, [id])
        await runtime.bind(owner: nil)
        await runtime.receiveTap(["activity_id": UUID().uuidString])
        await runtime.bind(owner: "account-b")
        XCTAssertEqual(opened, [id])
    }
}

@MainActor private final class NotificationPermissionFake: NativeNotificationPermissionClient {
    var granted = true
    var prompts = 0
    var registrations = 0
    var onRequest: (() async -> Void)?
    func requestPermission() async throws -> Bool { prompts += 1; await onRequest?(); return granted }
    func hasPermission() async -> Bool { granted }
    func register() { registrations += 1 }
    func unregister() {}
}

@MainActor private final class NotificationAPIFake: NativeNotificationRegistrationClient {
    struct Write { let id: UUID; let token: String; let owner: String }
    var available = true
    var writes: [Write] = []
    var deleted: [UUID] = []
    var capabilityOwners: [String] = []
    var revokeFails = false
    var onRevoke: (() async -> Void)?
    func capabilities(owner: String) async throws -> Bool { capabilityOwners.append(owner); return available }
    func put(id: UUID, token: String, owner: String) async throws -> NativeNotificationReceipt {
        writes.append(Write(id: id, token: token, owner: owner))
        return NativeNotificationReceipt(id: id, enabled: true, registrationVersion: 40 + writes.count)
    }
    func revoke(id: UUID, owner: String) async throws {
        if revokeFails { throw APIError.unsupported }
        await onRevoke?()
        deleted.append(id)
    }
}
