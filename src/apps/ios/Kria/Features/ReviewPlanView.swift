import SwiftUI

/// What opens the Review sheet: the feed's blocks (so it shows at once, before the network answers) and the
/// section whose Change was tapped, if any.
struct ReviewPlanPresentation: Identifiable {
    let id = UUID()
    var seed: [PlanBlock] = []
    var initialFlag: PlanSectionID?
}

/// KRI-440/444/445/451: the "Review your video" sheet, built to the Paper -B artboards (F2 layout, L1-B flag +
/// prompt, L2 updating, L3-B updated): white glass cards over a butter wash, a flagged card turns butter, a prompt
/// panel with butter chips sits above an ink + butter "Update video", and after an update each changed card shows
/// "Updated" with "Was ..." and Undo.
///
/// Surface rules (same as the live feed): glass only as a background leaf (`kriaGlassCard` / `kriaGlassPanel`),
/// never an identifier on or above a glass view, Inter only, fills not strokes, Reduce Motion = fades only.
struct ReviewPlanView: View {
    @State private var model: ReviewPlanModel
    let close: () -> Void
    @FocusState private var promptFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    init(seed: [PlanBlock], initialFlag: PlanSectionID?, actions: ReviewPlanActions, close: @escaping () -> Void) {
        _model = State(initialValue: ReviewPlanModel(seed: seed, initialFlag: initialFlag, actions: actions))
        self.close = close
    }

    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }

    var body: some View {
        ZStack(alignment: .top) {
            KriaButterWash().ignoresSafeArea()
            ScrollView {
                VStack(spacing: 14) {
                    cards
                    if let notice = model.notice {
                        Text(notice)
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                            .multilineTextAlignment(.center)
                            .frame(maxWidth: .infinity)
                            .padding(.horizontal, 8)
                            .accessibilityIdentifier("review-notice")
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 118)
                .padding(.bottom, 28)
            }
            .scrollDismissesKeyboard(.interactively)
            topFade
            header
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { bottomArea }
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.4, dampingFraction: 0.88), value: model.phase)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.36, dampingFraction: 0.88), value: model.draft.scope)
        .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.4, dampingFraction: 0.88), value: model.showsUpdated)
        .task { await model.load() }
        .onDisappear { model.cancelFollowing() }
        .onChange(of: model.phase) { _, phase in if phase != .reviewing { promptFocused = false } }
    }

    // MARK: Header

    private var topFade: some View {
        LinearGradient(
            // Opaque enough under the title and subtitle that scrolled cards never read through them.
            stops: [.init(color: Color.white.opacity(0.98), location: 0), .init(color: Color.white.opacity(0.96), location: 0.62),
                    .init(color: Color.white.opacity(0), location: 1)],
            startPoint: .top, endPoint: .bottom
        )
        .frame(height: 160)
        .ignoresSafeArea(edges: .top)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private var subtitle: String {
        if model.isUpdating { return "Kria is updating your video" }
        if model.showsUpdated { return "Your changes are in. Undo any you don’t like" }
        return "Tap Change on anything you’d like different"
    }

    private var header: some View {
        VStack(spacing: 2) {
            HStack(spacing: 12) {
                KriaGlassCloseButton(identifier: "review-close", action: close)
                Text("Review your video")
                    .font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("review-title")
                Color.clear.frame(width: 44, height: 44)
            }
            Text(subtitle)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier("review-subtitle")
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
    }

    // MARK: Cards

    @ViewBuilder private var cards: some View {
        if model.isLoading {
            ProgressView("Loading your plan")
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, minHeight: 160)
                .accessibilityIdentifier("review-loading")
        } else if model.blocks.isEmpty {
            VStack(spacing: 12) {
                Text(model.loadFailed ? "Kria couldn’t load your plan." : "Nothing to review yet.")
                    .font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk)
                if model.loadFailed {
                    Button("Try again") { Task { await model.refresh() } }
                        .buttonStyle(KriaInkCTAButtonStyle(height: 44))
                        .frame(maxWidth: 200)
                        .accessibilityIdentifier("review-retry")
                }
            }
            .frame(maxWidth: .infinity, minHeight: 160)
        } else {
            ForEach(model.blocks) { block in
                ReviewSectionCard(model: model, block: block)
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .scale(scale: 0.98)))
            }
        }
    }

    // MARK: Bottom

    private var hasPanel: Bool { !model.isLoading && !model.blocks.isEmpty && model.status != .empty }

    @ViewBuilder private var bottomArea: some View {
        if hasPanel {
            VStack(alignment: .trailing, spacing: 12) {
                if model.isUpdating {
                    updatingPanel
                } else if model.showsUpdated && !model.draft.canUpdate {
                    summaryPanel
                } else {
                    keepAsIs
                    promptPanel
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 10)
            .transition(.opacity)
        }
    }

    /// The floating 44pt glass pill. The glass is a leaf beside the button; the identifier sits on the button.
    private var keepAsIs: some View {
        ZStack {
            KriaGlassBackground(shape: Capsule(), whiteness: 0.75, shadowOpacity: 0.16, shadowRadius: 14, shadowY: 8)
            Button(action: close) {
                Text("Keep as is")
                    .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 18)
                    .frame(height: 44)
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("review-keep-as-is")
        }
        .fixedSize()
        .padding(.trailing, 2)
    }

    private var promptPanel: some View {
        let scope = model.draft.scope
        return VStack(alignment: .leading, spacing: 10) {
            if !scope.isEmpty {
                PlanChipFlow(spacing: 6) {
                    ForEach(scope, id: \.self) { section in
                        Button { model.toggle(section) } label: {
                            HStack(spacing: 6) {
                                Text(section.label).font(KriaFont.body(12).weight(.semibold))
                                Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
                            }
                            .foregroundStyle(KriaColor.ink)
                            .padding(.leading, 12).padding(.trailing, 10)
                            .frame(height: 30)
                            .background(KriaColor.butter, in: Capsule())
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("\(section.label), flagged. Remove")
                        .accessibilityIdentifier("review-chip-\(section.rawValue)")
                    }
                }
            }
            // At accessibility sizes the button drops under the field instead of squeezing it.
            let layout = typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .trailing, spacing: 8)) : AnyLayout(HStackLayout(alignment: .bottom, spacing: 8))
            layout {
                TextField(
                    scope.isEmpty ? "Tap Change on a section, then tell Kria what you want" : "Tell Kria what to change (optional)",
                    text: Binding(get: { model.draft.prompt }, set: { model.draft.prompt = $0 }),
                    axis: .vertical
                )
                .font(KriaFont.body(15)).foregroundStyle(KriaColor.ink)
                .lineLimit(1...4)
                .focused($promptFocused)
                .submitLabel(.done)
                .onSubmit { promptFocused = false }
                .padding(.horizontal, 6).padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("review-prompt-field")
                Button { model.submitUpdate() } label: { Text("Update video") }
                    .buttonStyle(ReviewUpdateButtonStyle())
                    .disabled(!model.draft.canUpdate || model.isBusy)
                    .accessibilityIdentifier("review-update-video")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassPanel(cornerRadius: 32)
    }

    private var updatingPanel: some View {
        let count = max(1, model.updatingScope.count)
        return VStack(alignment: .leading, spacing: 14) {
            if let asked = model.askedPrompt {
                Text("You asked: “\(asked)”")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .lineLimit(2)
                    .accessibilityIdentifier("review-asked")
            }
            HStack(spacing: 10) {
                Image(systemName: "sparkle").font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .accessibilityHidden(true)
                Text("Kria is updating \(count) section\(count == 1 ? "" : "s")")
                    .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("review-updating")
            }
            KriaProgressTrack(progress: model.updateProgress)
                .accessibilityElement()
                .accessibilityLabel("Update progress")
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassPanel(cornerRadius: 32)
    }

    private var summaryPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "sparkle").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .padding(.top, 2).accessibilityHidden(true)
                Text(model.updateSummaryText ?? "Done.")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("review-summary")
            }
            HStack(spacing: 8) {
                if model.canUndoAll {
                    Button { model.undoAll() } label: {
                        Text("Undo all")
                            .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 20)
                            .frame(minHeight: 48)
                            .background(Color.white.opacity(0.75), in: Capsule())
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(model.isBusy)
                    .accessibilityIdentifier("review-undo-all")
                }
                Button(action: close) { Text("Done") }
                    .buttonStyle(KriaInkCTAButtonStyle())
                    .accessibilityIdentifier("review-done")
            }
            Text("Want more changes? Tap Change on any section.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity)
                .multilineTextAlignment(.center)
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .kriaGlassPanel(cornerRadius: 32)
    }
}

