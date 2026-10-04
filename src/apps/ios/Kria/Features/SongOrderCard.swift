import AVFoundation
import KriaMediaEngine
import SwiftUI

/// How a song-order question is shown under its message (KRI-374).
enum SongOrderCardMode {
    /// The latest unanswered question: interactive. `submit` receives the structured answer and its readable message.
    case active(isSending: Bool, submit: (SongOrderSubmission, String) -> Void)
    /// Already answered (or superseded): read-only, collapsed. `summary` is nil after a relaunch.
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

    var body: some View {
        switch mode {
        case .answered(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                Text(summary.map { "Order confirmed: \($0)" } ?? "Order confirmed")
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
                    SongOrderPreview(mediaID: selectedMediaID, url: previewURL(selectedMediaID),
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
            let count = question.uncertainCount
            Text(count > 0
                 ? "Kria wasn’t sure where \(count == 1 ? "1 clip fits" : "\(count) clips fit") in your song. Tap a clip to watch it, then drag or use the arrows to fix the order."
                 : "Tap a clip to watch it, then drag or use the arrows to change the order.")
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
                    .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
            }
            .onMove { source, destination in
                guard !isSending else { return }
                state.move(from: source, to: destination)
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
        let isSelected = selectedMediaID == mediaID
        return HStack(spacing: 10) {
            Button { selectedMediaID = isSelected ? nil : mediaID } label: {
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
            .accessibilityIdentifier("song-order-take-\(mediaID)")
            VStack(spacing: 0) {
                arrow("chevron.up", label: "Move clip \(number) up", id: "song-order-up-\(mediaID)",
                      enabled: state.canMoveUp(mediaID) && !isSending) { state.moveUp(mediaID) }
                arrow("chevron.down", label: "Move clip \(number) down", id: "song-order-down-\(mediaID)",
                      enabled: state.canMoveDown(mediaID) && !isSending) { state.moveDown(mediaID) }
            }
        }
        .frame(height: rowHeight)
        .accessibilityElement(children: .contain)
        .accessibilityAction(named: "Move up") { if !isSending { state.moveUp(mediaID) } }
        .accessibilityAction(named: "Move down") { if !isSending { state.moveDown(mediaID) } }
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
        guard let item else { return "Kria couldn’t place this clip" }
        switch item.status {
        case .confident:
            return item.songStartS.map { "Starts at \(DurationFormatter.clock($0)) in the song" } ?? "Placed in your song"
        case .ambiguous:
            return item.songStartS.map { "Could fit a few places, about \(DurationFormatter.clock($0))" } ?? "Could fit a few places"
        case .unmatched:
            return "Kria couldn’t place this clip"
        }
    }
}

/// The "?" on a take Kria isn't sure about.
private struct UncertainBadge: View {
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
private struct SongOrderPreview: View {
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

@MainActor private final class SongOrderPreviewModel: ObservableObject {
    let player = AVQueuePlayer()
    private var looper: AVPlayerLooper?

    func play(_ url: URL) {
        stop()
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        try? AVAudioSession.sharedInstance().setActive(true)
        looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
        player.play()
    }

    func stop() {
        player.pause()
        looper?.disableLooping()
        looper = nil
        player.removeAllItems()
    }
}

/// A bare video surface. SwiftUI's `VideoPlayer` builds hidden playback controls that stall XCUITest's
/// idle tracking (see `NativeEditorPlayerSurface`), and a muted-by-chrome preview needs none of them.
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
