import XCTest
@testable import Kria

/// A bench A/B is only worth anything if each arm really ran what its name says. Failure modes of the Debug override:
/// 1. a misspelled lever name silently leaves the new behaviour on, so both arms run the same code;
/// 2. `all` leaves one lever on;
/// 3. a partial list flips a lever it did not name;
/// 4. a process with no override runs anything but the shipping defaults.
final class LiveBackgroundTuningTests: XCTestCase {
    func testNoOverrideIsTheShippingBehaviour() {
        XCTAssertEqual(LiveBackgroundTuning.resolve(environment: [:]), LiveBackgroundTuning())
        XCTAssertEqual(LiveBackgroundTuning.resolve(environment: [LiveBackgroundTuning.overrideKey: ""]), LiveBackgroundTuning())
        XCTAssertEqual(LiveBackgroundTuning().summary, "fresh=1 input=1 async=1 video=1")
    }

    func testAllTurnsEveryLeverBack() {
        let parsed = LiveBackgroundTuning.parse(oldBehaviours: "all")
        XCTAssertEqual(parsed.tuning.summary, "fresh=0 input=0 async=0 video=0")
        XCTAssertTrue(parsed.unknown.isEmpty)
    }

    func testAListTurnsBackOnlyTheNamedLevers() {
        for (list, expected) in [("async", "fresh=1 input=1 async=0 video=1"),
                                 ("fresh, video", "fresh=0 input=1 async=1 video=0"),
                                 ("input,async,fresh", "fresh=0 input=0 async=0 video=1")] {
            let parsed = LiveBackgroundTuning.parse(oldBehaviours: list)
            XCTAssertEqual(parsed.tuning.summary, expected, list)
            XCTAssertTrue(parsed.unknown.isEmpty, list)
        }
    }

    func testAMisspelledLeverIsReportedInsteadOfIgnored() {
        let parsed = LiveBackgroundTuning.parse(oldBehaviours: "asnyc,fresh")
        XCTAssertEqual(parsed.unknown, ["asnyc"])
        XCTAssertEqual(parsed.tuning.summary, "fresh=0 input=1 async=1 video=1")
    }
}
