import SwiftUI

/// KRI-288: the Effects tab of the Sounds panel. Screens (all inside the connected panel):
/// home ("In this edit") → library (add) → edit sound (selected SFX) → trim.
/// "Edit sound" is derived from the selected sound effect so timeline taps land on it.
enum NativeSfxFormat {
    static func clock(_ seconds: Double) -> String {
        let total = max(0, seconds)
        return String(format: "%d:%04.1f", Int(total) / 60, total.truncatingRemainder(dividingBy: 60))
    }
    static func seconds(_ value: Double) -> String { String(format: "%.1fs", value) }
}

struct NativeSoundEffectsPanelBody: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    @ObservedObject var player: NativeSfxAuditionPlayer
    @ObservedObject var waveforms: NativeSfxWaveformStore

    static func selectedEffect(_ session: NativeEditorSession) -> EditorTimedEffect? {
        guard let selection = session.selection, selection.kind == .soundEffect else { return nil }
        return session.document.soundEffects.first(where: { $0.id == selection.id })
    }

    /// Changes whenever a different screen (or sound) is shown, so the shared panel scroll resets.
    private var screenKey: String {
        if let selected = Self.selectedEffect(session) { return "\(selected.id)-\(panelDrafts.sfxScreen == .trim ? "trim" : "edit")" }
        return panelDrafts.sfxScreen == .library ? "library" : "home"
    }

    @ViewBuilder private var screen: some View {
        if let selected = Self.selectedEffect(session) {
            if panelDrafts.sfxScreen == .trim {
                NativeSfxTrimView(session: session, panelDrafts: panelDrafts, player: player, waveforms: waveforms, effect: selected)
            } else {
                NativeSfxEditView(session: session, panelDrafts: panelDrafts, player: player, effect: selected)
            }
        } else if panelDrafts.sfxScreen == .library {
            NativeSfxLibraryView(session: session, panelDrafts: panelDrafts, player: player)
        } else {
            NativeSfxHomeView(session: session, panelDrafts: panelDrafts, player: player)
        }
    }

    var body: some View {
        ScrollViewReader { proxy in
            VStack(alignment: .leading, spacing: 0) {
                Color.clear.frame(height: 0).id("sfx-top")
                screen
            }
            .onChange(of: screenKey) { _, _ in proxy.scrollTo("sfx-top", anchor: .top) }
        }
        .task { await session.loadSoundEffectCatalog() }
        .onChange(of: panelDrafts.sfxScreen) { _, _ in player.stop() }
        .onChange(of: session.selection) { _, _ in player.stop() }
        .onDisappear { player.stop() }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didEnterBackgroundNotification)) { _ in player.stop() }
    }
}

@MainActor private func sfxKept(_ session: NativeEditorSession, _ effect: EditorTimedEffect) -> Double {
    let window = NativeEditorSession.soundEffectTrimWindow(effect, minimum: NativeEditorSession.sfxMinimumTrim)
    return window.end - window.start
}

private func sfxGain(_ effect: EditorTimedEffect) -> Double {
    if case .number(let value) = effect.raw["gain"] { return min(1, max(0, value)) }
    return 1
}

private func sfxName(_ effect: EditorTimedEffect) -> String { effect.raw["label"]?.stringValue ?? "Sound effect" }

private struct NativeSfxRowBackground: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(.horizontal, 6).frame(minHeight: 52)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Home

