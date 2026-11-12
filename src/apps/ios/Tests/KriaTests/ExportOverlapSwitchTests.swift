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

    func testUnsetKeepsTheEngineDefault() throws {
        ExportFrameOverlap.framesInFlight = ExportFrameOverlap.defaultFramesInFlight
        ExportOverlapSwitch.installIfConfigured(defaults: try defaults())
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 2)
    }

    func testLaunchArgumentSpellingTurnsOverlapOff() throws {
        let suite = try defaults()
        XCTAssertEqual(ExportOverlapSwitch.defaultsKey, "kria.exportFramesInFlight")
        suite.set("1", forKey: ExportOverlapSwitch.defaultsKey)
        ExportOverlapSwitch.installIfConfigured(defaults: suite)
        XCTAssertEqual(ExportFrameOverlap.framesInFlight, 1, "a launch argument arrives as a string")
    }

    func testValuesAreClampedToWhatTheEngineSupports() throws {
        let suite = try defaults()
        for (raw, expected) in [(0, 1), (-5, 1), (3, 3), (99, ExportFrameOverlap.range.upperBound)] {
            suite.set(raw, forKey: ExportOverlapSwitch.defaultsKey)
            ExportOverlapSwitch.installIfConfigured(defaults: suite)
            XCTAssertEqual(ExportFrameOverlap.framesInFlight, expected, "\(raw)")
        }
    }
}
