import XCTest
@testable import Kria

/// Pure text-rule coverage for the Account screen's two-line identity block
/// (KRI-113). `AccountIdentity` has no view/network dependency, so these run
/// synchronously with no stubbing.
final class AccountIdentityTests: XCTestCase {
    func testPrimaryLinePrefersNameOverEmail() {
        XCTAssertEqual(AccountIdentity.primaryLine(name: "Emir Erben", email: "emir@example.com"), "Emir Erben")
    }

    func testPrimaryLineFallsBackToEmailWhenNameIsNilOrBlank() {
        XCTAssertEqual(AccountIdentity.primaryLine(name: nil, email: "emir@example.com"), "emir@example.com")
        XCTAssertEqual(AccountIdentity.primaryLine(name: "   ", email: "emir@example.com"), "emir@example.com")
    }

    func testDetailLineCombinesEmailAndProviderWhenNameIsKnown() {
        let detail = AccountIdentity.detailLine(name: "Emir Erben", email: "emir@example.com", providers: ["google"])
        XCTAssertEqual(detail, "emir@example.com · Google")
    }

    func testDetailLineIsJustProviderWhenNameIsKnownButEmailIsMissing() {
        let detail = AccountIdentity.detailLine(name: "Emir Erben", email: nil, providers: ["google"])
        XCTAssertEqual(detail, "Signed in with Google")
    }

    func testDetailLineIsProviderOnlyWhenNameIsUnknown() {
        let detail = AccountIdentity.detailLine(name: nil, email: "emir@example.com", providers: ["google"])
        XCTAssertEqual(detail, "Signed in with Google")
    }

    func testDetailLineAddsHiddenEmailCaptionForApplePrivateRelay() {
        let detail = AccountIdentity.detailLine(name: nil, email: "k7f3m9x2@privaterelay.appleid.com", providers: ["apple"])
        XCTAssertEqual(detail, "Signed in with Apple · hidden email")
    }

    func testDetailLineOmitsHiddenEmailCaptionForOrdinaryAppleEmail() {
        let detail = AccountIdentity.detailLine(name: nil, email: "emir@icloud.com", providers: ["apple"])
        XCTAssertEqual(detail, "Signed in with Apple")
    }

    func testDetailLineIsNilWhenNothingIsKnownYet() {
        XCTAssertNil(AccountIdentity.detailLine(name: nil, email: nil, providers: []))
    }

    func testIsPrivateRelayMatchesOnlyTheAppleRelaySuffix() {
        XCTAssertTrue(AccountIdentity.isPrivateRelay(email: "abc123@privaterelay.appleid.com"))
        XCTAssertTrue(AccountIdentity.isPrivateRelay(email: "ABC123@PRIVATERELAY.APPLEID.COM"), "the check must be case-insensitive")
        XCTAssertFalse(AccountIdentity.isPrivateRelay(email: "emir@icloud.com"))
        XCTAssertFalse(AccountIdentity.isPrivateRelay(email: "emir@gmail.com"))
    }

    func testInitialPrefersNameOverEmail() {
        XCTAssertEqual(AccountIdentity.initial(name: "Emir Erben", email: "creator@example.com"), "E")
    }

    func testInitialFallsBackToEmailWhenNameIsNilOrBlank() {
        XCTAssertEqual(AccountIdentity.initial(name: nil, email: "creator@example.com"), "C")
        XCTAssertEqual(AccountIdentity.initial(name: "", email: "creator@example.com"), "C")
    }

    func testInitialFallsBackToQuestionMarkWhenNothingIsKnown() {
        XCTAssertEqual(AccountIdentity.initial(name: nil, email: nil), "?")
    }
}