/// "Update video": ink fill (135deg) with a butter label and a warm glow, 44pt, as in the Paper prompt panel.
struct ReviewUpdateButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(14).weight(.semibold))
            .foregroundStyle(Color(red: 1, green: 0.914, blue: 0.541))
            .padding(.horizontal, 18)
            .frame(minHeight: 44)
            .background(
                LinearGradient(colors: [KriaColor.ink, KriaColor.ink.opacity(0.92)], startPoint: .topLeading, endPoint: .bottomTrailing),
                in: Capsule()
            )
            .shadow(color: KriaColor.butterGlow.opacity(isEnabled ? 0.35 : 0), radius: 10)
            .contentShape(Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.85 : 1) : 0.35)
            .fixedSize()
    }
}

// MARK: - Section card

/// One section of the plan: header (title, summary, state pill), its content, and in the Updated state the
/// "Was ..." footer with Undo. Butter when flagged, being redone or just updated; white glass otherwise.
private struct ReviewSectionCard: View {
    let model: ReviewPlanModel
    let block: PlanBlock

    private var section: PlanSectionID { block.section }
    private var state: PlanBlockState { model.displayState(block) }
    private var isFlagged: Bool { model.draft.isFlagged(section) && model.canChange(block) }
    private var isUpdated: Bool { model.showsUpdated && block.changed }
    private var isDeciding: Bool { state == .deciding }
    private var showsButter: Bool { isDeciding || isFlagged || isUpdated }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerRow
            if isDeciding {
                PlanSkeletonContent(section: section)
            } else if !block.skipped, block.state == .decided {
                content
            }
            if isUpdated, !isDeciding { footer }
        }
        .padding(.horizontal, 18).padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        // The identified marker is a plain leaf behind the content, never a wrapper around the glass.
        .background {
            Color.clear
                .accessibilityElement()
                .accessibilityLabel(section.cardTitle)
                .accessibilityValue(stateValue)
                .accessibilityIdentifier("review-section-\(section.rawValue)")
                .allowsHitTesting(false)
        }
        .modifier(ReviewCardSurface(butter: showsButter))
    }

    private var stateValue: String {
        if isDeciding { return "updating" }
        if isUpdated { return "updated" }
        if isFlagged { return "flagged" }
        if block.skipped { return "not used" }
        if !section.isScopable || !block.editable { return "read only" }
        return "idle"
    }

    // MARK: Header

    private var subtitle: String? {
        if block.skipped { return "Not used" }
        guard block.state == .decided, !isDeciding, block.payload != nil, section != .title,
              let summary = block.summary, !summary.isEmpty else { return nil }
        return summary
    }

    private var headerRow: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(section == .title && !block.skipped ? "Title" : section.cardTitle)
                    .font(KriaFont.body(section == .title ? 13 : 17).weight(section == .title ? .regular : .semibold))
                    .foregroundStyle(section == .title ? KriaColor.mutedInk : KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                if let subtitle {
                    Text(subtitle)
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            pill
        }
    }

    @ViewBuilder private var pill: some View {
        if isDeciding {
            KriaPlanPill(title: "", style: .working("Updating"))
                .accessibilityIdentifier("review-updating-\(section.rawValue)")
        } else if isUpdated {
            KriaPlanPill(title: "Updated", style: .updated)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(section.label) updated")
                .accessibilityIdentifier("review-updated-\(section.rawValue)")
        } else if model.canChange(block) {
            Button { model.toggle(section) } label: {
                KriaPlanPill(title: isFlagged ? "Changing" : "Change", style: isFlagged ? .changing : .change)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isFlagged ? "Changing \(section.label). Tap to cancel" : "Change \(section.label)")
            .accessibilityIdentifier("review-change-\(section.rawValue)")
        }
    }

    // MARK: Content

    @ViewBuilder private var content: some View {
        let editable = model.canChange(block)
        switch block.payload {
        case .title(let payload):
            ReviewTitleField(model: model, payload: payload, editable: editable && payload.barID != nil)
        case .clips(let payload) where !payload.clips.isEmpty:
            ReviewClipRows(clips: payload.clips)
        case .captions(let payload) where !payload.lines.isEmpty:
            ReviewCaptionRows(model: model, payload: payload, previous: block.previous, editable: editable, showsChanges: isUpdated)
        case .music(let payload):
            PlanBlockContent(block: block)
            if let mix = payload.mix, mix.musicLevel != nil || mix.originalLevel != nil {
                ReviewMixSliders(model: model, mix: mix, editable: editable)
            }
        default:
            if section == .title {
                Text(block.displaySummary)
                    .font(KriaFont.body(22).weight(.semibold)).foregroundStyle(KriaColor.ink)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                PlanBlockContent(block: block)
            }
        }
    }

    // MARK: Updated footer

    private var footer: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(wasText)
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.ink.opacity(0.7))
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("review-was-\(section.rawValue)")
            if block.previous != nil {
                Button { model.undo(section) } label: {
                    Text("Undo")
                        .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 6)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(model.isBusy)
                .accessibilityLabel("Undo \(section.label)")
                .accessibilityIdentifier("review-undo-\(section.rawValue)")
            }
        }
    }

    private var wasText: String {
        guard let previous = block.previous else { return "Updated" }
        if previous.skipped { return "Was not used" }
        if let summary = previous.summary, !summary.isEmpty { return "Was \(summary)" }
        return "Was the previous version"
    }
}

