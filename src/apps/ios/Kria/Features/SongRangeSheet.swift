import SwiftUI

// KRI-580: "Where in the song did you film?" -- a quiet row under the attached song and the small sheet it opens.
// Optional everywhere: nothing here blocks attaching, sending or generating.

/// The row under the attached song: the question while nothing is set, the range once it is.
struct SongRangeRow: View {
    let range: SongFilmingRange?
    var isEnabled = true
    let onTap: () -> Void

    private var title: String {
        guard let range else { return "Where in the song did you film?" }
        return range.summary
    }
    private var accessibilityText: String {
        guard let range else { return "Where in the song did you film?" }
        return range.spokenSummary
    }

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 8) {
                Text(title).font(KriaFont.body(14)).foregroundStyle(KriaColor.ink).lineLimit(2)
                if range != nil {
                    Text("·").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                    Text("Change").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(KriaColor.mutedInk)
            }
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!isEnabled)
        .opacity(isEnabled ? 1 : 0.5)
        .accessibilityLabel(accessibilityText)
        .accessibilityHint("Opens the part of the song you used")
        .accessibilityIdentifier("song-range-row")
    }
}

/// Two time wheels (From, To), live validation, and Save / Use the whole song / Cancel.
struct SongRangeSheet: View {
    let duration: Double?
    let existing: SongFilmingRange?
    /// Sends the range (nil = clear). The sheet shows a failure inline and closes itself on success.
    let submit: (SongFilmingRange?) async -> SongRangeSubmission
    @Environment(\.dismiss) private var dismiss
    @State private var draft: SongRangeDraft?
    @State private var saving = false
    @State private var serverMessage: String?

    init(duration: Double?, existing: SongFilmingRange?, submit: @escaping (SongFilmingRange?) async -> SongRangeSubmission) {
        self.duration = duration
        self.existing = existing
        self.submit = submit
        _draft = State(initialValue: SongRangeDraft(duration: duration, existing: existing))
    }

    private var fromBinding: Binding<Int> {
        Binding(get: { draft?.from ?? 0 }, set: { value in
            draft?.setFrom(value)
            serverMessage = nil
        })
    }
    private var toBinding: Binding<Int> {
        Binding(get: { draft?.to ?? 0 }, set: { value in
            draft?.setTo(value)
            serverMessage = nil
        })
    }
    private var shownMessage: String? { serverMessage ?? draft?.message }
    private var canSave: Bool { draft?.range != nil && !saving }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let draft {
                    wheels(draft)
                    messageLine
                    actions
                }
            }
            .padding(20)
        }
        .presentationDetents([.medium, .large])
        .accessibilityIdentifier("song-range-sheet")
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Where in the song did you film?")
                .font(KriaFont.body(22).weight(.semibold)).foregroundStyle(KriaColor.ink)
                .accessibilityAddTraits(.isHeader)
            Text("Songs repeat their verses. Tell us the part you used and we’ll only match your clips there.")
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
        }
    }

    private func wheels(_ draft: SongRangeDraft) -> some View {
        HStack(alignment: .top, spacing: 12) {
            wheel(title: "From", selection: fromBinding, upper: draft.maxFrom, id: "song-range-from")
            wheel(title: "To", selection: toBinding, upper: draft.limit, id: "song-range-to")
        }
    }

    private func wheel(title: String, selection: Binding<Int>, upper: Int, id: String) -> some View {
        VStack(spacing: 4) {
            Text(title).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            Picker(title, selection: selection) {
                ForEach(0...upper, id: \.self) { second in
                    Text(SongRangeMath.timecode(Double(second))).tag(second)
                }
            }
            .pickerStyle(.wheel)
            .frame(height: 120)
            .clipped()
            .accessibilityLabel(title)
            .accessibilityIdentifier(id)
        }
        .frame(maxWidth: .infinity)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 12))
    }

    @ViewBuilder private var messageLine: some View {
        if let shownMessage {
            Text(shownMessage)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                .accessibilityIdentifier("song-range-error")
        }
    }

    private var actions: some View {
        VStack(spacing: 8) {
            Button("Save") { Task { await save(draft?.range) } }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 48))
                .frame(maxWidth: .infinity)
                .disabled(!canSave)
                .accessibilityIdentifier("song-range-save")
            if existing != nil {
                Button("Use the whole song") { Task { await save(nil) } }
                    .buttonStyle(SongRangeFillButtonStyle())
                    .disabled(saving)
                    .accessibilityIdentifier("song-range-clear")
            }
            Button("Cancel") { dismiss() }
                .font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, minHeight: 44)
                .disabled(saving)
                .accessibilityIdentifier("song-range-cancel")
        }
    }

    private func save(_ range: SongFilmingRange?) async {
        guard !saving else { return }
        saving = true
        serverMessage = nil
        let result = await submit(range)
        saving = false
        switch result {
        case .saved, .unavailable: dismiss()
        case .failed(let text): serverMessage = text
        }
    }
}

/// A quiet filled capsule: secondary actions use a fill, not an outline.
private struct SongRangeFillButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(16).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(KriaColor.fill, in: Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.65 : 1) : 0.45)
    }
}
