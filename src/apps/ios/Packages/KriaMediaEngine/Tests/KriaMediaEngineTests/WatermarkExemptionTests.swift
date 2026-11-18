import XCTest
@testable import KriaMediaEngine

final class WatermarkExemptionTests: XCTestCase {
    func testExemptionIsOwnerBoundAndPreservesOutro() {
        let grant = WatermarkExemption(ownerHash: String(repeating: "a", count: 64))
        XCTAssertFalse(grant.branding(for: String(repeating: "a", count: 64)).watermark)
        XCTAssertTrue(grant.branding(for: String(repeating: "a", count: 64)).outro)
        XCTAssertEqual(grant.branding(for: String(repeating: "a", count: 64)).contractTail, "standard")
        XCTAssertEqual(grant.branding(for: nil), .standard)
        XCTAssertEqual(grant.branding(for: String(repeating: "b", count: 64)), .standard)
        XCTAssertEqual(WatermarkExemption(ownerHash: "").branding(for: ""), .standard)
    }

    func testWireShapeAndSameAccountRefresh() throws {
        let owner = String(repeating: "c", count: 64)
        let data = Data("{\"owner_hash\":\"\(owner)\"}".utf8)
        let grant = try RecipeJSON.decoder().decode(WatermarkExemption.self, from: data)
        XCTAssertFalse(grant.branding(for: owner).watermark)
        XCTAssertEqual(grant.branding(for: owner), grant.branding(for: owner))
    }
}