struct NativeSfxHomeView: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    @ObservedObject var player: NativeSfxAuditionPlayer

    private var effects: [EditorTimedEffect] { session.document.soundEffects.sorted { $0.startS < $1.startS } }
    private var canEdit: Bool { session.canEdit(.soundEffects) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if effects.isEmpty {
                emptyState
            } else {
                HStack {
                    Text("In this edit").font(KriaFont.body(15).weight(.semibold))
                    Spacer()
                    if canEdit {
                        Button("+ Add sound") { panelDrafts.sfxScreen = .library }
                            .font(KriaFont.body(14).weight(.semibold)).frame(minHeight: 44)
                            .accessibilityIdentifier("native-editor-sfx-add-sound")
                    }
                }
                ForEach(effects, id: \.id) { effect in row(effect) }
                if !canEdit { lockedNote }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-sfx-home")
    }

    @ViewBuilder private var emptyState: some View {
        if canEdit {
            stateCard(title: "No sounds yet", detail: "Add a sound effect to this edit.", button: "Add sound", id: "native-editor-sfx-add-sound") {
                panelDrafts.sfxScreen = .library
            }
        } else {
            stateCard(title: "Sound effects unavailable", detail: "Sound effects can’t be changed in this edit.", button: nil, id: "") {}
        }
    }

    private var lockedNote: some View {
        Label("Sound effects can’t be changed in this edit.", systemImage: "lock")
            .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func row(_ effect: EditorTimedEffect) -> some View {
        let failed = player.failedIDs.contains(effect.id)
        return HStack(spacing: 4) {
            NativeSfxPlayButton(player: player, id: effect.id, name: sfxName(effect)) {
                player.toggle(id: effect.id, url: session.soundEffectPreviewURL(for: effect),
                              range: previewRange(effect), gain: sfxGain(effect))
            }
            Button {
                session.select(EditorSelection(kind: .soundEffect, id: effect.id))
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(sfxName(effect)).font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        Text(failed ? "Preview unavailable · Retry"
                             : "\(NativeSfxFormat.clock(effect.startS)) · \(NativeSfxFormat.seconds(sfxKept(session, effect)))")
                            .font(KriaFont.body(12)).foregroundStyle(failed ? KriaColor.failureText : KriaColor.mutedInk)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(KriaColor.ink)
                }
                .frame(maxWidth: .infinity, minHeight: 44).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.trailing, 8)
            .accessibilityIdentifier("native-editor-sfx-row-\(effect.id)")
        }
        .modifier(NativeSfxRowBackground())
    }

    private func previewRange(_ effect: EditorTimedEffect) -> ClosedRange<Double>? {
        let window = NativeEditorSession.soundEffectTrimWindow(effect, minimum: NativeEditorSession.sfxMinimumTrim)
        return window.start...window.end
    }
}

private func stateCard(title: String, detail: String, button: String?, id: String, action: @escaping () -> Void) -> some View {
    VStack(alignment: .leading, spacing: 8) {
        Text(title).font(KriaFont.body(16).weight(.semibold))
        Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
        if let button {
            Button(button, action: action).buttonStyle(KriaSecondaryButtonStyle()).accessibilityIdentifier(id)
        }
    }
    .frame(maxWidth: .infinity, alignment: .leading).padding(16)
    .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16))
}

// MARK: - Library

