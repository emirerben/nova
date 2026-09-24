import XCTest
@testable import Kria

/// Coverage for `NativeSfxBrowse`, the Swift port of
/// `src/apps/web/src/lib/sfx-browse.ts`'s search + category grouping.
final class NativeSfxBrowseTests: XCTestCase {
    private func effect(_ id: String, _ name: String, category: String? = nil, terms: [String] = []) -> NativeEditorSoundEffect {
        NativeEditorSoundEffect(id: id, name: name, durationS: 1, previewAudioURL: nil, roleTags: [], category: category, searchTerms: terms)
    }

    func testEmptyQueryGroupsEverythingInCategoryOrder() {
        let effects = [
            effect("1", "Wrong buzzer", category: "rejection"),
            effect("2", "Cash register", category: "money"),
            effect("3", "Legacy upload"),
        ]
        let groups = NativeSfxBrowse.groupEffects(effects)
        XCTAssertEqual(groups.flatMap(\.effects).count, 3, "an empty query matches everything")
        // categoryOrder is transition/…/rejection/…/money/…, not insertion order.
        XCTAssertEqual(groups.map(\.key), ["rejection", "money", "other"])
    }

    func testWholeWordQueryFindsTheEffectByNameOrSearchTerm() {
        let effects = [
            effect("1", "Wrong buzzer", category: "rejection", terms: ["buzzer", "wrong"]),
            effect("2", "Cash register", category: "money"),
        ]
        let matched = NativeSfxBrowse.groupEffects(effects, query: "buzzer").flatMap(\.effects)
        XCTAssertEqual(matched.map(\.id), ["1"])
    }

    func testFillerOnlyQueryMatchesTheFillerWordLiterallyInsteadOfEverything() {
        // "noise" is in the filler list, but a query that is ONLY filler
        // words is matched literally (`wanted` falls back to every word),
        // so it still narrows the list rather than behaving like an empty query.
        let effects = [
            effect("1", "White noise", category: "transition", terms: ["noise"]),
            effect("2", "Wrong buzzer", category: "rejection"),
        ]
        let matched = NativeSfxBrowse.groupEffects(effects, query: "noise").flatMap(\.effects)
        XCTAssertEqual(matched.map(\.id), ["1"])
    }

    func testPrefixFallbackMatchesAWordStillBeingTyped() {
        // No whole-word match for "who" -> the last word falls back to a
        // word-start match. "Whip" does not start with "who" (w-h-i), so it
        // stays excluded even under the fallback.
        let effects = [
            effect("1", "Whoosh", category: "transition"),
            effect("2", "Whip crack", category: "impact"),
        ]
        let matched = NativeSfxBrowse.groupEffects(effects, query: "who").flatMap(\.effects)
        XCTAssertEqual(matched.map(\.id), ["1"])
    }

    func testUnknownCategoryFallsIntoOther() {
        let effects = [effect("1", "Mystery sound", category: "unrecognized-category")]
        let groups = NativeSfxBrowse.groupEffects(effects)
        XCTAssertEqual(groups.map(\.key), ["other"])
        XCTAssertEqual(groups.first?.label, "Other")
        XCTAssertFalse(NativeSfxBrowse.categoryOrder.contains("unrecognized-category"))
    }

    func testNoCategoriesProducesASingleFlatGroup() {
        let effects = [effect("1", "Legacy one"), effect("2", "Legacy two")]
        XCTAssertFalse(NativeSfxBrowse.hasCategories(effects))
        let groups = NativeSfxBrowse.groupEffects(effects)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups.first?.key, "other")
        XCTAssertEqual(groups.first?.effects.count, 2)
    }

    func testHasCategoriesIsTrueAsSoonAsOneEffectHasAKnownCategory() {
        let effects = [effect("1", "Legacy one"), effect("2", "Wrong buzzer", category: "rejection")]
        XCTAssertTrue(NativeSfxBrowse.hasCategories(effects))
    }
}
