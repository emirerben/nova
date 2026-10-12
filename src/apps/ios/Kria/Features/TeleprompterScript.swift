import Combine
import CoreGraphics
import Foundation

struct TeleprompterScriptDraft: Codable, Equatable, Sendable {
    var text: String
    var revision: UUID
    var anchorUTF16: Int
    var window: TeleprompterWindowPreference

    init(text: String = "", revision: UUID = UUID(), anchorUTF16: Int = 0,
         window: TeleprompterWindowPreference = .init()) {
        self.text = text
        self.revision = revision
        self.anchorUTF16 = max(0, min(anchorUTF16, text.utf16.count))
        self.window = window
    }

    private enum CodingKeys: String, CodingKey { case text, revision, anchorUTF16, window }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let text = try values.decode(String.self, forKey: .text)
        let revision = try values.decode(UUID.self, forKey: .revision)
        let anchor = try values.decode(Int.self, forKey: .anchorUTF16)
        let window = (try? values.decodeIfPresent(TeleprompterWindowPreference.self, forKey: .window)) ?? .init()
        self.init(text: text, revision: revision, anchorUTF16: anchor, window: window)
    }
}

enum TeleprompterPlaybackState: Equatable, Sendable { case paused, playing, ended }

struct TeleprompterVisualLine: Equatable, Sendable, Identifiable {
    let range: Range<Int>
    let anchor: CGFloat
    let text: String

    var id: Int { range.lowerBound }
}

/// Playback is intentionally independent of camera capture. Its clock uses uptime
/// rather than display frames, so changing refresh rate cannot change reading pace.
@MainActor
final class TeleprompterScript: ObservableObject {
    static let defaultWordsPerMinute = 140
    static let defaultFontSize: CGFloat = 22

    @Published private(set) var text: String
    @Published private(set) var revision: UUID
    @Published private(set) var anchorUTF16: Int
    @Published private(set) var playbackState: TeleprompterPlaybackState = .paused
    @Published private(set) var scrollPosition: CGFloat = 0
    @Published private(set) var visualLines: [TeleprompterVisualLine] = []
    @Published private(set) var travelDistance: CGFloat = 0
    @Published private(set) var wordsPerMinute: Int
    @Published private(set) var fontSize: CGFloat
    @Published private(set) var windowPreference: TeleprompterWindowPreference

    private let uptime: () -> TimeInterval
    private var lastTick: TimeInterval?
    private var elapsedPlayback: TimeInterval = 0

    init(draft: TeleprompterScriptDraft = .init(), wordsPerMinute: Int? = nil,
         fontSize: CGFloat? = nil,
         uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        text = draft.text
        revision = draft.revision
        anchorUTF16 = max(0, min(draft.anchorUTF16, draft.text.utf16.count))
        self.wordsPerMinute = Self.normalizedPace(wordsPerMinute ?? Self.preferredWordsPerMinute)
        self.fontSize = max(12, fontSize ?? Self.preferredFontSize)
        windowPreference = draft.window
        self.uptime = uptime
    }

    convenience init(text: String, wordsPerMinute: Int? = nil,
                     fontSize: CGFloat? = nil,
                     uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.init(draft: .init(text: text), wordsPerMinute: wordsPerMinute, fontSize: fontSize, uptime: uptime)
    }

    var draft: TeleprompterScriptDraft {
        get { .init(text: text, revision: revision, anchorUTF16: anchorUTF16, window: windowPreference) }
        set { replaceDraft(newValue) }
    }
    var isPlaying: Bool { playbackState == .playing }
    var hasEnded: Bool { playbackState == .ended }
    var readingUnitCount: Int { Self.readingUnitCount(in: text) }
    var estimatedReadTime: TimeInterval { 60 * Double(max(readingUnitCount, 1)) / Double(wordsPerMinute) }
    /// UI-facing approximate full-script time, rounded up as promised to creators.
    var readTime: TimeInterval { ceil(estimatedReadTime) }
    var pointVelocity: CGFloat { guard travelDistance > 0 else { return 0 }; return travelDistance / estimatedReadTime }

    func updateText(_ newText: String) {
        guard newText != text else { return }
        text = newText
        revision = UUID()
        anchorUTF16 = 0
        scrollPosition = 0
        visualLines = []
        travelDistance = 0
        playbackState = .paused
        lastTick = nil
        elapsedPlayback = 0
    }

    func setWordsPerMinute(_ value: Int) {
        advanceToNow()
        wordsPerMinute = Self.normalizedPace(value)
        UserDefaults.standard.set(wordsPerMinute, forKey: Self.wordsPerMinuteKey)
    }

    func setFontSize(_ value: CGFloat) {
        fontSize = max(12, value)
        UserDefaults.standard.set(Double(fontSize), forKey: Self.fontSizeKey)
    }

    func setWindowPreference(_ value: TeleprompterWindowPreference) {
        windowPreference = .init(appearance: value.appearance, normalizedX: value.normalizedX, normalizedY: value.normalizedY,
                                 widthFraction: value.widthFraction, preferredHeight: value.preferredHeight)
    }

