import AVFoundation
import SwiftUI

// KRI-723: "put your moments in order and show me where each one is". The rows come in Kria's order (the
// creator's words); the arrows reorder them. Each row takes one of three answers: "Show where" opens the KRI-717
// source-window picker on this iPhone's original of the take, "Not in my clips" leaves the moment out, "You pick"
// lets Kria choose a part. A moment the clip record placed starts pointed at its evidence.

/// How a moment question is shown under its message.
enum MomentOrderCardMode {
    /// The newest unanswered question: interactive. `problem` is the server's reason for refusing the last answer.
    case active(isSending: Bool, problem: String?, submit: (MomentOrderAnswer) -> Void)
    /// Answered by a `moment_order` message: one read-only line.
    case answered(summary: String)
}

/// Finds the take's original on this iPhone (phone projects keep originals in the project directory).
@MainActor enum MomentOrderSource {
    static func localURL(projectID: UUID?, mediaID: String) -> URL? {
        #if DEBUG
        if let fixture = MomentOrderUITestFixture.clipURL(mediaID: mediaID) { return fixture }
        #endif
        return ObjectMarkSource.localURL(projectID: projectID, mediaID: mediaID)
    }
}

struct MomentOrderCard: View {
    let question: MomentOrderQuestion
    var projectID: UUID? = nil
    var language: ClipAnswerLanguage = .english
    let mode: MomentOrderCardMode

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                    .accessibilityHidden(true)
                Text(summary)
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(summary)
            .accessibilityIdentifier("moment-order-answered")
        case .active(let isSending, let problem, let submit):
            MomentOrderActiveCard(question: question, projectID: projectID, strings: MomentOrderStrings(language),
                                  isSending: isSending, problem: problem, submit: submit)
        }
    }
}

private struct MomentOrderActiveCard: View {
    let question: MomentOrderQuestion
    let projectID: UUID?
    let strings: MomentOrderStrings
    let isSending: Bool
    let problem: String?
    let submit: (MomentOrderAnswer) -> Void

    @State private var draft: MomentOrderDraft
    /// The moment whose window picker is open.
    @State private var picking: PickTarget?
    /// The server's reason is only true for the answer that was sent: any change hides it.
    @State private var problemSeen = false

    struct PickTarget: Identifiable { let key: String; var id: String { key } }

    init(question: MomentOrderQuestion, projectID: UUID?, strings: MomentOrderStrings, isSending: Bool,
         problem: String?, submit: @escaping (MomentOrderAnswer) -> Void) {
        self.question = question
        self.projectID = projectID
        self.strings = strings
        self.isSending = isSending
        self.problem = problem
        self.submit = submit
        _draft = State(initialValue: MomentOrderDraft(question: question))
    }

    private var localURL: URL? { MomentOrderSource.localURL(projectID: projectID, mediaID: question.mediaID) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(strings.title)
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                Text(strings.hint)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 8) {
                ForEach(draft.order, id: \.self) { key in
                    if let moment = question.moment(key) { row(moment) }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityValue(draft.order.joined(separator: ","))
            .accessibilityIdentifier("moment-order-list")
            if localURL == nil {
                Text(strings.notOnThisPhone)
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("moment-order-missing")
            }
            footer
        }
        .padding(14)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .excludesDrawerGesture()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("moment-order-card")
        .onChange(of: problem) { _, _ in problemSeen = false }
        .onChange(of: draft) { _, _ in problemSeen = true }
        .sheet(item: $picking) { target in
            if let url = localURL {
                MomentWindowSheet(url: url, takeDuration: question.durationS, strings: strings,
                                  initial: question.initialWindow(for: target.key, current: pointed(target.key))) { window in
                    draft.choose(.point(question.clamped(window)), for: target.key)
                }
            }
        }
    }

    private func pointed(_ key: String) -> MomentWindow? {
        if case .point(let window)? = draft.choice(for: key) { return window }
        return nil
    }

