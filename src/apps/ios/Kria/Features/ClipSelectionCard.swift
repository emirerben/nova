import SwiftUI

/// How a clip question is shown under its message (KRI-282).
enum ClipSelectionCardMode {
    /// The latest unanswered question: interactive. `submit` receives the structured answer and its readable message.
    case active(isSending: Bool, submit: (ClipSelectionSubmission, String) -> Void)
    /// Already answered (or superseded): read-only, collapsed. `summary` is nil after a relaunch.
    case answered(summary: String?)
}

/// Tap-to-pick clip chooser: per category a thumbnail strip, "None of these", then Send / Skip.
struct ClipSelectionCard: View {
    let question: ClipQuestion
    let media: [CreationAttachedMedia]
    let mode: ClipSelectionCardMode
    @State private var state: ClipSelectionState

    init(question: ClipQuestion, media: [CreationAttachedMedia], mode: ClipSelectionCardMode) {
        self.question = question
        self.media = media
        self.mode = mode
        _state = State(initialValue: ClipSelectionState(question: question))
    }

    private var positions: [String: Int] { ClipPositions.map(media: media, question: question) }

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                Text(summary.map { "Clips chosen: \($0)" } ?? "Clips chosen")
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("clip-card-answered")
        case .active(let isSending, let submit):
            VStack(alignment: .leading, spacing: 18) {
                ForEach(question.categories) { category in
                    categorySection(category, isSending: isSending)
                }
                footer(isSending: isSending, submit: submit)
            }
            .padding(14)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("clip-card")
        }
    }

    private func categorySection(_ category: ClipQuestion.Category, isSending: Bool) -> some View {
        let count = category.candidateMediaIDs.filter { state.isSelected($0, in: category.key) }.count
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(category.label).font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                Spacer(minLength: 8)
                if count > 0 {
                    Text("\(count) selected").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            .accessibilityAddTraits(.isHeader)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(category.candidateMediaIDs, id: \.self) { mediaID in
                        thumbnail(mediaID, category: category, isSending: isSending)
                    }
                }
                .padding(.vertical, 2)
            }
            .background {
                GeometryReader { geometry in
                    Color.clear.preference(key: DrawerGestureExclusionPreference.self, value: [geometry.frame(in: .global)])
                }
            }
            if question.allowNone {
                let none = state.isNone(category.key)
                Button { state.toggleNone(category.key) } label: {
                    HStack(spacing: 6) {
                        Image(systemName: none ? "checkmark.circle.fill" : "circle")
                        Text("None of these")
                    }
                    .font(KriaFont.body(13).weight(.medium))
                    .foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 12)
                    .frame(minHeight: 44)
                    .background(none ? KriaColor.selectionSoft : KriaColor.paper, in: Capsule())
                    .overlay(Capsule().stroke(none ? KriaColor.ink : KriaColor.line, lineWidth: none ? 1.5 : 1))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(isSending)
                .accessibilityAddTraits(none ? .isSelected : [])
                .accessibilityIdentifier("clip-none-\(category.key)")
            }
        }
    }

    private func thumbnail(_ mediaID: String, category: ClipQuestion.Category, isSending: Bool) -> some View {
        let position = positions[mediaID] ?? 0
        let isOn = state.isSelected(mediaID, in: category.key)
        // Missing from thread state -> placeholder tile; the cache miss falls back to "Clip N" below.
        let attached = media.first { $0.id == mediaID }
            ?? CreationAttachedMedia(id: mediaID, filename: "Clip \(position)", kind: "video", previewURL: nil)
        return Button { state.toggle(mediaID, in: category.key) } label: {
            VStack(spacing: 4) {
                CreationAttachmentThumbnail(media: attached)
                    .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(isOn ? KriaColor.ink : .clear, lineWidth: 2.5))
                    .overlay(alignment: .topLeading) {
                        Text("\(position)")
                            .font(KriaFont.body(11).weight(.bold)).foregroundStyle(Color.white)
                            .frame(minWidth: 18, minHeight: 18).padding(.horizontal, 2)
                            .background(KriaColor.ink.opacity(0.78), in: Capsule())
                            .padding(3)
                    }
                    .overlay(alignment: .bottomTrailing) {
                        if isOn {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 20)).foregroundStyle(Color.white, KriaColor.ink)
                                .padding(2)
                        }
                    }
                Text("Clip \(position)").font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk)
            }
            .frame(minWidth: 52, minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isSending)
        .accessibilityLabel("Clip \(position)")
        .accessibilityValue(isOn ? "Selected" : "Not selected")
        .accessibilityAddTraits(isOn ? .isSelected : [])
        .accessibilityIdentifier("clip-thumb-\(category.key)-\(mediaID)")
    }

    private func footer(isSending: Bool, submit: @escaping (ClipSelectionSubmission, String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button("Send") {
                let submission = state.submission
                submit(submission, submission.message(question: question, positions: positions))
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
            .disabled(!state.canSend || isSending)
            .accessibilityIdentifier("clip-send")
            Button("Skip, decide for me") {
                let submission = ClipSelectionSubmission.skip(question)
                submit(submission, submission.message(question: question, positions: positions))
            }
            .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
            .disabled(isSending)
            .accessibilityIdentifier("clip-skip")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