    /// Called after TextKit lays out the reading window. Reflow chooses the
    /// line containing the persisted UTF-16 anchor instead of preserving pixels.
    func setLayout(lines: [TeleprompterVisualLine], travelDistance: CGFloat) {
        advanceToNow()
        visualLines = lines
        self.travelDistance = max(0, travelDistance)
        guard !lines.isEmpty else { scrollPosition = 0; return }
        let line = line(containing: anchorUTF16) ?? lines[0]
        scrollPosition = min(max(0, line.anchor), self.travelDistance)
        if playbackState == .ended, scrollPosition < self.travelDistance { playbackState = .paused }
    }

    func resume() {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        if hasEnded || (travelDistance > 0 && scrollPosition >= travelDistance) { reset() }
        playbackState = .playing
        lastTick = uptime()
    }

    func pause() {
        advanceToNow()
        // Keep end-of-script intent through countdown/lifecycle pauses so a
        // one-line script gets its full reading time when resumed for a take.
        if playbackState != .ended { playbackState = .paused }
        lastTick = nil
    }

    func reset() {
        scrollPosition = 0
        anchorUTF16 = 0
        playbackState = .paused
        lastTick = nil
        elapsedPlayback = 0
    }

    func tick() { advanceToNow() }

    func setScrollPosition(_ value: CGFloat) {
        pause()
        scrollPosition = min(max(0, value), travelDistance)
        guard let nearest = visualLines.min(by: { abs($0.anchor - scrollPosition) < abs($1.anchor - scrollPosition) }) else { return }
        anchorUTF16 = nearest.range.lowerBound
        if scrollPosition < travelDistance { playbackState = .paused }
    }

    func backLine() { moveLine(by: -1) }
    func nextLine() { moveLine(by: 1) }

    private func replaceDraft(_ draft: TeleprompterScriptDraft) {
        text = draft.text
        revision = draft.revision
        anchorUTF16 = max(0, min(draft.anchorUTF16, draft.text.utf16.count))
        windowPreference = draft.window
        scrollPosition = 0
        visualLines = []
        travelDistance = 0
        playbackState = .paused
        lastTick = nil
        elapsedPlayback = 0
    }

    private func moveLine(by delta: Int) {
        pause()
        guard let current = TeleprompterVisualLine.index(containing: anchorUTF16, in: visualLines) else { return }
        let target = min(max(0, current + delta), visualLines.count - 1)
        guard target != current else { return }
        anchorUTF16 = visualLines[target].range.lowerBound
        scrollPosition = min(visualLines[target].anchor, travelDistance)
        playbackState = .paused
    }

    private func advanceToNow() {
        guard playbackState == .playing else { return }
        let now = uptime()
        defer { if playbackState == .playing { lastTick = now } }
        guard let lastTick else { return }
        let elapsed = max(0, now - lastTick)
        elapsedPlayback += elapsed
        guard travelDistance > 0 else {
            // A one-line script remains readable for its planned duration.
            if elapsedPlayback >= estimatedReadTime { playbackState = .ended; self.lastTick = nil }
            return
        }
        scrollPosition = min(travelDistance, scrollPosition + pointVelocity * elapsed)
        if scrollPosition >= travelDistance {
            anchorUTF16 = visualLines.last?.range.lowerBound ?? anchorUTF16
            playbackState = .ended
            self.lastTick = nil
        } else if let line = visualLines.min(by: { abs($0.anchor - scrollPosition) < abs($1.anchor - scrollPosition) }) {
            anchorUTF16 = line.range.lowerBound
        }
    }

    private func line(containing offset: Int) -> TeleprompterVisualLine? {
        guard let index = TeleprompterVisualLine.index(containing: offset, in: visualLines) else { return nil }
        return visualLines[index]
    }

    private static func normalizedPace(_ value: Int) -> Int {
        let clamped = min(240, max(60, value))
        return Int((Double(clamped) / 5).rounded()) * 5
    }

    private static let wordsPerMinuteKey = "kria.teleprompter.words-per-minute"
    private static let fontSizeKey = "kria.teleprompter.font-size"
    private static var preferredWordsPerMinute: Int {
        let stored = UserDefaults.standard.integer(forKey: wordsPerMinuteKey)
        return stored == 0 ? defaultWordsPerMinute : stored
    }
    private static var preferredFontSize: CGFloat {
        let stored = UserDefaults.standard.double(forKey: fontSizeKey)
        return stored == 0 ? defaultFontSize : CGFloat(stored)
    }

    static func readingUnitCount(in text: String) -> Int {
        text.split(whereSeparator: { $0.isWhitespace }).count
    }
}

extension TeleprompterVisualLine {
    /// A shared boundary is the start of the following visual line. Only the
    /// terminal end of the whole text falls back to its preceding final line.
    static func index(containing offset: Int, in lines: [Self]) -> Int? {
        if let index = lines.firstIndex(where: { $0.range.contains(offset) }) { return index }
        return lines.lastIndex(where: { $0.range.upperBound == offset })
    }
}