    private func row(_ moment: MomentOrderQuestion.Moment) -> some View {
        let key = moment.key
        let position = draft.position(of: key) ?? 0
        let choice = draft.choice(for: key)
        let overlaps = draft.overlapping.contains(key)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                Text("\(position)")
                    .font(KriaFont.body(13).weight(.bold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 24, height: 24).background(KriaColor.butter, in: Circle())
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(moment.words)
                        .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(overlaps ? strings.overlap : strings.status(moment, choice: choice))
                        .font(KriaFont.body(12))
                        .foregroundStyle(overlaps || choice == nil ? KriaColor.failureText : KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("moment-order-status-\(key)")
                }
                .accessibilityElement(children: .combine)
                .accessibilityValue(pointed(key)?.exact ?? "")
                .accessibilityIdentifier("moment-order-row-\(key)")
                .accessibilityAction(named: strings.moveUp(moment.words)) { if !isSending { draft.moveUp(key) } }
                .accessibilityAction(named: strings.moveDown(moment.words)) { if !isSending { draft.moveDown(key) } }
                Spacer(minLength: 0)
                HStack(spacing: 0) {
                    arrow("chevron.up", label: strings.moveUp(moment.words), id: "moment-order-up-\(key)",
                          enabled: draft.canMoveUp(key) && !isSending) { draft.moveUp(key) }
                    arrow("chevron.down", label: strings.moveDown(moment.words), id: "moment-order-down-\(key)",
                          enabled: draft.canMoveDown(key) && !isSending) { draft.moveDown(key) }
                }
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    chip(strings.point, selected: pointed(key) != nil, id: "moment-order-point-\(key)",
                         enabled: localURL != nil || pointed(key) != nil) {
                        if localURL != nil { picking = PickTarget(key: key) }
                    }
                    chip(strings.skip, selected: choice == .skip, id: "moment-order-skip-\(key)", enabled: true) {
                        draft.choose(.skip, for: key)
                    }
                    chip(strings.delegate, selected: choice == .delegate, id: "moment-order-delegate-\(key)", enabled: true) {
                        draft.choose(.delegate, for: key)
                    }
                }
            }
        }
        .padding(10)
        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    /// The capsule chip of the existing pickers: ink fill when chosen, a soft fill otherwise (no borders).
    private func chip(_ title: String, selected: Bool, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(KriaFont.body(13).weight(.semibold))
            .foregroundStyle(selected ? Color.white : KriaColor.ink)
            .padding(.horizontal, 14)
            .frame(minHeight: 44)
            .background(selected ? KriaColor.ink : KriaColor.fill, in: Capsule())
            .opacity(enabled ? 1 : 0.45)
            .accessibilityAddTraits(selected ? .isSelected : [])
            .accessibilityIdentifier(id)
            .disabled(isSending || !enabled)
    }

    private func arrow(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 14, weight: .semibold))
                .frame(width: 44, height: 44).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(enabled ? KriaColor.ink : KriaColor.line)
        .disabled(!enabled)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
    }

    private var footer: some View {
        let blocked = !draft.isComplete || !draft.overlapping.isEmpty
        return VStack(alignment: .leading, spacing: 8) {
            if let problem, !problemSeen {
                Text(problem)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("moment-order-error")
            } else if !draft.isComplete {
                Text(strings.chooseEach)
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .accessibilityIdentifier("moment-order-incomplete")
            }
            Button(strings.send) {
                if let answer = draft.answer(question: question) { submit(answer) }
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
            .frame(maxWidth: .infinity)
            .disabled(isSending || blocked)
            .accessibilityIdentifier("moment-order-send")
        }
    }
}

/// The KRI-717 Range picker, aimed at one moment of the take. The window is on the original's clock (rate 1).
private struct MomentWindowSheet: View {
    let url: URL
    let takeDuration: Double
    let strings: MomentOrderStrings
    let initial: MomentWindow
    let onDone: (MomentWindow) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var window: KriaSourceWindowGeometry.Window?
    @State private var sourceDuration: Double?
    @State private var triedLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if let sourceDuration, let window {
                    KriaSourceWindowEditor(url: url, rate: 1, sourceDuration: sourceDuration, committed: window,
                                           onCommit: { next, _ in self.window = next })
                } else if triedLoading {
                    Text(strings.couldNotLoad)
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .padding(16)
            .background(KriaColor.paper)
            .navigationTitle(strings.sheetTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(strings.cancel) { dismiss() }.accessibilityIdentifier("moment-order-window-cancel")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(strings.done) {
                        if let window { onDone(MomentWindow(start: window.start, end: window.start + window.length)) }
                        dismiss()
                    }
                    .disabled(window == nil)
                    .accessibilityIdentifier("moment-order-window-done")
                }
            }
        }
        .tint(KriaColor.ink)
        .presentationDetents([.large])
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("moment-order-window-sheet")
        .task {
            // The proxy on this iPhone may run a frame past (or short of) the original: never offer more than both.
            let loaded = await KriaSourceFilmstripLoader.shared.duration(of: url)
            triedLoading = true
            guard let loaded else { return }
            let duration = min(loaded, takeDuration)
            let start = min(max(0, initial.start), max(0, duration - KriaSourceWindowGeometry.minimumLength))
            let end = min(max(initial.end, start + KriaSourceWindowGeometry.minimumLength), duration)
            sourceDuration = duration
            window = KriaSourceWindowGeometry.Window(start: start, length: max(KriaSourceWindowGeometry.minimumLength, end - start))
        }
    }
}

#if DEBUG
/// `KRIA_CHAT_MOMENT_ORDER`: the take (`fixture-clip`) has a local original, a generated 8 s clip.
enum MomentOrderUITestFixture {
    @MainActor static func clipURL(mediaID: String) -> URL? {
        guard ProcessInfo.processInfo.environment["KRIA_CHAT_MOMENT_ORDER"] != nil, mediaID == "fixture-clip" else { return nil }
        return SongTimelineFixture.clipURL(mediaID: mediaID, force: true)
    }
}
#endif
