import Foundation
import XCTest
@testable import KriaMediaEngine

final class DeviceRenderIntentTests: XCTestCase {
    func testCrashBeforeAcknowledgementKeepsCurrentRetryHeldWithoutInventingAck() throws {
        let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
        let key = key(), source = identity(revision: 1)
        try DeviceRenderIntentStore(root: root).save(.init(sourceIdentity: source, baseGeneration: "approved-a", state: .retryPending), for: key)
        let recovered = try XCTUnwrap(DeviceRenderIntentStore(root: root).load(key))
        XCTAssertNil(recovered.acknowledgement)
        XCTAssertEqual(recovered.decision(identity: identity(revision: 2), baseGeneration: "approved-a", published: false), .hold)
        XCTAssertNil(recovered.reportTarget(identity: identity(revision: 2), baseGeneration: "approved-a"))
    }

    func testStoppedUnknownRetryPersistsAndUsesObservedIdentityWithoutCallingItAcknowledged() throws {
        let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
        let key = key(), store = DeviceRenderIntentStore(root: root)
        let intent = DeviceRenderIntent(sourceIdentity: identity(revision: 1), baseGeneration: "approved-a", state: .stopped)
        try store.save(intent, for: key)
        let reopened = DeviceRenderIntentStore(root: root)
        var saved = try XCTUnwrap(reopened.load(key))
        let target = try XCTUnwrap(saved.reportTarget(identity: identity(revision: 2), baseGeneration: "approved-a"))
        XCTAssertEqual(target, identity(revision: 2))
        saved.observedIdentity = target
        try reopened.save(saved, for: key)
        saved = try XCTUnwrap(DeviceRenderIntentStore(root: root).load(key))
        XCTAssertNil(saved.acknowledgement)
        XCTAssertNil(saved.reportTarget(identity: identity(revision: 1), baseGeneration: "approved-a"))
        XCTAssertEqual(saved.decision(identity: identity(revision: 1), baseGeneration: "approved-a", published: false), .hold)
        XCTAssertEqual(saved.decision(identity: target, baseGeneration: "approved-a", published: false), .hold)
    }

    func testAcknowledgedStopSurvivesRestartAndOnlyNewApprovedGenerationReleasesAutomatically() throws {
        let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
        var intent = DeviceRenderIntent(sourceIdentity: identity(revision: 1), baseGeneration: "approved-a", state: .stopped)
        intent.acknowledgement = identity(revision: 2)
        try DeviceRenderIntentStore(root: root).save(intent, for: key())
        let restored = try XCTUnwrap(DeviceRenderIntentStore(root: root).load(key()))
        XCTAssertEqual(restored.decision(identity: identity(revision: 3), baseGeneration: "approved-a", published: false), .hold)
        XCTAssertEqual(restored.decision(identity: identity(revision: 3), baseGeneration: nil, published: false), .hold)
        XCTAssertEqual(restored.decision(identity: identity(revision: 3), baseGeneration: "approved-b", published: false), .hold)
        XCTAssertEqual(restored.decision(identity: identity(revision: 1), baseGeneration: "approved-b", published: false), .hold)
        XCTAssertEqual(restored.decision(identity: identity(revision: 2), baseGeneration: "approved-a", published: true), .published)
    }

    func testExplicitResumeReleasesCurrentObservedIdentityAndFencesLateAcknowledgement() throws {
        let store = DeviceRenderIntentStore(), key = key()
        let old = DeviceRenderIntent(sourceIdentity: identity(revision: 1), baseGeneration: "approved-a", state: .retryPending)
        try store.save(old, for: key)
        let resumed = DeviceRenderIntent(sourceIdentity: identity(revision: 2), baseGeneration: "approved-a", state: .released)
        try store.save(resumed, for: key)
        XCTAssertFalse(try store.update(key, operationID: old.operationID) { $0.acknowledgement = self.identity(revision: 2); $0.state = .stopped })
        XCTAssertEqual(try store.load(key), resumed)
        XCTAssertNil(resumed.acknowledgement)
    }

    func testOwnerSeparationAndExplicitAccountRemoval() throws {
        let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
        let store = DeviceRenderIntentStore(root: root)
        let intent = DeviceRenderIntent(sourceIdentity: identity(revision: 1), baseGeneration: "a", state: .stopped)
        try store.save(intent, for: key(owner: "account-a"))
        XCTAssertNil(try store.load(key(owner: "account-b")))
        try store.save(intent, for: key(owner: "account-b"))
        try store.removeAccount(owner: "account-a")
        XCTAssertNil(try store.load(key(owner: "account-a")))
        XCTAssertEqual(try store.load(key(owner: "account-b")), intent)
        XCTAssertThrowsError(try store.load(key(owner: "")))
    }

    func testMalformedOrUnreadableJournalIsNotAbsence() throws {
        let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
        let store = DeviceRenderIntentStore(root: root)
        try store.save(.init(sourceIdentity: identity(revision: 1), baseGeneration: "a", state: .stopped), for: key())
        let files = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil))
        let file = try XCTUnwrap(files.allObjects.compactMap { $0 as? URL }.first { $0.pathExtension == "json" })
        try Data("malformed".utf8).write(to: file)
        XCTAssertThrowsError(try DeviceRenderIntentStore(root: root).load(key()))
        try FileManager.default.removeItem(at: file)
        try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false)
        XCTAssertThrowsError(try DeviceRenderIntentStore(root: root).load(key()))
    }

    func testDiskEnvelopeVersionAndIdentityAreValidatedOnReload() throws {
        for corruption in ["ownerDigest", "projectID", "version", "jobID", "variantID"] {
            let root = scratch(); defer { try? FileManager.default.removeItem(at: root) }
            let store = DeviceRenderIntentStore(root: root)
            try store.save(.init(sourceIdentity: identity(revision: 1), baseGeneration: "a", state: .stopped), for: key())
            let files = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil))
            let file = try XCTUnwrap(files.allObjects.compactMap { $0 as? URL }.first { $0.pathExtension == "json" })
            var envelope = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
            if corruption == "ownerDigest" { envelope[corruption] = "wrong-owner" }
            else if corruption == "projectID" { envelope[corruption] = UUID().uuidString }
            else {
                var record = try XCTUnwrap(envelope["intent"] as? [String: Any])
                if corruption == "version" { record["version"] = 99 }
                else {
                    var source = try XCTUnwrap(record["sourceIdentity"] as? [String: Any])
                    source[corruption == "jobID" ? "jobId" : "variantId"] = corruption == "jobID" ? UUID().uuidString : "wrong-variant"
                    record["sourceIdentity"] = source
                }
                envelope["intent"] = record
            }
            try JSONSerialization.data(withJSONObject: envelope).write(to: file)
            XCTAssertThrowsError(try DeviceRenderIntentStore(root: root).load(key()), corruption)
        }
    }

    private let job = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!
    private let project = UUID(uuidString: "00000000-0000-0000-0000-000000000002")!
    private func key(owner: String = "account-a") -> DeviceRenderIntentKey { .init(owner: owner, projectID: project, jobID: job, variantID: "first") }
    private func identity(revision: Int) -> DeviceRenderIdentity { .init(jobID: job, variantID: "first", recipeRevision: revision, recipeDigest: String(repeating: "a", count: 64)) }
    private func scratch() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }
}