/// Glass for a normal card, the butter gradient for a flagged / updating / updated one.
private struct ReviewCardSurface: ViewModifier {
    let butter: Bool

    func body(content: Content) -> some View {
        if butter {
            content.kriaButterCard(cornerRadius: 28)
        } else {
            content.kriaGlassCard(cornerRadius: 28)
        }
    }
}

// MARK: - Title

private struct ReviewTitleField: View {
    let model: ReviewPlanModel
    let payload: PlanTitlePayload
    let editable: Bool

    var body: some View {
        if editable {
            TextField(
                "Title",
                text: Binding(get: { model.draft.titleText ?? payload.text }, set: { model.draft.editTitle($0, original: payload.text) }),
                axis: .vertical
            )
            .font(KriaFont.body(22).weight(.semibold)).foregroundStyle(KriaColor.ink)
            .lineLimit(1...3)
            .submitLabel(.done)
            .accessibilityIdentifier("review-edit-title")
        } else {
            Text(payload.text)
                .font(KriaFont.body(22).weight(.semibold)).foregroundStyle(KriaColor.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Clips

private struct ReviewClipRows: View {
    let clips: [PlanClipItem]

    private func transitionName(_ transition: PlanTransition) -> String {
        switch transition {
        case .cut: "Cut"
        case .dissolve: "Dissolve"
        case .whip: "Whip"
        case .fade: "Fade out"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(clips) { clip in
                HStack(spacing: 12) {
                    PlanClipTile(clip: clip, width: 40, height: 54)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(clip.role ?? clip.label ?? "Clip \(clip.index + 1)")
                            .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink).lineLimit(1)
                        Text("\(PlanTimeFormat.clock(clip.startS)) to \(PlanTimeFormat.clock(clip.endS))")
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.7))
                    }
                    Spacer(minLength: 4)
                    if let transition = clip.transition {
                        Text(transitionName(transition))
                            .font(KriaFont.body(12).weight(.medium)).foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 11).frame(height: 26)
                            .background(KriaColor.ink.opacity(0.07), in: Capsule())
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
        .accessibilityIdentifier("review-clips")
    }
}

// MARK: - Captions

private struct ReviewCaptionRows: View {
    let model: ReviewPlanModel
    let payload: PlanCaptionsPayload
    let previous: PlanPreviousValue?
    let editable: Bool
    let showsChanges: Bool

    /// Lines whose text differs from the previous version (or did not exist): marked with a butter dot.
    private var changedIDs: Set<String> {
        guard showsChanges, case .captions(let before)? = previous?.payload else { return [] }
        let old = Dictionary(before.lines.map { ($0.id, $0.text) }, uniquingKeysWith: { first, _ in first })
        return Set(payload.lines.filter { old[$0.id] != $0.text }.map(\.id))
    }

    var body: some View {
        let changed = changedIDs
        VStack(alignment: .leading, spacing: 8) {
            ForEach(payload.lines) { line in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(PlanTimeFormat.clock(line.startS))
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink.opacity(0.65))
                        .frame(width: 34, alignment: .leading)
                    if editable {
                        TextField(
                            "Caption",
                            text: Binding(get: { model.draft.captionText(for: line) }, set: { model.draft.editCaption(id: line.id, text: $0, original: line.text) }),
                            axis: .vertical
                        )
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                        .lineLimit(1...3)
                        .accessibilityIdentifier("review-edit-caption-\(line.id)")
                    } else {
                        Text(line.text)
                            .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Circle()
                        .fill(Color(red: 0.949, green: 0.761, blue: 0))
                        .frame(width: 8, height: 8)
                        .opacity(changed.contains(line.id) ? 1 : 0)
                        .accessibilityIdentifier(changed.contains(line.id) ? "review-changed-line-\(line.id)" : "review-line-\(line.id)")
                        .accessibilityLabel(changed.contains(line.id) ? "Changed line" : "")
                }
            }
            let more = payload.count - payload.lines.count
            if payload.truncated || more > 0 {
                Text("+\(max(more, 0)) more not shown")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .padding(.leading, 46)
            }
        }
    }
}

extension ReviewPlanDraft {
    /// What a caption line's field shows: the creator's text if they typed one, else the line's own.
    func captionText(for line: PlanCaptionLine) -> String { captionEdits[line.id] ?? line.text }
}

// MARK: - Sound mix

private struct ReviewMixSliders: View {
    let model: ReviewPlanModel
    let mix: PlanMixLevels
    let editable: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let music = mix.musicLevel {
                row(title: "Music", id: "music_level", value: Binding(
                    get: { model.draft.musicLevel ?? music },
                    set: { model.draft.editMix(musicLevel: $0, current: mix) }
                ))
            }
            if let original = mix.originalLevel {
                row(title: "Original sound", id: "original_level", value: Binding(
                    get: { model.draft.originalLevel ?? original },
                    set: { model.draft.editMix(originalLevel: $0, current: mix) }
                ))
            }
        }
    }

