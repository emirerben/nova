import XCTest
import KriaMediaEngine
@testable import Kria

final class AuthoredMergeTests: XCTestCase {
    private func entity(_ id: String, _ text: String, color: String = "white") -> JSONValue {
        .object(["id": .string(id), "text": .string(text), "color": .string(color)])
    }

    func testIndependentEntitiesAndFieldsSurviveTheSameLaneMerge() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B")])
        let local: JSONValue = .array([entity("a", "Local"), entity("b", "B")])
        let remote: JSONValue = .array([entity("a", "A", color: "blue"), entity("b", "Remote")])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, .array([entity("a", "Local", color: "blue"), entity("b", "Remote")]))
    }

    func testSameFieldConflictRetainsBothValuesAndOnlyResolvesThatField() {
        let base = entity("a", "A"), local = entity("a", "Local"), remote = entity("a", "Remote", color: "blue")
        let unresolved = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertEqual(unresolved.conflicts.map(\.path), [["text"]])
        XCTAssertEqual(unresolved.conflicts.first?.local, .string("Local"))
        XCTAssertEqual(unresolved.conflicts.first?.remote, .string("Remote"))
        let resolved = AuthoredJSONMerge.merge(base: base, local: local, remote: remote, preference: .local)
        XCTAssertTrue(resolved.conflicts.isEmpty)
        XCTAssertEqual(resolved.value, entity("a", "Local", color: "blue"))
    }

    func testDeletionVersusEditConflictsAndUnchangedDeletedEntityDoesNotReturn() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B")])
        let local: JSONValue = .array([entity("a", "A")])
        XCTAssertEqual(AuthoredJSONMerge.merge(base: base, local: local, remote: base).value, local)
        let remote: JSONValue = .array([entity("a", "A"), entity("b", "Changed")])
        XCTAssertFalse(AuthoredJSONMerge.merge(base: base, local: local, remote: remote).conflicts.isEmpty)
    }

    func testReorderAndIndependentRemoteEditMergeByIdentity() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B")])
        let local: JSONValue = .array([entity("b", "B"), entity("a", "A")])
        let remote: JSONValue = .array([entity("a", "Remote"), entity("b", "B")])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, .array([entity("b", "B"), entity("a", "Remote")]))
    }

    func testIndependentInsertionsRetainTheirOwnOrder() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B")])
        let local: JSONValue = .array([entity("a", "A"), entity("x", "X"), entity("b", "B")])
        let remote: JSONValue = .array([entity("a", "A"), entity("y", "Y"), entity("b", "B")])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, .array([entity("a", "A"), entity("x", "X"), entity("y", "Y"), entity("b", "B")]))
    }

    func testAmbiguousLegacyIdentityAndIncompatibleOrderNeverGuess() {
        let base: JSONValue = .array([.string("a"), .string("b")])
        let local: JSONValue = .array([.string("local"), .string("b")])
        let remote: JSONValue = .array([.string("a"), .string("remote")])
        XCTAssertFalse(AuthoredJSONMerge.merge(base: base, local: local, remote: remote).conflicts.isEmpty)
        let rows: JSONValue = .array([entity("a", "A"), entity("b", "B"), entity("c", "C")])
        let left: JSONValue = .array([entity("b", "B"), entity("a", "A"), entity("c", "C")])
        let right: JSONValue = .array([entity("a", "A"), entity("c", "C"), entity("b", "B")])
        XCTAssertFalse(AuthoredJSONMerge.merge(base: rows, local: left, remote: right).conflicts.isEmpty)
    }

    func testDifferentConcurrentAdditionsWithSameIDConflict() {
        let result = AuthoredJSONMerge.merge(base: .array([]), local: .array([entity("x", "Local")]), remote: .array([entity("x", "Remote")]))
        XCTAssertFalse(result.conflicts.isEmpty)
    }

    func testRetainedDeleteEditEntityKeepsItsRemotePosition() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B"), entity("c", "C")])
        let local: JSONValue = .array([entity("a", "A"), entity("c", "C")])
        let remote: JSONValue = .array([entity("a", "A"), entity("b", "Edited"), entity("c", "C")])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote, preference: .remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, remote)
    }

    func testOrderPreferencePreservesCompatibleRemoteInsertion() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B"), entity("c", "C")])
        let local: JSONValue = .array([entity("b", "B"), entity("a", "A"), entity("c", "C")])
        let remote: JSONValue = .array([entity("a", "A"), entity("y", "Y"), entity("c", "C"), entity("b", "B")])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote, preference: .local)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, .array([entity("b", "B"), entity("a", "A"), entity("y", "Y"), entity("c", "C")]))
    }

    func testInsertionOrderCycleExposesBothFullOrdersAndResolvesOnlyTheConflict() {
        let base: JSONValue = .array([entity("a", "A"), entity("b", "B")])
        let local: JSONValue = .array([entity("b", "B"), entity("a", "A")])
        let remote: JSONValue = .array([entity("a", "A"), entity("x", "X"), entity("b", "B")])
        let unresolved = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertEqual(unresolved.conflicts.last?.remote, .array([.string("a"), .string("x"), .string("b")]))
        XCTAssertEqual(AuthoredJSONMerge.merge(base: base, local: local, remote: remote, preference: .local).value,
                       .array([entity("b", "B"), entity("a", "A"), entity("x", "X")]))
        XCTAssertEqual(AuthoredJSONMerge.merge(base: base, local: local, remote: remote, preference: .remote).value, remote)
    }

    func testRemoteOpaqueFieldsAndNullVersusAbsenceRemainLossless() {
        let base: JSONValue = .object(["caption": .string("A"), "optional": .null])
        let local: JSONValue = .object(["caption": .string("Local"), "optional": .null])
        let remote: JSONValue = .object(["caption": .string("A"), "opaque": .object(["future": .number(7)])])
        let result = AuthoredJSONMerge.merge(base: base, local: local, remote: remote)
        XCTAssertTrue(result.conflicts.isEmpty)
        XCTAssertEqual(result.value, .object(["caption": .string("Local"), "opaque": .object(["future": .number(7)])]))
    }
}
