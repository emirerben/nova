import SwiftUI

/// CA-6: "Use these facts?" on a research draft. One row per sourced fact with a tick, the fact, where it sits in the
/// video and where it came from; the primary names how many stay. Ticking none is a waiver and asks to confirm it.
/// The card draws one `AskPhase`: a send in flight dims the rest, a failure is red with a retry of the same pick, a
/// stale card is a slate notice over the refreshed list, and an answer is the "You kept" receipt.
struct ResearchSelectionCard: View {
    let question: ResearchSelectionQuestion
    let phase: AskPhase<ResearchSelectionLabel.Receipt>
    var isBusy = false
    var submit: ([String]) -> Void = { _ in }

    @State private var ticked: Set<String>
    @State private var opened: ResearchPreview.Source?
    /// The server's reason (or the stale notice) is only true for the pick as it was: any tick hides it.
    @State private var pickChanged = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(question: ResearchSelectionQuestion, phase: AskPhase<ResearchSelectionLabel.Receipt>, isBusy: Bool = false,
         submit: @escaping ([String]) -> Void) {
        self.question = question
        self.phase = phase
        self.isBusy = isBusy
        self.submit = submit
        _ticked = State(initialValue: Set(question.facts.map(\.id)))
    }

    var body: some View {
        if case .answered(let receipt) = phase {
            KriaYouChose(text: receipt.text, caption: receipt.caption)
                .accessibilityIdentifier("research-selection-receipt")
        } else {
            activeCard
        }
    }

    private var isSending: Bool {
        if case .submitting = phase { true } else { false }
    }

    private var keptCount: Int { ticked.count }

    private var activeCard: some View {
        KriaQuestionCard(title: String(localized: "Use these facts?")) {
            VStack(spacing: 8) {
                ForEach(Array(question.facts.enumerated()), id: \.element.id) { index, fact in
                    row(index, fact)
                }
            }
            footer
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("research-selection-card")
        .sensoryFeedback(.selection, trigger: ticked)
        .onChange(of: phase) { _, _ in pickChanged = false }
        .onChange(of: ticked) { _, _ in pickChanged = true }
        .sheet(item: $opened) { source in
            ResearchSourceSafariView(url: source.url).ignoresSafeArea()
        }
    }

    private func row(_ index: Int, _ fact: ResearchSelectionQuestion.Fact) -> some View {
        let on = ticked.contains(fact.id)
        let prefix = fact.window.map { String(localized: "On screen \($0)") + " · " } ?? ""
        return VStack(alignment: .leading, spacing: 0) {
            Button { toggle(fact.id) } label: {
                HStack(alignment: .top, spacing: 12) {
                    tick(on)
                    Text(fact.text).kriaType(.button).foregroundStyle(KriaColor.ink)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.top, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Text(verbatim: "\(fact.text), \(prefix.dropLast(3))\(prefix.isEmpty ? "" : ", ")\(fact.source.domain)"))
            .accessibilityAddTraits(on ? .isSelected : [])
            .accessibilityIdentifier("research-fact-\(index)")
            Button { opened = fact.source } label: {
                HStack(spacing: 0) {
                    Text(prefix).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    Text(fact.source.domain).kriaType(.label).foregroundStyle(KriaColor.ink).underline()
                    Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(KriaColor.ink)
                        .padding(.leading, 4).accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .padding(.leading, 34).padding(.vertical, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Text(verbatim: String(localized: "Open source: \(fact.source.domain)")))
            .accessibilityIdentifier("research-fact-source-\(index)")
        }
        .padding(.horizontal, 14).padding(.bottom, 2)
        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .opacity(isSending && !on ? 0.4 : 1)
        .allowsHitTesting(!isSending)
    }

    private func tick(_ on: Bool) -> some View {
        RoundedRectangle(cornerRadius: 7, style: .continuous)
            .fill(on ? KriaColor.ink : KriaColor.fillPressed)
            .frame(width: 22, height: 22)
            .overlay {
                if on { Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(KriaColor.paper) }
            }
            .padding(.top, 1)
            .accessibilityHidden(true)
    }

    private func toggle(_ id: String) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.12)) {
            if ticked.contains(id) { ticked.remove(id) } else { ticked.insert(id) }
        }
    }

    private var failure: String? {
        if case .failed(let text) = phase, !pickChanged { text } else { nil }
    }

    private var isStale: Bool {
        if case .stale = phase, !pickChanged { true } else { false }
    }

    @ViewBuilder private var footer: some View {
        VStack(spacing: 8) {
            if let failure {
                KriaInlineFailure(text: failure).accessibilityIdentifier("research-selection-failed")
            } else if isStale {
                KriaInlineNotice(text: String(localized: "The draft changed while the sources were checked. These are the current facts. Pick again."))
                    .accessibilityIdentifier("research-selection-notice")
            }
            controls
        }
    }

    @ViewBuilder private var controls: some View {
        let kept = question.facts.map(\.id).filter(ticked.contains)
        if isSending {
            Button("Saving your choice") {}
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: true))
                .accessibilityIdentifier("research-selection-use")
        } else if failure != nil {
            Button("Try again") { submit(kept) }
                .buttonStyle(KriaButtonStyle(role: .ink, fullWidth: true))
                .disabled(isBusy)
                .accessibilityIdentifier("research-selection-use")
            if kept.isEmpty { pickFacts }
        } else if let label = ResearchSelectionLabel.primary(kept: kept.count, total: question.facts.count) {
            Button(label) { submit(kept) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                .disabled(isBusy)
                .accessibilityIdentifier("research-selection-use")
        } else {
            KriaInlineNotice(text: String(localized: "Without these facts, the text won’t include sourced details."))
                .accessibilityIdentifier("research-selection-notice")
            Button("Continue without facts") { submit([]) }
                .buttonStyle(KriaButtonStyle(role: .ink, fullWidth: true))
                .disabled(isBusy)
                .accessibilityIdentifier("research-selection-use")
            pickFacts
        }
    }

    private var pickFacts: some View {
        Button("Pick facts") { ticked = Set(question.facts.map(\.id)) }
            .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
            .disabled(isBusy || isSending)
            .accessibilityIdentifier("research-selection-pick")
    }
}