    private func row(title: String, id: String, value: Binding<Double>) -> some View {
        HStack(spacing: 12) {
            Text(title)
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                .frame(width: 108, alignment: .leading)
            if editable {
                Slider(value: value, in: 0...1)
                    .tint(KriaColor.ink)
                    .accessibilityLabel("\(title) level")
                    .accessibilityValue("\(Int((value.wrappedValue * 100).rounded())) percent")
                    .accessibilityIdentifier("review-mix-\(id)")
            } else {
                KriaProgressTrack(progress: value.wrappedValue).accessibilityHidden(true)
            }
            Text("\(Int((value.wrappedValue * 100).rounded()))%")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.7))
                .frame(width: 38, alignment: .trailing)
        }
    }
}

// MARK: - Entry from the project

/// The "Review your video" row under a finished video (second entry point, besides the feed's CTA). It asks the
/// server for the plan once per job and shows only when there is one to review (`ready` or `updating`); an empty
/// plan, an old server or a failed read hides it.
struct ProjectReviewEntry: View {
    let threadID: UUID
    let jobKey: String
    let api: any KriaAPIClient
    let open: () -> Void
    @State private var status: PlanSnapshot.Status?

    private var visible: Bool { status == .ready || status == .updating }

    var body: some View {
        // A stack, not a bare Group: modifiers on an empty Group never run, and the probe must run while hidden.
        VStack(spacing: 0) {
            if visible {
                Button(action: open) {
                    HStack(spacing: 12) {
                        Image(systemName: "sparkle")
                            .font(.system(size: 15, weight: .semibold)).foregroundStyle(KriaColor.ink)
                            .frame(width: 32, height: 32)
                            .background(KriaColor.butter, in: Circle())
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 0) {
                            Text("Review your video")
                                .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            Text("See every choice and change any of it")
                                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        }
                        Spacer(minLength: 8)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 12, weight: .semibold)).foregroundStyle(KriaColor.zinc)
                    }
                    .padding(.horizontal, 16)
                    .frame(maxWidth: .infinity, minHeight: 60, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Review your video")
                .accessibilityIdentifier("project-review-entry")
                .kriaGlassCard(cornerRadius: 22)
            }
        }
        .task(id: jobKey) {
            status = (try? await api.planSnapshot(threadID: threadID))?.status
        }
    }
}
