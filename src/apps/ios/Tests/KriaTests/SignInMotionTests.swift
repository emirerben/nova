import XCTest
@testable import Kria

final class SignInMotionTests: XCTestCase {
    func testDelaysAreNonDecreasingInDeclarationOrder() {
        let delays = SignInMotion.Element.allCases.map(SignInMotion.delay(for:))
        for (previous, next) in zip(delays, delays.dropFirst()) {
            XCTAssertLessThanOrEqual(previous, next)
        }
    }

    func testLastElementSettlesWellBeforeAnyBlockingBudget() {
        guard let last = SignInMotion.Element.allCases.last else {
            return XCTFail("Element has no cases")
        }
        XCTAssertLessThanOrEqual(SignInMotion.delay(for: last) + 0.34, 0.9)
    }

    func testAnimationIsNilUnderReduceMotion() {
        XCTAssertNil(SignInMotion.animation(reduceMotion: true))
    }

    func testAnimationExistsWithoutReduceMotion() {
        XCTAssertNotNil(SignInMotion.animation(reduceMotion: false))
    }

    func testAmbientPeriodIsWithinTheApprovedRange() {
        XCTAssertTrue((2...6).contains(SignInMotion.ambientPeriod))
    }
}
