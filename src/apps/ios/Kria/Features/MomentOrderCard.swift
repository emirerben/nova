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
    /// A newer question replaced this one before it was answered.
    case superseded
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
            KriaAnsweredReceipt(text: summary)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(summary)
                .accessibilityIdentifier("moment-order-answered")
        case .superseded:
            KriaSupersededLine().accessibilityIdentifier("moment-order-superseded")
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
    /// The row being dragged by its handle, how far it has moved, and each row's measured height.
    @State private var draggingKey: String?
    @State private var dragOffset: CGFloat = 0
    @State private var rowHeights: [String: CGFloat] = [:]
    /// A system-cancelled drag never reaches `onEnded`; without this the row stays lifted.
    @GestureState private var handleActive = false

    struct PickTarget: Identifiable { let key: String; var id: String { key } }

    private static let rowSpacing: CGFloat = 8

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
        KriaQuestionCard(title: strings.title, helper: strings.hint) {
            VStack(spacing: Self.rowSpacing) {
                ForEach(draft.order, id: \.self) { key in
                    if let moment = question.moment(key) { row(moment) }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityValue(draft.order.joined(separator: ","))
            .accessibilityIdentifier("moment-order-list")
            if localURL == nil {
                Text(strings.notOnThisPhone)
                    .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("moment-order-missing")
            }
            footer
        }
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
        let found = strings.isFound(moment, choice: choice)
        let dragging = draggingKey == key
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 0) {
                handle(key, words: moment.words)
                Text("\(position)")
                    .kriaType(.label).fontWeight(.semibold).foregroundStyle(KriaColor.ink)
                    // The digit stops growing at xxxLarge so it stays inside its 24pt disc.
                    .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
                    .frame(width: 24, height: 24).background(KriaColor.fill, in: Circle())
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text(moment.words)
                        .kriaType(.button).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        if found && !overlaps { KriaCheckDisc(size: 16) }
                        Text(overlaps ? strings.overlap : strings.status(moment, choice: choice))
                            .kriaType(.label)
                            .foregroundStyle(overlaps ? KriaColor.failureText : (choice == nil ? KriaColor.mutedInk : KriaColor.ink))
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("moment-order-status-\(key)")
                    }
                }
                .padding(.leading, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .combine)
                .accessibilityValue(pointed(key)?.exact ?? "")
                .accessibilityIdentifier("moment-order-row-\(key)")
            }
            options(for: key, choice: choice)
                .padding(.leading, 44)
        }
        .padding(.top, 4).padding(.trailing, 12).padding(.bottom, 12)
        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .onGeometryChange(for: CGFloat.self, of: { $0.size.height }) { rowHeights[key] = $0 }
        .offset(y: dragging ? dragOffset : 0)
        .zIndex(dragging ? 1 : 0)
        .shadow(color: .black.opacity(dragging ? 0.12 : 0), radius: 8, y: 2)
    }

    /// The drag handle: 44pt square. A drag moves the row past its neighbours; VoiceOver gets Move up / Move down.
    private func handle(_ key: String, words: String) -> some View {
        Image(systemName: "line.3.horizontal")
            .font(.system(size: 17, weight: .semibold)).foregroundStyle(KriaColor.zinc)
            .frame(width: 44, height: 44).contentShape(Rectangle())
            .highPriorityGesture(
                DragGesture(minimumDistance: 4, coordinateSpace: .global)
                    .updating($handleActive) { _, active, _ in active = true }
                    .onChanged { value in
                        guard !isSending else { return }
                        draggingKey = key
                        dragOffset = value.translation.height
                    }
                    .onEnded { value in
                        guard !isSending else { return }
                        settle(key, translation: value.translation.height)
                    }
            )
            .onChange(of: handleActive) { _, active in
                if !active, draggingKey == key {
                    withAnimation(.easeOut(duration: 0.18)) { draggingKey = nil; dragOffset = 0 }
                }
            }
            .accessibilityElement()
            .accessibilityLabel(strings.reorder(words))
            .accessibilityAction(named: strings.moveUp(words)) { if !isSending { moveUp(key) } }
            .accessibilityAction(named: strings.moveDown(words)) { if !isSending { moveDown(key) } }
            .accessibilityIdentifier("moment-order-handle-\(key)")
    }

    private func moveUp(_ key: String) { withAnimation(.easeOut(duration: 0.18)) { draft.moveUp(key) } }
    private func moveDown(_ key: String) { withAnimation(.easeOut(duration: 0.18)) { draft.moveDown(key) } }

    /// Moves the row one place at a time while the drag has covered more than half of the next row.
    private func settle(_ key: String, translation: CGFloat) {
        var remaining = translation
        withAnimation(.easeOut(duration: 0.18)) {
            while remaining > 0, let index = draft.order.firstIndex(of: key), index < draft.order.count - 1 {
                let next = rowHeights[draft.order[index + 1]] ?? 100
                guard remaining > next / 2 else { break }
                draft.moveDown(key)
                remaining -= next + Self.rowSpacing
            }
            while remaining < 0, let index = draft.order.firstIndex(of: key), index > 0 {
                let previous = rowHeights[draft.order[index - 1]] ?? 100
                guard -remaining > previous / 2 else { break }
                draft.moveUp(key)
                remaining += previous + Self.rowSpacing
            }
            draggingKey = nil
            dragOffset = 0
        }
    }

    /// The row's own three answers as wrapping chips (Paper S2.11): ink when chosen, fill otherwise. They sit on
    /// as many lines as the width needs and a long label wraps inside its chip, never inside a word.
    private func options(for key: String, choice: MomentChoice?) -> some View {
        KriaWrapLayout(spacing: 8) {
            segment(strings.point, selected: pointed(key) != nil, id: "moment-order-point-\(key)",
                    enabled: localURL != nil || pointed(key) != nil) {
                if localURL != nil { picking = PickTarget(key: key) }
            }
            segment(strings.skip, selected: choice == .skip, id: "moment-order-skip-\(key)", enabled: true) {
                draft.choose(.skip, for: key)
            }
            segment(strings.delegate, selected: choice == .delegate, id: "moment-order-delegate-\(key)", enabled: true) {
                draft.choose(.delegate, for: key)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
    }

    private func segment(_ title: String, selected: Bool, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .kriaType(.label).fontWeight(.semibold)
                .foregroundStyle(selected ? KriaColor.paper : KriaColor.ink)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 16).padding(.vertical, 8)
                .frame(minHeight: 44)
                .background(selected ? KriaColor.ink : KriaColor.fill, in: Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .opacity(enabled ? 1 : 0.4)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier(id)
        .disabled(isSending || !enabled)
    }

    private var footer: some View {
        let blocked = !draft.isComplete || !draft.overlapping.isEmpty
        let reason = (problem != nil && !problemSeen) ? nil : (!draft.isComplete ? strings.chooseEach : nil)
        return VStack(alignment: .leading, spacing: 8) {
            if let problem, !problemSeen {
                KriaInlineFailure(text: problem).accessibilityIdentifier("moment-order-error")
            }
            Button(strings.send) {
                if let answer = draft.answer(question: question) { submit(answer) }
            }
            .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, onFill: true, isLoading: isSending))
            .disabled(isSending || blocked)
            .kriaDisabledReason(reason)
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
        KriaSheetScaffold(heading: strings.sheetTitle, message: strings.dragHint,
                          closeIdentifier: "moment-order-window-cancel", onClose: { dismiss() }) {
            if let sourceDuration, let window {
                KriaSourceWindowEditor(url: url, rate: 1, sourceDuration: sourceDuration, committed: window,
                                       onCommit: { next, _ in self.window = next })
            } else if triedLoading {
                Text(strings.couldNotLoad)
                    .kriaType(.body).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 160)
            } else {
                KriaSpinner(size: 20).frame(maxWidth: .infinity, minHeight: 160)
            }
        } pinnedBar: {
            Button(strings.done) {
                if let window { onDone(MomentWindow(start: window.start, end: window.start + window.length)) }
                dismiss()
            }
            .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
            .disabled(window == nil)
            .accessibilityIdentifier("moment-order-window-done")
            Button(strings.cancel) { dismiss() }
                .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
                .accessibilityIdentifier("moment-order-window-cancel-text")
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
