import SwiftUI

/// One overlay set in the transcript: the ask, the prompt rows, the per-image progress and the finished set. A card holds
/// a heading, controls and state text only; the thread says where it is and `OverlaySetCoordinator` makes the calls.
struct OverlaySetRow: View {
    let current: OverlaySet
    let phase: AskPhase<String>
    @ObservedObject var coordinator: OverlaySetCoordinator
    let review: (OverlaySet) -> Void

    var body: some View {
        VStack(alignment: .trailing, spacing: 10) {
            switch phase {
            case .open, .submitting, .failed, .stale:
                if let draft = coordinator.draft(for: current.id) {
                    KriaYouChose(text: String(localized: "Describe them"))
                    OverlayComposeCard(current: current, draft: draft, coordinator: coordinator)
                } else {
                    OverlayAskCard(current: current, phase: phase, coordinator: coordinator)
                }
            case .answered(let text):
                KriaYouChose(text: text)
                OverlayStatusCard(current: current, coordinator: coordinator, review: review)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

private struct OverlayAskCard: View {
    let current: OverlaySet
    let phase: AskPhase<String>
    @ObservedObject var coordinator: OverlaySetCoordinator

    private var isSending: Bool { if case .submitting = phase { true } else { false } }

    var body: some View {
        KriaQuestionCard(title: String(localized: "Overlay images")) {
            VStack(spacing: 8) {
                Button("Let Kria decide") { coordinator.chooseAutomatic(current.id) }
                    .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: isSending))
                    .accessibilityIdentifier("overlay-ask-decide")
                Button("Describe them") { coordinator.beginDescribing(current.id, maxRows: current.maxOverlays) }
                    .buttonStyle(KriaButtonStyle(role: .secondary, fullWidth: true))
                    .disabled(isSending)
                    .accessibilityIdentifier("overlay-ask-describe")
                if case .failed(let reason) = phase {
                    KriaInlineFailure(text: reason).accessibilityIdentifier("overlay-ask-failed")
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("overlay-ask-card")
    }
}

private struct OverlayComposeCard: View {
    let current: OverlaySet
    let draft: OverlayComposeDraft
    @ObservedObject var coordinator: OverlaySetCoordinator
    @FocusState private var focused: Int?

    private var count: Int { draft.filled.count }
    private var makeTitle: String {
        let price = current.cost(rows: count).formatted(.currency(code: "USD"))
        switch count {
        case 0: return String(localized: "Make overlays")
        case 1: return String(localized: "Make 1 overlay · \(price)")
        default: return String(localized: "Make \(count) overlays · \(price)")
        }
    }

    var body: some View {
        KriaQuestionCard(title: String(localized: "Describe your overlays")) {
            VStack(spacing: 8) {
                ForEach(draft.rows.indices, id: \.self) { index in
                    HStack(spacing: 4) {
                        TextField("Describe an image", text: binding(index), axis: .vertical)
                            .kriaType(.button).foregroundStyle(KriaColor.ink).tint(KriaColor.sky)
                            .focused($focused, equals: index)
                            .lineLimit(1...3)
                            .padding(.leading, 14).padding(.vertical, 12)
                            .accessibilityIdentifier("overlay-compose-row-\(index)")
                        Button { coordinator.draftChanged(current.id) { $0.remove(index) } } label: {
                            Image(systemName: "xmark").font(.system(size: 13, weight: .semibold)).foregroundStyle(KriaColor.zinc)
                                .frame(width: 44, height: 44)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(Text("Remove image \(index + 1)"))
                        .accessibilityIdentifier("overlay-compose-remove-\(index)")
                    }
                    .frame(minHeight: 52)
                    .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                if draft.canAdd {
                    Button {
                        coordinator.draftChanged(current.id) { $0.add() }
                        focused = draft.rows.count
                    } label: {
                        HStack(spacing: 12) {
                            Text("Describe another image").kriaType(.button).foregroundStyle(KriaColor.zinc)
                            Spacer(minLength: 0)
                            Image(systemName: "plus").font(.system(size: 13, weight: .semibold)).foregroundStyle(KriaColor.zinc)
                        }
                        .padding(.horizontal, 14).frame(maxWidth: .infinity, minHeight: 52)
                        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("overlay-compose-add")
                }
            }
            VStack(spacing: 8) {
                Button(makeTitle) { coordinator.make(current.id) }
                    .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: coordinator.isSending(current.id)))
                    .disabled(!draft.canMake)
                    .accessibilityIdentifier("overlay-compose-make")
                if coordinator.failure(for: current.id) == .answer {
                    KriaInlineFailure(text: String(localized: "Couldn’t send your answer.")).accessibilityIdentifier("overlay-compose-failed")
                }
                Button("Cancel") { coordinator.cancel(current.id) }
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
                    .disabled(coordinator.isSending(current.id))
                    .accessibilityIdentifier("overlay-compose-cancel")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("overlay-compose-card")
    }

    private func binding(_ index: Int) -> Binding<String> {
        Binding(get: { draft.rows.indices.contains(index) ? draft.rows[index] : "" },
                set: { text in coordinator.draftChanged(current.id) { $0.set(index, text) } })
    }
}

private struct OverlayStatusCard: View {
    let current: OverlaySet
    @ObservedObject var coordinator: OverlaySetCoordinator
    let review: (OverlaySet) -> Void

    private var total: Int { current.overlays.count }
    private var allReady: Bool { current.phase == .done && current.failedCount == 0 && current.readyCount > 0 }
    private var title: String {
        if allReady { return String(localized: "Overlays ready") }
        return current.phase == .done ? String(localized: "Your overlays") : String(localized: "Making your overlays")
    }
    private var reviewTitle: String {
        if allReady || current.readyCount == 0 { return String(localized: "Review images") }
        return current.readyCount == 1 ? String(localized: "Review 1 image") : String(localized: "Review \(current.readyCount) images")
    }

    var body: some View {
        KriaQuestionCard(title: title, helper: allReady || total == 0 ? nil : String(localized: "\(current.readyCount) of \(total) ready")) {
            if allReady {
                HStack(spacing: 8) {
                    ForEach(current.overlays) { overlay in
                        OverlayThumbnail(url: coordinator.thumbnail(for: overlay.operationID), phase: .ready, width: 72, height: 72, radius: 12)
                    }
                }
            } else if total == 0 {
                planning
            } else {
                VStack(spacing: 8) { ForEach(current.overlays) { OverlayProgressRow(current: current, overlay: $0, coordinator: coordinator) } }
            }
            if coordinator.failure(for: current.id) == .plan {
                KriaInlineFailure(text: String(localized: "Couldn’t make your overlays.")).accessibilityIdentifier("overlay-plan-failed")
                Button("Try again") { coordinator.retryPlan(current.id) }
                    .buttonStyle(KriaButtonStyle(role: .secondary, fullWidth: true))
                    .accessibilityIdentifier("overlay-plan-retry")
            }
            if total > 0 {
                Button(reviewTitle) { review(current) }
                    .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
                    .disabled(current.reviewItems.isEmpty)
                    .accessibilityIdentifier("overlay-review")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("overlay-status-card")
        .onChange(of: current.readyCount) { _, ready in
            KriaAnnouncements.post(String(localized: "\(ready) of \(total) ready"), repeatsAfter: 0)
        }
    }

    private var planning: some View {
        HStack(spacing: 10) {
            KriaSpinner(size: 16)
            Text("Making").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
        }
        .frame(minHeight: 44, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("overlay-planning")
    }
}

private struct OverlayProgressRow: View {
    let current: OverlaySet
    let overlay: OverlaySet.Overlay
    @ObservedObject var coordinator: OverlaySetCoordinator

    private var isRetrying: Bool { coordinator.isRetrying(set: current.id, overlay: overlay.id) }
    private var failed: Bool { overlay.state == .failed && !isRetrying }
    private var stateText: String {
        if failed { return String(localized: "Couldn’t make this one") }
        return overlay.state == .ready ? String(localized: "Ready") : String(localized: "Making")
    }

    var body: some View {
        HStack(alignment: failed ? .top : .center, spacing: 12) {
            OverlayThumbnail(url: coordinator.thumbnail(for: overlay.operationID), phase: overlay.state == .ready ? .ready : (failed ? .failed : .making),
                             width: 44, height: 52, radius: 12)
            VStack(alignment: .leading, spacing: 4) {
                Text(overlay.title).kriaType(.button).foregroundStyle(KriaColor.ink).fixedSize(horizontal: false, vertical: true)
                if failed { state }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if failed {
                if overlay.canRetry {
                    Button("Retry") { coordinator.retry(set: current.id, overlay: overlay.id) }
                        .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                        .accessibilityIdentifier("overlay-retry-\(overlay.id)")
                }
            } else {
                state
            }
        }
        .padding(.horizontal, 14).padding(.vertical, failed ? 12 : 10)
        .frame(maxWidth: .infinity, minHeight: 68, alignment: .leading)
        .background(failed ? KriaColor.failureSoft : KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("overlay-row-\(overlay.id)")
    }

    @ViewBuilder private var state: some View {
        switch overlay.state {
        case .ready:
            HStack(spacing: 4) {
                Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).accessibilityHidden(true)
                Text(stateText).kriaType(.label)
            }
            .foregroundStyle(KriaColor.success)
        case .making:
            HStack(spacing: 6) {
                KriaSpinner(size: 14)
                Text(stateText).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            }
        case .failed:
            if isRetrying {
                HStack(spacing: 6) {
                    KriaSpinner(size: 14)
                    Text(String(localized: "Making")).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                }
            } else {
                Text(stateText).kriaType(.label).foregroundStyle(KriaColor.failureText)
            }
        }
    }
}
