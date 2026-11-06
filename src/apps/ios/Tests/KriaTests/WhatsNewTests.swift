import UIKit
import XCTest
@testable import Kria

/// Failure modes the guide must not have: updates in the wrong order, a card shown for something this account
/// cannot use (including when the capability fetch failed), an empty day, an internal identifier in user copy,
/// and a Try it that leads to a screen the account cannot open.
final class WhatsNewTests: XCTestCase {
    private func feature(_ id: String, requires: WhatsNewRequirement? = nil, tryIt: WhatsNewDestination? = nil) -> WhatsNewFeature {
        WhatsNewFeature(id: id, symbol: "sparkles", title: "Title \(id)", benefit: "Benefit \(id).", steps: [], requires: requires, tryIt: tryIt)
    }

    private func fix(_ text: String, requires: WhatsNewRequirement? = nil) -> WhatsNewFix {
        WhatsNewFix(text: LocalizedStringResource(stringLiteral: text), requires: requires)
    }

    private func release(_ day: WhatsNewDay, features: [WhatsNewFeature] = [], fixes: [WhatsNewFix] = []) -> WhatsNewRelease {
        WhatsNewRelease(day: day, features: features, fixes: fixes)
    }

    private func text(_ resource: LocalizedStringResource) -> String { String(localized: resource) }

    private var everyRequirementMet: WhatsNewAvailability { WhatsNewAvailability(met: Set(WhatsNewRequirement.allCases)) }

    func testReleasesComeBackNewestFirstWhateverTheInputOrder() {
        let days = [WhatsNewDay(2026, 10, 30), WhatsNewDay(2026, 11, 1), WhatsNewDay(2026, 10, 28), WhatsNewDay(2026, 10, 31)]
        let shuffled = days.map { release($0, fixes: [fix("Fix on \($0.id)")]) }
        let visible = everyRequirementMet.visible(shuffled)
        XCTAssertEqual(visible.map(\.day), days.sorted(by: >))
        XCTAssertEqual(visible.first?.day, WhatsNewDay(2026, 11, 1))
    }

    func testDayOrderingIsCalendarOrderNotAlphabetical() {
        XCTAssertLessThan(WhatsNewDay(2026, 9, 30), WhatsNewDay(2026, 10, 1))
        XCTAssertLessThan(WhatsNewDay(2025, 12, 31), WhatsNewDay(2026, 1, 1))
        XCTAssertLessThan(WhatsNewDay(2026, 10, 9), WhatsNewDay(2026, 10, 10))
    }

    func testEachRequirementHidesItsItemsUntilItIsMet() {
        for requirement in WhatsNewRequirement.allCases {
            let gated = release(WhatsNewDay(2026, 11, 1),
                                features: [feature("gated", requires: requirement), feature("open")],
                                fixes: [fix("gated fix", requires: requirement), fix("open fix")])

            let without = WhatsNewAvailability(met: Set(WhatsNewRequirement.allCases).subtracting([requirement])).visible([gated])
            XCTAssertEqual(without.first?.features.map(\.id), ["open"], "\(requirement) unmet must hide its card")
            XCTAssertEqual(without.first?.fixes.map { text($0.text) }, ["open fix"], "\(requirement) unmet must hide its line")

            let with = WhatsNewAvailability(met: [requirement]).visible([gated])
            XCTAssertEqual(with.first?.features.map(\.id), ["gated", "open"], "\(requirement) met must show its card")
            XCTAssertEqual(with.first?.fixes.map { text($0.text) }, ["gated fix", "open fix"], "\(requirement) met must show its line")
        }
    }

    func testADayWhoseItemsAreAllGatedDisappearsInsteadOfShowingEmpty() {
        let onlyGated = release(WhatsNewDay(2026, 10, 31),
                                features: [feature("a", requires: .textPreferences)],
                                fixes: [fix("b", requires: .cameraRoll)])
        let shown = release(WhatsNewDay(2026, 11, 1), fixes: [fix("c")])
        let visible = WhatsNewAvailability(met: []).visible([onlyGated, shown])
        XCTAssertEqual(visible.map(\.day), [WhatsNewDay(2026, 11, 1)])
        XCTAssertTrue(WhatsNewAvailability(met: []).visible([release(WhatsNewDay(2026, 11, 1))]).isEmpty)
    }

    func testAFailedFetchFailsClosedButUngatedItemsStillShow() {
        let unknown = WhatsNewAvailability(capabilities: nil, memory: nil)
        XCTAssertTrue(unknown.met.isEmpty)
        let catalog = unknown.visible(WhatsNewReleases.all)
        let shownFeatures = catalog.flatMap(\.features)
        let shownFixes = catalog.flatMap(\.fixes)
        XCTAssertFalse(shownFeatures.isEmpty, "Ungated cards must not depend on the network")
        XCTAssertTrue(shownFeatures.allSatisfy { $0.requires == nil && $0.tryIt == nil })
        XCTAssertTrue(shownFixes.allSatisfy { $0.requires == nil })
    }

