import AVFoundation
import SwiftUI

// KRI-723: "put your moments in order and show me where each one is". The rows come in Kria's order (the
// creator's words); the arrows reorder them. Each row takes one of three answers: "Show where" opens the KRI-717
// source-window picker on this iPhone's original of the take, "Not in my clips" leaves the moment out, "You pick"
// lets Kria choose a part. A moment the clip record placed starts pointed at its evidence.

/// Where a dragged row lands. Rows have different heights, so every step is measured in the row it passes.
/// `heights` are in current row order.
enum MomentReorderMath {
    /// A flick at least this fast (points/second) can carry a row one place past where its distance alone puts it.
    static let flickSpeed: CGFloat = 500

    /// The index a drag of `translation` reaches: one place at a time while the drag covers more than half of the next row.
    static func target(from index: Int, translation: CGFloat, heights: [CGFloat], spacing: CGFloat) -> Int {
        var remaining = translation
        var current = index
        while remaining > 0, current < heights.count - 1 {
            let next = heights[current + 1]
            guard remaining > next / 2 else { break }
            current += 1
            remaining -= next + spacing
        }
        while remaining < 0, current > 0 {
            let previous = heights[current - 1]
            guard -remaining > previous / 2 else { break }
            current -= 1
            remaining += previous + spacing
        }
        return current
    }

    /// Where a release lands: by distance, or one place further when the finger was flicked that way.
    static func releaseTarget(from index: Int, translation: CGFloat, predictedTranslation: CGFloat, velocity: CGFloat,
                              heights: [CGFloat], spacing: CGFloat) -> Int {
        let byDistance = target(from: index, translation: translation, heights: heights, spacing: spacing)
        guard velocity.isFinite, abs(velocity) >= flickSpeed else { return byDistance }
        let byFlick = target(from: index, translation: predictedTranslation, heights: heights, spacing: spacing)
        let extra = velocity > 0 ? min(1, max(0, byFlick - byDistance)) : max(-1, min(0, byFlick - byDistance))
        return min(max(0, byDistance + extra), heights.count - 1)
    }

    /// How far the layout moves the row when it goes from `index` to `target`.
    static func distance(from index: Int, to target: Int, heights: [CGFloat], spacing: CGFloat) -> CGFloat {
        guard index != target, heights.indices.contains(index), heights.indices.contains(target) else { return 0 }
        if target > index { return (index + 1...target).reduce(0) { $0 + heights[$1] + spacing } }
        return -(target..<index).reduce(0) { $0 + heights[$1] + spacing }
    }

    /// How far the row at `row` steps aside while `dragged` is held over `target`.
    static func neighbourShift(row: Int, dragged: Int, target: Int, draggedHeight: CGFloat, spacing: CGFloat) -> CGFloat {
        let step = draggedHeight + spacing
        if target > dragged, row > dragged, row <= target { return -step }
        if target < dragged, row >= target, row < dragged { return step }
        return 0
    }
}

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
    /// Where the held row would land if released now; the rows between step aside live.
    @State private var dragTarget: Int?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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
        .offset(y: dragging ? dragOffset : neighbourShift(of: key))
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
                        let target = liveTarget(for: key, translation: value.translation.height)
                        if target != dragTarget {
                            withAnimation(reduceMotion ? nil : .snappy(duration: 0.2)) { dragTarget = target }
                        }
                    }
                    .onEnded { value in
                        guard !isSending else { return }
                        drop(key, value: value)
                    }
            )
            .onChange(of: handleActive) { _, active in
                if !active, draggingKey == key { cancelDrag() }
            }
            .accessibilityElement()
            .accessibilityLabel(strings.reorder(words))
            .accessibilityAction(named: strings.moveUp(words)) { if !isSending { moveUp(key) } }
            .accessibilityAction(named: strings.moveDown(words)) { if !isSending { moveDown(key) } }
            .accessibilityIdentifier("moment-order-handle-\(key)")
    }

    private func moveUp(_ key: String) { withAnimation(.easeOut(duration: 0.18)) { draft.moveUp(key) } }
    private func moveDown(_ key: String) { withAnimation(.easeOut(duration: 0.18)) { draft.moveDown(key) } }

    private var orderedHeights: [CGFloat] { draft.order.map { rowHeights[$0] ?? 100 } }

    private func liveTarget(for key: String, translation: CGFloat) -> Int {
        guard let index = draft.order.firstIndex(of: key) else { return 0 }
        return MomentReorderMath.target(from: index, translation: translation, heights: orderedHeights, spacing: Self.rowSpacing)
    }

    /// How far a neighbour steps aside while another row is held over its new place.
    private func neighbourShift(of key: String) -> CGFloat {
        guard let held = draggingKey, let target = dragTarget,
              let dragged = draft.order.firstIndex(of: held), let row = draft.order.firstIndex(of: key) else { return 0 }
        return MomentReorderMath.neighbourShift(row: row, dragged: dragged, target: target,
                                                draggedHeight: rowHeights[held] ?? 100, spacing: Self.rowSpacing)
    }

    /// Lands the row by distance or flick, then lets it settle from where the finger left it at the speed it was going.
    private func drop(_ key: String, value: DragGesture.Value) {
        guard let index = draft.order.firstIndex(of: key) else { return cancelDrag() }
        let heights = orderedHeights
        let target = MomentReorderMath.releaseTarget(
            from: index, translation: value.translation.height, predictedTranslation: value.predictedEndTranslation.height,
            velocity: value.velocity.height, heights: heights, spacing: Self.rowSpacing)
        let moved = MomentReorderMath.distance(from: index, to: target, heights: heights, spacing: Self.rowSpacing)
        // One transaction without animation: the new order, the neighbours back at rest, and the row's offset
        // rebased so it stays exactly under the finger. Only then does the spring carry it home.
        var instant = Transaction(); instant.disablesAnimations = true
        withTransaction(instant) {
            if target > index { for _ in index..<target { draft.moveDown(key) } }
            if target < index { for _ in target..<index { draft.moveUp(key) } }
            dragTarget = nil
            dragOffset = value.translation.height - moved
        }
        let velocity = KriaGesturePhysics.unitVelocity(value.velocity.height, from: dragOffset, to: 0)
        let spring = DrawerMotion.settle(reduceMotion: reduceMotion, velocity: velocity)
        guard spring != nil else { draggingKey = nil; dragOffset = 0; return }
        withAnimation(spring, completionCriteria: .logicallyComplete) { dragOffset = 0 } completion: { draggingKey = nil }
    }

    /// A drag the system cancelled puts everything back without reordering.
    private func cancelDrag() {
        withAnimation(reduceMotion ? nil : .snappy(duration: 0.18)) { draggingKey = nil; dragOffset = 0; dragTarget = nil }
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
