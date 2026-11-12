import XCTest
import KriaMediaEngine
@testable import Kria

/// The launch switch for overlapped exports. `-kria.exportFramesInFlight 1` is the QA off switch, so the key's spelling, "unset
/// leaves the engine default" and "a garbage value cannot turn the switch into something bigger than the engine supports" are
/// the contract.
final class ExportOverlapSwitchTests: XCTestCase {
    private func defaults() throws -> UserDefaults {
        let name = "ExportOverlapSwitchTests-\(UUID().uuidString)"
        let suite = try XCTUnwrap(UserDefaults(suiteName: name))
        addTeardownBlock { UserDefaults(suiteName: name)?.removePersistentDomain(forName: name); ExportFrameOverlap.framesInFlight = ExportFrameOverlap.defaultFramesInFlight }
        return suite
    }

    private let sixGB: UInt64 = 5_700_000_000, fourGB: UInt64 = 3_700_000_000

    func testUnsetKeepsTheEngineDefaultOnAPhoneWithRoomForIt() throws {
        ExportFrameOverlap.framesInFlight = 1
        ExportOverlapSwitch.installIfConfigured(defaults: try defaults(), physicalMemory: sixGB)
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 2)
    }

    func testAFourGigabytePhoneKeepsTheSerialPathUnlessTheSwitchIsSet() throws {
        ExportOverlapSwitch.installIfConfigured(defaults: try defaults(), physicalMemory: fourGB)
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 1, "about 100 MB more at the peak is not free on 4 GB")
        let suite = try defaults()
        suite.set(2, forKey: ExportOverlapSwitch.defaultsKey)
        ExportOverlapSwitch.installIfConfigured(defaults: suite, physicalMemory: fourGB)
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 2, "an explicit value always wins")
    }

    func testTheMemoryLineSitsBetweenTheFourAndSixGigabytePhones() {
        XCTAssertEqual(ExportOverlapSwitch.defaultDepth(physicalMemory: fourGB), 1)
        XCTAssertEqual(ExportOverlapSwitch.defaultDepth(physicalMemory: sixGB), 2)
        XCTAssertEqual(ExportOverlapSwitch.defaultDepth(physicalMemory: 7_600_000_000), 2)
    }

    func testLaunchArgumentSpellingTurnsOverlapOff() throws {
        let suite = try defaults()
        XCTAssertEqual(ExportOverlapSwitch.defaultsKey, "kria.exportFramesInFlight")
        suite.set("1", forKey: ExportOverlapSwitch.defaultsKey)
        ExportOverlapSwitch.installIfConfigured(defaults: suite, physicalMemory: sixGB)
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 1, "a launch argument arrives as a string")
    }

    func testValuesAreClampedToWhatTheEngineSupports() throws {
        let suite = try defaults()
        for (raw, expected) in [(0, 1), (-5, 1), (3, 3), (99, ExportFrameOverlap.range.upperBound)] {
            suite.set(raw, forKey: ExportOverlapSwitch.defaultsKey)
            ExportOverlapSwitch.installIfConfigured(defaults: suite, physicalMemory: sixGB)
            XCTAssertEqual(ExportFrameOverlap.framesInFlight, expected, "\(raw)")
        }
    }
}
