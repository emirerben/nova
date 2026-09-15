import XCTest

final class PrivacyManifestTests: XCTestCase {
    func testCompiledAppContainsRequiredAPIReasonsAndTruthfulDataDeclarations() throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "PrivacyInfo", withExtension: "xcprivacy"), "Manifest must be shipped in the application bundle")
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: Data(contentsOf: url), format: nil) as? [String: Any])
        XCTAssertEqual(plist["NSPrivacyTracking"] as? Bool, false)
        XCTAssertEqual(plist["NSPrivacyTrackingDomains"] as? [String], [])
        let accessed = try XCTUnwrap(plist["NSPrivacyAccessedAPITypes"] as? [[String: Any]])
        let reasons = Dictionary(uniqueKeysWithValues: accessed.compactMap { row -> (String, [String])? in
            guard let category = row["NSPrivacyAccessedAPIType"] as? String,
                  let reasons = row["NSPrivacyAccessedAPITypeReasons"] as? [String] else { return nil }
            return (category, reasons)
        })
        XCTAssertTrue(reasons["NSPrivacyAccessedAPICategoryUserDefaults"]?.contains("CA92.1") == true)
        XCTAssertTrue(reasons["NSPrivacyAccessedAPICategoryDiskSpace"]?.contains("E174.1") == true)
        let collected = try XCTUnwrap(plist["NSPrivacyCollectedDataTypes"] as? [[String: Any]])
        let categories = Set(collected.compactMap { $0["NSPrivacyCollectedDataType"] as? String })
        for kind in ["Name", "EmailAddress", "UserID", "PhotosorVideos", "AudioData", "OtherUserContent", "ProductInteraction"] {
            XCTAssertTrue(categories.contains("NSPrivacyCollectedDataType" + kind), "Missing account-backed \(kind) declaration")
        }
        for category in collected {
            XCTAssertEqual(category["NSPrivacyCollectedDataTypeTracking"] as? Bool, false)
            XCTAssertEqual(category["NSPrivacyCollectedDataTypeLinked"] as? Bool, true)
            XCTAssertEqual(category["NSPrivacyCollectedDataTypePurposes"] as? [String], ["NSPrivacyCollectedDataTypePurposeAppFunctionality"])
        }
    }
}
