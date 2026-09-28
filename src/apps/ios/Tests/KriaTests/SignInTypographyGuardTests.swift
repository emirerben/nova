import XCTest
@testable import Kria

/// KRI-161: the signup and consent screens use `KriaFont.headline` (Inter
/// Bold). The font must be registered, or SwiftUI silently falls back to the
/// system face.
final class SignInTypographyGuardTests: XCTestCase {
    func testBundledInterBoldResolvesByItsPostScriptName() {
        // Registered via `UIAppFonts` in Info.plist; resolves at runtime
        // inside the test host without any extra registration.
        XCTAssertNotNil(UIFont(name: "Inter-Bold", size: 12), "Expected the bundled Inter-Bold.ttf to expose the PostScript name \"Inter-Bold\"")
    }
}
