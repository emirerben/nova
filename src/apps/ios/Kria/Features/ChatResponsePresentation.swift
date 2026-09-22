import Combine
import Foundation
import SwiftUI

/// Coordinates the one-shot presentation effects for responses received by a chat.
///
/// The model is deliberately independent of a particular view. A workspace and its
/// editor can therefore ask for the same response's start time without replaying the
/// reveal when either view appears.
@MainActor
final class ChatResponsePresentation: ObservableObject {
    @Published private(set) var hapticToken = 0

    private var trackedEventIDs = Set<String>()
    private var responseStartTimes: [String: Date] = [:]
    private let now: () -> Date

    init(now: @escaping () -> Date = Date.init) {
        self.now = now
    }

    /// Observes the current event projection. The first projection is always a
    /// baseline; only events first seen by a later projection can produce effects.
    func observe(events: [ThreadEvent], isInitialLoad: Bool, isActive: Bool) {
        if isInitialLoad {
            trackedEventIDs.formUnion(events.map(\.id))
            return
        }

        var newAssistantIDs: [String] = []
        for event in events where trackedEventIDs.insert(event.id).inserted {
            guard ChatTranscriptMessage.from(event: event)?.role == .assistant else { continue }
            newAssistantIDs.append(event.id)
        }

        // Background polling can update the projection, but it must not create a
        // reveal or haptic pulse that the user encounters later on foregrounding.
        guard isActive else { return }
        let startedAt = now()
        for id in newAssistantIDs {
            responseStartTimes[id] = startedAt
        }
        if !newAssistantIDs.isEmpty {
            hapticToken += 1
        }
    }

    func startTime(for id: String) -> Date? {
        responseStartTimes[id]
    }
}

struct ChatResponseText: View {
    let content: String
    let startedAt: Date?

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @State private var revealedWordCount = 0

    var body: some View {
        let tokens = Self.tokenize(content)
        let shouldReveal = startedAt != nil && !reduceMotion && !voiceOverEnabled
        let revealed = shouldReveal ? revealedWordCount : tokens.wordCount
        Self.attributedText(tokens: tokens, revealedWordCount: revealed)
            .accessibilityElement()
            .accessibilityAddTraits(.isStaticText)
            .accessibilityLabel(content)
            .task(id: startedAt) {
                await reveal(tokens: tokens, startedAt: startedAt, enabled: shouldReveal)
            }
            .onChange(of: reduceMotion) { _, _ in
                if reduceMotion { revealedWordCount = tokens.wordCount }
            }
            .onChange(of: voiceOverEnabled) { _, enabled in
                if enabled { revealedWordCount = tokens.wordCount }
            }
    }

    private func reveal(tokens: TokenizedText, startedAt: Date?, enabled: Bool) async {
        guard enabled, tokens.wordCount > 0 else {
            revealedWordCount = tokens.wordCount
            return
        }

        // Start from the shared response clock so mounting a second surface does
        // not replay a response that is already partly or fully revealed.
        let elapsed = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        revealedWordCount = Self.revealCount(elapsed: elapsed, wordCount: tokens.wordCount)
        let interval = Self.presentationInterval(wordCount: tokens.wordCount)
        while revealedWordCount < tokens.wordCount {
            do {
                try await Task.sleep(for: .seconds(interval))
            } catch {
                return
            }
            guard !Task.isCancelled else { return }
            // Re-read the shared clock after suspension. This catches up after
            // backgrounding and prevents a suspended task from extending the
            // reveal beyond its two-second cap.
            let elapsed = startedAt.map { Date().timeIntervalSince($0) } ?? 0
            revealedWordCount = Self.revealCount(elapsed: elapsed, wordCount: tokens.wordCount)
        }
    }

    static func presentationInterval(wordCount: Int) -> TimeInterval {
        guard wordCount > 0 else { return 0 }
        return min(0.035, 2.0 / Double(wordCount))
    }

    static func revealCount(elapsed: TimeInterval, wordCount: Int) -> Int {
        guard wordCount > 0, elapsed > 0 else { return 0 }
        let interval = presentationInterval(wordCount: wordCount)
        return min(wordCount, max(0, Int(elapsed / interval)))
    }

    struct TokenizedText: Equatable {
        let tokens: [Token]
        let wordCount: Int
    }

    struct Token: Equatable {
        let text: String
        let isWord: Bool
    }

    static func tokenize(_ content: String) -> TokenizedText {
        var tokens: [Token] = []
        var current = ""
        var currentIsWord: Bool?

        func flush() {
            guard !current.isEmpty, let currentIsWord else { return }
            tokens.append(Token(text: current, isWord: currentIsWord))
            current = ""
        }

        for character in content {
            // Keep punctuation, emoji, and combining marks with their adjacent
            // word so the unrevealed surface is entirely hidden while whitespace
            // remains in place to reserve the final layout.
            let isWord = !character.isWhitespace
            if currentIsWord != isWord {
                flush()
                currentIsWord = isWord
            }
            current.append(character)
        }
        flush()
        return TokenizedText(tokens: tokens, wordCount: tokens.reduce(into: 0) { count, token in
            if token.isWord { count += 1 }
        })
    }

    private static func attributedText(tokens: TokenizedText, revealedWordCount: Int) -> Text {
        var result = AttributedString()
        var seenWords = 0
        for token in tokens.tokens {
            var part = AttributedString(token.text)
            if token.isWord {
                seenWords += 1
                if seenWords > revealedWordCount {
                    part.foregroundColor = .clear
                }
            }
            result.append(part)
        }
        return Text(result)
    }
}
