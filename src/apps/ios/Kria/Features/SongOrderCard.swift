import AVFoundation
import KriaMediaEngine
import SwiftUI

/// How a song-order question is shown under its message (KRI-374).
enum SongOrderCardMode {
    /// The latest unanswered question: interactive. `submit` receives the structured answer and its readable message.
    case active(isSending: Bool, submit: (SongOrderSubmission, String) -> Void)
    /// Answered by a `song_order` message: read-only, collapsed. `summary` names the confirmed order; nil when
    /// that message's clips can no longer be named, which reads as a neutral "Order question closed".
    case answered(summary: String?)
}

/// Finds a take's original on this iPhone without hashing it: the card only needs something to play.
@MainActor enum SongOrderPreviewSource {
    static func localURL(projectID: UUID, mediaID: String) -> URL? {
        let project = BackgroundUploadCoordinator.projectDirectory(projectID)
        guard let binding = try? SourceAssetStore(project: project).bindings().first(where: { $0.mediaID == mediaID }) else { return nil }
        let parts = binding.original.relativePath.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 2, parts.first == "originals", parts.last != "..", parts.last != "." else { return nil }
        let url = project.root.appendingPathComponent(binding.original.relativePath)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}

/// Vertical list of take thumbnails in the proposed song order. Tap a take to preview it, drag or use the
/// arrows to reorder, then "Use this order" sends the confirmed order back on the next turn.
struct SongOrderCard: View {
    let question: SongOrderQuestion
    let media: [CreationAttachedMedia]
    let projectID: UUID
    let mode: SongOrderCardMode
    /// Resolves a take's local original for the inline preview. Injected so tests can supply a fixture file.
    var previewURL: @MainActor (String) -> URL?
    @State private var state: SongOrderState
    @State private var selectedMediaID: String?
    /// The selected take's local original, resolved ONCE when the selection changes. Resolving in `body` hit the
    /// source-asset store and the file system on every re-render, including every frame of a drag.
    @State private var selectedPreviewURL: URL?
    @ScaledMetric(relativeTo: .body) private var rowHeight: CGFloat = 92

    init(question: SongOrderQuestion, media: [CreationAttachedMedia], projectID: UUID, mode: SongOrderCardMode,
         previewURL: (@MainActor (String) -> URL?)? = nil) {
        self.question = question
        self.media = media
        self.projectID = projectID
        self.mode = mode
        self.previewURL = previewURL ?? { SongOrderPreviewSource.localURL(projectID: projectID, mediaID: $0) }
        _state = State(initialValue: SongOrderState(question: question))
    }

    private var positions: [String: Int] { SongOrderPositions.map(media: media, question: question) }

    private func toggleSelection(_ mediaID: String) {
        if selectedMediaID == mediaID {
            selectedMediaID = nil
            selectedPreviewURL = nil
        } else {
            selectedMediaID = mediaID
            selectedPreviewURL = previewURL(mediaID)
        }
    }