    func testCapabilitiesAndMemoryOpenOnlyTheirOwnGates() throws {
        let lipsync = CreationFormatCapability(id: "lipsync", editFormat: "montage", maxClips: 10)
        let montage = CreationFormatCapability(id: "montage", editFormat: "montage", maxClips: 50)
        XCTAssertEqual(WhatsNewAvailability(capabilities: CreationCapabilities(formats: [montage, lipsync]), memory: nil).met, [.lipsyncFormat])
        XCTAssertEqual(WhatsNewAvailability(capabilities: CreationCapabilities(formats: [montage]), memory: nil).met, [])
        XCTAssertEqual(WhatsNewAvailability(capabilities: CreationCapabilities(formats: [], mediaCollections: true), memory: nil).met, [.cameraRoll])
        XCTAssertEqual(WhatsNewAvailability(capabilities: CreationCapabilities(formats: [], mediaCollections: false, clipSelectionQuestions: true), memory: nil).met,
                       [.clipSelectionQuestions])

        let everything = WhatsNewAvailability(capabilities: CreationCapabilities(formats: [lipsync], mediaCollections: true, clipSelectionQuestions: true),
                                              memory: try memory(featureAvailable: true, textPreferencesAvailable: true))
        XCTAssertEqual(everything.met, Set(WhatsNewRequirement.allCases))
    }

    func testTextPreferencesNeedBothTheFeatureAndTheTextStylingFlag() throws {
        let cases: [(feature: Bool, styling: Bool?, expected: Bool)] = [(true, true, true), (true, false, false), (true, nil, false), (false, true, false)]
        for entry in cases {
            let met = WhatsNewAvailability(capabilities: nil,
                                           memory: try memory(featureAvailable: entry.feature, textPreferencesAvailable: entry.styling)).met
            XCTAssertEqual(met.contains(.textPreferences), entry.expected, "feature \(entry.feature), styling \(String(describing: entry.styling))")
        }
    }

    func testATryItIsOnlyOfferedWhenTheAccountCanOpenItsDestination() {
        let withTryIt = WhatsNewReleases.all.flatMap(\.features).filter { $0.tryIt != nil }
        XCTAssertFalse(withTryIt.isEmpty)
        let everythingButText = WhatsNewAvailability(met: Set(WhatsNewRequirement.allCases).subtracting([.textPreferences]))
        let shown = everythingButText.visible(WhatsNewReleases.all).flatMap(\.features)
        XCTAssertTrue(shown.allSatisfy { $0.tryIt == nil }, "Text preferences is not available, so no card may send the creator there")
        let allShown = everyRequirementMet.visible(WhatsNewReleases.all).flatMap(\.features)
        XCTAssertEqual(allShown.filter { $0.tryIt != nil }.count, withTryIt.count)
    }

    func testCatalogDaysAreRealUniqueDates() {
        let days = WhatsNewReleases.all.map(\.day)
        XCTAssertEqual(Set(days).count, days.count, "Two releases on one day would render as duplicate headings")
        for day in days { XCTAssertNotNil(day.date, "\(day.id) is not a calendar date") }
        XCTAssertFalse(days.isEmpty)
    }

    func testCatalogFeatureIDsAreUnique() {
        let ids = WhatsNewReleases.all.flatMap(\.features).map(\.id)
        XCTAssertEqual(Set(ids).count, ids.count)
    }

    func testNoInternalIdentifierReachesTheReader() throws {
        let forbidden = try [#"KRI-\d+"#, #"#\d{3,}"#, #"[A-Z]{3,}_[A-Z_]+"#, #"\bPR\b"#, #"\bprotocol\b"#].map { try NSRegularExpression(pattern: $0) }
        for string in everyUserString() {
            let range = NSRange(string.startIndex..., in: string)
            for pattern in forbidden {
                XCTAssertNil(pattern.firstMatch(in: string, range: range), "\"\(string)\" leaks an internal identifier (\(pattern.pattern))")
            }
        }
    }

    func testCopyStaysShort() {
        for feature in WhatsNewReleases.all.flatMap(\.features) {
            XCTAssertLessThanOrEqual(sentenceCount(text(feature.benefit)), 2, "\(feature.id) benefit")
            XCTAssertLessThanOrEqual(feature.steps.count, 4, "\(feature.id) steps")
            XCTAssertTrue(feature.steps.allSatisfy { !text($0).isEmpty })
            XCTAssertFalse(text(feature.title).isEmpty)
        }
        for fix in WhatsNewReleases.all.flatMap(\.fixes) {
            XCTAssertLessThanOrEqual(sentenceCount(text(fix.text)), 2)
        }
    }

    func testEveryCardSymbolExists() {
        for feature in WhatsNewReleases.all.flatMap(\.features) {
            XCTAssertNotNil(UIImage(systemName: feature.symbol), "\(feature.id) uses a symbol that does not exist")
        }
    }

    func testLongDateNamesTheDayInTheReadersLanguage() throws {
        let day = WhatsNewDay(2026, 11, 1)
        let en = day.longDate(locale: Locale(identifier: "en_US"))
        XCTAssertTrue(en.contains("November") && en.contains("1") && en.contains("2026"), en)
        let tr = day.longDate(locale: Locale(identifier: "tr_TR"))
        XCTAssertTrue(tr.contains("Kasım"), tr)
    }

    private func memory(featureAvailable: Bool, textPreferencesAvailable: Bool?) throws -> TextPreferenceMemory {
        var body: [String: Any] = ["feature_available": featureAvailable, "enabled": true, "revision": 1, "active": [], "suggestions": []]
        if let textPreferencesAvailable { body["text_preferences_available"] = textPreferencesAvailable }
        return try JSONDecoder().decode(TextPreferenceMemory.self, from: JSONSerialization.data(withJSONObject: body))
    }

    private func everyUserString() -> [String] {
        WhatsNewReleases.all.flatMap { release -> [String] in
            release.features.flatMap { [text($0.title), text($0.benefit)] + $0.steps.map(text) } + release.fixes.map { text($0.text) }
        }
    }

    private func sentenceCount(_ string: String) -> Int {
        string.split(whereSeparator: { ".!?".contains($0) }).filter { !$0.allSatisfy(\.isWhitespace) }.count
    }
}
