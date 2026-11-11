import XCTest
@testable import Kria

final class TeleprompterCinematicStagingTests: XCTestCase {
    private var base: URL!

    override func setUpWithError() throws {
        base = FileManager.default.temporaryDirectory.appendingPathComponent("staging-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: base) }

    private func write(_ url: URL, bytes: Int = 1_024) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(count: bytes).write(to: url)
    }

    private func exists(_ url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }

    func testASweepRemovesFilesAnEarlierProcessAbandonedButKeepsTheBakeRunningNow() throws {
        let staging = CinematicStaging(base: base)
        let abandoned = base.appendingPathComponent("CinematicBake/\(UUID().uuidString).mov")
        try write(abandoned)
        let running = staging.reserve()
        try write(running)

        XCTAssertEqual(staging.sweepAbandoned(), 1)
        XCTAssertFalse(exists(abandoned), "a bake the app was killed in the middle of must not keep its working file forever")
        XCTAssertTrue(exists(running), "the sweep must never delete the file of a bake that is running")

        staging.release(running)
        XCTAssertFalse(exists(running), "a finished bake removes its own working file")
        XCTAssertEqual(staging.sweepAbandoned(), 0)
    }

    func testASweepAlsoClearsWhatTheOldLayoutLeftInTheTemporaryFolderAndNothingElse() throws {
        let staging = CinematicStaging(base: base)
        let oldLayout = base.appendingPathComponent("cinematic-\(UUID().uuidString).mov")
        let someoneElses = base.appendingPathComponent("export.mov")
        let note = base.appendingPathComponent("cinematic-notes.txt")
        for url in [oldLayout, someoneElses, note] { try write(url) }

        XCTAssertEqual(staging.sweepAbandoned(), 1)
        XCTAssertFalse(exists(oldLayout))
        XCTAssertTrue(exists(someoneElses), "only the bake's own working files are touched")
        XCTAssertTrue(exists(note))
    }

    func testASweepWithNothingToDoIsHarmless() {
        XCTAssertEqual(CinematicStaging(base: base).sweepAbandoned(), 0)
        XCTAssertEqual(CinematicStaging(base: base.appendingPathComponent("missing")).sweepAbandoned(), 0)
    }

    func testEveryReservationIsAFreshPathInsideTheBakeFolder() {
        let staging = CinematicStaging(base: base)
        let first = staging.reserve(), second = staging.reserve()
        XCTAssertNotEqual(first, second)
        XCTAssertEqual(first.deletingLastPathComponent().lastPathComponent, "CinematicBake")
        XCTAssertEqual(first.pathExtension, "mov")
    }
}
