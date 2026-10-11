import SwiftUI

// KRI-588: "Which part of the song did you film?" asked in the chat. The creator answers with From / To wheels
// ("Use this part") or "Use the whole song". Same family as the take-order card; the wheels are the ones from the
// Add media sheet (KRI-580), so the same time math and the same five-second rule apply.

/// How a song-range question is shown under its message.
enum SongRangeCardMode {
    /// The newest unanswered question: interactive. `problem` is the server's reason for refusing the last range.
    /// `submit` receives the structured answer and its readable chat message.
    case active(isSending: Bool, problem: String?, submit: (SongRangeAnswer, String) -> Void)
    /// Answered by a `song_range` message: one read-only line.
    case answered(summary: String, spoken: String)
}

struct SongRangeQuestionCard: View {
    let question: SongRangeQuestion
    /// The song's length from the thread, for a server that does not put it on the question.
    var fallbackDurationS: Double? = nil
    let mode: SongRangeCardMode

    var body: some View {
        switch mode {
        case .answered(let summary, let spoken):
            SongRangeAnsweredLine(summary: summary, spoken: spoken)
        case .active(let isSending, let problem, let submit):
            SongRangeActiveCard(question: question, fallbackDurationS: fallbackDurationS,
                                isSending: isSending, problem: problem, submit: submit)
        }
    }
}

private struct SongRangeAnsweredLine: View {
    let summary: String
    let spoken: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                .accessibilityHidden(true)
            Text(summary)
                .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .accessibilityIdentifier("song-range-card-answered")
    }
}

private struct SongRangeActiveCard: View {
    let question: SongRangeQuestion
    let isSending: Bool
    let problem: String?
    let submit: (SongRangeAnswer, String) -> Void
    @State private var draft: SongRangeDraft?
    /// The server's reason is only true for the range that was sent: any change to the wheels hides it.
    @State private var problemSeen = false

    init(question: SongRangeQuestion, fallbackDurationS: Double?, isSending: Bool, problem: String?,
         submit: @escaping (SongRangeAnswer, String) -> Void) {
        self.question = question
        self.isSending = isSending
        self.problem = problem
        self.submit = submit
        _draft = State(initialValue: question.draft(fallbackDurationS: fallbackDurationS))
    }

    private var fromBinding: Binding<Int> {
        Binding(get: { draft?.from ?? 0 }, set: { value in
            draft?.setFrom(value)
            problemSeen = true
        })
    }

    private var toBinding: Binding<Int> {
        Binding(get: { draft?.to ?? 0 }, set: { value in
            draft?.setTo(value)
            problemSeen = true
        })
    }

    private var shownMessage: String? {
        if let draftMessage = draft?.message { return draftMessage }
        return problemSeen ? nil : problem
    }

    private var chosenRange: SongFilmingRange? { draft?.range }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let draft {
                SongRangeWheelPair(draft: draft, from: fromBinding, to: toBinding,
                                   fromID: "song-range-card-from", toID: "song-range-card-to", isEnabled: !isSending)
            }
            messageLine
            buttons
        }
        .padding(14)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .excludesDrawerGesture()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-range-card")
        .onChange(of: problem) { _, _ in problemSeen = false }
    }

    @ViewBuilder private var messageLine: some View {
        if let shownMessage {
            Text(shownMessage)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("song-range-card-error")
        }
    }

    private var buttons: some View {
        VStack(spacing: 8) {
            if draft != nil {
                Button("Use this part", action: sendPart)
                    .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
                    .frame(maxWidth: .infinity)
                    .disabled(isSending || chosenRange == nil)
                    .accessibilityIdentifier("song-range-card-use")
            }
            Button("Use the whole song", action: sendWholeSong)
                .buttonStyle(SongRangeFillButtonStyle())
                .disabled(isSending)
                .accessibilityIdentifier("song-range-card-whole")
        }
    }

    private func sendPart() {
        guard !isSending, let range = chosenRange else { return }
        let answer = SongRangeAnswer.part(questionID: question.questionID, range: range)
        submit(answer, answer.message)
    }

    private func sendWholeSong() {
        guard !isSending else { return }
        let answer = SongRangeAnswer.wholeSong(questionID: question.questionID)
        submit(answer, answer.message)
    }
}