struct NativeSfxLibraryView: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    @ObservedObject var player: NativeSfxAuditionPlayer

    private var canAdd: Bool {
        session.canEditOperation(["lanes.sfx.add", "sfx.add", "sound_effects.add", "lanes.sfx"], section: .soundEffects)
    }
    private var groups: [NativeSfxBrowse.Group] { NativeSfxBrowse.groupEffects(session.soundEffectCatalog, query: panelDrafts.sfxQuery) }
    private var showsCategoryLabels: Bool { NativeSfxBrowse.hasCategories(session.soundEffectCatalog) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button { panelDrafts.sfxScreen = .home } label: {
                    Text("‹ In this edit (\(session.document.soundEffects.count))").font(KriaFont.body(14).weight(.semibold))
                        .frame(minHeight: 44)
                }
                .accessibilityIdentifier("native-editor-sfx-library-back")
                Spacer()
                Text("Add at \(NativeSfxFormat.clock(session.currentTime))")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(KriaColor.zinc)
                TextField("Search sound effects", text: $panelDrafts.sfxQuery)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .accessibilityIdentifier("native-editor-sfx-search")
            }
            .padding(.horizontal, 12).frame(minHeight: 44)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
            content
            if !canAdd {
                Label("Sound effects aren’t available for this edit.", systemImage: "lock")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    @ViewBuilder private var content: some View {
        if session.soundEffectCatalogLoading && session.soundEffectCatalog.isEmpty {
            ProgressView("Loading sounds…").frame(maxWidth: .infinity, minHeight: 44)
        } else if session.soundEffectCatalog.isEmpty {
            stateCard(title: "Sounds couldn’t load", detail: "Your added sounds are still in this edit.", button: "Try again", id: "native-editor-sfx-retry-catalog") {
                Task { await session.loadSoundEffectCatalog() }
            }
        } else if groups.isEmpty {
            stateCard(title: "No sounds found", detail: "Try another word or clear your search.", button: "Clear search", id: "native-editor-sfx-clear-search") {
                panelDrafts.sfxQuery = ""
            }
        } else {
            ForEach(groups, id: \.key) { group in
                VStack(alignment: .leading, spacing: 6) {
                    if showsCategoryLabels {
                        Text("\(group.label) (\(group.effects.count))")
                            .font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.zinc)
                    }
                    ForEach(group.effects) { effect in row(effect) }
                }
            }
        }
    }

    private func row(_ effect: NativeEditorSoundEffect) -> some View {
        let failed = player.failedIDs.contains(effect.id)
        let playing = player.playingID == effect.id
        let duration = effect.durationS.map(NativeSfxFormat.seconds) ?? ""
        return HStack(spacing: 4) {
            NativeSfxPlayButton(player: player, id: effect.id, name: effect.name) {
                player.toggle(id: effect.id, url: effect.previewAudioURL)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(effect.name).font(KriaFont.body(15).weight(.semibold))
                Text(failed ? "Preview unavailable · Retry" : (playing ? "Playing · \(duration)" : duration))
                    .font(KriaFont.body(12)).foregroundStyle(failed ? KriaColor.failureText : KriaColor.mutedInk)
            }
            Spacer(minLength: 8)
            Button("Add") {
                player.stop()
                panelDrafts.sfxScreen = .home
                session.addSoundEffect(effect)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .disabled(!canAdd)
            .accessibilityIdentifier("native-editor-sfx-add-\(effect.id)")
        }
        .padding(.trailing, 6)
        .modifier(NativeSfxRowBackground())
    }
}

// MARK: - Edit sound

struct NativeSfxEditView: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    @ObservedObject var player: NativeSfxAuditionPlayer
    let effect: EditorTimedEffect

    private var volume: Binding<Double> {
        Binding(get: { sfxGain(effect) }, set: { session.setSoundEffectGain(id: effect.id, gain: $0) })
    }
    private var editable: Bool { session.canEdit(.soundEffects) }
    private var trimLabel: String {
        let window = NativeEditorSession.soundEffectTrimWindow(effect, minimum: NativeEditorSession.sfxMinimumTrim)
        let full = effect.raw["trim_start_s"] == nil && effect.raw["trim_end_s"] == nil
            || (window.start <= 0.001 && window.end >= window.source - 0.001)
        return full ? "Full sound" : String(format: "%.2f–%.2fs", window.start, window.end)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 8) {
                NativeSfxPlayButton(player: player, id: effect.id, name: sfxName(effect)) {
                    let window = NativeEditorSession.soundEffectTrimWindow(effect, minimum: NativeEditorSession.sfxMinimumTrim)
                    player.toggle(id: effect.id, url: session.soundEffectPreviewURL(for: effect),
                                  range: window.start...window.end, gain: sfxGain(effect))
                }
                .background(KriaColor.softZinc, in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(sfxName(effect)).font(KriaFont.body(15).weight(.semibold))
                    if player.failedIDs.contains(effect.id) {
                        Text("Preview unavailable · Retry").font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
                    }
                }
                Spacer()
                Text(NativeSfxFormat.seconds(sfxKept(session, effect))).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
            }
            VStack(spacing: 4) {
                HStack {
                    Text("Volume").font(KriaFont.body(14))
                    Spacer()
                    Text("\(Int((volume.wrappedValue * 100).rounded()))%").font(KriaFont.body(14).weight(.bold))
                }
                HStack(spacing: 10) {
                    Text("0%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    NativePaperSlider(session: session, value: volume, bounds: 0...1, step: 0.01, label: "Sound volume")
                        .frame(height: 44)
                    Text("100%").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("native-editor-sfx-volume")
            Button { panelDrafts.sfxScreen = .trim } label: {
                HStack {
                    Text("Trim sound").font(KriaFont.body(15))
                    Spacer()
                    Text(trimLabel).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold))
                }
                .frame(minHeight: 44).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .overlay(alignment: .bottom) { Divider() }
            .accessibilityIdentifier("native-editor-sfx-trim-row")
            if !editable {
                Label("Sound effects can’t be changed in this edit.", systemImage: "lock")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            }
        }
        .disabled(!editable)
    }
}

