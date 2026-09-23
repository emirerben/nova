import XCTest
@testable import Kria

/// KRI-161 rule: the signup and consent screens use `KriaFont.headline`
/// (Inter Bold), never `KriaFont.display` (Fraunces). Source-scans the two
/// files rather than the rendered font, since the rule is about which call
/// a future edit reaches for.
final class SignInTypographyGuardTests: XCTestCase {
    func testSignInViewNeverUsesDisplayOrFraunces() throws {
        try assertNoDisplayFont(atRelativePath: "src/apps/ios/Kria/Features/SignInView.swift")
    }

    func testAccountPrivacyViewsNeverUsesDisplayOrFraunces() throws {
        try assertNoDisplayFont(atRelativePath: "src/apps/ios/Kria/Features/AccountPrivacyViews.swift")
    }

    func testBundledInterBoldResolvesByItsPostScriptName() {
        // Registered via `UIAppFonts` in Info.plist; resolves at runtime
        // inside the test host without any extra registration.
        XCTAssertNotNil(UIFont(name: "Inter-Bold", size: 12), "Expected the bundled Inter-Bold.ttf to expose the PostScript name \"Inter-Bold\"")
    }

    private func assertNoDisplayFont(atRelativePath relativePath: String, file: StaticString = #filePath, line: UInt = #line) throws {
        let contents = try String(contentsOf: repoRoot().appendingPathComponent(relativePath), encoding: .utf8)
        XCTAssertFalse(contents.contains("KriaFont.display("), "\(relativePath) must not use KriaFont.display(", file: file, line: line)
        XCTAssertFalse(contents.contains("Fraunces"), "\(relativePath) must not reference Fraunces", file: file, line: line)
    }

    /// This test file lives at `<repo>/src/apps/ios/Tests/KriaTests/…swift`;
    /// walk up past `KriaTests`, `Tests`, `ios`, `apps`, `src` to the repo root.
    private func repoRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // KriaTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // ios
            .deletingLastPathComponent() // apps
            .deletingLastPathComponent() // src
            .deletingLastPathComponent() // <repo root>
    }
}
