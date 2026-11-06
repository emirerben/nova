import Foundation

/// What an account must be able to do before the guide mentions a feature. A card for something this account
/// cannot open would be a promise the app breaks, so every gate reads the same capability the feature itself uses.
enum WhatsNewRequirement: Hashable, CaseIterable {
    case lipsyncFormat
    case textPreferences
    case clipSelectionQuestions
    case cameraRoll
}

/// Screens a card may push. Only places that are safe to land on with no setup, so "Try it" never creates a
/// project or starts an upload.
enum WhatsNewDestination: Hashable {
    case textPreferences

    var actionTitle: LocalizedStringResource {
        switch self {
        case .textPreferences: "Try it in Text preferences"
        }
    }
}

struct WhatsNewDay: Hashable, Comparable, Sendable {
    let year: Int
    let month: Int
    let day: Int

    init(_ year: Int, _ month: Int, _ day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    var id: String { String(format: "%04d-%02d-%02d", year, month, day) }

    /// Built in the reader's calendar and time zone, so the printed day is the one in the changelog everywhere.
    var date: Date? { Calendar.current.date(from: DateComponents(year: year, month: month, day: day)) }

    func longDate(locale: Locale = .current) -> String {
        guard let date else { return id }
        var calendar = Calendar.current
        calendar.locale = locale
        let style = Date.FormatStyle(date: .long, time: .omitted, locale: locale, calendar: calendar, timeZone: calendar.timeZone)
        return date.formatted(style)
    }

    static func < (lhs: WhatsNewDay, rhs: WhatsNewDay) -> Bool {
        (lhs.year, lhs.month, lhs.day) < (rhs.year, rhs.month, rhs.day)
    }
}

struct WhatsNewFeature: Identifiable {
    let id: String
    let symbol: String
    let title: LocalizedStringResource
    let benefit: LocalizedStringResource
    let steps: [LocalizedStringResource]
    let requires: WhatsNewRequirement?
    let tryIt: WhatsNewDestination?
}

struct WhatsNewFix {
    let text: LocalizedStringResource
    let requires: WhatsNewRequirement?
}

struct WhatsNewRelease: Identifiable {
    let day: WhatsNewDay
    let features: [WhatsNewFeature]
    let fixes: [WhatsNewFix]
    var id: String { day.id }
}

struct WhatsNewAvailability: Equatable {
    let met: Set<WhatsNewRequirement>

    init(met: Set<WhatsNewRequirement>) { self.met = met }

    /// A nil input means that read failed or has not finished. Its gates stay closed, so the guide never
    /// advertises a feature it could not confirm.
    init(capabilities: CreationCapabilities?, memory: TextPreferenceMemory?) {
        var met: Set<WhatsNewRequirement> = []
        if let capabilities {
            if capabilities.formats.contains(where: { CreationFormat(serverValue: $0.id) == .lipsync }) { met.insert(.lipsyncFormat) }
            if capabilities.clipSelectionQuestionsEnabled { met.insert(.clipSelectionQuestions) }
            if capabilities.mediaCollections == true { met.insert(.cameraRoll) }
        }
        if memory?.textStylingAvailable == true { met.insert(.textPreferences) }
        self.met = met
    }

    /// Newest day first. Drops what this account cannot use, and drops a day left with nothing to show.
    func visible(_ releases: [WhatsNewRelease]) -> [WhatsNewRelease] {
        releases.sorted { $0.day > $1.day }.compactMap { release in
            let features = release.features.filter { allows($0.requires) }
            let fixes = release.fixes.filter { allows($0.requires) }
            guard !features.isEmpty || !fixes.isEmpty else { return nil }
            return WhatsNewRelease(day: release.day, features: features, fixes: fixes)
        }
    }

    private func allows(_ requirement: WhatsNewRequirement?) -> Bool {
        requirement.map(met.contains) ?? true
    }
}