// MARK: - Trim

struct NativeSfxTrimView: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    @ObservedObject var player: NativeSfxAuditionPlayer
    @ObservedObject var waveforms: NativeSfxWaveformStore
    let effect: EditorTimedEffect
    @State private var entering: Side?
    @State private var entry = ""
    enum Side { case from, to }

    private var window: (start: Double, end: Double, source: Double) {
        NativeEditorSession.soundEffectTrimWindow(effect, minimum: NativeEditorSession.sfxMinimumTrim)
    }
    private var isFull: Bool { window.start <= 0.001 && window.end >= window.source - 0.001 }
    private var waveformKey: String { effect.raw["sound_effect_id"]?.stringValue ?? effect.id }

    var body: some View {
        let w = window
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button { panelDrafts.sfxScreen = .home } label: {
                    Text("‹ Trim sound").font(KriaFont.body(15).weight(.semibold)).frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("native-editor-sfx-trim-back")
                Spacer()
                Button("Use full sound") { session.resetSoundEffectTrim(id: effect.id) }
                    .font(KriaFont.body(14)).frame(minHeight: 44).disabled(isFull)
                    .accessibilityIdentifier("native-editor-sfx-trim-reset")
            }
            NativeSfxTrimBar(source: w.source, start: w.start, end: w.end, bars: waveforms.bars[waveformKey],
                             minimum: NativeEditorSession.sfxMinimumTrim,
                             onChange: { session.setSoundEffectTrim(id: effect.id, trimStartS: $0, trimEndS: $1) },
                             onBegin: { player.stop(); session.beginTransaction() },
                             onEnd: { session.endTransaction() })
            HStack {
                Button(String(format: "From %.2fs", w.start)) { entry = String(format: "%.2f", w.start); entering = .from }
                    .frame(minHeight: 44).accessibilityIdentifier("native-editor-sfx-trim-from")
                Spacer()
                Text(String(format: "%.2fs kept", w.end - w.start)).foregroundStyle(KriaColor.mutedInk)
                Spacer()
                Button(String(format: "To %.2fs", w.end)) { entry = String(format: "%.2f", w.end); entering = .to }
                    .frame(minHeight: 44).accessibilityIdentifier("native-editor-sfx-trim-to")
            }
            .font(KriaFont.body(13))
            Button {
                player.toggle(id: effect.id, url: session.soundEffectPreviewURL(for: effect), range: w.start...w.end, gain: sfxGain(effect))
            } label: {
                Label(player.playingID == effect.id ? "Stop preview" : "Preview trimmed sound",
                      systemImage: player.playingID == effect.id ? "stop.fill" : "play.fill")
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(KriaSecondaryButtonStyle())
            .accessibilityIdentifier("native-editor-sfx-trim-preview")
        }
        .disabled(!session.canEdit(.soundEffects))
        .task(id: waveformKey) { await waveforms.load(key: waveformKey, url: session.soundEffectPreviewURL(for: effect)) }
        .alert(entering == .from ? "Trim from (seconds)" : "Trim to (seconds)", isPresented: Binding(get: { entering != nil }, set: { if !$0 { entering = nil } })) {
            TextField("Seconds", text: $entry).keyboardType(.decimalPad)
            Button("Set") {
                if let value = Double(entry.replacingOccurrences(of: ",", with: ".")) {
                    if entering == .from { session.setSoundEffectTrim(id: effect.id, trimStartS: value) }
                    else { session.setSoundEffectTrim(id: effect.id, trimEndS: value) }
                }
                entering = nil
            }
            Button("Cancel", role: .cancel) { entering = nil }
        }
    }
}
