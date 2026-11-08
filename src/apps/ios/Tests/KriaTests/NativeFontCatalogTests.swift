import CoreText
import XCTest
@testable import Kria

/// KRI-171: the editor offers every non-deprecated registry font, and each one
/// must resolve to its own bundled file (not a system fallback) so the picker
/// rows really draw in their own typeface.
final class NativeFontCatalogTests: XCTestCase {
    private var directory: URL { get throws { try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)) } }
    private func registry() throws -> [String: NativeFontRegistry.Entry] {
        try XCTUnwrap(NativeFontRegistry.entries(from: Data(contentsOf: directory.appendingPathComponent("font-registry.json"))))
    }

    func testPickerFontsAreTheNonDeprecatedRegistryKeysSorted() throws {
        let live = try registry().filter { $0.value.deprecated != true }.keys.sorted { $0.lowercased() < $1.lowercased() }
        XCTAssertEqual(NativeFontCatalog.shared.pickerFonts, live)
        XCTAssertFalse(live.contains("Inter Regular"))
        XCTAssertTrue(live.contains("Bebas Neue"))
        XCTAssertEqual(NativeEditorWireContract.captionFonts, live)
    }

    func testEveryPickerFontResolvesToItsOwnBundledFace() throws {
        let registry = try registry()
        var faces = Set<String>()
        for name in NativeFontCatalog.shared.pickerFonts {
            let url = try XCTUnwrap(NativeFontCatalog.shared.fontURL(for: name), name)
            XCTAssertEqual(url.lastPathComponent, registry[name]?.file, name)
            let font = try XCTUnwrap(NativeFontCatalog.shared.ctFont(name, size: 20), name)
            let postScript = CTFontCopyPostScriptName(font) as String
            XCTAssertFalse(postScript.isEmpty, name)
            faces.insert(postScript)
        }
        // Entries are distinct faces, apart from a few weights that share a
        // variable-font file.
        XCTAssertGreaterThanOrEqual(faces.count, 30)
    }

    func testUnknownAndDeprecatedNamesFallBackWithoutCrashing() {
        XCTAssertNil(NativeFontCatalog.shared.fontURL(for: "Definitely Not A Font"))
        XCTAssertNil(NativeFontCatalog.shared.ctFont("Definitely Not A Font", size: 20))
        _ = NativeFontCatalog.shared.previewFont("Definitely Not A Font", size: 16)
        _ = NativeFontCatalog.shared.previewFont(nil, size: 16)
        // A deprecated key still resolves so existing layers keep drawing.
        XCTAssertEqual(NativeFontCatalog.shared.fontURL(for: "Inter Regular")?.lastPathComponent, "Inter-Regular.ttf")
    }

    func testRegistryReaderKeepsTheLastDuplicateLikeThePythonAndJSParsers() throws {
        let json = #"""
        {"note": "a \"quoted\" } brace", "fonts": {
          "Outfit": {"file": "Outfit-Bold.ttf", "weight": 700, "deprecated": true},
          "Anton": {"file": "Anton-Regular.ttf", "css_family": "'Anton', sans-serif"},
          "Outfit": {"file": "Outfit-VF.ttf", "weight": 700}
        }, "tail": [1, 2, {"x": 3}]}
        """#
        let entries = try XCTUnwrap(NativeFontRegistry.entries(from: Data(json.utf8)))
        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries["Outfit"]?.file, "Outfit-VF.ttf")
        XCTAssertNil(entries["Outfit"]?.deprecated)
        XCTAssertEqual(entries["Anton"]?.file, "Anton-Regular.ttf")
        XCTAssertNil(NativeFontRegistry.entries(from: Data("not json".utf8)))
    }

    func testOutfitIsOfferedAndResolvesToTheLiveVariableFont() {
        XCTAssertTrue(NativeFontCatalog.shared.pickerFonts.contains("Outfit"))
        XCTAssertEqual(NativeFontCatalog.shared.fontURL(for: "Outfit")?.lastPathComponent, "Outfit-VF.ttf")
    }

    /// KRI-1312: the sideways tabs come from the registry's vibe (and mono category), not a hand list.
    func testPickerGroupsComeFromTheRegistryVibe() throws {
        let catalog = NativeFontCatalog.shared
        XCTAssertTrue(catalog.fonts(in: .editorial).contains("Playfair Display"))
        XCTAssertTrue(catalog.fonts(in: .headlines).contains("Bebas Neue"))
        XCTAssertTrue(catalog.fonts(in: .script).contains("Pacifico"))
        XCTAssertTrue(catalog.fonts(in: .handwriting).contains("Patrick Hand"))
        // A font is on at most one tab, and every tab font is a picker font.
        let grouped = NativeFontGroup.allCases.flatMap { catalog.fonts(in: $0) }
        XCTAssertEqual(grouped.count, Set(grouped).count)
        XCTAssertTrue(Set(grouped).isSubset(of: Set(catalog.pickerFonts)))
        // A mono face lands on Mono whatever its vibe says; a vibe the app does not know has no tab.
        XCTAssertEqual(NativeFontGroup(vibe: "clean_captions", category: "mono"), .mono)
        XCTAssertNil(NativeFontGroup(vibe: "something_new", category: "sans"))
        XCTAssertNil(NativeFontGroup(vibe: nil, category: nil))
        // Without the bundled registry there is nothing to group, so the picker shows only "All".
        XCTAssertTrue(NativeFontCatalog(directory: nil).populatedGroups.isEmpty)
    }

    func testMissingDirectoryFallsBackToTheLegacyList() {
        let catalog = NativeFontCatalog(directory: nil)
        XCTAssertEqual(catalog.pickerFonts, NativeFontCatalog.fallbackFonts)
        XCTAssertNil(catalog.ctFont("Inter", size: 12))
    }
}
