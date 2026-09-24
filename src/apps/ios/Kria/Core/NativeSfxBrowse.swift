import Foundation

/// Pure search + category grouping for the Effects tab (Sounds panel),
/// mirroring `src/apps/web/src/lib/sfx-browse.ts` so both platforms rank and
/// group the same KRI-173 library identically for the same query. Matching is
/// whole-word and case/accent-insensitive over `name` + `search_terms`, with
/// the same plural fold and filler-word list as the web port (and the API's
/// chat resolver, `app/services/sfx_catalog.py`'s `_stem`/`_FILLER`): "buzzer
/// sounds" finds "Wrong buzzer" but "tap" never finds "Tape rewind". A
/// half-typed last word matches word starts only when nothing matches whole.
enum NativeSfxBrowse {
    /// Display order: editing staples first, reactions next, niche last.
    /// Kept in step with the API's `SFX_CATEGORY_ORDER` by
    /// `tests/services/test_sfx_catalog_web_parity.py`.
    static let categoryOrder = [
        "transition", "impact", "comedy", "approval", "rejection", "suspense", "money", "sports", "ui",
    ]

    static let categoryLabels: [String: String] = [
        "transition": "Transitions", "impact": "Impacts", "comedy": "Comedy", "approval": "Approval",
        "rejection": "Rejection", "suspense": "Suspense", "money": "Money", "sports": "Sports",
        "ui": "Text & UI", "other": "Other",
    ]

    struct Group: Equatable {
        let key: String
        let label: String
        let effects: [NativeEditorSoundEffect]
    }

    /// Words that describe the request, not the sound ("a buzzer sound
    /// effect"). Same list as the API resolver's `_FILLER`.
    private static let queryFiller: Set<String> = [
        "a", "an", "the", "some", "any", "sound", "sounds", "effect", "effects", "sfx",
        "noise", "my", "this", "that", "it", "of", "to", "for", "with", "and", "please",
        "like", "kind", "type", "little", "quick", "one",
    ]

    /// Whole words of `text`: letters/digits in any script, lowercased and
    /// accent-folded. Locale-independent on purpose -- a Turkish "İmpact" or
    /// "ımpact" must still find "Impact", and "ui" must never fold to "uı".
    static func words(_ text: String?) -> [String] {
        guard let text, !text.isEmpty else { return [] }
        let folded = text
            .folding(options: [.diacriticInsensitive], locale: nil)
            .lowercased(with: Locale(identifier: "en_US_POSIX"))
            .replacingOccurrences(of: "ı", with: "i")
        var result: [String] = []
        var current = ""
        for scalar in folded.unicodeScalars {
            if CharacterSet.alphanumerics.contains(scalar) {
                current.unicodeScalars.append(scalar)
            } else if !current.isEmpty {
                result.append(current)
                current = ""
            }
        }
        if !current.isEmpty { result.append(current) }
        return result
    }

    private static func stem(_ word: String) -> String {
        word.count > 3 && word.hasSuffix("s") && !word.hasSuffix("ss") ? String(word.dropLast()) : word
    }

    /// Every form a word can match as: itself, the plural-folded stem, and
    /// "-es" off sh/ch/x/z/ss plurals ("punches" -> "punch"), which the plain
    /// stem misses. Both the query and the effect words expand this way, so
    /// either side may be plural.
    private static func wordForms(_ word: String) -> [String] {
        var forms = [word, stem(word)]
        if word.count > 4, ["sh", "ch", "x", "z", "ss"].contains(where: { word.hasSuffix($0 + "es") }) {
            forms.append(String(word.dropLast(2)))
        }
        return forms
    }

    private static func effectWords(_ effect: NativeEditorSoundEffect) -> Set<String> {
        var result: Set<String> = []
        for word in words(effect.name) + effect.searchTerms.flatMap({ words($0) }) {
            for form in wordForms(word) { result.insert(form) }
        }
        return result
    }

    private static func groupKey(_ category: String?) -> String {
        guard let category, categoryOrder.contains(category) else { return "other" }
        return category
    }

    /// True when any effect has a known category -- i.e. the library is not
    /// legacy-only, so the Effects tab should show group headings.
    static func hasCategories(_ effects: [NativeEditorSoundEffect]) -> Bool {
        effects.contains { groupKey($0.category) != "other" }
    }

    /// A matcher for `query`: every meaningful query word must be a whole
    /// word of the effect's name or search terms. Filler words are ignored
    /// unless the query is nothing but filler ("noise" still finds "White
    /// noise"). A query with no words at all matches everything. With
    /// `prefixLast`, the last word only has to start a word -- the fallback
    /// for a word the creator is still typing.
    private static func matcher(query: String, prefixLast: Bool = false) -> (NativeEditorSoundEffect) -> Bool {
        let allWords = words(query)
        let meaningful = allWords.filter { !queryFiller.contains($0) }
        let wanted = meaningful.isEmpty ? allWords : meaningful
        guard !wanted.isEmpty else { return { _ in true } }
        let whole = prefixLast ? Array(wanted.dropLast()) : wanted
        // The half-typed word is matched exactly as typed ("whis" never folds
        // to "whi" and pulls in "Whip").
        let partial = prefixLast ? wanted.last : nil
        return { effect in
            let have = effectWords(effect)
            guard whole.allSatisfy({ word in wordForms(word).contains(where: have.contains) }) else { return false }
            guard let partial else { return true }
            return have.contains { $0.hasPrefix(partial) }
        }
    }

    /// Filters `effects` by `query`, then groups them in `categoryOrder` with
    /// "other" (legacy/unknown category) last. Groups are A->Z and never
    /// empty. Whole-word matches win; only when there are none does the last
    /// word match as a word start. A half-typed filler word ("whoosh sou…")
    /// that empties the results is dropped, so the list doesn't blink out
    /// while typing it.
    static func groupEffects(_ effects: [NativeEditorSoundEffect], query: String = "") -> [Group] {
        var matched = effects.filter(matcher(query: query))
        if matched.isEmpty { matched = effects.filter(matcher(query: query, prefixLast: true)) }
        if matched.isEmpty {
            let queryWords = words(query)
            if let last = queryWords.last, queryWords.count > 1, queryFiller.contains(where: { $0.hasPrefix(last) }) {
                return groupEffects(effects, query: queryWords.dropLast().joined(separator: " "))
            }
        }
        var buckets: [String: [NativeEditorSoundEffect]] = [:]
        for effect in matched { buckets[groupKey(effect.category), default: []].append(effect) }
        return (categoryOrder + ["other"]).compactMap { key in
            guard let bucket = buckets[key] else { return nil }
            let sorted = bucket.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            return Group(key: key, label: categoryLabels[key] ?? key.capitalized, effects: sorted)
        }
    }
}