    /// Applies a reorder and tells VoiceOver where the take ended up (the row also moves, so focus alone says little).
    private func reorder(_ mediaID: String, _ change: (inout SongOrderState) -> Void) {
        change(&state)
        let number = positions[mediaID] ?? 0
        let position = state.position(of: mediaID) ?? 0
        UIAccessibility.post(notification: .announcement,
                             argument: "Clip \(number) moved to position \(position) of \(state.order.count)")
    }

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: summary == nil ? "circle.dashed" : "checkmark.circle.fill")
                    .foregroundStyle(summary == nil ? KriaColor.mutedInk : KriaColor.success)
                Text(Self.answeredText(summary: summary))
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("song-order-answered")
        case .active(let isSending, let submit):
            VStack(alignment: .leading, spacing: 14) {
                header
                if let selectedMediaID {
                    SongOrderPreview(mediaID: selectedMediaID, url: selectedPreviewURL,
                                     title: "Clip \(positions[selectedMediaID] ?? 0)")
                }
                list(isSending: isSending)
                footer(isSending: isSending, submit: submit)
            }
            .padding(14)
            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("song-order-card")
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Check the order of your clips")
                .font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink)
                .accessibilityAddTraits(.isHeader)
            Text(Self.hint(ambiguous: question.ambiguousCount, unmatched: question.unmatchedCount))
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // A List gives real drag-to-reorder (`onMove`). It sits inside the chat's own scroll view, so it is sized
    // to its rows and never scrolls itself; rows have one fixed height so that size is exact.
    private func list(isSending: Bool) -> some View {
        List {
            ForEach(state.order, id: \.self) { mediaID in
                row(mediaID, isSending: isSending)
                    .moveDisabled(isUnplaced(mediaID))
                    .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            .onMove { source, destination in
                guard !isSending, let first = source.min(), state.order.indices.contains(first) else { return }
                reorder(state.order[first]) { $0.move(from: source, to: destination) }
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .scrollDisabled(true)
        .environment(\.editMode, .constant(isSending ? .inactive : .active))
        .frame(height: CGFloat(state.order.count) * (rowHeight + 8))
        .accessibilityIdentifier("song-order-list")
    }

    private func row(_ mediaID: String, isSending: Bool) -> some View {
        let item = question.item(for: mediaID)
        let position = state.position(of: mediaID) ?? 0
        let number = positions[mediaID] ?? 0
        let attached = media.first { $0.id == mediaID }
            ?? CreationAttachedMedia(id: mediaID, filename: "Clip \(number)", kind: "video", previewURL: nil)
        let uncertain = item?.status.isUncertain ?? true
        let unplaced = isUnplaced(mediaID)
        let isSelected = selectedMediaID == mediaID
        return HStack(spacing: 10) {
            Button { toggleSelection(mediaID) } label: {
                HStack(spacing: 10) {
                    Text("\(position)")
                        .font(KriaFont.body(13).weight(.bold)).foregroundStyle(KriaColor.ink)
                        .frame(width: 22, height: 22).background(KriaColor.butter, in: Circle())
                        .accessibilityHidden(true)
                    CreationAttachmentThumbnail(media: attached)
                        .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(isSelected ? KriaColor.ink : .clear, lineWidth: 2.5))
                        .overlay(alignment: .topTrailing) {
                            if uncertain { UncertainBadge().offset(x: 6, y: -6) }
                        }
                        .overlay {
                            Image(systemName: isSelected ? "pause.circle.fill" : "play.circle.fill")
                                .font(.system(size: 22)).foregroundStyle(Color.white, KriaColor.ink.opacity(0.7))
                                .accessibilityHidden(true)
                        }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Clip \(number)").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
                        Text(Self.caption(for: item))
                            .font(KriaFont.body(12)).foregroundStyle(uncertain ? KriaColor.failureText : KriaColor.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Clip \(number), position \(position) of \(state.order.count). \(Self.caption(for: item))")
            .accessibilityHint(isSelected ? "Stops the preview" : "Plays a preview")
            .accessibilityAddTraits(isSelected ? .isSelected : [])
            // The custom actions sit on the take itself, which is a real accessibility element. On the row they
            // were attached to a `.contain` container, which VoiceOver does not expose as an element, so they never
            // showed in the actions rotor; the arrow buttons stay separate, reachable elements next to it.
            .accessibilityAction(named: "Move up") { if !isSending && !unplaced && state.canMoveUp(mediaID) { reorder(mediaID) { $0.moveUp(mediaID) } } }
            .accessibilityAction(named: "Move down") { if !isSending && !unplaced && state.canMoveDown(mediaID) { reorder(mediaID) { $0.moveDown(mediaID) } } }
            .accessibilityIdentifier("song-order-take-\(mediaID)")
            // A take Kria could not place has no position in the song: the server can only use it as filler, so
            // reordering it would promise something that does not happen. It stays listed, without arrows.
            if unplaced {
                Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
            } else {
                VStack(spacing: 0) {
                    arrow("chevron.up", label: "Move clip \(number) up", id: "song-order-up-\(mediaID)",
                          enabled: state.canMoveUp(mediaID) && !isSending) { reorder(mediaID) { $0.moveUp(mediaID) } }
                    arrow("chevron.down", label: "Move clip \(number) down", id: "song-order-down-\(mediaID)",
                          enabled: state.canMoveDown(mediaID) && !isSending) { reorder(mediaID) { $0.moveDown(mediaID) } }
                }
            }
        }
        .frame(height: rowHeight)
    }

    /// True for a take the server could not place at all. It has no position in the song, so it is not reorderable.
    private func isUnplaced(_ mediaID: String) -> Bool {
        (question.item(for: mediaID)?.status ?? .unmatched) == .unmatched
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

    private func footer(isSending: Bool, submit: @escaping (SongOrderSubmission, String) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button("Use this order") {
                let submission = state.submission
                submit(submission, submission.message(positions: positions))
            }
            .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
            .disabled(isSending)
            .accessibilityIdentifier("song-order-use")
            if state.isChanged {
                Button("Reset to Kria’s order") { state.reset() }
                    .buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    .disabled(isSending)
                    .accessibilityIdentifier("song-order-reset")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One line under a take's name: where Kria put it, or why it needs a look.
    static func caption(for item: SongOrderQuestion.Item?) -> String {
        guard let item else { return unplacedCaption }
        switch item.status {
        case .confident:
            return item.songStartS.map { "Starts at \(DurationFormatter.clock($0)) in the song" } ?? "Placed in your song"
        case .ambiguous:
            return item.songStartS.map { "Could fit a few places, about \(DurationFormatter.clock($0))" } ?? "Could fit a few places"
        case .unmatched:
            return unplacedCaption
        }
    }

    /// Honest about what happens to it: with no position in the song the server can only use the clip as filler.
    static let unplacedCaption = "Kria couldn’t place this clip — it will be used as filler"

    /// The sentence under the card title. The "drag or use the arrows" advice is only given when some take can
    /// actually be moved to fix a doubtful position; unplaced takes are explained instead.
    static func hint(ambiguous: Int, unmatched: Int) -> String {
        func clips(_ n: Int) -> String { n == 1 ? "1 clip" : "\(n) clips" }
        var parts: [String] = []
        if ambiguous > 0 {
            parts.append("Kria wasn’t sure where \(ambiguous == 1 ? "1 clip fits" : "\(ambiguous) clips fit") in your song. Tap a clip to watch it, then drag or use the arrows to fix the order.")
        } else {
            parts.append("Tap a clip to watch it, then drag or use the arrows to change the order.")
        }
        if unmatched > 0 {
            parts.append("\(clips(unmatched)) Kria couldn’t place will be used as filler.")
        }
        return parts.joined(separator: " ")
    }

    /// The collapsed line once answered. Without a nameable order it stays neutral rather than claiming a confirmation.
    static func answeredText(summary: String?) -> String {
        summary.map { "Order confirmed: \($0)" } ?? "Order question closed"
    }
}

/// The "?" on a take Kria isn't sure about.
struct UncertainBadge: View {
    var body: some View {
        Text("?")
            .font(KriaFont.body(12).weight(.bold)).foregroundStyle(KriaColor.ink)
            .frame(width: 20, height: 20)
            .background(KriaColor.butter, in: Circle())
            .overlay(Circle().stroke(KriaColor.ink, lineWidth: 1))
            .accessibilityHidden(true)
    }
}

/// Inline, looping preview of the take's original from the start. Plays only while a take is selected.
struct SongOrderPreview: View {
    let mediaID: String
    let url: URL?
    let title: String
    @StateObject private var model = SongOrderPreviewModel()

    var body: some View {
        Group {
            if let url {
                SongOrderPlayerSurface(player: model.player)
                    .frame(height: 200)
                    .frame(maxWidth: .infinity)
                    .background(Color.black, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .task(id: url) { model.play(url) }
                    .onDisappear { model.stop() }
                    // The voiceover recorder switches the shared session to play-and-record; a preview still playing
                    // underneath would fight it, so the recorder announces itself and the preview steps aside.
                    .onReceive(NotificationCenter.default.publisher(for: .kriaAudioCaptureWillStart)) { _ in model.stop() }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("Preview of \(title)")
            } else {
                Text("\(title) isn’t on this iPhone anymore, so it can’t be previewed. You can still place it.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
        .accessibilityIdentifier("song-order-preview")
    }
}

@MainActor final class SongOrderPreviewModel: ObservableObject {
    let player = AVQueuePlayer()
    private var looper: AVPlayerLooper?
    private var isSessionActive = false

    func play(_ url: URL) {
        halt()
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        isSessionActive = (try? AVAudioSession.sharedInstance().setActive(true)) != nil
        looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
        player.play()
    }

    /// Stops playback and gives the audio session back, so music the creator had playing resumes.
    func stop() {
        halt()
        guard isSessionActive else { return }
        isSessionActive = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func halt() {
        player.pause()
        looper?.disableLooping()
        looper = nil
        player.removeAllItems()
    }
}

/// A bare video surface. SwiftUI's `VideoPlayer` builds hidden playback controls that stall XCUITest's
/// idle tracking (see `NativeEditorPlayerSurface`), and a preview needs none of them. The preview plays the
/// take's own audio (the creator is checking where it sits against the song), so there is no mute control.
private struct SongOrderPlayerSurface: UIViewRepresentable {
    let player: AVPlayer

    func makeUIView(context: Context) -> Surface {
        let view = Surface()
        view.playerLayer.player = player
        return view
    }

    func updateUIView(_ view: Surface, context: Context) {
        if view.playerLayer.player !== player { view.playerLayer.player = player }
    }

    final class Surface: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
        override init(frame: CGRect) {
            super.init(frame: frame)
            playerLayer.videoGravity = .resizeAspect
            backgroundColor = .clear
        }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    }
}
